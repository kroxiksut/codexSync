from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .guardian_models import GuardianConfig, require_guardian_machine_id
from .jsonl_codec import parse_codec
from .path_mapping import PathMappingRule
from .process_knowledge import default_background_process_names, default_process_names
from .models import (
    MIN_SCHEDULER_INTERVAL_SECONDS,
    SCHEDULER_MODES,
    AppConfig,
    BackupConfig,
    ConflictConfig,
    FiltersConfig,
    IdentityConfig,
    LoggingConfig,
    PathsConfig,
    ProcessDetectionConfig,
    SafetyConfig,
    SchedulerConfig,
    SemanticConfig,
    StateConfig,
    SyncConfig,
    TargetsConfig,
)


#: Every substitution a path value may use, in one place. A screen that lists
#: them reads this rather than keeping a second copy in a language file, so a
#: new substitution cannot be documented in one language and forgotten in the
#: other -- or documented and never implemented.
PATH_SUBSTITUTIONS: tuple[str, ...] = ("${workspace_root}",)


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

    resolved = _expand_workspace_var(value, workspace_root, field_name)
    raw = Path(resolved).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    anchor = workspace_root if workspace_root else base_dir
    return (anchor / raw).resolve()


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

    identity_raw = raw.get("identity", {})
    paths_raw = raw.get("paths", {})
    sync_raw = raw.get("sync", {})
    safety_raw = raw.get("safety", {})
    proc_raw = raw.get("process_detection", {})
    backup_raw = raw.get("backup", {})
    filters_raw = raw.get("filters", {})
    targets_raw = raw.get("targets", {})
    conflict_raw = raw.get("conflict", {})
    state_raw = raw.get("state", {})
    logging_raw = raw.get("logging", {})
    guardian_raw = raw.get("guardian", {})
    semantic_raw = raw.get("semantic", {})
    scheduler_raw = raw.get("scheduler", {})
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
        max_state_bytes=int(guardian_raw.get("max_state_bytes", 64 * 1024 * 1024)),
        shrink_min_count=int(guardian_raw.get("shrink_min_count", 2)),
        shrink_ratio=float(guardian_raw.get("shrink_ratio", 0.25)),
        retention_days=int(guardian_raw.get("retention_days", 30)),
        max_snapshots=int(guardian_raw.get("max_snapshots", 100)),
        quarantine_retention_days=int(guardian_raw.get("quarantine_retention_days", 30)),
        staging_retention_hours=int(guardian_raw.get("staging_retention_hours", 24)),
        poll_interval_seconds=float(guardian_raw.get("poll_interval_seconds", 3)),
        debounce_seconds=float(guardian_raw.get("debounce_seconds", 2)),
        stable_reads=int(guardian_raw.get("stable_reads", 3)),
        stable_read_interval_seconds=float(guardian_raw.get("stable_read_interval_seconds", 0.5)),
        fallback_scan_seconds=float(guardian_raw.get("fallback_scan_seconds", 60)),
        once_timeout_seconds=float(guardian_raw.get("once_timeout_seconds", 120)),
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
    semantic = SemanticConfig(
        semantic_root,
        int(semantic_raw.get("max_jsonl_line_bytes", 64 * 1024 * 1024)),
        mirror_compression,
    )

    sync = SyncConfig(
        mode=sync_raw.get("mode", "cold"),
        direction=sync_raw.get("direction", "bidirectional"),
        compare=str(sync_raw.get("compare", "mtime")).strip().lower(),
        time_tolerance_seconds=int(sync_raw.get("time_tolerance_seconds", 0)),
        equal_mtime_action=str(sync_raw.get("equal_mtime_action", "skip")).strip().lower(),
        dry_run_default=bool(sync_raw.get("dry_run_default", True)),
        delete_policy=sync_raw.get("delete_policy", "never"),
        session_mode=(
            str(sync_raw.get("session_mode")).strip().lower()
            if sync_raw.get("session_mode") is not None
            else None
        ),
    )

    safety = SafetyConfig(
        require_codex_stopped=bool(safety_raw.get("require_codex_stopped", True)),
        fail_on_unknown=bool(safety_raw.get("fail_on_unknown", True)),
    )

    background_process_names = _parse_background_process_names(proc_raw)
    process_detection = ProcessDetectionConfig(
        process_names=_parse_process_names(proc_raw.get("process_names", default_process_names())),
        grace_period_seconds=int(proc_raw.get("grace_period_seconds", 2)),
        allow_terminate_if_running=bool(proc_raw.get("allow_terminate_if_running", False)),
        manual_terminate_confirmation=bool(proc_raw.get("manual_terminate_confirmation", True)),
        terminate_confirmation_mode=str(proc_raw.get("terminate_confirmation_mode", "gui")).strip().lower(),
        terminate_timeout_seconds=int(proc_raw.get("terminate_timeout_seconds", 20)),
        background_process_names=background_process_names,
    )

    backup = BackupConfig(
        backup_before_overwrite=bool(backup_raw.get("backup_before_overwrite", True)),
        retention_days=int(backup_raw.get("retention_days", 30)),
        max_backups=int(backup_raw.get("max_backups", 0)),
        compression=str(backup_raw.get("compression", "none")).strip().lower(),
    )

    filters = FiltersConfig(exclude_globs=list(filters_raw.get("exclude_globs", [])))
    targets = TargetsConfig(include_roots=list(targets_raw.get("include_roots", [])))
    conflict = ConflictConfig(
        policy=conflict_raw.get("policy", "manual_abort"),
        report_conflicts=bool(conflict_raw.get("report_conflicts", True)),
    )
    state = StateConfig(
        manifest_file=_to_path(
            state_raw.get("manifest_file"),
            "state.manifest_file",
            base_dir=base_dir,
            workspace_root=workspace_root_dir,
            required=False,
        ),
        data_version=int(state_raw.get("data_version", 1)),
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
        format=logging_raw.get("format", "text"),
        retention_days=int(logging_raw.get("retention_days", 7)),
        archive_mode=str(logging_raw.get("archive_mode", "zip")).strip().lower(),
        max_file_size_mb=int(logging_raw.get("max_file_size_mb", 10)),
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
    )
    _validate_config(cfg)
    if "guardian" in raw:
        _require_guardian_identity(cfg)
    return cfg


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
            raise ConfigError(f"{field_name} must be >= {minimum}")


