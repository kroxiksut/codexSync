from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .guardian_models import GuardianConfig
from .jsonl_codec import JsonlCodec
from .path_mapping import PathMappingRule


@dataclass(slots=True)
class PathsConfig:
    workspace_root_dir: Path | None
    local_state_dir: Path | None
    cloud_root_dir: Path
    backup_dir: Path
    temp_dir: Path


@dataclass(slots=True)
class IdentityConfig:
    machine_id: str | None = None


@dataclass(slots=True)
class SyncConfig:
    mode: str = "cold"
    direction: str = "bidirectional"
    compare: str = "mtime"
    time_tolerance_seconds: int = 0
    equal_mtime_action: str = "skip"
    dry_run_default: bool = True
    delete_policy: str = "never"
    session_mode: str | None = None


@dataclass(slots=True)
class SafetyConfig:
    require_codex_stopped: bool = True
    fail_on_unknown: bool = True


@dataclass(slots=True)
class ProcessDetectionConfig:
    process_names: list[str] = field(default_factory=lambda: ["codex.exe", "codex"])
    grace_period_seconds: int = 2
    # Kept only to produce a clear migration error for old configuration files.
    # codexSync 0.2 never terminates Codex.
    allow_terminate_if_running: bool = False
    manual_terminate_confirmation: bool = True
    terminate_confirmation_mode: str = "gui"
    terminate_timeout_seconds: int = 20
    background_process_names: dict[str, list[str]] = field(
        default_factory=lambda: {
            "windows": ["codex-windows-sandbox"],
            "macos": [],
            "linux": [],
        }
    )


@dataclass(slots=True)
class BackupConfig:
    backup_before_overwrite: bool = True
    retention_days: int = 30
    max_backups: int = 0
    compression: str = "none"


@dataclass(slots=True)
class FiltersConfig:
    exclude_globs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TargetsConfig:
    include_roots: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConflictConfig:
    policy: str = "manual_abort"
    report_conflicts: bool = True


@dataclass(slots=True)
class StateConfig:
    manifest_file: Path | None = None
    data_version: int = 1


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    format: str = "text"
    retention_days: int = 7
    archive_mode: str = "zip"
    max_file_size_mb: int = 10
    machine_id: str | None = None


@dataclass(slots=True, frozen=True)
class SemanticConfig:
    root_dir: Path
    max_jsonl_line_bytes: int = 64 * 1024 * 1024
    #: Container the cloud mirror stores a branch in. Only the mirror: a branch
    #: written into a directory the Codex runtime reads is always plain JSONL.
    mirror_compression: JsonlCodec = JsonlCodec.XZ


@dataclass(slots=True)
class AppConfig:
    identity: IdentityConfig
    paths: PathsConfig
    sync: SyncConfig
    safety: SafetyConfig
    process_detection: ProcessDetectionConfig
    backup: BackupConfig
    filters: FiltersConfig
    targets: TargetsConfig
    conflict: ConflictConfig
    state: StateConfig
    logging: LoggingConfig
    guardian: GuardianConfig = field(default_factory=lambda: GuardianConfig(root_dir=Path("guardian")))
    path_mappings: list[PathMappingRule] = field(default_factory=list)
    semantic: SemanticConfig = field(default_factory=lambda: SemanticConfig(root_dir=Path("semantic")))


@dataclass(slots=True, frozen=True)
class FileMeta:
    relative_path: str
    abs_path: Path
    mtime_ns: int
    size: int


@dataclass(slots=True, frozen=True)
class CopyAction:
    src: Path
    dst: Path
    relative_path: str
    #: Container the destination is written in, for an action that knows it is
    #: moving a session branch. ``None`` -- the default, and what every ordinary
    #: file copy uses -- means the bytes are carried across untouched.
    #:
    #: The distinction is load-bearing: a file named `notes.jsonl.gz` under an
    #: included root is a user's file, not a branch, and a plain `sync` that
    #: decompressed it because of its name would write content that no longer
    #: matches it. Only a transfer plan, which knows what it is carrying, sets
    #: this -- to NONE for a directory the Codex runtime reads, or to the
    #: mirror's container.
    codec: JsonlCodec | None = None


@dataclass(slots=True, frozen=True)
class SnapshotFingerprint:
    mtime_ns: int
    size: int


@dataclass(slots=True, frozen=True)
class ManifestEntry:
    local: SnapshotFingerprint | None
    cloud: SnapshotFingerprint | None


@dataclass(slots=True)
class SyncManifest:
    data_version: int
    files: dict[str, ManifestEntry] = field(default_factory=dict)


@dataclass(slots=True)
class SyncPlan:
    to_local: list[CopyAction] = field(default_factory=list)
    to_cloud: list[CopyAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        return len(self.to_local) + len(self.to_cloud)
