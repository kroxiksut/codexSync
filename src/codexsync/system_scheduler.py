"""Install, inspect and remove the user-level OS task that runs a *safe* job.

``scheduler.py`` renders Guardian templates for a person to register by hand.
This module does the registering, so a GUI can offer one switch instead of a
how-to. It is deliberately narrow, and each limit is a safety property rather
than a missing feature:

* **Only a read-only job can be expressed.** ``ScheduledJob.mode`` is one of
  three names and ``job_arguments`` maps each to a fixed argument list. There
  is no way to pass ``--apply``, ``restore`` or a plan id through here, so a
  bug or a hand-edited config cannot turn a periodic task into a periodic
  mutation that runs while nobody is watching.
* **User level only.** Task Scheduler runs the task as the current user with
  ``InteractiveToken``/``LeastPrivilege``; launchd gets a LaunchAgent, systemd
  a ``--user`` unit. Nothing asks for elevation, nothing registers as SYSTEM
  or as a service, nothing touches the network.
* **Never inside the Codex state directory.** The files this module writes
  (a temporary task XML, a plist, two unit files, the log directory) are
  checked against ``protected_roots`` before they are written.
* **No real OS tool in tests.** Every adapter takes ``run`` (and a home, uid,
  temp dir or clock where relevant) so the suite asserts on argument lists and
  rendered files instead of registering anything on the developer's machine.

``status`` answers two separate questions. ``installed``/``enabled``/run times
describe what the OS has. ``definition_matches`` says whether that is still
what ``install`` would write for a given job, so a GUI can say "the task is out
of date with config.toml" without the user comparing XML by eye.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
import codecs
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
from typing import Any, Protocol
from xml.etree import ElementTree
from xml.sax.saxutils import escape as _xml_escape


# --------------------------------------------------------------------------
# The job: what may run
# --------------------------------------------------------------------------

#: The complete set of jobs a scheduled task may run. Each is read-only or
#: side-effect free with respect to Codex state; ``sync --dry-run`` stays a
#: dry run because ``--dry-run`` and ``--apply`` are mutually exclusive in the
#: CLI and nothing here can add the second one.
_JOB_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "guardian_snapshot": ("guardian", "snapshot", "--once"),
    "preflight": ("preflight", "--for", "sync"),
    "sync_dry_run": ("sync", "--dry-run"),
}
JOB_MODES: tuple[str, ...] = tuple(_JOB_SUBCOMMANDS)

MIN_INTERVAL_SECONDS = 60


class SchedulerError(RuntimeError):
    """The OS scheduler refused a request or could not be invoked at all."""


def _require_non_negative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """What to run and how often; validated on construction."""

    mode: str
    interval_seconds: int
    run_at_login: bool
    startup_delay_seconds: int = 0
    jitter_seconds: int = 0

    def __post_init__(self) -> None:
        if self.mode not in _JOB_SUBCOMMANDS:
            raise ValueError(f"Unsupported scheduled job mode: {self.mode!r}; expected one of {', '.join(JOB_MODES)}")
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, int)
            or self.interval_seconds < MIN_INTERVAL_SECONDS
        ):
            # Task Scheduler's documented repetition minimum is one minute;
            # holding every platform to it keeps one job meaning the same thing.
            raise ValueError(f"interval_seconds must be an integer >= {MIN_INTERVAL_SECONDS}")
        if not isinstance(self.run_at_login, bool):
            raise ValueError("run_at_login must be a bool")
        _require_non_negative_int(self.startup_delay_seconds, "startup_delay_seconds")
        _require_non_negative_int(self.jitter_seconds, "jitter_seconds")


def job_arguments(mode: str, config_path: Path) -> list[str]:
    """CLI arguments (after the command prefix) for one scheduled job mode.

    The config path must be absolute: a scheduled task starts in a working
    directory of the OS's choosing (``System32`` on Windows), so a relative
    path would silently name a different file or none.
    """
    if mode not in _JOB_SUBCOMMANDS:
        raise ValueError(f"Unsupported scheduled job mode: {mode!r}")
    config = Path(config_path)
    if not config.is_absolute():
        raise ValueError("Scheduled job config path must be absolute")
    return ["-c", str(config), *_JOB_SUBCOMMANDS[mode]]


def job_command(
    executable: Path | None = None,
    *,
    frozen: bool | None = None,
    platform: str | None = None,
) -> list[str]:
    """The command prefix that starts the codexSync CLI.

    A frozen build is its own CLI. Otherwise the interpreter runs ``-m
    codexsync``, which needs the package importable by that interpreter (an
    installed or ``pip install -e`` checkout; a bare source tree is not on the
    task's path). On Windows ``pythonw.exe`` is preferred because the task runs
    every minute or so and ``python.exe`` would flash a console window each
    time.
    """
    exe = Path(sys.executable) if executable is None else Path(executable)
    if not str(exe) or str(exe) == ".":
        raise ValueError("Cannot determine the interpreter that runs codexSync")
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return [str(exe)]
    if _platform_key(platform) == "windows":
        windowless = exe.with_name("pythonw.exe")
        if windowless.is_file():
            exe = windowless
    return [str(exe), "-m", "codexsync"]


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """Everything ``install`` needs, as one comparable value.

    ``status(expected=...)`` takes the same value, which is what lets it answer
    "is the installed task exactly what install would write for this?".
    """

    job: ScheduledJob
    command: tuple[str, ...]
    config_path: Path
    log_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(str(item) for item in self.command))
        object.__setattr__(self, "config_path", Path(self.config_path))
        object.__setattr__(self, "log_dir", Path(self.log_dir))
        if not self.command or not self.command[0]:
            raise ValueError("Scheduled job command must not be empty")
        # An absolute program keeps the task from depending on the PATH the OS
        # happens to give a background process.
        if not Path(self.command[0]).is_absolute():
            raise ValueError("Scheduled job program path must be absolute")
        if not self.log_dir.is_absolute():
            raise ValueError("Scheduled job log directory must be absolute")
        job_arguments(self.job.mode, self.config_path)  # validates the config path

    def argv(self) -> list[str]:
        return [*self.command, *job_arguments(self.job.mode, self.config_path)]


# --------------------------------------------------------------------------
# Adapter contract
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """What the OS reports about the task.

    ``definition_matches`` is ``None`` when it was not asked for (no
    ``expected``), when nothing is installed, or when the installed definition
    could not be read; ``True``/``False`` only when a comparison was made.
    """

    installed: bool
    enabled: bool | None = None
    last_run_utc: str | None = None
    next_run_utc: str | None = None
    last_result: int | None = None
    detail: str = ""
    definition_matches: bool | None = None


class RunResult(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


Runner = Callable[[list[str]], RunResult]


class SystemScheduler(Protocol):
    platform: str
    #: Whether ``status`` can ever fill ``last_run_utc``/``next_run_utc``.
    reports_run_times: bool

    def ignored_settings(self, job: ScheduledJob) -> tuple[str, ...]:
        """``ScheduledJob`` field names this platform cannot honour for ``job``.

        A GUI greys these out; ``install`` accepts the job anyway and the
        values have no effect.
        """
        ...

    def render(self, definition: JobDefinition) -> dict[str, bytes]: ...

    def install(self, job: ScheduledJob, *, command: Sequence[str], config_path: Path, log_dir: Path) -> None: ...

    def remove(self) -> bool: ...

    def status(self, expected: JobDefinition | None = None) -> SchedulerStatus: ...


_RUN_TIMEOUT_SECONDS = 60


def _default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an OS tool without a console window and without stdin.

    On Windows the output is decoded with the OEM code page. ``schtasks``
    writes in its console's output code page whatever its XML declaration
    claims (observed: UTF-8 in an inherited ``chcp 65001`` console, cp1251
    when detached, cp866 under ``CREATE_NO_WINDOW`` on a Russian Windows), and
    ``CREATE_NO_WINDOW`` gives it a fresh console on the OEM code page. The
    ANSI default of ``text=True`` would mangle every Cyrillic path. A
    character outside the OEM code page cannot survive that pipe at all.
    """
    kwargs: dict[str, Any] = {"encoding": "utf-8"}
    if sys.platform == "win32":
        kwargs = {"encoding": "oem", "creationflags": subprocess.CREATE_NO_WINDOW}
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
        **kwargs,
    )


