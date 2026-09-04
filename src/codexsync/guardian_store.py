from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .exceptions import GuardianIntegrityError
from .guardian_lock import GuardianWriterLock
from .guardian_manifest import (
    build_guardian_manifest,
    load_guardian_manifest,
    serialize_guardian_manifest,
    verify_guardian_snapshot,
)
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_PAYLOAD_NAME,
    GUARDIAN_QUARANTINE_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GUARDIAN_STAGING_DIR_NAME,
    GuardianManifest,
    GuardianResult,
    GuardianResultStatus,
    GuardianSnapshot,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
    build_snapshot_id,
    require_guardian_machine_id,
)
from .guardian_pointer import publish_latest_good
from .guardian_retention import prune_quarantine, prune_snapshots, prune_staging


QUARANTINE_PAYLOAD_NAME = "source.bin"
QUARANTINE_REASON_CODES = frozenset(
    {
        "NUL_BYTE", "UNSUPPORTED_BOM", "INVALID_UTF8", "INVALID_JSON", "DUPLICATE_JSON_KEY",
        "ROOT_NOT_OBJECT", "SOURCE_TOO_LARGE", "SOURCE_DISAPPEARED", "UNKNOWN_SCHEMA",
        "BROKEN_PROJECT_REFERENCE", "BROKEN_ORDER_REFERENCE", "BROKEN_BINDING_REFERENCE",
        "PROJECT_COUNT_DROP", "BINDING_COUNT_DROP", "READ_CHANGED", "READ_ERROR", "VALIDATOR_ERROR",
        "DUPLICATE_ORDER_REFERENCE", "BASELINE_UNVERIFIED", "BASELINE_SCHEMA_MISMATCH",
    }
)


