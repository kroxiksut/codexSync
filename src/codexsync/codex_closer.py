"""Ask Codex to quit before a sync, when the person turned that on (D-029).

codexSync never forces Codex closed. 0.1 did -- `taskkill /T /F`, which is a
crash as far as Codex can tell, and a crash is what leaves its global state and
its SQLite catalogue half written. What is here only *asks*, through the
channel the operating system itself uses to close applications for an update,
and then the ordinary gate waits for every Codex process to be gone. An
application that declines stays open, and the sync writes nothing.

* **Windows.** The desktop app is `ChatGPT.exe` from the `OpenAI.Codex_`
  package (since 26.930 one window holds ChatGPT and Codex); its `codex.exe`
  children are Codex's own server. A click on the window's close button only
  hides it to the tray, so the request goes through the Restart Manager:
  `RmShutdown` *without* `RmForceShutdown`, aimed at the app's root process.
  Observed on 2026-10-06 (`PROVEN_CLOSERS`): answer 0, all twelve `ChatGPT.exe`
  and both `codex.exe` gone in two seconds, the state intact.
* **macOS.** The app is asked to quit by its bundle id, as the Dock's *Quit*
  does. Not observed -- there is no Mac -- so the gate stays closed there.
* A Codex CLI running in a terminal is never asked: it has no window to ask,
  and closing someone's terminal session is not a sync's business.

Everything that touches the system is injected, so no test asks anything real.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import subprocess
import sys

LOG = logging.getLogger(__name__)

#: Platforms where asking was watched to close Codex cleanly, with what was
#: seen. Filled only from a live observation, like `PROVEN_DETECTORS`.
PROVEN_CLOSERS: dict[str, str] = {
    "win32": (
        "Windows 10 19045, Codex 26.930.4958.0 (ChatGPT.exe, OpenAI.Codex package): Restart "
        "Manager without force, answer 0, 12 ChatGPT.exe and 2 codex.exe gone in 2 s, state "
        "intact; observed 2026-10-06"
    ),
}

#: The Windows package the Codex desktop app ships in, as it appears in the path.
WINDOWS_PACKAGE_MARKER = "\\openai.codex_"
WINDOWS_APP_NAME = "chatgpt.exe"
WINDOWS_CODEX_NAME = "codex.exe"
MACOS_BUNDLE_ID = "com.openai.codex"

#: `RmShutdown` answers worth naming; anything else is reported as its number.
_RM_ANSWERS = {
    0: "agreed to close",
    5: "access denied",
    351: "the application declined to close",
    352: "the application asked for a restart instead",
}


@dataclass(frozen=True, slots=True)
class WindowsProcess:
    pid: int
    parent_pid: int
    name: str
    path: str
    #: Creation time as a FILETIME integer, which the Restart Manager needs to
    #: be sure the pid still names the same process.
    started: int


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What asking did. ``asked`` is false when nothing could be asked."""

    asked: bool
    accepted: bool
    detail: str
    #: Processes asked to quit (their pids), for the log.
    pids: tuple[int, ...] = ()


def can_close(platform: str | None = None) -> bool:
    """Whether asking Codex to quit was observed to work on this platform."""
    return bool(PROVEN_CLOSERS.get(platform or sys.platform))


