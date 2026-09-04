"""Long-running orchestration for the read-only Codex Guardian source."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
import time
from typing import Callable

from .exceptions import GuardianBusyError, GuardianIntegrityError
from .guardian_lock import GuardianRunnerLock
from .guardian_manifest import load_guardian_manifest, verify_guardian_snapshot
from .guardian_models import (
    GuardianConfig,
    GuardianResult,
    GuardianResultStatus,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
)
from .guardian_pointer import resolve_or_restore_latest_good
from .guardian_schema import validate_global_state_references
from .guardian_shrink import assess_suspicious_shrink
from .guardian_store import GuardianStore
from .stable_reader import SourceMissingError, SourceTooLargeError, SourceUnstableError, StableReader


LOG = logging.getLogger(__name__)
SOURCE_MISSING = "SOURCE_DISAPPEARED"
READ_ERROR = "READ_ERROR"


class GuardianRunnerState(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    DIRTY = "DIRTY"
    DEBOUNCING = "DEBOUNCING"
    VERIFYING = "VERIFYING"
    COMMITTING = "COMMITTING"
    QUARANTINING = "QUARANTINING"
    BACKOFF = "BACKOFF"
    STOPPING = "STOPPING"


@dataclass(frozen=True, slots=True)
class GuardianRunOutcome:
    status: GuardianResultStatus
    state: GuardianRunnerState
    detail: str | None = None
    result: GuardianResult | None = None


class GuardianRunner:
    """Coalescing poller with a shared stable-read/validation/commit pipeline."""

    def __init__(
        self,
        source_path: Path,
        store: GuardianStore,
        config: GuardianConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source_path = source_path
        self.store = store
        self.config = config
        self.reader = StableReader(source_path, max_bytes=config.max_state_bytes)
        self._monotonic = monotonic
        self._sleep = sleep
        self.state = GuardianRunnerState.STARTING
        self._last_signature = None
        self._missing_reported = False
        self._backoff_seconds = 1.0

    def once(self, *, timeout_seconds: float | None = None) -> GuardianRunOutcome:
        """One complete pipeline; used by watch startup and snapshot --once."""
        timeout = self.config.once_timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = self._monotonic() + timeout
        try:
            with GuardianRunnerLock(self.store.root_dir, self.store.machine_id):
                self._verify_store()
                return self._process_candidate(deadline=deadline, debounce=True)
        except GuardianBusyError:
            return GuardianRunOutcome(GuardianResultStatus.BUSY, GuardianRunnerState.STOPPING, "active Guardian watcher")
        except KeyboardInterrupt:
            self.state = GuardianRunnerState.STOPPING
            return GuardianRunOutcome(GuardianResultStatus.UNCHANGED, self.state, "stopped by user")

    def watch(self, *, should_stop: Callable[[], bool] | None = None) -> GuardianRunOutcome:
        should_stop = should_stop or (lambda: False)
        try:
            with GuardianRunnerLock(self.store.root_dir, self.store.machine_id):
                self._verify_store()
                initial = self._process_candidate(deadline=None, debounce=False)
                last_outcome = initial
                self.state = GuardianRunnerState.IDLE
                fallback_due = self._monotonic() + self.config.fallback_scan_seconds
                while not should_stop():
                    self._sleep(self.config.poll_interval_seconds)
                    if should_stop():
                        break
                    try:
                        signature = self.reader.signature()
                    except SourceUnstableError:
                        last_outcome = self._backoff("source metadata is unavailable")
                        continue
                    now = self._monotonic()
                    if signature != self._last_signature:
                        last_outcome = self._process_candidate(deadline=None, debounce=True)
                        fallback_due = self._monotonic() + self.config.fallback_scan_seconds
                    elif now >= fallback_due:
                        # A complete re-read catches replacements that preserve
                        # mtime/size/file identity or a missed filesystem event.
                        self._verify_store()
                        last_outcome = self._process_candidate(deadline=None, debounce=True)
                        fallback_due = self._monotonic() + self.config.fallback_scan_seconds
                    else:
                        self._backoff_seconds = 1.0
                self.state = GuardianRunnerState.STOPPING
                return last_outcome
        except GuardianBusyError:
            return GuardianRunOutcome(GuardianResultStatus.BUSY, GuardianRunnerState.STOPPING, "active Guardian watcher")
        except KeyboardInterrupt:
            self.state = GuardianRunnerState.STOPPING
            return GuardianRunOutcome(GuardianResultStatus.UNCHANGED, self.state, "stopped by user")

    def _process_candidate(self, *, deadline: float | None, debounce: bool) -> GuardianRunOutcome:
        self.state = GuardianRunnerState.DIRTY
        try:
            signature = self.reader.signature()
            self._last_signature = signature
            if not signature.exists:
                return self._source_missing()
            if debounce:
                self.state = GuardianRunnerState.DEBOUNCING
                self._sleep_bounded(self.config.debounce_seconds, deadline)
                # A change during debounce restarts debounce, so only a quiet
                # interval can enter the stable-read phase.
                while self.reader.signature() != signature:
                    signature = self.reader.signature()
                    self._last_signature = signature
                    self._sleep_bounded(self.config.debounce_seconds, deadline)
            self.state = GuardianRunnerState.VERIFYING
            observation = self.reader.read_stable(
                reads=self.config.stable_reads,
                interval_seconds=self.config.stable_read_interval_seconds,
                sleep=lambda seconds: self._sleep_bounded(seconds, deadline),
            )
            self._last_signature = self.reader.signature()
            self._missing_reported = False
            self._backoff_seconds = 1.0
        except SourceMissingError:
            return self._source_missing()
        except SourceTooLargeError:
            return self._quarantine(_unstable_observation(self.source_path), ValidationReport(ValidationStatus.INVALID, ("SOURCE_TOO_LARGE",)))
        except SourceUnstableError as exc:
            return self._backoff(str(exc))

        validation = validate_global_state_references(observation.payload)
        validation = self._assess_shrink(validation)
        if validation.status in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
            self.state = GuardianRunnerState.COMMITTING
            result = self.store.commit(observation, validation)
            self.state = GuardianRunnerState.IDLE
            return GuardianRunOutcome(result.status, self.state, result.detail, result)
        return self._quarantine(observation, validation)

    def _verify_store(self) -> None:
        # Pointer recovery is safe and only ever writes inside Guardian root.
        resolve_or_restore_latest_good(self.store.root_dir, self.store.machine_id)

    def _assess_shrink(self, validation: ValidationReport) -> ValidationReport:
        latest = resolve_or_restore_latest_good(self.store.root_dir, self.store.machine_id)
        if latest is None:
            return validation
        try:
            baseline = load_guardian_manifest(latest.manifest_path)
            verify_guardian_snapshot(latest)
        except GuardianIntegrityError:
            return ValidationReport(ValidationStatus.INDETERMINATE, ("BASELINE_UNVERIFIED",))
        return assess_suspicious_shrink(
            validation,
            baseline=baseline,
            baseline_verified=True,
            shrink_min_count=self.config.shrink_min_count,
            shrink_ratio=self.config.shrink_ratio,
        )

    def _source_missing(self) -> GuardianRunOutcome:
        self._last_signature = self.reader.signature()
        if self._missing_reported:
            self.state = GuardianRunnerState.BACKOFF
            return GuardianRunOutcome(GuardianResultStatus.UNCHANGED, self.state, "source remains missing")
        self._missing_reported = True
        return self._quarantine(
            _unstable_observation(self.source_path),
            ValidationReport(ValidationStatus.INDETERMINATE, (SOURCE_MISSING,)),
        )

    def _quarantine(self, observation: SourceObservation, validation: ValidationReport) -> GuardianRunOutcome:
        self.state = GuardianRunnerState.QUARANTINING
        result = self.store.quarantine(observation, validation)
        self.state = GuardianRunnerState.IDLE
        return GuardianRunOutcome(result.status, self.state, result.detail, result)

    def _backoff(self, detail: str) -> GuardianRunOutcome:
        self.state = GuardianRunnerState.BACKOFF
        wait = self._backoff_seconds
        self._backoff_seconds = min(60.0, self._backoff_seconds * 2.0)
        LOG.warning("Guardian source read is unavailable; retrying in %.0fs", wait)
        self._sleep(wait)
        return GuardianRunOutcome(GuardianResultStatus.FAILED, self.state, detail)

    def _sleep_bounded(self, seconds: float, deadline: float | None) -> None:
        if deadline is not None and self._monotonic() + seconds > deadline:
            raise SourceUnstableError("Guardian stable-read timeout")
        self._sleep(seconds)


def _unstable_observation(path: Path) -> SourceObservation:
    return SourceObservation(
        payload=b"",
        source_name=path.name,
        source_size_before=None,
        source_size_after=None,
        source_mtime_ns_before=None,
        source_mtime_ns_after=None,
        source_file_id_before=None,
        source_file_id_after=None,
        is_stable=False,
    )
