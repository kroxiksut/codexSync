from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .guardian_models import GuardianConfig
from .jsonl_codec import JsonlCodec
from .path_mapping import PathMappingRule
from .process_knowledge import default_background_process_names, default_process_names


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
    #: What `codexsync sync` carries: ``full`` -- settings, chats and projects,
    #: the window's Synchronise -- or ``settings``, the files under
    #: `targets.include_roots` only (D-028).
    scope: str = "full"
    #: Ask Codex to quit when a sync starts while it is open (D-029). Never
    #: forced; off unless the person turns it on.
    close_codex: bool = False


@dataclass(slots=True)
class SafetyConfig:
    require_codex_stopped: bool = True
    fail_on_unknown: bool = True


@dataclass(slots=True)
class ProcessDetectionConfig:
    process_names: list[str] = field(default_factory=default_process_names)
    grace_period_seconds: int = 2
    # Kept only to produce a clear migration error for old configuration files.
    # codexSync 0.2 never terminates Codex.
    allow_terminate_if_running: bool = False
    manual_terminate_confirmation: bool = True
    terminate_confirmation_mode: str = "gui"
    terminate_timeout_seconds: int = 20
    background_process_names: dict[str, list[str]] = field(
        default_factory=default_background_process_names
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
    #: Whether the file names `include_roots` at all. A written empty list is
    #: refused; an absent key synchronises nothing (CS-289).
    listed: bool = False


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


#: Accepted values of `[semantic] new_chats`, the default first.
NEW_CHATS_VALUES: tuple[str, ...] = ("same_path", "keep_in_cloud")


@dataclass(slots=True, frozen=True)
class SemanticConfig:
    root_dir: Path
    max_jsonl_line_bytes: int = 64 * 1024 * 1024
    #: Container the cloud mirror stores a branch in. Only the mirror: a branch
    #: written into a directory the Codex runtime reads is always plain JSONL.
    mirror_compression: JsonlCodec = JsonlCodec.XZ
    #: What a transfer does with a chat this machine has never held:
    #: `same_path` (the default) writes it into `.codex` at the path it has on
    #: its own machine, `keep_in_cloud` leaves it in the mirror (D-020).
    new_chats: str = "same_path"


#: Jobs the scheduler may run. Every one of them is read-only towards the Codex
#: state: a scheduled task fires with nobody watching, so it can never be the
#: thing that decides a write is safe. `sync` is deliberately not a mode.
SCHEDULER_MODES: tuple[str, ...] = ("guardian_snapshot", "preflight", "sync_dry_run")

#: Shortest accepted repeat interval: five minutes. A periodic job is a
#: safety net, not a watcher -- Guardian's snapshot is worth taking when the
#: state has had time to change, and each run samples the process list and
#: may write a snapshot into a cloud-synced folder.
MIN_SCHEDULER_INTERVAL_SECONDS = 300
#: What a config that names no interval gets: half an hour.
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 1800
#: Task Scheduler's longest repetition interval (`RepetitionPattern.Interval`):
#: a task asking for more is refused at registration. Every platform is held
#: to it, so one config means the same schedule everywhere.
MAX_SCHEDULER_INTERVAL_SECONDS = 31 * 24 * 3600


@dataclass(slots=True, frozen=True)
class SchedulerConfig:
    """The `[scheduler]` section (CS-232): periodic safe work as a user-level task.

    Values are stored as read from the file and checked by `_validate_config`
    rather than coerced here, so `enabled = "true"` is refused instead of being
    silently read as a truthy string.
    """

    enabled: bool = False
    mode: str = "guardian_snapshot"
    interval_seconds: int = DEFAULT_SCHEDULER_INTERVAL_SECONDS
    run_at_login: bool = True
    startup_delay_seconds: int = 0
    jitter_seconds: int = 0
    #: A settings sync once after sign-in, in its own task (CS-267, `D-016`).
    #: Independent of ``enabled``/``mode``, which stay read-only and periodic.
    sync_at_login: bool = False


#: Longest accepted period between two copies of the Codex state: the OS
#: repetition limit (31 days), since the copy runs as a repeating OS task.
MAX_STATE_BACKUP_INTERVAL_HOURS = MAX_SCHEDULER_INTERVAL_SECONDS // 3600


@dataclass(slots=True, frozen=True)
class StateBackupConfig:
    """The `[state_backup]` section (CS-276, `D-017`): copies of `.codex`.

    ``root_dir`` is ``None`` until the user chooses a folder: nothing proposes
    one, and without it no copy is taken and no task can be switched on.
    Values are kept as read and checked by `_validate_config`, like
    `SchedulerConfig`.
    """

    root_dir: Path | None = None
    #: A copy once after signing in (waiting for Codex to close if it is open).
    at_login: bool = False
    #: A copy every N hours; 0 switches the periodic copy off.
    interval_hours: int = 0
    #: How many copies of this machine to keep; the oldest go only after a
    #: newer copy has been written and verified.
    keep: int = 5

    @property
    def scheduled(self) -> bool:
        return self.at_login is True or (
            isinstance(self.interval_hours, int) and not isinstance(self.interval_hours, bool)
            and self.interval_hours > 0
        )


#: Longest a sign-in load waits for the cloud to deliver another machine's
#: handoff: a day, beyond which the wait is no longer about delivery.
MAX_HANDOFF_DELIVERY_WAIT_MINUTES = 24 * 60


@dataclass(slots=True, frozen=True)
class HandoffConfig:
    """The `[handoff]` section (CS-328, `D-018`): passing work between machines.

    ``root_dir`` holds one small file per machine saying what it last handed
    off; it has to be in the folder the cloud client syncs, beside the mirror.
    ``None`` means no handoff at all. ``enabled`` installs the background
    watcher that loads at sign-in and hands off when Codex closes; without it
    the same handoff is still available by hand. Values are kept as read and
    checked by `_validate_config`, like `SchedulerConfig`.
    """

    root_dir: Path | None = None
    enabled: bool = False
    #: How long a load waits for another machine's handoff to arrive.
    delivery_wait_minutes: int = 15
    #: Say what happened with an operating-system notification.
    notify: bool = True


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
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    state_backup: StateBackupConfig = field(default_factory=StateBackupConfig)
    handoff: HandoffConfig = field(default_factory=HandoffConfig)


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
    """The two-sided fingerprints one machine recorded at its last sync.

    The file lives beside the cloud mirror and is shared by every machine, so
    it holds one baseline *per machine* (CS-288): ``files`` is this machine's,
    and ``others`` carries every other machine's entries through untouched.
    A single shared pair made machine B read machine A's local fingerprint as
    its own, see its stale file as "changed", and copy it over A's newer one.
    """

    data_version: int
    files: dict[str, ManifestEntry] = field(default_factory=dict)
    #: Whose baseline ``files`` is; ``None`` only for an empty manifest.
    machine_id: str | None = None
    #: Other machines' baselines, kept exactly as read so saving never drops them.
    others: dict[str, dict[str, ManifestEntry]] = field(default_factory=dict)


@dataclass(slots=True)
class DeleteAction:
    """One file to remove because the other side proved it was removed there.

    Only ever produced under `sync.delete_policy = "propagate"`, and only for a
    path the previous manifest shows both sides held and this side has not
    touched since (`D-013`). Carries the relative path so the file can be
    backed up -- and restored -- by the same name as any overwrite.
    """

    path: Path
    relative_path: str
    #: ``local`` or ``cloud``: which side the file is being removed from.
    side: str


@dataclass
class SyncPlan:
    to_local: list[CopyAction] = field(default_factory=list)
    to_cloud: list[CopyAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    #: Files a proven deletion on the other side removes here.
    deletions: list[DeleteAction] = field(default_factory=list)
    #: Paths a one-way direction did not act on. They are *not* synchronised,
    #: which is why the manifest carries their previous entry over unchanged
    #: instead of recording what both sides look like now (`D-012`).
    skipped: list[str] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        return len(self.to_local) + len(self.to_cloud) + len(self.deletions)