def ask_codex_to_quit(
    platform: str | None = None,
    *,
    list_windows_processes: Callable[[], Sequence[WindowsProcess]] | None = None,
    restart_manager: Callable[[Sequence[WindowsProcess]], int] | None = None,
    run: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> CloseOutcome:
    """Ask the Codex app to quit, once, without force. Never waits for it."""
    platform = platform or sys.platform
    if not can_close(platform):
        return CloseOutcome(
            False, False,
            f"asking Codex to quit has not been observed to work on {platform}; close it yourself",
        )
    if platform.startswith("win"):
        return _ask_windows(
            list_windows_processes or _list_windows_processes, restart_manager or _restart_manager_shutdown,
        )
    if platform == "darwin":
        return _ask_macos(run or _run)
    return CloseOutcome(False, False, f"no way to ask Codex to quit on {platform}")


def codex_app_roots(processes: Sequence[WindowsProcess]) -> tuple[WindowsProcess, ...]:
    """The Codex app's root processes: `ChatGPT.exe` from the package, not a child of itself."""
    app = [
        proc for proc in processes
        if proc.name.lower() == WINDOWS_APP_NAME and WINDOWS_PACKAGE_MARKER in proc.path.lower()
    ]
    app_pids = {proc.pid for proc in app}
    return tuple(proc for proc in app if proc.parent_pid not in app_pids)


def terminal_codex(processes: Sequence[WindowsProcess]) -> tuple[WindowsProcess, ...]:
    """`codex.exe` processes no Codex app started: a Codex CLI in a terminal."""
    by_pid = {proc.pid: proc for proc in processes}
    app_pids = {
        proc.pid for proc in processes
        if proc.name.lower() == WINDOWS_APP_NAME and WINDOWS_PACKAGE_MARKER in proc.path.lower()
    }
    found = []
    for proc in processes:
        if proc.name.lower() != WINDOWS_CODEX_NAME:
            continue
        seen, parent = set(), by_pid.get(proc.parent_pid)
        while parent is not None and parent.pid not in seen and parent.pid not in app_pids:
            seen.add(parent.pid)
            parent = by_pid.get(parent.parent_pid)
        if parent is None or parent.pid not in app_pids:
            found.append(proc)
    return tuple(found)


def _ask_windows(
    list_processes: Callable[[], Sequence[WindowsProcess]],
    shutdown: Callable[[Sequence[WindowsProcess]], int],
) -> CloseOutcome:
    try:
        processes = list(list_processes())
    except OSError as exc:
        return CloseOutcome(False, False, f"the process list could not be read: {exc}")
    cli = terminal_codex(processes)
    if cli:
        return CloseOutcome(
            False, False,
            "Codex is running in a terminal (pid " + ", ".join(str(proc.pid) for proc in cli)
            + "); close it there -- a terminal is never closed for you",
        )
    roots = codex_app_roots(processes)
    if not roots:
        return CloseOutcome(False, False, "no Codex app window to ask; close Codex yourself")
    try:
        answer = shutdown(roots)
    except OSError as exc:
        return CloseOutcome(False, False, f"the Restart Manager could not be asked: {exc}")
    pids = tuple(proc.pid for proc in roots)
    meaning = _RM_ANSWERS.get(answer, f"answer {answer}")
    return CloseOutcome(True, answer == 0, f"Restart Manager: {meaning}", pids)


def _ask_macos(run: Callable[[list[str]], subprocess.CompletedProcess]) -> CloseOutcome:
    script = f'tell application id "{MACOS_BUNDLE_ID}" to quit'
    try:
        result = run(["osascript", "-e", script])
    except OSError as exc:
        return CloseOutcome(False, False, f"the app could not be asked: {exc}")
    if result.returncode != 0:
        return CloseOutcome(True, False, "the app did not accept the request to quit")
    return CloseOutcome(True, True, "asked the app to quit")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


# --- Windows, through ctypes -----------------------------------------------------


def _list_windows_processes() -> list[WindowsProcess]:  # pragma: no cover - needs Windows
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    entries: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    wanted = {WINDOWS_APP_NAME, WINDOWS_CODEX_NAME}
    processes = []
    for pid, parent, name in entries:
        path, started = "", 0
        if name.lower() in wanted:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(buffer))
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        path = buffer.value
                    times = [wintypes.FILETIME() for _ in range(4)]
                    if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                        started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                finally:
                    kernel32.CloseHandle(handle)
        processes.append(WindowsProcess(pid, parent, name, path, started))
    return processes


def _restart_manager_shutdown(roots: Sequence[WindowsProcess]) -> int:  # pragma: no cover - needs Windows
    """`RmShutdown` with no flags: ask, never force (`RmForceShutdown` is 0x1)."""
    import ctypes
    from ctypes import wintypes

    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    rstrtmgr = ctypes.WinDLL("rstrtmgr")
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(64)
    answer = rstrtmgr.RmStartSession(ctypes.byref(session), 0, key)
    if answer:
        return answer
    try:
        apps = (RM_UNIQUE_PROCESS * len(roots))()
        for slot, proc in zip(apps, roots):
            slot.dwProcessId = proc.pid
            slot.ProcessStartTime.dwLowDateTime = proc.started & 0xFFFFFFFF
            slot.ProcessStartTime.dwHighDateTime = proc.started >> 32
        answer = rstrtmgr.RmRegisterResources(session, 0, None, len(roots), apps, 0, None)
        if answer:
            return answer
        return rstrtmgr.RmShutdown(session, 0, None)
    finally:
        rstrtmgr.RmEndSession(session)
