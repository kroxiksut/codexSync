"""`[scheduler]` in config.toml, applied to the operating system's user task.

`config.toml` stays the only source of truth for automation. The OS task is an
applied *representation* of the `[scheduler]` section, never a second place to
configure it: `apply_automation` renders the task from the config as it is on
disk and installs it, or removes it when the section says `enabled = false`;
`automation_status` reads the task back and says whether it still matches what
the config would install.

The job itself can only be one of the safe modes `system_scheduler` can
express. Running it *now* goes through the same functions the CLI command the
task would start calls, in this process, so "run now" and the scheduled run
cannot disagree about what the job does.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .exceptions import FailSafeError
from .models import AppConfig
from .process_detector import CodexProcessDetector
from .system_scheduler import (
    HANDOFF_MODE,
    HANDOFF_SLOT,
    LOGIN_SYNC_MODE,
    LOGIN_SYNC_SLOT,
    STATE_BACKUP_MODE,
    STATE_BACKUP_SLOT,
    JobDefinition,
    LegacyTask,
    ScheduledJob,
    SchedulerError,
    SchedulerStatus,
    SystemScheduler,
    default_protected_roots,
    find_01_tasks,
    job_command,
    system_scheduler,
)


@dataclass(frozen=True, slots=True)
class AutomationView:
    enabled: bool
    mode: str
    interval_seconds: int
    run_at_login: bool
    startup_delay_seconds: int
    jitter_seconds: int
    #: What the task runs, as an argument list, for the user to read.
    argv: tuple[str, ...]
    #: Settings this platform's scheduler cannot honour for this job.
    ignored: tuple[str, ...]
    reports_run_times: bool
    status: SchedulerStatus | None
    #: Why the OS could not be asked, when it could not.
    status_error: str | None = None
    #: `[scheduler] sync_at_login` and its own task (CS-267).
    sync_at_login: bool = False
    login_argv: tuple[str, ...] = ()
    login_status: SchedulerStatus | None = None
    login_status_error: str | None = None
    #: `[state_backup]` and its own task (CS-276, `D-017`).
    backup_root: Path | None = None
    backup_at_login: bool = False
    backup_interval_hours: int = 0
    backup_keep: int = 5
    backup_argv: tuple[str, ...] = ()
    backup_status: SchedulerStatus | None = None
    backup_status_error: str | None = None
    #: `[handoff]` and the watcher's own task (CS-328, `D-018`).
    handoff_root: Path | None = None
    handoff_enabled: bool = False
    handoff_wait_minutes: int = 15
    handoff_notify: bool = True
    handoff_argv: tuple[str, ...] = ()
    handoff_status: SchedulerStatus | None = None
    handoff_status_error: str | None = None
    #: Tasks the 0.1 scheduler scripts installed, still running `codexsync sync`
    #: on their own timer; found so they can be named, never touched.
    legacy_tasks: tuple[LegacyTask, ...] = ()

    @property
    def backup_scheduled(self) -> bool:
        return self.backup_root is not None and (self.backup_at_login or self.backup_interval_hours > 0)


def _log_dir(cfg: AppConfig, config_path: Path) -> Path:
    if cfg.logging.file is not None:
        return cfg.logging.file.parent.resolve()
    root = cfg.paths.workspace_root_dir or config_path.resolve().parent
    return (root / "logs").resolve()


def _scheduler(
    cfg: AppConfig, scheduler: SystemScheduler | None, *, slot: str | None = None
) -> SystemScheduler:
    if scheduler is not None:
        return scheduler
    roots = list(default_protected_roots())
    if cfg.paths.local_state_dir is not None:
        roots.append(cfg.paths.local_state_dir)
    if slot is None:
        return system_scheduler(protected_roots=tuple(roots))
    return system_scheduler(protected_roots=tuple(roots), slot=slot)


def _require_all(*adapters: SystemScheduler | None) -> None:
    """Every adapter injected, or none.

    Injecting one used to leave the other to the real OS, so a test that faked
    the periodic task ran the real ``schtasks`` for the sign-in one.
    """
    if len({adapter is None for adapter in adapters}) > 1:
        raise ValueError(
            "inject scheduler, login_scheduler, backup_scheduler and handoff_scheduler together, or none of them"
        )


def login_sync_definition(cfg: AppConfig, config_path: Path) -> JobDefinition:
    """The sign-in sync: once, after `startup_delay_seconds`, never repeated."""
    job = ScheduledJob(LOGIN_SYNC_MODE, None, True, cfg.scheduler.startup_delay_seconds, 0)
    return JobDefinition(job, tuple(job_command()), config_path.resolve(), _log_dir(cfg, config_path))


def state_backup_definition(cfg: AppConfig, config_path: Path) -> JobDefinition:
    """The copy of `.codex`: at sign-in and/or every N hours, as `[state_backup]` says.

    Built even when neither is switched on (as a sign-in job), so the command
    a person would get can be shown before they switch it on.
    """
    settings = cfg.state_backup
    hours = settings.interval_hours
    interval = hours * 3600 if isinstance(hours, int) and hours > 0 else None
    at_login = bool(settings.at_login) or interval is None
    job = ScheduledJob(STATE_BACKUP_MODE, interval, at_login, cfg.scheduler.startup_delay_seconds if at_login else 0, 0)
    return JobDefinition(job, tuple(job_command()), config_path.resolve(), _log_dir(cfg, config_path))


def handoff_definition(cfg: AppConfig, config_path: Path) -> JobDefinition:
    """The handoff watcher: started at sign-in and left running (`D-018`)."""
    job = ScheduledJob(HANDOFF_MODE, None, True, cfg.scheduler.startup_delay_seconds, 0)
    return JobDefinition(job, tuple(job_command()), config_path.resolve(), _log_dir(cfg, config_path))


def job_definition(cfg: AppConfig, config_path: Path) -> JobDefinition:
    settings = cfg.scheduler
    job = ScheduledJob(
        settings.mode,
        settings.interval_seconds,
        settings.run_at_login,
        settings.startup_delay_seconds,
        settings.jitter_seconds,
    )
    return JobDefinition(job, tuple(job_command()), config_path.resolve(), _log_dir(cfg, config_path))


def automation_status(
    config_path: Path,
    *,
    scheduler: SystemScheduler | None = None,
    login_scheduler: SystemScheduler | None = None,
    backup_scheduler: SystemScheduler | None = None,
    handoff_scheduler: SystemScheduler | None = None,
    find_legacy: Callable[[], tuple[LegacyTask, ...]] | None = None,
) -> AutomationView:
    """What `[scheduler]` asks for, and what the OS actually has. Reads only.

    Tasks 0.1 installed are looked for only against the real OS: with the
    adapters injected (a test) and no ``find_legacy``, none are reported, so a
    fake scheduler never comes with a real PowerShell call beside it.
    """
    _require_all(scheduler, login_scheduler, backup_scheduler, handoff_scheduler)
    if find_legacy is None:
        find_legacy = find_01_tasks if scheduler is None else (lambda: ())
    cfg = load_config(config_path)
    login_definition = login_sync_definition(cfg, config_path)
    login_status: SchedulerStatus | None = None
    login_error: str | None = None
    try:
        login_status = _scheduler(cfg, login_scheduler, slot=LOGIN_SYNC_SLOT).status(
            expected=login_definition
        )
    except SchedulerError as exc:
        login_error = str(exc)
    backup_definition = state_backup_definition(cfg, config_path)
    backup_status: SchedulerStatus | None = None
    backup_error: str | None = None
    try:
        backup_status = _scheduler(cfg, backup_scheduler, slot=STATE_BACKUP_SLOT).status(
            expected=backup_definition
        )
    except SchedulerError as exc:
        backup_error = str(exc)
    watcher_definition = handoff_definition(cfg, config_path)
    watcher_status: SchedulerStatus | None = None
    watcher_error: str | None = None
    try:
        watcher_status = _scheduler(cfg, handoff_scheduler, slot=HANDOFF_SLOT).status(
            expected=watcher_definition
        )
    except SchedulerError as exc:
        watcher_error = str(exc)
    definition = job_definition(cfg, config_path)
    adapter = _scheduler(cfg, scheduler)
    status: SchedulerStatus | None = None
    error: str | None = None
    try:
        status = adapter.status(expected=definition)
    except SchedulerError as exc:
        error = str(exc)
    settings = cfg.scheduler
    return AutomationView(
        enabled=settings.enabled,
        mode=settings.mode,
        interval_seconds=settings.interval_seconds,
        run_at_login=settings.run_at_login,
        startup_delay_seconds=settings.startup_delay_seconds,
        jitter_seconds=settings.jitter_seconds,
        argv=tuple(definition.argv()),
        ignored=tuple(adapter.ignored_settings(definition.job)),
        reports_run_times=bool(getattr(adapter, "reports_run_times", False)),
        status=status,
        status_error=error,
        sync_at_login=cfg.scheduler.sync_at_login,
        login_argv=tuple(login_definition.argv()),
        login_status=login_status,
        login_status_error=login_error,
        backup_root=cfg.state_backup.root_dir,
        backup_at_login=bool(cfg.state_backup.at_login),
        backup_interval_hours=int(cfg.state_backup.interval_hours),
        backup_keep=int(cfg.state_backup.keep),
        backup_argv=tuple(backup_definition.argv()),
        backup_status=backup_status,
        backup_status_error=backup_error,
        handoff_root=cfg.handoff.root_dir,
        handoff_enabled=bool(cfg.handoff.enabled),
        handoff_wait_minutes=int(cfg.handoff.delivery_wait_minutes),
        handoff_notify=bool(cfg.handoff.notify),
        handoff_argv=tuple(watcher_definition.argv()),
        handoff_status=watcher_status,
        handoff_status_error=watcher_error,
        legacy_tasks=find_legacy(),
    )


def _codex_detectable(cfg: AppConfig) -> bool:
    return CodexProcessDetector(cfg.process_detection.process_names).capability().supported


def _require_detectable_codex(cfg: AppConfig) -> None:
    """Refuse a task that could never do its job on this platform.

    The copy of `.codex` and the sign-in sync both need Codex proven closed;
    where the process detector is not proven, the gate reads UNKNOWN and every
    run exits 5. Installing such a task would look like protection and be
    none. Checked before any task is touched, so a refusal changes nothing.
    """
    wanted = [
        name for name, on in (
            ("state_backup (at_login / interval_hours)", cfg.state_backup.scheduled),
            ("scheduler.sync_at_login", cfg.scheduler.sync_at_login),
            ("handoff.enabled", cfg.handoff.enabled is True),
        ) if on
    ]
    if wanted and not _codex_detectable(cfg):
        raise FailSafeError(
            "The scheduled tasks were not updated: " + " and ".join(wanted)
            + " need Codex proven closed, and this platform's process detector is not proven, "
            "so every run would stop without doing anything. Switch them off to apply the rest."
        )


def apply_automation(
    config_path: Path,
    *,
    scheduler: SystemScheduler | None = None,
    login_scheduler: SystemScheduler | None = None,
    backup_scheduler: SystemScheduler | None = None,
    handoff_scheduler: SystemScheduler | None = None,
) -> AutomationView:
    """Make every OS task match the config: install what is on, remove what is off."""
    _require_all(scheduler, login_scheduler, backup_scheduler, handoff_scheduler)
    cfg = load_config(config_path)
    _require_detectable_codex(cfg)
    adapter = _scheduler(cfg, scheduler)
    login_adapter = _scheduler(cfg, login_scheduler, slot=LOGIN_SYNC_SLOT)
    backup_adapter = _scheduler(cfg, backup_scheduler, slot=STATE_BACKUP_SLOT)
    handoff_adapter = _scheduler(cfg, handoff_scheduler, slot=HANDOFF_SLOT)
    try:
        if cfg.handoff.enabled is True:
            definition = handoff_definition(cfg, config_path)
            handoff_adapter.install(
                definition.job,
                command=definition.command,
                config_path=definition.config_path,
                log_dir=definition.log_dir,
            )
        else:
            handoff_adapter.remove()
        if cfg.state_backup.scheduled:
            definition = state_backup_definition(cfg, config_path)
            backup_adapter.install(
                definition.job,
                command=definition.command,
                config_path=definition.config_path,
                log_dir=definition.log_dir,
            )
        else:
            backup_adapter.remove()
        if cfg.scheduler.sync_at_login:
            definition = login_sync_definition(cfg, config_path)
            login_adapter.install(
                definition.job,
                command=definition.command,
                config_path=definition.config_path,
                log_dir=definition.log_dir,
            )
        else:
            login_adapter.remove()
        if cfg.scheduler.enabled:
            definition = job_definition(cfg, config_path)
            adapter.install(
                definition.job,
                command=definition.command,
                config_path=definition.config_path,
                log_dir=definition.log_dir,
            )
        else:
            adapter.remove()
    except (SchedulerError, ValueError) as exc:
        # The OS refused or the definition cannot be expressed; either way the
        # task was not changed by this call, which is what fail-safe means here.
        raise FailSafeError(f"The scheduled task was not updated: {exc}") from exc
    return automation_status(
        config_path, scheduler=adapter, login_scheduler=login_adapter, backup_scheduler=backup_adapter,
        handoff_scheduler=handoff_adapter,
        # The real OS was asked above only when nothing was injected.
        find_legacy=find_01_tasks if scheduler is None else None,
    )


def remove_automation(
    config_path: Path,
    *,
    scheduler: SystemScheduler | None = None,
    login_scheduler: SystemScheduler | None = None,
    backup_scheduler: SystemScheduler | None = None,
    handoff_scheduler: SystemScheduler | None = None,
) -> bool:
    """Remove every task; ``True`` if any existed."""
    _require_all(scheduler, login_scheduler, backup_scheduler, handoff_scheduler)
    cfg = load_config(config_path)
    try:
        periodic = _scheduler(cfg, scheduler).remove()
        login = _scheduler(cfg, login_scheduler, slot=LOGIN_SYNC_SLOT).remove()
        backup = _scheduler(cfg, backup_scheduler, slot=STATE_BACKUP_SLOT).remove()
        watcher = _scheduler(cfg, handoff_scheduler, slot=HANDOFF_SLOT).remove()
        return periodic or login or backup or watcher
    except SchedulerError as exc:
        raise FailSafeError(f"The scheduled task was not removed: {exc}") from exc


__all__ = [
    "AutomationView",
    "SchedulerError",
    "apply_automation",
    "automation_status",
    "handoff_definition",
    "job_definition",
    "login_sync_definition",
    "state_backup_definition",
    "remove_automation",
]
