"""User-level scheduler templates for Guardian snapshot --once only.

Deprecated: `[scheduler]` in config.toml and `codexsync automation apply` are
the way to schedule a job. The templates are still written correctly (CS-543):
the program prefix is the one a real task runs (`system_scheduler.job_command`,
which is `python -m codexsync` from pip), and each argument is quoted the way
that scheduler reads it back.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
import plistlib

from .system_scheduler import _default_user_id, quote_systemd_argument, quote_windows_argument


@dataclass(frozen=True, slots=True)
class SchedulerTemplates:
    files: dict[str, bytes]


def render_scheduler_templates(
    platform_name: str,
    *,
    command: Sequence[str],
    config_path: Path,
    log_dir: Path,
    interval_seconds: int = 60,
    user_id: str | None = None,
) -> SchedulerTemplates:
    """``command`` is the program prefix, e.g. ``[python, "-m", "codexsync"]`` or a frozen build."""
    command = [str(item) for item in command]
    if not command or not command[0]:
        raise ValueError("Scheduler command must not be empty")
    for path, label in ((Path(command[0]), "executable"), (config_path, "config"), (log_dir, "log directory")):
        if not path.is_absolute():
            raise ValueError(f"Scheduler {label} path must be absolute")
    if interval_seconds < 60:
        raise ValueError("Scheduler interval must be at least 60 seconds")
    args = [*command, "-c", str(config_path), "guardian", "snapshot", "--once"]
    platform_key = platform_name.lower()
    if platform_key == "windows":
        arguments = " ".join(quote_windows_argument(item) for item in args[1:])
        # A LogonTrigger without a user is "any user", which only an
        # administrator may register; the principal names the same account.
        user = escape(user_id or _default_user_id())
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Repetition><Interval>PT{interval_seconds}S</Interval></Repetition></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><RunLevel>LeastPrivilege</RunLevel><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><ExecutionTimeLimit>PT2M</ExecutionTimeLimit><StartWhenAvailable>true</StartWhenAvailable></Settings>
  <Actions Context="Author"><Exec><Command>{escape(args[0])}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
'''
        return SchedulerTemplates({"codexsync-guardian.xml": xml.encode("utf-16")})
    if platform_key == "macos":
        plist = {
            "Label": "io.codexsync.guardian",
            "ProgramArguments": args,
            "RunAtLoad": True,
            "StartInterval": interval_seconds,
            "StandardOutPath": str(log_dir / "guardian.out.log"),
            "StandardErrorPath": str(log_dir / "guardian.err.log"),
            "ProcessType": "Background",
        }
        return SchedulerTemplates({"io.codexsync.guardian.plist": plistlib.dumps(plist, sort_keys=True)})
    if platform_key == "linux":
        command_line = " ".join(quote_systemd_argument(item) for item in args)
        service = f"""[Unit]
Description=codexSync Guardian snapshot

[Service]
Type=oneshot
ExecStart={command_line}
TimeoutStartSec=120
StandardOutput=append:{log_dir / 'guardian.out.log'}
StandardError=append:{log_dir / 'guardian.err.log'}
"""
        timer = f"""[Unit]
Description=Run codexSync Guardian every {interval_seconds} seconds

[Timer]
OnStartupSec=15s
OnUnitActiveSec={interval_seconds}s
Persistent=true
Unit=codexsync-guardian.service

[Install]
WantedBy=timers.target
"""
        return SchedulerTemplates({
            "codexsync-guardian.service": service.encode("utf-8"),
            "codexsync-guardian.timer": timer.encode("utf-8"),
        })
    raise ValueError("Unsupported scheduler platform")


def write_scheduler_templates(templates: SchedulerTemplates, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in templates.files.items():
        path = output_dir / name
        if path.exists():
            raise FileExistsError(path)
        path.write_bytes(payload)
        written.append(path)
    return written