def _validate_config(cfg: AppConfig) -> None:
    if cfg.sync.mode != "cold":
        raise ConfigError("Only cold sync mode is supported")

    if cfg.sync.compare not in {"mtime", "mtime_hash_fallback"}:
        raise ConfigError("sync.compare must be one of: mtime, mtime_hash_fallback")

    if cfg.sync.direction not in {"bidirectional", "to_cloud", "to_local"}:
        raise ConfigError("sync.direction must be one of: bidirectional, to_cloud, to_local")

    if cfg.sync.delete_policy not in {"never", "propagate"}:
        raise ConfigError("sync.delete_policy must be one of: never, propagate")

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
    if cfg.semantic.max_jsonl_line_bytes < 1024 * 1024:
        raise ConfigError("semantic.max_jsonl_line_bytes must be at least 1 MiB")
    semantic_root = cfg.semantic.root_dir.resolve()
    if cfg.paths.local_state_dir and _paths_overlap(semantic_root, cfg.paths.local_state_dir.resolve()):
        raise ConfigError("semantic.root_dir must be outside paths.local_state_dir")
    for field_name, protected in (("paths.backup_dir", cfg.paths.backup_dir), ("paths.temp_dir", cfg.paths.temp_dir), ("guardian.root_dir", cfg.guardian.root_dir)):
        if _paths_overlap(semantic_root, protected.resolve()):
            raise ConfigError(f"semantic.root_dir must not overlap {field_name}")


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
