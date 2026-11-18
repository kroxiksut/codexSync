"""Toolkit-neutral progress events and the legacy three-number callback."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
import time
from typing import Callable

__all__ = [
    "PHASES", "ProgressCallback", "ProgressEstimate", "ProgressEstimator", "ProgressEvent",
    "ProgressEventCallback", "ProgressObserver", "report",
]

#: ``(phase, done, total)``. ``total`` may be 0 while it is still unknown.
ProgressCallback = Callable[[str, int, int], None]

#: Every phase id a core scan reports, which is also what a catalogue of
#: labels has to cover. Adding one here without a label makes the i18n test
#: fail, which is the point.
PHASES: tuple[str, ...] = (
    "sessions",
    "sessions_cloud",
    "chats",
    "state_backup",
    # A full sync (`run_handoff`) says which half it is in (CS-339).
    "sync_settings",
    "sync_chats",
    "stage_chats",
    "apply_chats",
    "backup_chats",
    "delete_chat_files",
    "sync_projects",
    "follow_chat_archive",
    "sync_chat_names",
    "refresh_catalogue",
    "check_project_files",
    "transfer",
)

_EVENT_STATUSES = frozenset({
    "running", "pending", "applied", "failed", "completed", "cancelled", "rolled_back", "recovery_required",
})
_EVENT_DIRECTIONS = frozenset({"local", "cloud"})
_PATH_COMPONENT = re.compile(r"[\w. -]+\Z")
_SESSION_HASH = re.compile(r"[0-9a-f]{12}(?:[0-9a-f]{52})?\Z")


def safe_relative_path(value: str | None) -> str | None:
    """Permit a small filename alphabet and reject secret names at any depth."""
    if not value or len(value) > 512:
        return None
    parts = value.replace("\\", "/").split("/")
    for part in parts:
        name = part.casefold().rstrip(" .")
        if (
            not _PATH_COMPONENT.fullmatch(part) or not name
            or any(word in name for word in ("auth", "secret", "token", "credential", "cap_sid"))
            or name.startswith((".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"))
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            return None
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One toolkit-neutral progress observation.

    ``total`` is unknown rather than zero while discovery is still in progress.
    Item metadata holds a sync-relative filename or a short branch reference.
    """

    operation: str
    phase: str
    completed: int
    total: int | None
    bytes_completed: int | None = None
    bytes_total: int | None = None
    item_id: str | None = None
    relative_path: str | None = None
    direction: str | None = None
    item_bytes: int | None = None
    status: str = "running"
    started_at: float | None = None
    at: float | None = None
    monotonic_at: float | None = None

    def __post_init__(self) -> None:
        if not self.operation or not self.phase:
            raise ValueError("progress operation and phase must not be empty")
        if self.completed < 0:
            raise ValueError("progress completed must not be negative")
        if self.total is not None and (self.total < 0 or self.completed > self.total):
            raise ValueError("progress total must be non-negative and not below completed")
        if self.bytes_completed is not None and self.bytes_completed < 0:
            raise ValueError("progress bytes_completed must not be negative")
        if self.bytes_total is not None:
            if self.bytes_total < 0:
                raise ValueError("progress bytes_total must not be negative")
            if self.bytes_completed is not None and self.bytes_completed > self.bytes_total:
                raise ValueError("progress bytes_total must not be below bytes_completed")
        if self.item_bytes is not None and self.item_bytes < 0:
            raise ValueError("progress item_bytes must not be negative")
        item = self.item_id
        item = item[:12] if item and _SESSION_HASH.fullmatch(item) else None
        object.__setattr__(self, "item_id", item)
        path = safe_relative_path(self.relative_path) if self.operation != "sessions" else None
        object.__setattr__(self, "relative_path", path)
        if self.direction not in _EVENT_DIRECTIONS:
            object.__setattr__(self, "direction", None)
        if self.status not in _EVENT_STATUSES:
            object.__setattr__(self, "status", "running")


ProgressEventCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True, slots=True)
class ProgressObserver:
    """Legacy callable with an optional structured-event channel."""

    counts: ProgressCallback
    event: ProgressEventCallback

    def __call__(self, phase: str, done: int, total: int) -> None:
        self.counts(phase, done, total)


@dataclass(frozen=True, slots=True)
class ProgressEstimate:
    """Rates derived from observations of one operation phase."""

    items_per_second: float | None
    bytes_per_second: float | None
    remaining_seconds: float | None


@dataclass(frozen=True, slots=True)
class _TimedProgress:
    at: float
    completed: int
    bytes_completed: int | None


class ProgressEstimator:
    """Estimate a phase from a bounded window without affecting the work.

    Samples never cross an operation or phase boundary. A decreasing counter
    means a new attempt reused the same name, so its old rate is discarded.
    ``now`` is injectable for deterministic tests; production uses monotonic
    time and is therefore unaffected by wall-clock corrections between PCs.
    """

    def __init__(self, *, window: int = 40) -> None:
        if window < 2:
            raise ValueError("progress estimate window must hold at least two samples")
        self._window = window
        self._samples: dict[tuple[str, str], deque[_TimedProgress]] = {}

    def update(self, event: ProgressEvent, *, now: float | None = None) -> ProgressEstimate:
        at = time.monotonic() if now is None else float(now)
        key = event.operation, event.phase
        samples = self._samples.setdefault(key, deque(maxlen=self._window))
        if samples and (
            event.completed < samples[-1].completed
            or (
                event.bytes_completed is not None
                and samples[-1].bytes_completed is not None
                and event.bytes_completed < samples[-1].bytes_completed
            )
        ):
            samples.clear()
        samples.append(_TimedProgress(at, event.completed, event.bytes_completed))
        if len(samples) < 2:
            return ProgressEstimate(None, None, None)

        first, last = samples[0], samples[-1]
        elapsed = last.at - first.at
        completed = last.completed - first.completed
        item_rate = completed / elapsed if elapsed > 0 and completed > 0 else None
        byte_rate = None
        if (
            elapsed > 0
            and first.bytes_completed is not None
            and last.bytes_completed is not None
            and last.bytes_completed > first.bytes_completed
        ):
            byte_rate = (last.bytes_completed - first.bytes_completed) / elapsed

        remaining = None
        if event.total is not None and item_rate is not None:
            remaining = max(0.0, (event.total - event.completed) / item_rate)
        return ProgressEstimate(item_rate, byte_rate, remaining)


def report(progress: ProgressCallback | None, phase: str, done: int, total: int) -> None:
    """Tell the caller where the read is, and never fail because of it."""
    if progress is None:
        return
    try:
        progress(phase, done, total)
    except Exception:  # noqa: BLE001 - a broken indicator must not break a read
        pass