def _invoke(run: Runner, argv: list[str]) -> RunResult:
    try:
        return run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SchedulerError(f"Could not run {Path(argv[0]).name}: {exc}") from exc


def _first_line(*texts: str | None, limit: int = 200) -> str:
    for text in texts:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:limit]
    return ""


def _require_success(result: RunResult, action: str) -> None:
    if result.returncode != 0:
        reason = _first_line(result.stderr, result.stdout) or f"exit code {result.returncode}"
        raise SchedulerError(f"{action} failed: {reason}")


def default_protected_roots(home: Path | None = None) -> tuple[Path, ...]:
    """Where Codex state may live when no config says otherwise."""
    roots: list[Path] = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.append(Path(codex_home).expanduser())
    roots.append((Path.home() if home is None else Path(home)) / ".codex")
    return tuple(roots)


def _refuse_protected(path: Path, protected_roots: Iterable[Path]) -> None:
    target = Path(os.path.abspath(path))
    for root in protected_roots:
        guarded = Path(os.path.abspath(root))
        if os.path.normcase(str(target)) == os.path.normcase(str(guarded)) or _is_within(target, guarded):
            raise SchedulerError(f"Refusing to write scheduler files inside Codex state: {target}")


def _is_within(path: Path, root: Path) -> bool:
    path_parts = [os.path.normcase(part) for part in path.parts]
    root_parts = [os.path.normcase(part) for part in root.parts]
    return len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _write_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _platform_key(platform: str | None) -> str:
    raw = (sys.platform if platform is None else platform).lower()
    if raw in ("win32", "windows", "cygwin") or raw.startswith("win"):
        return "windows"
    if raw in ("darwin", "macos"):
        return "macos"
    if raw.startswith("linux"):
        return "linux"
    raise ValueError(f"Unsupported scheduler platform: {raw}")


def system_scheduler(platform: str | None = None, **injections: Any) -> SystemScheduler:
    """The adapter for this OS (or for ``platform``), with test injections."""
    key = _platform_key(platform)
    if key == "windows":
        return WindowsTaskScheduler(**injections)
    if key == "macos":
        return LaunchdScheduler(**injections)
    return SystemdUserScheduler(**injections)


