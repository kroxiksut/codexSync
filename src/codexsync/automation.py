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


def _log_dir(cfg: AppConfig, config_path: Path) -> Path:
    if cfg.logging.file is not None:
        return cfg.logging.file.parent.resolve()
    root = cfg.paths.workspace_root_dir or config_path.resolve().parent
    return (root / "logs").resolve()


def _scheduler(cfg: AppConfig, scheduler: SystemScheduler | None) -> SystemScheduler:
    if scheduler is not None:
        return scheduler
    roots = list(default_protected_roots())
    if cfg.paths.local_state_dir is not None:
        roots.append(cfg.paths.local_state_dir)
    return system_scheduler(protected_roots=tuple(roots))


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


def automation_status(config_path: Path, *, scheduler: SystemScheduler | None = None) -> AutomationView:
    """What `[scheduler]` asks for, and what the OS actually has. Reads only."""
    cfg = load_config(config_path)
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
    )


def apply_automation(config_path: Path, *, scheduler: SystemScheduler | None = None) -> AutomationView:
    """Make the OS task match `[scheduler]`: install it when enabled, remove it otherwise."""
    cfg = load_config(config_path)
    adapter = _scheduler(cfg, scheduler)
    try:
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
    return automation_status(config_path, scheduler=adapter)


def remove_automation(config_path: Path, *, scheduler: SystemScheduler | None = None) -> bool:
    cfg = load_config(config_path)
    try:
        return _scheduler(cfg, scheduler).remove()
    except SchedulerError as exc:
        raise FailSafeError(f"The scheduled task was not removed: {exc}") from exc


__all__ = [
    "AutomationView",
    "SchedulerError",
    "apply_automation",
    "automation_status",
    "job_definition",
    "remove_automation",
]
