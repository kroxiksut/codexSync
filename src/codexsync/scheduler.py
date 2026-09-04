"""User-level scheduler templates for Guardian snapshot --once only."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
import plistlib
import shlex


@dataclass(frozen=True, slots=True)
class SchedulerTemplates:
    files: dict[str, bytes]


def render_scheduler_templates(
    platform_name: str,
    *,
    executable: Path,
    config_path: Path,
    log_dir: Path,
    interval_seconds: int = 60,
) -> SchedulerTemplates:
    for path, label in ((executable, "executable"), (config_path, "config"), (log_dir, "log directory")):
        if not path.is_absolute():
            raise ValueError(f"Scheduler {label} path must be absolute")
    if interval_seconds < 60:
        raise ValueError("Scheduler interval must be at least 60 seconds")
    args = [str(executable), "-c", str(config_path), "guardian", "snapshot", "--once"]
    platform_key = platform_name.lower()
    if platform_key == "windows":
        arguments = " ".join(_windows_quote(item) for item in args[1:])
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><Repetition><Interval>PT{interval_seconds}S</Interval></Repetition></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><RunLevel>LeastPrivilege</RunLevel><LogonType>InteractiveToken</LogonType></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><ExecutionTimeLimit>PT2M</ExecutionTimeLimit><StartWhenAvailable>true</StartWhenAvailable></Settings>
  <Actions Context="Author"><Exec><Command>{escape(str(executable))}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
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
        command = " ".join(shlex.quote(item) for item in args)
        service = f"""[Unit]
Description=codexSync Guardian snapshot

[Service]
Type=oneshot
ExecStart={command}
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


def _windows_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'