def _ignored_notes(
    ignored_settings: Callable[[ScheduledJob], tuple[str, ...]],
    expected: JobDefinition | None,
) -> list[str]:
    """Say which configured, non-zero settings the platform cannot apply."""
    if expected is None:
        return []
    names = [name for name in ignored_settings(expected.job) if getattr(expected.job, name)]
    return [f"not supported here and ignored: {', '.join(names)}"] if names else []


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Windows: Task Scheduler
# --------------------------------------------------------------------------

TASK_FOLDER = "\\CodexSync\\"
TASK_LEAF_NAME = "CodexSync Job"
TASK_NAME = TASK_FOLDER + TASK_LEAF_NAME
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

#: ``SCHED_S_*`` informational values Task Scheduler puts in LastTaskResult
#: instead of a process exit code; none of them is the job's result.
_TASK_STATUS_NOT_A_RESULT = {
    0x41300: "task is ready",
    0x41301: "task is currently running",
    0x41302: "task is disabled",
    0x41303: "task has not run yet",
    0x41304: "no more runs scheduled",
    0x41306: "last run was terminated",
}
_NEVER_RUN_RESULT = 0x41303
_WINDOWS_ENV_REFERENCE = re.compile(r"%[^%]+%")


def quote_windows_argument(argument: str) -> str:
    """Quote one argument so ``CommandLineToArgvW``/the MSVC runtime reads it back.

    Backslashes are literal except in front of a quote, where they escape in
    pairs; that is the case a naive ``replace('"', '\\"')`` gets wrong for a
    directory path ending in ``\\`` inside quotes.
    """
    if argument and not any(char in argument for char in ' \t\n\v"'):
        return argument
    pieces = ['"']
    backslashes = 0
    for char in argument:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            pieces.append("\\" * (2 * backslashes + 1))
        else:
            pieces.append("\\" * backslashes)
        pieces.append(char)
        backslashes = 0
    pieces.append("\\" * (2 * backslashes))
    pieces.append('"')
    return "".join(pieces)


def _check_windows_argument(argument: str) -> None:
    if any(char in argument for char in "\0\r\n"):
        raise ValueError("Scheduled task arguments must not contain NUL or line breaks")
    # Task Scheduler expands %VAR% in an Exec action and offers no escape, so
    # such a path would quietly name a different file.
    if _WINDOWS_ENV_REFERENCE.search(argument):
        raise ValueError(f"Scheduled task path would be expanded by Task Scheduler: {argument}")


def task_duration(seconds: int) -> str:
    """ISO 8601 duration as Task Scheduler writes it: minutes when whole."""
    if seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _duration_seconds(text: str | None) -> int | str | None:
    if text is None:
        return None
    match = _DURATION.match(text.strip())
    if not match or text.strip() in ("P", "PT"):
        return text.strip()
    days, hours, minutes, seconds = (int(group or 0) for group in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _default_user_id() -> str:
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def _windows_tool(relative: str, bare: str) -> str:
    system_root = os.environ.get("SystemRoot")
    if system_root:
        return str(Path(system_root) / "System32" / relative)
    return bare


def render_task_xml(definition: JobDefinition, *, user_id: str, now: datetime) -> str:
    """Task Scheduler 1.4 XML for one job.

    Exactly one trigger. ``run_at_login`` gives a LogonTrigger for this user,
    which is when an InteractiveToken task can run anyway; otherwise a
    TimeTrigger starting now. Both repeat indefinitely at the interval. The
    startup delay exists on the logon trigger only (a time trigger has no
    "startup" to delay from) and jitter on the time trigger only: Task
    Scheduler's schema rejects ``RandomDelay`` inside a LogonTrigger, checked
    with ``RegisterTask(..., TASK_VALIDATE_ONLY)`` against every combination.
    """
    job = definition.job
    command = definition.command[0]
    arguments = [*definition.command[1:], *job_arguments(job.mode, definition.config_path)]
    for item in (command, *arguments):
        _check_windows_argument(item)
    command_text = quote_windows_argument(command)
    arguments_text = " ".join(quote_windows_argument(item) for item in arguments)

    trigger_lines = [
        "      <Enabled>true</Enabled>",
        "      <Repetition>",
        f"        <Interval>{task_duration(job.interval_seconds)}</Interval>",
        "        <StopAtDurationEnd>false</StopAtDurationEnd>",
        "      </Repetition>",
    ]
    if job.run_at_login:
        tag = "LogonTrigger"
        trigger_lines.append(f"      <UserId>{_xml_escape(user_id)}</UserId>")
        if job.startup_delay_seconds > 0:
            trigger_lines.append(f"      <Delay>{task_duration(job.startup_delay_seconds)}</Delay>")
    else:
        tag = "TimeTrigger"
        start = now.replace(microsecond=0, tzinfo=None).isoformat()
        trigger_lines.insert(1, f"      <StartBoundary>{start}</StartBoundary>")
        if job.jitter_seconds > 0:
            trigger_lines.append(f"      <RandomDelay>{task_duration(job.jitter_seconds)}</RandomDelay>")

    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<Task version="1.4" xmlns="{_TASK_NAMESPACE}">',
        "  <RegistrationInfo>",
        f"    <Description>codexSync periodic read-only job ({_xml_escape(job.mode)}). Managed by codexSync.</Description>",
        "  </RegistrationInfo>",
        "  <Triggers>",
        f"    <{tag}>",
        *trigger_lines,
        f"    </{tag}>",
        "  </Triggers>",
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{_xml_escape(user_id)}</UserId>",
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <StartWhenAvailable>true</StartWhenAvailable>",
        "    <Hidden>false</Hidden>",
        "    <Enabled>true</Enabled>",
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{_xml_escape(command_text)}</Command>",
        f"      <Arguments>{_xml_escape(arguments_text)}</Arguments>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
        "",
    ]
    return "\r\n".join(lines)


