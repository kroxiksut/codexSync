from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .guardian_models import GuardianConfig, require_guardian_machine_id
from .jsonl_codec import parse_codec
from .path_mapping import PathMappingRule
from .process_knowledge import default_background_process_names, default_process_names
from .models import (
    MAX_HANDOFF_DELIVERY_WAIT_MINUTES,
    MAX_SCHEDULER_INTERVAL_SECONDS,
    MAX_STATE_BACKUP_INTERVAL_HOURS,
    MIN_SCHEDULER_INTERVAL_SECONDS,
    NEW_CHATS_VALUES,
    SCHEDULER_MODES,
    AppConfig,
    BackupConfig,
    ConflictConfig,
    FiltersConfig,
    HandoffConfig,
    IdentityConfig,
    LoggingConfig,
    PathsConfig,
    ProcessDetectionConfig,
    SafetyConfig,
    SchedulerConfig,
    SemanticConfig,
    StateBackupConfig,
    StateConfig,
    SyncConfig,
    TargetsConfig,
)


#: Every substitution a path value may use, in one place. A screen that lists
#: them reads this rather than keeping a second copy in a language file, so a
#: new substitution cannot be documented in one language and forgotten in the
#: other -- or documented and never implemented.
PATH_SUBSTITUTIONS: tuple[str, ...] = ("${workspace_root}",)
#: `[sync] scope`: what `codexsync sync` carries (D-028). ``full`` is the
#: window's Synchronise -- settings, chats, projects; ``settings`` is the files
#: under `targets.include_roots` only, which is all `sync` did before D-028.
SYNC_SCOPES: tuple[str, ...] = ("full", "settings")


def preview_path(
    value: str,
    *,
    base_dir: Path,
    workspace_root: Path | None = None,
) -> Path | None:
    """What a path field in the config would resolve to, without loading it.

    The same resolution the loader performs, exposed so a settings screen can
    show the computed path under the box as it is typed. Raises ``ConfigError``
    for a value the loader would also refuse -- which is the answer to show.
    """
    return _to_path(
        value, "path", base_dir=base_dir, workspace_root=workspace_root, required=False
    )


def _to_path(
    value: str | None,
    field_name: str,
    *,
    base_dir: Path,
    workspace_root: Path | None = None,
    required: bool = True,
) -> Path | None:
    if not value:
        if required:
            raise ConfigError(f"Missing required path field: {field_name}")
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string path")

    resolved = strip_extended_length_prefix(_expand_workspace_var(value, workspace_root, field_name))
    raw = Path(resolved).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    anchor = workspace_root if workspace_root else base_dir
    return (anchor / raw).resolve()


_EXTENDED_UNC = re.compile(r"^[\\/]{2}\?[\\/]UNC[\\/]", re.IGNORECASE)
_EXTENDED = re.compile(r"^[\\/]{2}\?[\\/]")


def strip_extended_length_prefix(value: str) -> str:
    r"""``\\?\C:\x`` -> ``C:\x`` and ``\\?\UNC\srv\share`` -> ``\\srv\share`` (CS-320).

    `Path.resolve` turns an extended-length path into ``C:x`` -- a drive
    without a root -- so it compared as unrelated to every other folder and
    walked straight past each overlap check. The prefix only lifts the length
    limit; the folder it names is the one without it.
    """
    if _EXTENDED_UNC.match(value):
        return "\\\\" + _EXTENDED_UNC.sub("", value, count=1)
    return _EXTENDED.sub("", value, count=1)


def _expand_workspace_var(raw_value: str, workspace_root: Path | None, field_name: str) -> str:
    token = "${workspace_root}"
    if token not in raw_value:
        return raw_value
    if workspace_root is None:
        raise ConfigError(
            f"{field_name} uses {token}, but paths.workspace_root_dir is not configured"
        )
    return raw_value.replace(token, str(workspace_root))


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {path}. {exc}") from exc
    return parse_config_text(
        decode_config_bytes(data, source=str(path)),
        base_dir=path.parent.resolve(),
        source=str(path),
    )


