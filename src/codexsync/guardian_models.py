from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
from uuid import UUID, uuid4


# These names are a versioned Guardian storage contract. A committed snapshot
# is self-contained and consumers must not infer alternative names.
GUARDIAN_STORAGE_VERSION = 1
GUARDIAN_PAYLOAD_NAME = ".codex-global-state.json"
GUARDIAN_MANIFEST_NAME = "manifest.json"
GUARDIAN_COMMITTED_NAME = "COMMITTED"
GUARDIAN_STAGING_DIR_NAME = ".staging"
GUARDIAN_QUARANTINE_DIR_NAME = "quarantine"
GUARDIAN_LATEST_GOOD_DIR_NAME = "latest-good"
GUARDIAN_SNAPSHOTS_DIR_NAME = "snapshots"
GUARDIAN_LOCKS_DIR_NAME = "locks"
SNAPSHOT_HASH_PREFIX_LENGTH = 12


class ValidationStatus(str, Enum):
    PASS = "PASS"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    UNCHANGED = "UNCHANGED"
    INVALID = "INVALID"
    INDETERMINATE = "INDETERMINATE"
    SUSPICIOUS = "SUSPICIOUS"


class GuardianResultStatus(str, Enum):
    COMMITTED = "COMMITTED"
    UNCHANGED = "UNCHANGED"
    QUARANTINED = "QUARANTINED"
    BUSY = "BUSY"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class GuardianConfig:
    """Location of the independent Guardian store, always outside .codex."""

    root_dir: Path
    max_state_bytes: int = 64 * 1024 * 1024
    shrink_min_count: int = 2
    shrink_ratio: float = 0.25
    retention_days: int = 30
    max_snapshots: int = 100
    quarantine_retention_days: int = 30
    staging_retention_hours: int = 24
    poll_interval_seconds: float = 3.0
    debounce_seconds: float = 2.0
    stable_reads: int = 3
    stable_read_interval_seconds: float = 0.5
    fallback_scan_seconds: float = 60.0
    once_timeout_seconds: float = 120.0


@dataclass(slots=True, frozen=True)
class SourceObservation:
    """Immutable hand-off from a future watcher to the Guardian core."""

    payload: bytes
    source_name: str
    source_size_before: int | None
    source_size_after: int | None
    source_mtime_ns_before: int | None
    source_mtime_ns_after: int | None
    source_file_id_before: str | None
    source_file_id_after: str | None
    is_stable: bool


@dataclass(slots=True, frozen=True)
class GuardianSnapshot:
    """An immutable committed snapshot location and its identity."""

    root_dir: Path
    machine_id: str
    snapshot_id: str
    generation: int

    @property
    def directory(self) -> Path:
        return self.root_dir / GUARDIAN_SNAPSHOTS_DIR_NAME / self.machine_id / self.snapshot_id

    @property
    def payload_path(self) -> Path:
        return self.directory / GUARDIAN_PAYLOAD_NAME

    @property
    def manifest_path(self) -> Path:
        return self.directory / GUARDIAN_MANIFEST_NAME

    @property
    def committed_path(self) -> Path:
        return self.directory / GUARDIAN_COMMITTED_NAME


@dataclass(slots=True, frozen=True)
class GuardianManifest:
    manifest_version: int
    snapshot_id: str
    generation: int
    created_at_utc: str
    machine_id: str
    source_name: str
    source_size: int
    source_mtime_ns_before: int
    source_mtime_ns_after: int
    source_file_id_before: str | None
    source_file_id_after: str | None
    hash_algorithm: str
    sha256: str
    validation_status: ValidationStatus
    validation_codes: tuple[str, ...]
    project_count: int
    binding_count: int
    previous_good_snapshot_id: str | None
    previous_good_sha256: str | None
    producer_version: str
    schema_id: str | None = None


@dataclass(slots=True, frozen=True)
class ValidationReport:
    status: ValidationStatus
    codes: tuple[str, ...] = ()
    project_count: int | None = None
    binding_count: int | None = None
    schema_id: str | None = None


@dataclass(slots=True, frozen=True)
class GuardianResult:
    status: GuardianResultStatus
    validation: ValidationReport | None = None
    snapshot: GuardianSnapshot | None = None
    quarantine_event_id: str | None = None
    detail: str | None = None


def normalize_machine_id(raw: str | None, *, allow_unknown: bool = True) -> str | None:
    """Return the shared backup/Guardian-safe machine-id representation."""
    if raw is None or not raw.strip():
        return "unknown-machine" if allow_unknown else None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw.strip()).strip("-.")
    if not cleaned:
        return "unknown-machine" if allow_unknown else None
    return cleaned


def require_guardian_machine_id(raw: str | None) -> str:
    machine_id = normalize_machine_id(raw, allow_unknown=False)
    if machine_id is None:
        raise ValueError("Guardian requires a non-empty identity.machine_id")
    return machine_id


def build_snapshot_id(
    payload_sha256: str,
    *,
    operation_id: UUID | str | None = None,
    now: datetime | None = None,
) -> str:
    """Build a unique, inspectable snapshot id without relying only on clock precision."""
    sha256 = payload_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("payload_sha256 must be a 64-character SHA-256 hex digest")
    operation_uuid = UUID(str(operation_id)) if operation_id is not None else uuid4()
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    formatted_timestamp = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{formatted_timestamp}-{operation_uuid}-{sha256[:SNAPSHOT_HASH_PREFIX_LENGTH]}"