def encode_task_xml(text: str) -> bytes:
    """UTF-16 LE with a BOM, which is what ``schtasks /XML`` expects on disk."""
    return codecs.BOM_UTF16_LE + text.encode("utf-16-le")


def _strip_namespace(element: ElementTree.Element) -> None:
    for node in element.iter():
        if isinstance(node.tag, str) and "}" in node.tag:
            node.tag = node.tag.split("}", 1)[1]


def parse_task_xml(text: str) -> ElementTree.Element:
    """Parse task XML however it arrived.

    The declaration is dropped before parsing: ``schtasks /Query /XML`` claims
    UTF-16 while handing over UTF-8 text, and the parser would believe it.
    """
    body = text.lstrip("\ufeff").replace("\r\r\n", "\n")
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", body, count=1)
    root = ElementTree.fromstring(body)
    _strip_namespace(root)
    return root


def _child_text(element: ElementTree.Element | None, path: str) -> str | None:
    if element is None:
        return None
    found = element.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _children(root: ElementTree.Element, path: str) -> list[ElementTree.Element]:
    container = root.find(path)
    return [] if container is None else list(container)


def _bool_text(text: str | None, default: bool) -> bool:
    if text is None:
        return default
    return text.strip().lower() == "true"


#: Schema defaults for the settings compared, because Windows omits an element
#: whose value is the default when it writes the definition back out.
_SETTING_DEFAULTS: dict[str, str] = {
    "MultipleInstancesPolicy": "IgnoreNew",
    "DisallowStartIfOnBatteries": "true",
    "StopIfGoingOnBatteries": "true",
    "StartWhenAvailable": "false",
    "Hidden": "false",
    "ExecutionTimeLimit": "PT72H",
}


def normalise_task_definition(root: ElementTree.Element) -> dict[str, Any]:
    """The parts of a task that decide what runs and when, in a stable form.

    Windows rewrites the stored XML (reorders elements, drops defaults, turns
    the principal's user into a SID), so bytes are useless for comparison.
    Deliberately left out: ``StartBoundary`` (it is "now" at install time),
    ``Settings/Enabled`` (reported separately as ``enabled``, so disabling the
    task does not also read as "out of date"), the principal's ``UserId``
    (stored as a SID) and descriptive registration info.
    """
    triggers: list[dict[str, Any]] = []
    for trigger in _children(root, "Triggers"):
        triggers.append({
            "type": trigger.tag,
            "enabled": _bool_text(_child_text(trigger, "Enabled"), True),
            "interval": _duration_seconds(_child_text(trigger, "Repetition/Interval")),
            "duration": _duration_seconds(_child_text(trigger, "Repetition/Duration")),
            "stop_at_duration_end": _bool_text(_child_text(trigger, "Repetition/StopAtDurationEnd"), False),
            "delay": _duration_seconds(_child_text(trigger, "Delay")) or 0,
            "random_delay": _duration_seconds(_child_text(trigger, "RandomDelay")) or 0,
            "user": (_child_text(trigger, "UserId") or "").casefold() or None,
            "end_boundary": _child_text(trigger, "EndBoundary"),
        })
    actions: list[dict[str, Any]] = []
    for action in _children(root, "Actions"):
        actions.append({
            "type": action.tag,
            "command": _child_text(action, "Command"),
            "arguments": _child_text(action, "Arguments"),
            "working_directory": _child_text(action, "WorkingDirectory"),
        })
    settings = root.find("Settings")
    normalised_settings: dict[str, Any] = {}
    for name, default in _SETTING_DEFAULTS.items():
        value = _child_text(settings, name) or default
        if name == "ExecutionTimeLimit":
            normalised_settings[name] = _duration_seconds(value)
        elif value.lower() in ("true", "false"):
            normalised_settings[name] = value.lower() == "true"
        else:
            normalised_settings[name] = value
    principal = root.find("Principals/Principal")
    return {
        "triggers": triggers,
        "actions": actions,
        "settings": normalised_settings,
        "principal": {
            "logon_type": _child_text(principal, "LogonType"),
            "run_level": _child_text(principal, "RunLevel") or "LeastPrivilege",
        },
    }


