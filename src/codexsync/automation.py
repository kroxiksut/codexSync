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

from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .exceptions import FailSafeError
from .models import AppConfig
from .system_scheduler import (
    LOGIN_SYNC_MODE,
    LOGIN_SYNC_SLOT,
    JobDefinition,
    ScheduledJob,
    SchedulerError,
    SchedulerStatus,
    SystemScheduler,
    default_protected_roots,
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


def _require_pair(scheduler: SystemScheduler | None, login_scheduler: SystemScheduler | None) -> None:
    """Both adapters injected, or neither.

    Injecting one used to leave the other to the real OS, so a test that faked
    the periodic task ran the real ``schtasks`` for the sign-in one.
    """
    if (scheduler is None) != (login_scheduler is None):
        raise ValueError("inject both scheduler and login_scheduler, or neither")


def login_sync_definition(cfg: AppConfig, config_path: Path) -> JobDefinition:
    """The sign-in sync: once, after `startup_delay_seconds`, never repeated."""
    job = ScheduledJob(LOGIN_SYNC_MODE, None, True, cfg.scheduler.startup_delay_seconds, 0)
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
) -> AutomationView:
    """What `[scheduler]` asks for, and what the OS actually has. Reads only."""
    _require_pair(scheduler, login_scheduler)
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
    )


def apply_automation(
    config_path: Path,
    *,
    scheduler: SystemScheduler | None = None,
    login_scheduler: SystemScheduler | None = None,
) -> AutomationView:
    """Make both OS tasks match `[scheduler]`: install what is on, remove what is off."""
    _require_pair(scheduler, login_scheduler)
    cfg = load_config(config_path)
    adapter = _scheduler(cfg, scheduler)
    login_adapter = _scheduler(cfg, login_scheduler, slot=LOGIN_SYNC_SLOT)
    try:
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
    return automation_status(config_path, scheduler=adapter, login_scheduler=login_adapter)


def remove_automation(
    config_path: Path,
    *,
    scheduler: SystemScheduler | None = None,
    login_scheduler: SystemScheduler | None = None,
) -> bool:
    """Remove both tasks; ``True`` if either existed."""
    _require_pair(scheduler, login_scheduler)
    cfg = load_config(config_path)
    try:
        periodic = _scheduler(cfg, scheduler).remove()
        login = _scheduler(cfg, login_scheduler, slot=LOGIN_SYNC_SLOT).remove()
        return periodic or login
    except SchedulerError as exc:
        raise FailSafeError(f"The scheduled task was not removed: {exc}") from exc


__all__ = [
    "AutomationView",
    "SchedulerError",
    "apply_automation",
    "automation_status",
    "job_definition",
    "login_sync_definition",
    "remove_automation",
]