class GuardianStore:
    """Transactional writer for the immutable Guardian snapshot store."""

    def __init__(
        self,
        root_dir: Path,
        machine_id: str,
        *,
        producer_version: str,
        retention_days: int = 30,
        max_snapshots: int = 100,
        quarantine_retention_days: int = 30,
        staging_retention_hours: int = 24,
    ) -> None:
        self.root_dir = root_dir.resolve()
        self.machine_id = require_guardian_machine_id(machine_id)
        self.producer_version = producer_version
        self.retention_days = retention_days
        self.max_snapshots = max_snapshots
        self.quarantine_retention_days = quarantine_retention_days
        self.staging_retention_hours = staging_retention_hours

    def commit(
        self,
        observation: SourceObservation,
        validation: ValidationReport,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> GuardianResult:
        if validation.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
            raise GuardianIntegrityError("Only fully validated Guardian observations may be committed")
        operation_id = str(uuid4())
        with GuardianWriterLock(self.root_dir, self.machine_id, operation_id=operation_id):
            duplicate = self._find_committed_duplicate(observation.payload)
            if duplicate is not None:
                return GuardianResult(
                    GuardianResultStatus.UNCHANGED,
                    validation=validation,
                    snapshot=duplicate,
                )
            previous = self._latest_committed_manifest()
            generation = 1 if previous is None else previous.generation + 1
            payload_sha256 = hashlib.sha256(observation.payload).hexdigest()
            snapshot = GuardianSnapshot(
                root_dir=self.root_dir,
                machine_id=self.machine_id,
                snapshot_id=build_snapshot_id(payload_sha256, operation_id=operation_id),
                generation=generation,
            )
            manifest = build_guardian_manifest(
                snapshot,
                observation,
                validation,
                producer_version=self.producer_version,
                previous_good=previous,
            )
            stage_dir = self._stage_directory(operation_id)
            try:
                self._write_stage(stage_dir, observation.payload, manifest, fault_hook)
                self._publish_stage(stage_dir, snapshot, fault_hook)
                self._write_committed_marker(snapshot, manifest, fault_hook)
            except Exception:
                # Staging and uncommitted snapshot directories are intentionally retained
                # for crash diagnostics; neither is a readable committed snapshot.
                raise
            verify_guardian_snapshot(snapshot, predecessor=previous)
            publish_latest_good(self.root_dir, snapshot)
            prune_snapshots(
                self.root_dir,
                self.machine_id,
                retention_days=self.retention_days,
                max_snapshots=self.max_snapshots,
            )
            prune_staging(self.root_dir, self.machine_id, retention_hours=self.staging_retention_hours)
            return GuardianResult(
                GuardianResultStatus.COMMITTED,
                validation=validation,
                snapshot=snapshot,
            )

    def quarantine(self, observation: SourceObservation, validation: ValidationReport) -> GuardianResult:
        """Persist rejected data diagnostically without ever creating COMMITTED."""
        if validation.status not in {
            ValidationStatus.INVALID,
            ValidationStatus.INDETERMINATE,
            ValidationStatus.SUSPICIOUS,
        }:
            raise GuardianIntegrityError("Only rejected Guardian observations may be quarantined")
        _validate_quarantine_codes(validation.codes)
        operation_id = str(uuid4())
        with GuardianWriterLock(self.root_dir, self.machine_id, operation_id=operation_id):
            payload = observation.payload if _has_stable_payload(observation) else None
            payload_sha256 = hashlib.sha256(payload).hexdigest() if payload is not None else None
            existing_event = self._find_quarantine_duplicate(payload_sha256, validation.codes)
            if existing_event is not None:
                return GuardianResult(
                    GuardianResultStatus.QUARANTINED,
                    validation=validation,
                    quarantine_event_id=existing_event,
                    detail="duplicate",
                )
            event_id = _build_event_id(operation_id)
            stage_dir = self._stage_directory(f"quarantine-{operation_id}")
            _mkdir_private(stage_dir)
            if payload is not None:
                _write_private_file(stage_dir / QUARANTINE_PAYLOAD_NAME, payload)
            event_manifest = {
                "event_id": event_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "machine_id": self.machine_id,
                "category": validation.status.value,
                "reason_codes": list(validation.codes),
                "explanation": _safe_explanation(validation.codes),
                "source_size": len(payload) if payload is not None else None,
                "sha256": payload_sha256,
                "payload_saved": payload is not None,
                "project_count": validation.project_count,
                "binding_count": validation.binding_count,
            }
            _write_private_file(
                stage_dir / GUARDIAN_MANIFEST_NAME,
                (json.dumps(event_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            )
            destination_parent = self.root_dir / GUARDIAN_QUARANTINE_DIR_NAME / self.machine_id
            _ensure_private_dir(destination_parent)
            destination = destination_parent / event_id
            if destination.exists():
                raise GuardianIntegrityError("Refusing to overwrite an existing Guardian quarantine event")
            os.replace(stage_dir, destination)
            _fsync_directory(destination_parent)
            prune_quarantine(self.root_dir, self.machine_id, retention_days=self.quarantine_retention_days)
            return GuardianResult(
                GuardianResultStatus.QUARANTINED,
                validation=validation,
                quarantine_event_id=event_id,
            )

    def _stage_directory(self, operation_id: str) -> Path:
        return self.root_dir / GUARDIAN_STAGING_DIR_NAME / self.machine_id / operation_id

    def _write_stage(
        self,
        stage_dir: Path,
        payload: bytes,
        manifest: GuardianManifest,
        fault_hook: Callable[[str], None] | None,
    ) -> None:
        _mkdir_private(stage_dir)
        _write_private_file(stage_dir / GUARDIAN_PAYLOAD_NAME, payload)
        _fault(fault_hook, "payload_written")
        _write_private_file(stage_dir / GUARDIAN_MANIFEST_NAME, serialize_guardian_manifest(manifest))
        _fault(fault_hook, "manifest_written")
        staged_snapshot = GuardianSnapshot(self.root_dir, self.machine_id, manifest.snapshot_id, manifest.generation)
        _verify_staged_payload(stage_dir, manifest)
        _fault(fault_hook, "validated")
        # The staged layout must already be the final snapshot layout internally.
        if stage_dir.name == staged_snapshot.snapshot_id:
            raise AssertionError("staging operation id must never be a snapshot id")

    def _publish_stage(
        self,
        stage_dir: Path,
        snapshot: GuardianSnapshot,
        fault_hook: Callable[[str], None] | None,
    ) -> None:
        destination_parent = snapshot.directory.parent
        _ensure_private_dir(destination_parent)
        if snapshot.directory.exists():
            raise GuardianIntegrityError("Refusing to overwrite an existing Guardian snapshot")
        os.replace(stage_dir, snapshot.directory)
        _fsync_directory(destination_parent)
        _fault(fault_hook, "snapshot_published")

    def _write_committed_marker(
        self,
        snapshot: GuardianSnapshot,
        manifest: GuardianManifest,
        fault_hook: Callable[[str], None] | None,
    ) -> None:
        payload = {
            "snapshot_id": manifest.snapshot_id,
            "generation": manifest.generation,
            "sha256": manifest.sha256,
            "committed_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        marker = snapshot.committed_path
        temp_marker = marker.with_name(f".{GUARDIAN_COMMITTED_NAME}.{uuid4().hex}.tmp")
        _write_private_file(temp_marker, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.replace(temp_marker, marker)
        _fsync_directory(snapshot.directory)
        _fault(fault_hook, "committed")

    def _latest_committed_manifest(self) -> GuardianManifest | None:
        snapshots_root = self.root_dir / GUARDIAN_SNAPSHOTS_DIR_NAME / self.machine_id
        if not snapshots_root.exists():
            return None
        candidates: list[tuple[GuardianManifest, GuardianSnapshot]] = []
        for directory in snapshots_root.iterdir():
            if not directory.is_dir() or not (directory / GUARDIAN_COMMITTED_NAME).is_file():
                continue
            try:
                manifest = load_guardian_manifest(directory / GUARDIAN_MANIFEST_NAME)
                snapshot = GuardianSnapshot(self.root_dir, self.machine_id, manifest.snapshot_id, manifest.generation)
                if snapshot.directory != directory:
                    continue
                verify_guardian_snapshot(snapshot)
                _verify_committed_marker(snapshot, manifest)
            except GuardianIntegrityError:
                continue
            candidates.append((manifest, snapshot))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0].generation, reverse=True)
        highest = candidates[0][0]
        if sum(1 for manifest, _ in candidates if manifest.generation == highest.generation) != 1:
            raise GuardianIntegrityError("Guardian committed snapshots have duplicate generations")
        return highest

    def _find_committed_duplicate(self, payload: bytes) -> GuardianSnapshot | None:
        root = self.root_dir / GUARDIAN_SNAPSHOTS_DIR_NAME / self.machine_id
        if not root.is_dir():
            return None
        digest = hashlib.sha256(payload).hexdigest()
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not (directory / GUARDIAN_COMMITTED_NAME).is_file():
                continue
            try:
                manifest = load_guardian_manifest(directory / GUARDIAN_MANIFEST_NAME)
                snapshot = GuardianSnapshot(self.root_dir, self.machine_id, manifest.snapshot_id, manifest.generation)
                if snapshot.directory != directory or manifest.sha256 != digest or manifest.source_size != len(payload):
                    continue
                _verify_committed_marker(snapshot, manifest)
                if snapshot.payload_path.read_bytes() == payload:
                    return snapshot
            except (GuardianIntegrityError, OSError):
                continue
        return None

    def _find_quarantine_duplicate(self, payload_sha256: str | None, reason_codes: tuple[str, ...]) -> str | None:
        if payload_sha256 is None:
            return None
        root = self.root_dir / GUARDIAN_QUARANTINE_DIR_NAME / self.machine_id
        if not root.exists():
            return None
        for event_dir in root.iterdir():
            if event_dir.is_symlink() or not event_dir.is_dir():
                continue
            try:
                raw = json.loads((event_dir / GUARDIAN_MANIFEST_NAME).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(raw, dict)
                and raw.get("sha256") == payload_sha256
                and raw.get("reason_codes") == list(reason_codes)
                and isinstance(raw.get("event_id"), str)
            ):
                return raw["event_id"]
        return None


def _verify_staged_payload(stage_dir: Path, manifest: GuardianManifest) -> None:
    payload_path = stage_dir / GUARDIAN_PAYLOAD_NAME
    try:
        payload = payload_path.read_bytes()
    except OSError as exc:
        raise GuardianIntegrityError("Cannot reread staged Guardian payload") from exc
    if len(payload) != manifest.source_size or hashlib.sha256(payload).hexdigest() != manifest.sha256:
        raise GuardianIntegrityError("Staged Guardian payload does not match manifest")


def _verify_committed_marker(snapshot: GuardianSnapshot, manifest: GuardianManifest) -> None:
    try:
        raw = json.loads(snapshot.committed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardianIntegrityError("Guardian COMMITTED marker is invalid") from exc
    if not isinstance(raw, dict):
        raise GuardianIntegrityError("Guardian COMMITTED marker root must be an object")
    if (
        raw.get("snapshot_id") != manifest.snapshot_id
        or raw.get("generation") != manifest.generation
        or raw.get("sha256") != manifest.sha256
        or not isinstance(raw.get("committed_at_utc"), str)
    ):
        raise GuardianIntegrityError("Guardian COMMITTED marker does not match manifest")


def _write_private_file(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            if os.name != "nt":
                os.chmod(path, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise GuardianIntegrityError(f"Cannot write Guardian storage artifact: {path.name}") from exc


def _mkdir_private(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise GuardianIntegrityError(f"Guardian storage artifact already exists: {path.name}") from exc
    except OSError as exc:
        raise GuardianIntegrityError(f"Cannot create Guardian storage artifact: {path.name}") from exc
    if os.name != "nt":
        os.chmod(path, 0o700)


def _ensure_private_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise GuardianIntegrityError(f"Cannot create Guardian storage directory: {path.name}") from exc
    if not path.is_dir():
        raise GuardianIntegrityError(f"Guardian storage path is not a directory: {path.name}")
    if os.name != "nt":
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


def _fault(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _has_stable_payload(observation: SourceObservation) -> bool:
    return (
        observation.is_stable
        and observation.source_size_before == observation.source_size_after == len(observation.payload)
        and observation.source_mtime_ns_before == observation.source_mtime_ns_after
        and observation.source_size_before is not None
        and observation.source_mtime_ns_before is not None
        and observation.source_file_id_before == observation.source_file_id_after
    )


def _validate_quarantine_codes(codes: tuple[str, ...]) -> None:
    if not codes or any(code not in QUARANTINE_REASON_CODES for code in codes):
        raise GuardianIntegrityError("Guardian quarantine has unsupported reason codes")


def _safe_explanation(codes: tuple[str, ...]) -> str:
    return {
        "NUL_BYTE": "Source bytes contain a NUL byte.",
        "UNSUPPORTED_BOM": "Source bytes contain an unsupported byte-order mark.",
        "INVALID_UTF8": "Source bytes are not valid UTF-8.",
        "INVALID_JSON": "Source bytes are not a single valid JSON document.",
        "DUPLICATE_JSON_KEY": "Source JSON contains duplicate object keys.",
        "ROOT_NOT_OBJECT": "Source JSON root is not an object.",
        "SOURCE_TOO_LARGE": "Source exceeds the configured size limit.",
        "READ_CHANGED": "Source metadata changed during reading.",
        "READ_ERROR": "Source could not be read safely.",
    }.get(codes[0], "Guardian validation rejected the source state.")


def _build_event_id(operation_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{operation_id}"