_MS_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
_ISO_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_task_time(value: Any) -> str | None:
    """A Get-ScheduledTaskInfo time as UTC ``YYYY-MM-DDTHH:MM:SSZ``, or None.

    Windows PowerShell 5.1 serialises ``DateTime`` as ``/Date(ms)/`` (UTC
    epoch milliseconds); PowerShell 7 writes ISO 8601, with or without an
    offset (without one it is local time). Task Scheduler's "never" is
    1999-11-30 or year 1, so anything before 2000 is None.
    """
    if isinstance(value, dict):
        value = value.get("value", value.get("DateTime"))
    if value is None or not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    match = _MS_DATE.match(text)
    try:
        if match:
            moment = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(_ISO_FRACTION.sub(r"\1", text.replace("Z", "+00:00")))
            if moment.tzinfo is None:
                moment = moment.astimezone()
    except (ValueError, OverflowError, OSError):
        return None
    if moment.year < 2000:
        return None
    return _utc_text(moment)


def parse_task_info(text: str) -> tuple[str | None, str | None, int | None, str]:
    """``(last_run_utc, next_run_utc, last_result, note)`` from the PowerShell JSON."""
    data = json.loads(text.strip().lstrip("\ufeff"))
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("unexpected task info shape")
    raw_result = data.get("LastTaskResult")
    note = ""
    last_result: int | None = None
    if isinstance(raw_result, int) and not isinstance(raw_result, bool):
        if raw_result in _TASK_STATUS_NOT_A_RESULT:
            note = _TASK_STATUS_NOT_A_RESULT[raw_result]
        else:
            last_result = raw_result
    return parse_task_time(data.get("LastRunTime")), parse_task_time(data.get("NextRunTime")), last_result, note


class WindowsTaskScheduler:
    """``\\CodexSync\\CodexSync Job`` in the current user's Task Scheduler.

    ``log_dir`` is accepted for a uniform contract but unused: an Exec action
    has no output redirection short of wrapping the job in ``cmd.exe``, which
    would bring back the console window; codexSync writes its own log file
    from ``[logging]`` in the config.
    """

    platform = "windows"
    reports_run_times = True

    def ignored_settings(self, job: ScheduledJob) -> tuple[str, ...]:
        # LogonTrigger has no RandomDelay (Task Scheduler rejects the XML), and
        # a TimeTrigger has no startup to delay from.
        return ("jitter_seconds",) if job.run_at_login else ("startup_delay_seconds",)

    def __init__(
        self,
        *,
        run: Runner = _default_run,
        user_id: str | None = None,
        now: Callable[[], datetime] = datetime.now,
        temp_dir: Path | None = None,
        protected_roots: Sequence[Path] | None = None,
        schtasks: str | None = None,
        powershell: str | None = None,
    ) -> None:
        self._run = run
        self._user_id = user_id or _default_user_id()
        self._now = now
        self._temp_dir = Path(tempfile.gettempdir()) if temp_dir is None else Path(temp_dir)
        self._protected = tuple(default_protected_roots() if protected_roots is None else protected_roots)
        self.schtasks = schtasks or _windows_tool("schtasks.exe", "schtasks")
        self.powershell = powershell or _windows_tool(r"WindowsPowerShell\v1.0\powershell.exe", "powershell")

    def render(self, definition: JobDefinition) -> dict[str, bytes]:
        text = render_task_xml(definition, user_id=self._user_id, now=self._now())
        return {"codexsync-job.xml": encode_task_xml(text)}

    def install(self, job: ScheduledJob, *, command: Sequence[str], config_path: Path, log_dir: Path) -> None:
        definition = JobDefinition(job, tuple(command), config_path, log_dir)
        payload = self.render(definition)["codexsync-job.xml"]
        _refuse_protected(self._temp_dir, self._protected)
        handle, name = tempfile.mkstemp(prefix="codexsync-task-", suffix=".xml", dir=self._temp_dir)
        path = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            result = _invoke(self._run, [self.schtasks, "/Create", "/XML", str(path), "/TN", TASK_NAME, "/F"])
            _require_success(result, "Registering the scheduled task")
        finally:
            path.unlink(missing_ok=True)

    def remove(self) -> bool:
        result = _invoke(self._run, [self.schtasks, "/Delete", "/TN", TASK_NAME, "/F"])
        if result.returncode == 0:
            self._remove_empty_folder()
            return True
        # The "cannot find" message is localized; ask again instead of reading it.
        if self._query_xml()[0]:
            _require_success(result, "Removing the scheduled task")
        return False

    def _remove_empty_folder_argv(self) -> list[str]:
        # Only an empty folder is deleted, so a task someone else filed under
        # the same name is never touched. The folder is ours: install made it.
        folder = TASK_FOLDER.strip("\\")
        script = (
            "$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
            f"$f = $s.GetFolder('\\{folder}'); "
            "if ($f.GetTasks(1).Count -eq 0 -and $f.GetFolders(0).Count -eq 0) "
            f"{{ $s.GetFolder('\\').DeleteFolder('{folder}', 0) }}"
        )
        return [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script]

    def _remove_empty_folder(self) -> None:
        """Best effort: leaving an empty folder behind is untidy, not unsafe."""
        try:
            _invoke(self._run, self._remove_empty_folder_argv())
        except SchedulerError:
            pass

    def _query_xml(self) -> tuple[bool, RunResult]:
        result = _invoke(self._run, [self.schtasks, "/Query", "/TN", TASK_NAME, "/XML"])
        return result.returncode == 0, result

    def _task_info_argv(self) -> list[str]:
        script = (
            f"Get-ScheduledTaskInfo -TaskPath '{TASK_FOLDER}' -TaskName '{TASK_LEAF_NAME}'"
            " | Select-Object LastRunTime,NextRunTime,LastTaskResult | ConvertTo-Json -Compress"
        )
        return [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script]

    def status(self, expected: JobDefinition | None = None) -> SchedulerStatus:
        installed, query = self._query_xml()
        if not installed:
            return SchedulerStatus(installed=False, detail="Scheduled task is not installed")
        notes: list[str] = []
        enabled: bool | None = None
        matches: bool | None = None
        try:
            root = parse_task_xml(query.stdout or "")
        except ElementTree.ParseError as exc:
            notes.append(f"task definition unreadable: {exc}")
        else:
            enabled = _bool_text(_child_text(root.find("Settings"), "Enabled"), True)
            notes.append("enabled" if enabled else "disabled")
            if expected is not None:
                rendered = parse_task_xml(render_task_xml(expected, user_id=self._user_id, now=self._now()))
                matches = normalise_task_definition(root) == normalise_task_definition(rendered)
                if not matches:
                    notes.append("definition differs from configuration")
        notes.extend(_ignored_notes(self.ignored_settings, expected))

        last_run = next_run = None
        last_result: int | None = None
        try:
            info = _invoke(self._run, self._task_info_argv())
            if info.returncode != 0:
                raise ValueError(_first_line(info.stderr, info.stdout) or f"exit code {info.returncode}")
            last_run, next_run, last_result, note = parse_task_info(info.stdout or "")
            if note:
                notes.append(note)
        except (SchedulerError, ValueError) as exc:
            notes.append(f"run times unavailable: {exc}")
        return SchedulerStatus(
            installed=True,
            enabled=enabled,
            last_run_utc=last_run,
            next_run_utc=next_run,
            last_result=last_result,
            detail="; ".join(notes),
            definition_matches=matches,
        )