def decode_config_bytes(data: bytes, *, source: str) -> str:
    """Decode a config file as UTF-8, accepting a byte-order mark.

    TOML is UTF-8 by definition, but Windows editors still prepend a BOM, and
    a file one tool accepts while another refuses is worse than either rule.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Config file is not valid UTF-8: {source}. {exc}") from exc


def parse_config_text(text: str, *, base_dir: Path, source: str = "<config text>") -> AppConfig:
    """Parse and validate config text as if it were a file inside ``base_dir``.

    This is the only loader: `load_config` reads a file and calls it, and a
    GUI validating an unsaved edit calls it directly. A second validator for
    unsaved text would drift from the one the CLI enforces, and then an edit
    the editor accepted could make every command exit 4.
    """
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {source}: {exc}") from exc

    identity_raw = _section(raw, "identity")
    paths_raw = _section(raw, "paths")
    sync_raw = _section(raw, "sync")
    safety_raw = _section(raw, "safety")
    proc_raw = _section(raw, "process_detection")
    backup_raw = _section(raw, "backup")
    filters_raw = _section(raw, "filters")
    targets_raw = _section(raw, "targets")
    conflict_raw = _section(raw, "conflict")
    state_raw = _section(raw, "state")
    logging_raw = _section(raw, "logging")
    guardian_raw = _section(raw, "guardian")
    semantic_raw = _section(raw, "semantic")
    scheduler_raw = _section(raw, "scheduler")
    state_backup_raw = _section(raw, "state_backup")
    handoff_raw = _section(raw, "handoff")
    path_mappings_raw = raw.get("path_mappings", [])

    identity = IdentityConfig(machine_id=identity_raw.get("machine_id"))

    workspace_root_dir = _to_path(
        paths_raw.get("workspace_root_dir"),
        "paths.workspace_root_dir",
        base_dir=base_dir,
        required=False,
    )
    cloud_root_dir = _to_path(
        paths_raw.get("cloud_root_dir"),
        "paths.cloud_root_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    backup_dir = _to_path(
        paths_raw.get("backup_dir"),
        "paths.backup_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    temp_dir = _to_path(
        paths_raw.get("temp_dir"),
        "paths.temp_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    if cloud_root_dir is None or backup_dir is None or temp_dir is None:
        raise ConfigError("paths.cloud_root_dir, paths.backup_dir and paths.temp_dir are required")

    paths = PathsConfig(
        workspace_root_dir=workspace_root_dir,
        local_state_dir=_to_path(
            paths_raw.get("local_state_dir"),
            "paths.local_state_dir",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        cloud_root_dir=cloud_root_dir,
        backup_dir=backup_dir,
        temp_dir=temp_dir,
    )

    guardian_root = _to_path(
        guardian_raw.get("root_dir", "${workspace_root}/guardian" if workspace_root_dir else "guardian"),
        "guardian.root_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    assert guardian_root is not None
    guardian = GuardianConfig(
        root_dir=guardian_root,
        max_state_bytes=_int_value(guardian_raw, "max_state_bytes", 64 * 1024 * 1024, "guardian.max_state_bytes"),
        shrink_min_count=_int_value(guardian_raw, "shrink_min_count", 2, "guardian.shrink_min_count"),
        shrink_ratio=_float_value(guardian_raw, "shrink_ratio", 0.25, "guardian.shrink_ratio"),
        retention_days=_int_value(guardian_raw, "retention_days", 30, "guardian.retention_days"),
        max_snapshots=_int_value(guardian_raw, "max_snapshots", 100, "guardian.max_snapshots"),
        quarantine_retention_days=_int_value(guardian_raw, "quarantine_retention_days", 30, "guardian.quarantine_retention_days"),
        staging_retention_hours=_int_value(guardian_raw, "staging_retention_hours", 24, "guardian.staging_retention_hours"),
        poll_interval_seconds=_float_value(guardian_raw, "poll_interval_seconds", 3, "guardian.poll_interval_seconds"),
        debounce_seconds=_float_value(guardian_raw, "debounce_seconds", 2, "guardian.debounce_seconds"),
        stable_reads=_int_value(guardian_raw, "stable_reads", 3, "guardian.stable_reads"),
        stable_read_interval_seconds=_float_value(guardian_raw, "stable_read_interval_seconds", 0.5, "guardian.stable_read_interval_seconds"),
        fallback_scan_seconds=_float_value(guardian_raw, "fallback_scan_seconds", 60, "guardian.fallback_scan_seconds"),
        once_timeout_seconds=_float_value(guardian_raw, "once_timeout_seconds", 120, "guardian.once_timeout_seconds"),
    )
    semantic_root = _to_path(
        semantic_raw.get("root_dir", "${workspace_root}/semantic" if workspace_root_dir else "semantic"),
        "semantic.root_dir",
        base_dir=base_dir,
        workspace_root=workspace_root_dir,
    )
    assert semantic_root is not None
    try:
        mirror_compression = parse_codec(str(semantic_raw.get("mirror_compression", "xz")))
    except ValueError as exc:
        raise ConfigError(f"semantic.mirror_compression: {exc}") from exc
    new_chats = str(semantic_raw.get("new_chats", "same_path")).strip().lower()
    if new_chats not in NEW_CHATS_VALUES:
        raise ConfigError(
            f"semantic.new_chats must be one of {', '.join(NEW_CHATS_VALUES)}, got {new_chats!r}"
        )
    semantic = SemanticConfig(
        semantic_root,
        _int_value(semantic_raw, "max_jsonl_line_bytes", 64 * 1024 * 1024, "semantic.max_jsonl_line_bytes"),
        mirror_compression,
        new_chats,
    )

    sync = SyncConfig(
        mode=sync_raw.get("mode", "cold"),
        direction=sync_raw.get("direction", "bidirectional"),
        compare=str(sync_raw.get("compare", "mtime")).strip().lower(),
        time_tolerance_seconds=_int_value(sync_raw, "time_tolerance_seconds", 0, "sync.time_tolerance_seconds"),
        equal_mtime_action=str(sync_raw.get("equal_mtime_action", "skip")).strip().lower(),
        dry_run_default=_bool_value(sync_raw, "dry_run_default", True, "sync.dry_run_default"),
        delete_policy=sync_raw.get("delete_policy", "never"),
        scope=str(sync_raw.get("scope", "full")).strip().lower(),
        close_codex=_bool_value(sync_raw, "close_codex", False, "sync.close_codex"),
        session_mode=(
            str(sync_raw.get("session_mode")).strip().lower()
            if sync_raw.get("session_mode") is not None
            else None
        ),
    )

    safety = SafetyConfig(
        require_codex_stopped=_bool_value(safety_raw, "require_codex_stopped", True, "safety.require_codex_stopped"),
        fail_on_unknown=_bool_value(safety_raw, "fail_on_unknown", True, "safety.fail_on_unknown"),
    )

    background_process_names = _parse_background_process_names(proc_raw)
    process_detection = ProcessDetectionConfig(
        process_names=_parse_process_names(proc_raw.get("process_names", default_process_names())),
        grace_period_seconds=_int_value(proc_raw, "grace_period_seconds", 2, "process_detection.grace_period_seconds"),
        allow_terminate_if_running=_bool_value(proc_raw, "allow_terminate_if_running", False, "process_detection.allow_terminate_if_running"),
        manual_terminate_confirmation=_bool_value(proc_raw, "manual_terminate_confirmation", True, "process_detection.manual_terminate_confirmation"),
        terminate_confirmation_mode=str(proc_raw.get("terminate_confirmation_mode", "gui")).strip().lower(),
        terminate_timeout_seconds=_int_value(proc_raw, "terminate_timeout_seconds", 20, "process_detection.terminate_timeout_seconds"),
        background_process_names=background_process_names,
    )

    backup = BackupConfig(
        backup_before_overwrite=_bool_value(backup_raw, "backup_before_overwrite", True, "backup.backup_before_overwrite"),
        retention_days=_int_value(backup_raw, "retention_days", 30, "backup.retention_days"),
        max_backups=_int_value(backup_raw, "max_backups", 0, "backup.max_backups"),
        compression=str(backup_raw.get("compression", "none")).strip().lower(),
    )

    filters = FiltersConfig(exclude_globs=_string_list(filters_raw, "exclude_globs", "filters.exclude_globs"))
    targets = TargetsConfig(
        include_roots=_string_list(targets_raw, "include_roots", "targets.include_roots"),
        listed="include_roots" in targets_raw,
    )
    conflict = ConflictConfig(
        policy=conflict_raw.get("policy", "prefer_newer_mtime"),
        report_conflicts=_bool_value(conflict_raw, "report_conflicts", True, "conflict.report_conflicts"),
    )
    state = StateConfig(
        manifest_file=_to_path(
            state_raw.get("manifest_file"),
            "state.manifest_file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        data_version=_int_value(state_raw, "data_version", 1, "state.data_version"),
    )

    log_file = logging_raw.get("file")
    logging_cfg = LoggingConfig(
        level=logging_raw.get("level", "INFO"),
        file=_to_path(
            log_file,
            "logging.file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        format=str(logging_raw.get("format", "text")),
        retention_days=_int_value(logging_raw, "retention_days", 7, "logging.retention_days"),
        archive_mode=str(logging_raw.get("archive_mode", "zip")).strip().lower(),
        max_file_size_mb=_int_value(logging_raw, "max_file_size_mb", 10, "logging.max_file_size_mb"),
        machine_id=identity.machine_id,
    )

    cfg = AppConfig(
        identity=identity,
        paths=paths,
        sync=sync,
        safety=safety,
        process_detection=process_detection,
        backup=backup,
        filters=filters,
        targets=targets,
        conflict=conflict,
        state=state,
        logging=logging_cfg,
        guardian=guardian,
        path_mappings=_parse_path_mappings(path_mappings_raw),
        semantic=semantic,
        scheduler=_parse_scheduler(scheduler_raw),
        state_backup=_parse_state_backup(
            state_backup_raw, base_dir=base_dir, workspace_root=workspace_root_dir
        ),
        handoff=_parse_handoff(
            handoff_raw, base_dir=base_dir, workspace_root=workspace_root_dir,
            default_root=state.manifest_file.parent / "handoff" if state.manifest_file else None,
        ),
    )
    _validate_config(cfg)
    if "guardian" in raw or cfg.handoff.enabled is True:
        # A handoff file is named after this machine, like a Guardian snapshot.
        _require_guardian_identity(cfg)
    return cfg


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _int_value(section: dict[str, Any], key: str, default: int, field_name: str) -> int:
    """An integer setting, or a `ConfigError` naming it (CS-319).

    A bare `int(...)` turned `retention_days = "a week"` into a `ValueError`
    and exit 1, and `2.9` into 2 without a word. A quoted whole number is still
    accepted, because earlier versions accepted it.
    """
    value = section.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer, not {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ConfigError(f"{field_name} must be an integer, not {value!r}")


def _bool_value(section: dict[str, Any], key: str, default: bool, field_name: str) -> bool:
    """A true/false setting written as TOML `true`/`false` (CS-319).

    `bool("false")` is True: a quoted `backup_before_overwrite = "false"` or
    `require_codex_stopped = "false"` meant the opposite of what it said, with
    no error. Anything but a real boolean is refused and named.
    """
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field_name} must be true or false without quotes, not {value!r}")


def _float_value(section: dict[str, Any], key: str, default: float, field_name: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a number, not {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ConfigError(f"{field_name} must be a number, not {value!r}")


def _string_list(section: dict[str, Any], key: str, field_name: str) -> list[str]:
    """A list of strings. A lone string is refused rather than read letter by letter.

    `list("**/*.lock")` is ``["*", "*", "/", ...]``: every glob character
    became an exclusion of its own, and the one the user wrote was lost.
    """
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} must be a list of strings, for example [\"a\", \"b\"]")
    return list(value)


def _parse_scheduler(raw: Any) -> SchedulerConfig:
    """Read `[scheduler]`, ignoring keys this version does not know.

    Configs written before CS-232 carry `kind = "windows_task_scheduler"` and
    `interval_minutes`; refusing them would make a user's existing file stop
    loading over a section that was never read. Values are passed through
    uncoerced so `_validate_config` can refuse a wrong type rather than guess.
    """
    if not isinstance(raw, dict):
        raise ConfigError("scheduler must be a table")
    defaults = SchedulerConfig()
    mode = raw.get("mode", defaults.mode)
    if isinstance(mode, str):
        mode = mode.strip().lower()
    return SchedulerConfig(
        enabled=raw.get("enabled", defaults.enabled),
        mode=mode,
        interval_seconds=raw.get("interval_seconds", defaults.interval_seconds),
        run_at_login=raw.get("run_at_login", defaults.run_at_login),
        startup_delay_seconds=raw.get("startup_delay_seconds", defaults.startup_delay_seconds),
        jitter_seconds=raw.get("jitter_seconds", defaults.jitter_seconds),
        sync_at_login=raw.get("sync_at_login", defaults.sync_at_login),
    )


def _parse_state_backup(raw: Any, *, base_dir: Path, workspace_root: Path | None) -> StateBackupConfig:
    """Read `[state_backup]` (CS-276). An empty or absent `root_dir` means none.

    No folder is ever proposed here: where copies of a whole working state go
    is the user's decision, and a default would have put them somewhere nobody
    chose -- possibly into a cloud folder that uploads every copy.
    """
    if not isinstance(raw, dict):
        raise ConfigError("state_backup must be a table")
    defaults = StateBackupConfig()
    root_value = raw.get("root_dir", "")
    if not isinstance(root_value, str):
        raise ConfigError("state_backup.root_dir must be a string path")
    return StateBackupConfig(
        root_dir=_to_path(
            root_value.strip(), "state_backup.root_dir",
            base_dir=base_dir, workspace_root=workspace_root, required=False,
        ),
        at_login=raw.get("at_login", defaults.at_login),
        interval_hours=raw.get("interval_hours", defaults.interval_hours),
        keep=raw.get("keep", defaults.keep),
    )


def _validate_state_backup(cfg: AppConfig) -> None:
    settings = cfg.state_backup
    if not isinstance(settings.at_login, bool):
        raise ConfigError("state_backup.at_login must be a boolean (true or false, without quotes)")
    for field_name, value, minimum, maximum in (
        ("state_backup.interval_hours", settings.interval_hours, 0, MAX_STATE_BACKUP_INTERVAL_HOURS),
        ("state_backup.keep", settings.keep, 1, 1000),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{field_name} must be an integer")
        if not minimum <= value <= maximum:
            raise ConfigError(f"{field_name} must be between {minimum} and {maximum}")
    root = settings.root_dir
    if root is None:
        if settings.scheduled:
            raise ConfigError(
                "state_backup.root_dir is empty: choose the folder for copies of the Codex state "
                "before switching on at_login or interval_hours"
            )
        return
    resolved = root.resolve()
    if cfg.paths.local_state_dir and _paths_overlap(resolved, cfg.paths.local_state_dir.resolve()):
        raise ConfigError("state_backup.root_dir must be outside paths.local_state_dir")
    # Each of these has an owner that prunes, sweeps, mirrors or snapshots it:
    # backups are deleted by age, temp is swept, the cloud mirror is synced
    # file by file, and the Guardian and semantic stores have their own rules.
    for field_name, protected in (
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
        ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
        ("guardian.root_dir", cfg.guardian.root_dir),
        ("semantic.root_dir", cfg.semantic.root_dir),
    ):
        if _paths_overlap(resolved, protected.resolve()):
            raise ConfigError(f"state_backup.root_dir must not overlap {field_name}")


def _parse_handoff(
    raw: Any, *, base_dir: Path, workspace_root: Path | None, default_root: Path | None = None,
) -> HandoffConfig:
    """Read `[handoff]` (CS-328).

    An empty or absent `root_dir` means the `handoff` folder beside the sync
    manifest (CS-335): that folder is already the shared one machines
    coordinate through, so a person never has to pick a second one. The key
    stays as an override.
    """
    if not isinstance(raw, dict):
        raise ConfigError("handoff must be a table")
    defaults = HandoffConfig()
    root_value = raw.get("root_dir", "")
    if not isinstance(root_value, str):
        raise ConfigError("handoff.root_dir must be a string path")
    root_dir = _to_path(
        root_value.strip(), "handoff.root_dir",
        base_dir=base_dir, workspace_root=workspace_root, required=False,
    )
    return HandoffConfig(
        root_dir=root_dir if root_dir is not None else default_root,
        enabled=raw.get("enabled", defaults.enabled),
        delivery_wait_minutes=raw.get("delivery_wait_minutes", defaults.delivery_wait_minutes),
        notify=raw.get("notify", defaults.notify),
    )


def _validate_handoff(cfg: AppConfig) -> None:
    settings = cfg.handoff
    for field_name, value in (("handoff.enabled", settings.enabled), ("handoff.notify", settings.notify)):
        if not isinstance(value, bool):
            raise ConfigError(f"{field_name} must be a boolean (true or false, without quotes)")
    wait = settings.delivery_wait_minutes
    if isinstance(wait, bool) or not isinstance(wait, int):
        raise ConfigError("handoff.delivery_wait_minutes must be an integer")
    if not 0 <= wait <= MAX_HANDOFF_DELIVERY_WAIT_MINUTES:
        raise ConfigError(
            f"handoff.delivery_wait_minutes must be between 0 and {MAX_HANDOFF_DELIVERY_WAIT_MINUTES}"
        )
    if settings.enabled and cfg.scheduler.sync_at_login is True:
        # The watcher loads at sign-in itself; two tasks would sync twice.
        raise ConfigError(
            "handoff.enabled and scheduler.sync_at_login are both on: the handoff watcher "
            "already syncs at sign-in, so switch scheduler.sync_at_login off"
        )
    root = settings.root_dir
    if root is None:
        if settings.enabled:
            raise ConfigError(
                "handoff.root_dir is empty: choose a folder inside the synced workspace "
                "before switching handoff on"
            )
        return
    resolved = root.resolve()
    if cfg.paths.local_state_dir and _paths_overlap(resolved, cfg.paths.local_state_dir.resolve()):
        raise ConfigError("handoff.root_dir must be outside paths.local_state_dir")
    protected = [
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
        ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
        ("guardian.root_dir", cfg.guardian.root_dir),
        ("semantic.root_dir", cfg.semantic.root_dir),
    ]
    if cfg.state_backup.root_dir is not None:
        protected.append(("state_backup.root_dir", cfg.state_backup.root_dir))
    for field_name, other in protected:
        if _paths_overlap(resolved, other.resolve()):
            raise ConfigError(f"handoff.root_dir must not overlap {field_name}")


def _validate_scheduler(scheduler: SchedulerConfig) -> None:
    for field_name, value in (
        ("scheduler.enabled", scheduler.enabled),
        ("scheduler.run_at_login", scheduler.run_at_login),
        ("scheduler.sync_at_login", scheduler.sync_at_login),
    ):
        if not isinstance(value, bool):
            raise ConfigError(f"{field_name} must be a boolean (true or false, without quotes)")
    if not isinstance(scheduler.mode, str) or scheduler.mode not in SCHEDULER_MODES:
        raise ConfigError(
            "scheduler.mode must be one of: "
            + ", ".join(SCHEDULER_MODES)
            + "; scheduled automation never mutates state, so sync, restore and repair are not valid modes"
        )
    for field_name, value, minimum in (
        ("scheduler.interval_seconds", scheduler.interval_seconds, MIN_SCHEDULER_INTERVAL_SECONDS),
        ("scheduler.startup_delay_seconds", scheduler.startup_delay_seconds, 0),
        ("scheduler.jitter_seconds", scheduler.jitter_seconds, 0),
    ):
        # bool is an int subclass; `interval_seconds = true` is not a number.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{field_name} must be an integer")
        if value < minimum:
            if field_name == "scheduler.interval_seconds":
                raise ConfigError(
                    f"scheduler.interval_seconds must be >= {minimum} (5 minutes); "
                    "`codexsync config upgrade` raises a shorter period to it"
                )
            raise ConfigError(f"{field_name} must be >= {minimum}")
    if scheduler.interval_seconds > MAX_SCHEDULER_INTERVAL_SECONDS:
        raise ConfigError(
            f"scheduler.interval_seconds must be <= {MAX_SCHEDULER_INTERVAL_SECONDS} (31 days, "
            "the longest interval the OS scheduler repeats a task at)"
        )


def _validate_config(cfg: AppConfig) -> None:
    if cfg.sync.mode != "cold":
        raise ConfigError("Only cold sync mode is supported")

    if cfg.sync.compare not in {"mtime", "mtime_hash_fallback"}:
        raise ConfigError("sync.compare must be one of: mtime, mtime_hash_fallback")

    if cfg.sync.direction not in {"bidirectional", "to_cloud", "to_local"}:
        raise ConfigError("sync.direction must be one of: bidirectional, to_cloud, to_local")

    if cfg.sync.delete_policy not in {"never", "propagate"}:
        raise ConfigError("sync.delete_policy must be one of: never, propagate")

    if cfg.sync.scope not in SYNC_SCOPES:
        raise ConfigError("sync.scope must be one of: " + ", ".join(SYNC_SCOPES))

    if cfg.sync.time_tolerance_seconds < 0:
        raise ConfigError("sync.time_tolerance_seconds must be >= 0")

    allowed_equal_mtime_actions = {"skip", "prefer_local", "prefer_cloud", "manual_abort"}
    if cfg.sync.equal_mtime_action not in allowed_equal_mtime_actions:
        raise ConfigError(
            "sync.equal_mtime_action must be one of: skip, prefer_local, prefer_cloud, manual_abort"
        )

    allowed_session_modes = {None, "all", "last_date_only"}
    if cfg.sync.session_mode not in allowed_session_modes:
        raise ConfigError("sync.session_mode must be one of: all, last_date_only")

    allowed_conflict_policies = {"manual_abort", "prefer_cloud", "prefer_local", "prefer_newer_mtime"}
    if cfg.conflict.policy not in allowed_conflict_policies:
        raise ConfigError(
            "conflict.policy must be one of: manual_abort, prefer_cloud, prefer_local, prefer_newer_mtime"
        )

    if cfg.backup.compression not in {"none", "zip"}:
        raise ConfigError("backup.compression must be one of: none, zip")

    if not cfg.process_detection.process_names:
        raise ConfigError("process_detection.process_names must not be empty")

    if cfg.process_detection.terminate_confirmation_mode not in {"gui", "console"}:
        raise ConfigError("process_detection.terminate_confirmation_mode must be one of: gui, console")

    allowed_os_keys = {"windows", "macos", "linux"}
    for os_key, names in cfg.process_detection.background_process_names.items():
        if os_key not in allowed_os_keys:
            raise ConfigError(
                f"process_detection.background_process_names has unsupported OS key: {os_key}"
            )
        if not isinstance(names, list):
            raise ConfigError(
                f"process_detection.background_process_names.{os_key} must be a list of process names"
            )

    if cfg.logging.format.lower() not in {"text", "json", "logfmt"}:
        raise ConfigError("logging.format must be one of: text, json, logfmt")

    if cfg.logging.archive_mode not in {"text", "zip"}:
        raise ConfigError("logging.archive_mode must be one of: text, zip")

    if cfg.logging.retention_days < 0:
        raise ConfigError("logging.retention_days must be >= 0")

    if cfg.logging.max_file_size_mb <= 0:
        raise ConfigError("logging.max_file_size_mb must be > 0")

    if cfg.paths.local_state_dir and cfg.paths.local_state_dir == cfg.paths.cloud_root_dir:
        raise ConfigError("paths.local_state_dir and paths.cloud_root_dir must be different")
    if cfg.paths.local_state_dir:
        local_root = cfg.paths.local_state_dir.resolve()
        for field_name, external in (
            ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
            ("paths.backup_dir", cfg.paths.backup_dir),
            ("paths.temp_dir", cfg.paths.temp_dir),
        ):
            if _paths_overlap(local_root, external.resolve()):
                raise ConfigError(f"{field_name} must be outside paths.local_state_dir")
        if cfg.state.manifest_file and _paths_overlap(local_root, cfg.state.manifest_file.resolve()):
            raise ConfigError("state.manifest_file must be outside paths.local_state_dir")
        if cfg.logging.file and _paths_overlap(local_root, cfg.logging.file.resolve()):
            raise ConfigError("logging.file must be outside paths.local_state_dir")

    _validate_owned_paths_apart(cfg)
    _validate_include_roots(cfg.targets.include_roots, listed=cfg.targets.listed)
    _validate_scalar_ranges(cfg)
    _validate_guardian_root(cfg)
    if not 1 * 1024 * 1024 <= cfg.guardian.max_state_bytes <= 1 * 1024 * 1024 * 1024:
        raise ConfigError("guardian.max_state_bytes must be between 1 MiB and 1 GiB")
    if cfg.guardian.shrink_min_count < 1:
        raise ConfigError("guardian.shrink_min_count must be >= 1")
    if not 0.0 <= cfg.guardian.shrink_ratio <= 1.0:
        raise ConfigError("guardian.shrink_ratio must be between 0 and 1")
    for field_name, value in (
        ("guardian.retention_days", cfg.guardian.retention_days),
        ("guardian.max_snapshots", cfg.guardian.max_snapshots),
        ("guardian.quarantine_retention_days", cfg.guardian.quarantine_retention_days),
        ("guardian.staging_retention_hours", cfg.guardian.staging_retention_hours),
    ):
        if value < 0:
            raise ConfigError(f"{field_name} must be >= 0")
    if cfg.guardian.stable_reads < 3:
        raise ConfigError("guardian.stable_reads must be >= 3")
    for field_name, value in (
        ("guardian.poll_interval_seconds", cfg.guardian.poll_interval_seconds),
        ("guardian.debounce_seconds", cfg.guardian.debounce_seconds),
        ("guardian.stable_read_interval_seconds", cfg.guardian.stable_read_interval_seconds),
        ("guardian.fallback_scan_seconds", cfg.guardian.fallback_scan_seconds),
        ("guardian.once_timeout_seconds", cfg.guardian.once_timeout_seconds),
    ):
        if value <= 0:
            raise ConfigError(f"{field_name} must be > 0")
    _validate_scheduler(cfg.scheduler)
    _validate_state_backup(cfg)
    _validate_handoff(cfg)
    if cfg.semantic.max_jsonl_line_bytes < 1024 * 1024:
        raise ConfigError("semantic.max_jsonl_line_bytes must be at least 1 MiB")
    semantic_root = cfg.semantic.root_dir.resolve()
    if cfg.paths.local_state_dir and _paths_overlap(semantic_root, cfg.paths.local_state_dir.resolve()):
        raise ConfigError("semantic.root_dir must be outside paths.local_state_dir")
    for field_name, protected in (("paths.backup_dir", cfg.paths.backup_dir), ("paths.temp_dir", cfg.paths.temp_dir), ("guardian.root_dir", cfg.guardian.root_dir)):
        if _paths_overlap(semantic_root, protected.resolve()):
            raise ConfigError(f"semantic.root_dir must not overlap {field_name}")


def _validate_owned_paths_apart(cfg: AppConfig) -> None:
    """The mirror, the backups, the temp folder and the manifest never nest (CS-290).

    Each has an owner that deletes from it on its own schedule: backups are
    pruned by age, temp is swept, the mirror is synchronised file by file. A
    backup folder that was the mirror had its `sessions/` removed as an "old
    snapshot"; a manifest inside the backups was pruned with them.
    """
    owned: list[tuple[str, Path]] = [
        ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
    ]
    if cfg.state.manifest_file:
        owned.append(("state.manifest_file", cfg.state.manifest_file))
    for index, (first_name, first) in enumerate(owned):
        for second_name, second in owned[index + 1:]:
            if _paths_overlap(first.resolve(), second.resolve()):
                raise ConfigError(f"{first_name} and {second_name} must not overlap")


def _validate_include_roots(include_roots: list[str], *, listed: bool) -> None:
    """Every root names something *inside* the state directory (CS-289).

    An empty list, ``""`` or ``"."`` used to mean "everything", and everything
    includes `auth.json`. Clearing the list in Settings wrote exactly that.
    A config without the key (one used only for Guardian, say) still loads;
    it synchronises nothing, `sync` refuses with "nothing to synchronise", and
    `validate` says so up front.
    """
    if listed and not include_roots:
        raise ConfigError(
            "targets.include_roots must name at least one folder or file to synchronise; "
            "an empty list would synchronise the whole Codex state directory, credentials included"
        )
    for root in include_roots:
        text = root.strip()
        parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts:
            raise ConfigError(
                f"targets.include_roots entry {root!r} names the whole state directory; "
                "list the folders and files to synchronise instead"
            )
        if Path(text).is_absolute() or text.startswith(("/", "\\")) or ".." in parts:
            raise ConfigError(f"targets.include_roots must be relative paths inside the state directory: {root!r}")


def _validate_scalar_ranges(cfg: AppConfig) -> None:
    level = cfg.logging.level
    if not isinstance(level, str) or level.strip().upper() not in _LOG_LEVELS:
        raise ConfigError("logging.level must be one of: " + ", ".join(_LOG_LEVELS))
    for field_name, value in (
        ("process_detection.grace_period_seconds", cfg.process_detection.grace_period_seconds),
        ("backup.retention_days", cfg.backup.retention_days),
        ("backup.max_backups", cfg.backup.max_backups),
    ):
        if value < 0:
            raise ConfigError(f"{field_name} must be >= 0")


#: What `logging.level` may name; anything else used to become INFO silently.
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _require_guardian_identity(cfg: AppConfig) -> str:
    try:
        return require_guardian_machine_id(cfg.identity.machine_id)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def require_guardian_identity(cfg: AppConfig) -> str:
    """Validate machine identity immediately before any Guardian operation."""
    return _require_guardian_identity(cfg)


def _validate_guardian_root(cfg: AppConfig) -> None:
    root = cfg.guardian.root_dir.resolve()
    local_state = cfg.paths.local_state_dir.resolve() if cfg.paths.local_state_dir else None
    if local_state and _paths_overlap(root, local_state):
        raise ConfigError("guardian.root_dir must be outside paths.local_state_dir")

    for field_name, protected_root in (
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
        ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
    ):
        if _paths_overlap(root, protected_root.resolve()):
            raise ConfigError(f"guardian.root_dir must not overlap {field_name}")


def _parse_path_mappings(raw: Any) -> list[PathMappingRule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("path_mappings must be an array of tables")
    result: list[PathMappingRule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("Each path_mappings entry must be a table")
        required = ("rule_id", "source_machine", "target_machine", "from", "to")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
            raise ConfigError("Path mapping requires rule_id, source_machine, target_machine, from and to")
        if item["rule_id"] in seen:
            raise ConfigError("path_mappings.rule_id must be unique")
        seen.add(item["rule_id"])
        case_sensitive = item.get("case_sensitive")
        if case_sensitive is not None and not isinstance(case_sensitive, bool):
            raise ConfigError("path_mappings.case_sensitive must be a boolean")
        result.append(PathMappingRule(
            item["rule_id"], item["source_machine"], item["target_machine"],
            item["from"], item["to"], case_sensitive,
        ))
    return result


def external_roots(cfg: AppConfig) -> list[tuple[str, Path]]:
    """Every folder or file codexSync writes that must never sit in or around `.codex`."""
    roots: list[tuple[str, Path]] = [
        ("paths.cloud_root_dir", cfg.paths.cloud_root_dir),
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
        ("guardian.root_dir", cfg.guardian.root_dir),
        ("semantic.root_dir", cfg.semantic.root_dir),
    ]
    if cfg.state.manifest_file:
        roots.append(("state.manifest_file", cfg.state.manifest_file))
    if cfg.state_backup.root_dir is not None:
        roots.append(("state_backup.root_dir", cfg.state_backup.root_dir))
    if cfg.handoff.root_dir is not None:
        roots.append(("handoff.root_dir", cfg.handoff.root_dir))
    if cfg.logging.file:
        roots.append(("logging.file", cfg.logging.file))
    return roots


def require_outside_state_dir(cfg: AppConfig, state_dir: Path) -> None:
    """Refuse a config whose own folders overlap the Codex state folder in use.

    Loading compares them with `paths.local_state_dir` as configured. When that
    folder does not exist the locator falls back to CODEX_HOME or ~/.codex,
    which loading never saw -- so every command checks again against the
    folder it actually found, before it reads or writes anything.
    """
    resolved = state_dir.resolve()
    for field_name, root in external_roots(cfg):
        if _paths_overlap(root.resolve(), resolved):
            raise ConfigError(
                f"{field_name} ({root}) must be outside the Codex state folder in use ({state_dir})"
            )


def _paths_overlap(first: Path, second: Path) -> bool:
    """True when either resolved path contains the other, including junction escapes."""
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _parse_background_process_names(proc_raw: dict[str, Any]) -> dict[str, list[str]]:
    default_mapping = default_background_process_names()
    raw_mapping = proc_raw.get("background_process_names")
    if isinstance(raw_mapping, dict):
        parsed: dict[str, list[str]] = {}
        for key in ("windows", "macos", "linux"):
            value = raw_mapping.get(key, default_mapping[key])
            if not isinstance(value, list):
                raise ConfigError(
                    f"process_detection.background_process_names.{key} must be a list of process names"
                )
            parsed[key] = [str(name).strip() for name in value if str(name).strip()]
        return parsed
    return default_mapping


def _parse_process_names(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        raise ConfigError("process_detection.process_names must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        name = str(item).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result