# --------------------------------------------------------------------------
# macOS: launchd LaunchAgent
# --------------------------------------------------------------------------

LAUNCHD_LABEL = "io.codexsync.job"
_LAST_EXIT_CODE = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)", re.MULTILINE)


class LaunchdScheduler:
    """``~/Library/LaunchAgents/io.codexsync.job.plist`` in the GUI domain.

    launchd has no startup delay or jitter for ``StartInterval`` and does not
    expose last/next run times; those are reported as unsupported rather than
    emulated with a sleep wrapper that would hold a process open.
    """

    platform = "macos"
    reports_run_times = False

    def ignored_settings(self, job: ScheduledJob) -> tuple[str, ...]:
        return ("startup_delay_seconds", "jitter_seconds")

    def __init__(
        self,
        *,
        run: Runner = _default_run,
        home: Path | None = None,
        uid: int | None = None,
        protected_roots: Sequence[Path] | None = None,
        launchctl: str = "/bin/launchctl",
    ) -> None:
        self._run = run
        self._home = Path.home() if home is None else Path(home)
        self._uid = uid if uid is not None else getattr(os, "getuid", lambda: 0)()
        self._protected = tuple(default_protected_roots(self._home) if protected_roots is None else protected_roots)
        self.launchctl = launchctl

    @property
    def plist_path(self) -> Path:
        return self._home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    @property
    def _domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def _service(self) -> str:
        return f"{self._domain}/{LAUNCHD_LABEL}"

    def definition(self, definition: JobDefinition) -> dict[str, Any]:
        job = definition.job
        return {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": definition.argv(),
            "StartInterval": job.interval_seconds,
            "RunAtLoad": job.run_at_login,
            "StandardOutPath": str(definition.log_dir / "codexsync-job.out.log"),
            "StandardErrorPath": str(definition.log_dir / "codexsync-job.err.log"),
            "ProcessType": "Background",
        }

    def render(self, definition: JobDefinition) -> dict[str, bytes]:
        return {self.plist_path.name: plistlib.dumps(self.definition(definition), sort_keys=True)}

    def install(self, job: ScheduledJob, *, command: Sequence[str], config_path: Path, log_dir: Path) -> None:
        definition = JobDefinition(job, tuple(command), config_path, log_dir)
        payload = self.render(definition)[self.plist_path.name]
        _refuse_protected(self.plist_path, self._protected)
        _refuse_protected(definition.log_dir, self._protected)
        # launchd opens the log files but does not create their directory.
        definition.log_dir.mkdir(parents=True, exist_ok=True)
        _write_atomically(self.plist_path, payload)
        _invoke(self._run, [self.launchctl, "bootout", self._service])  # not loaded is fine
        _require_success(
            _invoke(self._run, [self.launchctl, "bootstrap", self._domain, str(self.plist_path)]),
            "Loading the LaunchAgent",
        )

    def remove(self) -> bool:
        booted = _invoke(self._run, [self.launchctl, "bootout", self._service]).returncode == 0
        existed = self.plist_path.exists()
        if existed:
            self.plist_path.unlink()
        return existed or booted

    def status(self, expected: JobDefinition | None = None) -> SchedulerStatus:
        present = self.plist_path.is_file()
        loaded = _invoke(self._run, [self.launchctl, "print", self._service])
        if not present and loaded.returncode != 0:
            return SchedulerStatus(installed=False, detail="LaunchAgent is not installed")
        notes: list[str] = []
        enabled = loaded.returncode == 0
        notes.append("loaded" if enabled else "installed but not loaded")
        if not present:
            notes.append("plist file is missing")
        last_result: int | None = None
        match = _LAST_EXIT_CODE.search(loaded.stdout or "") if enabled else None
        if match:
            last_result = int(match.group(1))
        matches: bool | None = None
        if expected is not None and present:
            try:
                current = plistlib.loads(self.plist_path.read_bytes())
            except (plistlib.InvalidFileException, ValueError, OSError) as exc:
                notes.append(f"plist unreadable: {exc}")
            else:
                matches = current == self.definition(expected)
                if not matches:
                    notes.append("definition differs from configuration")
        notes.extend(_ignored_notes(self.ignored_settings, expected))
        notes.append("launchd does not report run times")
        return SchedulerStatus(
            installed=True,
            enabled=enabled,
            last_result=last_result,
            detail="; ".join(notes),
            definition_matches=matches,
        )


# --------------------------------------------------------------------------
# Linux: systemd user timer
# --------------------------------------------------------------------------

SYSTEMD_SERVICE = "codexsync-job.service"
SYSTEMD_TIMER = "codexsync-job.timer"
_SYSTEMD_SAFE = re.compile(r"^[A-Za-z0-9_@+=:,./-]+$")


def quote_systemd_argument(argument: str) -> str:
    """Quote one ``ExecStart=`` word.

    Not ``shlex.join``: systemd is not a POSIX shell. It expands ``%``
    specifiers and ``$VAR`` even inside single quotes and applies C escapes to
    backslashes, so a shell-quoted path containing any of those would reach
    the program changed. Double quotes with those four characters escaped read
    back verbatim.
    """
    if any(char in argument for char in "\0\r\n"):
        raise ValueError("systemd arguments must not contain NUL or line breaks")
    if _SYSTEMD_SAFE.match(argument):
        return argument
    escaped = (
        argument.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    )
    return f'"{escaped}"'


def _systemd_path_value(path: Path) -> str:
    text = str(path)
    if any(char in text for char in "\0\r\n"):
        raise ValueError("systemd paths must not contain NUL or line breaks")
    return text.replace("%", "%%")


_SYSTEMD_TIMESTAMP = re.compile(r"^\w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?: (\S+))?$")


def parse_systemd_time(value: str | None) -> str | None:
    """``systemctl show`` timestamp as UTC text, or None for ``n/a``/0.

    Accepts ``@<epoch>`` and ``Www YYYY-MM-DD HH:MM:SS ZONE``. A zone other
    than UTC/GMT is read as this process's local time: systemctl formats in
    the same user session's zone, and an abbreviation like MSK is not
    something to guess an offset from.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or text in ("n/a", "0"):
        return None
    try:
        if text.startswith("@"):
            moment = datetime.fromtimestamp(int(text[1:]), tz=timezone.utc)
        else:
            match = _SYSTEMD_TIMESTAMP.match(text)
            if not match:
                return None
            naive = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            zone = (match.group(2) or "").upper()
            moment = naive.replace(tzinfo=timezone.utc) if zone in ("UTC", "GMT") else naive.astimezone()
    except (ValueError, OverflowError, OSError):
        return None
    return _utc_text(moment)


def _parse_show(text: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


class SystemdUserScheduler:
    """``codexsync-job.timer`` + ``.service`` under ``~/.config/systemd/user``.

    The user manager starts ``timers.target`` when the user's session starts,
    so the timer is (re)activated at login and on install. ``OnActiveSec``
    counts from that activation and ``OnUnitActiveSec`` from the last run:

    * ``run_at_login=True``: first run ``startup_delay_seconds`` (at least 1s)
      after activation, then every interval;
    * ``run_at_login=False``: first run one full interval after activation.

    ``Persistent=false`` because a missed run of a read-only job is not worth
    catching up; the next interval runs it.
    """

    platform = "linux"
    reports_run_times = True

    def ignored_settings(self, job: ScheduledJob) -> tuple[str, ...]:
        return () if job.run_at_login else ("startup_delay_seconds",)

    def __init__(
        self,
        *,
        run: Runner = _default_run,
        home: Path | None = None,
        protected_roots: Sequence[Path] | None = None,
        systemctl: str = "systemctl",
    ) -> None:
        self._run = run
        self._home = Path.home() if home is None else Path(home)
        self._protected = tuple(default_protected_roots(self._home) if protected_roots is None else protected_roots)
        self.systemctl = systemctl

    @property
    def unit_dir(self) -> Path:
        return self._home / ".config" / "systemd" / "user"

    def _ctl(self, *args: str) -> list[str]:
        return [self.systemctl, "--user", *args]

    def render(self, definition: JobDefinition) -> dict[str, bytes]:
        job = definition.job
        exec_start = " ".join(quote_systemd_argument(item) for item in definition.argv())
        service = "\n".join([
            "[Unit]",
            f"Description=codexSync periodic read-only job ({job.mode})",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={exec_start}",
            "TimeoutStartSec=10min",
            f"StandardOutput=append:{_systemd_path_value(definition.log_dir / 'codexsync-job.out.log')}",
            f"StandardError=append:{_systemd_path_value(definition.log_dir / 'codexsync-job.err.log')}",
            "",
        ])
        first_run = max(job.startup_delay_seconds, 1) if job.run_at_login else job.interval_seconds
        timer_lines = [
            "[Unit]",
            f"Description=Run codexSync {job.mode} every {job.interval_seconds} seconds",
            "",
            "[Timer]",
            f"OnActiveSec={first_run}s",
            f"OnUnitActiveSec={job.interval_seconds}s",
            # The default accuracy of one minute would let a 60s job drift to 120s.
            "AccuracySec=1s",
        ]
        if job.jitter_seconds > 0:
            timer_lines.append(f"RandomizedDelaySec={job.jitter_seconds}s")
        timer_lines += ["Persistent=false", f"Unit={SYSTEMD_SERVICE}", "", "[Install]", "WantedBy=timers.target", ""]
        return {
            SYSTEMD_SERVICE: service.encode("utf-8"),
            SYSTEMD_TIMER: "\n".join(timer_lines).encode("utf-8"),
        }

    def install(self, job: ScheduledJob, *, command: Sequence[str], config_path: Path, log_dir: Path) -> None:
        definition = JobDefinition(job, tuple(command), config_path, log_dir)
        files = self.render(definition)
        _refuse_protected(self.unit_dir, self._protected)
        _refuse_protected(definition.log_dir, self._protected)
        # systemd opens append: targets but does not create their directory.
        definition.log_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in files.items():
            _write_atomically(self.unit_dir / name, payload)
        _require_success(_invoke(self._run, self._ctl("daemon-reload")), "Reloading systemd user units")
        _require_success(_invoke(self._run, self._ctl("enable", SYSTEMD_TIMER)), "Enabling the timer")
        # restart, not start: an already running timer keeps its old schedule
        # until it is re-armed, and an update must take effect now.
        _require_success(_invoke(self._run, self._ctl("restart", SYSTEMD_TIMER)), "Starting the timer")

    def remove(self) -> bool:
        paths = [self.unit_dir / SYSTEMD_TIMER, self.unit_dir / SYSTEMD_SERVICE]
        existed = any(path.exists() for path in paths)
        if not existed:
            return False
        _invoke(self._run, self._ctl("disable", "--now", SYSTEMD_TIMER))
        for path in paths:
            path.unlink(missing_ok=True)
        _require_success(_invoke(self._run, self._ctl("daemon-reload")), "Reloading systemd user units")
        return True

    def status(self, expected: JobDefinition | None = None) -> SchedulerStatus:
        timer_path = self.unit_dir / SYSTEMD_TIMER
        service_path = self.unit_dir / SYSTEMD_SERVICE
        if not timer_path.is_file() and not service_path.is_file():
            return SchedulerStatus(installed=False, detail="systemd user timer is not installed")
        notes: list[str] = []
        timer = _invoke(self._run, self._ctl(
            "show", SYSTEMD_TIMER,
            "--property=ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime",
        ))
        service = _invoke(self._run, self._ctl(
            "show", SYSTEMD_SERVICE, "--property=ExecMainStatus,ExecMainExitTimestampMonotonic",
        ))
        enabled: bool | None = None
        last_run = next_run = None
        last_result: int | None = None
        if timer.returncode == 0:
            values = _parse_show(timer.stdout)
            active, file_state = values.get("ActiveState", ""), values.get("UnitFileState", "")
            enabled = file_state == "enabled" and active == "active"
            notes.append(f"timer {active or 'unknown'}, {file_state or 'unknown'}")
            last_run = parse_systemd_time(values.get("LastTriggerUSec"))
            next_run = parse_systemd_time(values.get("NextElapseUSecRealtime"))
        else:
            notes.append(f"timer state unavailable: {_first_line(timer.stderr, timer.stdout)}")
        if service.returncode == 0:
            values = _parse_show(service.stdout)
            # ExecMainStatus is 0 both after a clean run and before any run.
            if values.get("ExecMainExitTimestampMonotonic", "0") not in ("", "0"):
                try:
                    last_result = int(values.get("ExecMainStatus", ""))
                except ValueError:
                    last_result = None
        if not (timer_path.is_file() and service_path.is_file()):
            notes.append("a unit file is missing")
        matches: bool | None = None
        if expected is not None:
            rendered = self.render(expected)
            try:
                matches = all(
                    _normalise_unit_text((self.unit_dir / name).read_text(encoding="utf-8"))
                    == _normalise_unit_text(payload.decode("utf-8"))
                    for name, payload in rendered.items()
                )
            except (OSError, UnicodeDecodeError):
                matches = False
            if not matches:
                notes.append("definition differs from configuration")
        notes.extend(_ignored_notes(self.ignored_settings, expected))
        return SchedulerStatus(
            installed=True,
            enabled=enabled,
            last_run_utc=last_run,
            next_run_utc=next_run,
            last_result=last_result,
            detail="; ".join(notes),
            definition_matches=matches,
        )


def _normalise_unit_text(text: str) -> list[str]:
    return [line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n")]


__all__ = [
    "JOB_MODES",
    "JobDefinition",
    "LaunchdScheduler",
    "SchedulerError",
    "SchedulerStatus",
    "ScheduledJob",
    "SystemScheduler",
    "SystemdUserScheduler",
    "WindowsTaskScheduler",
    "job_arguments",
    "job_command",
    "system_scheduler",
]
