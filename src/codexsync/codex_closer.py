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
* **Linux.** The desktop app (`/usr/lib/chatgpt/ChatGPT`, Electron, its
  window drawn through XWayland) is asked the way a person quits it: the *Quit*
  entry of its tray menu (a StatusNotifierItem whose `com.canonical.dbusmenu`
  menu is reached with `busctl`), clicked over D-Bus. Signed in, a close of
  its window only hides it to that tray (observed 2026-10-19: 19 processes
  stayed, no window). Without a tray icon the window is asked instead, with
  the EWMH request `_NET_CLOSE_WINDOW` through the system's `libX11` -- which
  quit the app when it was not signed in, and otherwise leaves it in the
  background, where the sync's wait runs out and nothing is written. The menu
  also lists chat titles; they are read only to find *Quit* and never kept.
  `SIGTERM` -- the obvious alternative -- is *not* a request to this app: it
  ends at once, leaves the server orphaned and writes nothing on the way out,
  so it is never sent.
* **macOS.** The app is asked to quit by its bundle id, as the Dock's *Quit*
  does. Not observed -- there is no Mac -- so the gate stays closed there.
* A Codex CLI running in a terminal is never asked: it has no window to ask,
  and closing someone's terminal session is not a sync's business.
* Codex CLI 0.163 leaves a shared app-server running after the CLI quits
  (`<CODEX_HOME>/packages/app-server-daemon/`, CS-429). On Linux, with no CLI
  open, it is asked to stop with Codex's own `codex app-server daemon stop`
  (observed 2026-10-19: clean, 0.4 s). Elsewhere a refusal names that command.

Everything that touches the system is injected, so no test asks anything real.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
import sys

from .process_detector import proc_program
from .process_knowledge import BACKGROUND_PROCESS_NAMES, CLI_DAEMON_STOP_COMMAND, is_cli_daemon_path

LOG = logging.getLogger(__name__)

#: Platforms where asking was watched to close Codex cleanly, with what was
#: seen. Filled only from a live observation, like `PROVEN_DETECTORS`.
PROVEN_CLOSERS: dict[str, str] = {
    "win32": (
        "Windows 10 19045, Codex 26.930.4958.0 (ChatGPT.exe, OpenAI.Codex package): Restart "
        "Manager without force, answer 0, 12 ChatGPT.exe and 2 codex.exe gone in 2 s, state "
        "intact; observed 2026-10-06"
    ),
    "linux": (
        "Ubuntu 26.04, GNOME 50 on Wayland, Codex desktop deb 26.1013.40934 signed in: tray menu Quit "
        "over D-Bus, every process gone in 2-3 s (codex app-server included), global state written on "
        "quit, SQLite logs closed, two runs; its window's close only hid it to the tray. Offline "
        "(26.1002.52244, 26.1013), _NET_CLOSE_WINDOW quit it in 2 s, server in 7-10 s, five runs. "
        "SIGTERM ended it abruptly (server orphaned, log left open); observed 2026-10-19"
    ),
}

#: The Windows package the Codex desktop app ships in, as it appears in the path.
WINDOWS_PACKAGE_MARKER = "\\openai.codex_"
WINDOWS_APP_NAME = "chatgpt.exe"
WINDOWS_CODEX_NAME = "codex.exe"
MACOS_BUNDLE_ID = "com.openai.codex"
#: Where the Linux desktop app's programs live: the path markers the process
#: detector already uses for Linux, so the two never disagree about what the
#: app is.
LINUX_APP_MARKERS: tuple[str, ...] = tuple(
    name for name in BACKGROUND_PROCESS_NAMES["linux"] if "/" in name
)
LINUX_CODEX_NAME = "codex"

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
class LinuxProcess:
    pid: int
    parent_pid: int
    name: str
    #: The program's path: `/proc/<pid>/exe`, else the first argument.
    path: str


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
    list_linux_processes: Callable[[], Sequence[LinuxProcess]] | None = None,
    x11: "X11Windows | None" = None,
    tray: "TrayMenus | None" = None,
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
    if platform.startswith("linux"):
        return _ask_linux(
            list_linux_processes or _list_linux_processes, x11 or _LibX11(), run or _run, tray or _BusctlTray(),
        )
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
    outside = terminal_codex(processes)
    cli = tuple(proc for proc in outside if not is_cli_daemon_path(proc.path))
    if outside and not cli:
        # Codex CLI's server, left after the CLI quit (CS-429). Stopping it is
        # observed on Linux only, so here it is named, not asked.
        return CloseOutcome(
            False, False,
            "Codex CLI's background server is running (pid " + ", ".join(str(proc.pid) for proc in outside)
            + f"); it stays after the CLI is closed -- stop it with `{CLI_DAEMON_STOP_COMMAND}`",
        )
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


# --- Linux -----------------------------------------------------------------------


class X11Windows:
    """What asking needs from the display: the windows and their processes, and a close."""

    def windows(self) -> Sequence[tuple[int, int]]:  # pragma: no cover - interface
        """``(window id, pid)`` of every top-level window the window manager lists."""
        raise NotImplementedError

    def close(self, windows: Sequence[int]) -> None:  # pragma: no cover - interface
        """Send each window the window manager's close request."""
        raise NotImplementedError


#: The tray menu entry that quits the app, as Codex 26.1013 labels it (English
#: even with a Russian interface). A dbusmenu label may carry `_` mnemonics.
TRAY_QUIT_LABELS = frozenset({"Quit"})


@dataclass(frozen=True, slots=True)
class TrayEntry:
    """One *Quit* entry of a tray icon's menu, and whose icon it is."""

    bus_name: str
    menu_path: str
    item_id: int
    pid: int


class TrayMenus:
    """What asking needs from the tray: the app's *Quit* entries, and a click."""

    def quit_entries(self, app_pids: set[int]) -> Sequence[TrayEntry]:  # pragma: no cover - interface
        raise NotImplementedError

    def click(self, entries: Sequence[TrayEntry]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def is_linux_app(proc: LinuxProcess) -> bool:
    return any(proc.path.startswith(marker) for marker in LINUX_APP_MARKERS)


def linux_terminal_codex(processes: Sequence[LinuxProcess]) -> tuple[LinuxProcess, ...]:
    """`codex` processes the desktop app did not start: a Codex CLI in a terminal."""
    by_pid = {proc.pid: proc for proc in processes}
    found = []
    for proc in processes:
        if proc.name != LINUX_CODEX_NAME or is_linux_app(proc):
            continue
        seen, parent = set(), by_pid.get(proc.parent_pid)
        while parent is not None and parent.pid not in seen and not is_linux_app(parent):
            seen.add(parent.pid)
            parent = by_pid.get(parent.parent_pid)
        if parent is None or not is_linux_app(parent):
            found.append(proc)
    return tuple(found)


def _ask_linux(
    list_processes: Callable[[], Sequence[LinuxProcess]],
    x11: X11Windows,
    run: Callable[[list[str]], subprocess.CompletedProcess],
    tray: TrayMenus | None = None,
) -> CloseOutcome:
    try:
        processes = list(list_processes())
    except OSError as exc:
        return CloseOutcome(False, False, f"the process list could not be read: {exc}")
    outside = linux_terminal_codex(processes)
    daemons = [proc for proc in outside if is_cli_daemon_path(proc.path)]
    cli = [proc for proc in outside if proc not in daemons]
    if cli:
        # Also what keeps the daemon running: a CLI open in a terminal is its client.
        return CloseOutcome(
            False, False,
            "Codex is running in a terminal (pid " + ", ".join(str(proc.pid) for proc in cli)
            + "); close it there -- a terminal is never closed for you",
        )
    app = {proc.pid for proc in processes if is_linux_app(proc)}
    targets: list[tuple[int, int]] = []
    quits: list[TrayEntry] = []
    if app and tray is not None:
        # Signed in, the window's close only hides the app to its tray; the
        # tray's own Quit is how a person ends it (observed 2026-10-19).
        try:
            quits = list(tray.quit_entries(app))
        except OSError as exc:
            LOG.info("Codex's tray menu could not be read (%s); asking its window instead", exc)
    if app and not quits:
        try:
            targets = [(window, pid) for window, pid in x11.windows() if pid in app]
        except OSError as exc:
            return CloseOutcome(False, False, f"the windows could not be listed: {exc}; close Codex yourself")
        if not targets:
            return CloseOutcome(
                False, False,
                "no Codex window to ask (only a window on X11 can be asked from outside); close Codex yourself",
            )
    if not targets and not quits and not daemons:
        return CloseOutcome(False, False, "no Codex app window to ask; close Codex yourself")
    said = []
    if quits:
        try:
            tray.click(quits)  # type: ignore[union-attr]
        except OSError as exc:
            return CloseOutcome(False, False, f"Codex's tray menu could not be asked to quit: {exc}")
        said.append("asked Codex to quit from its tray menu")
    elif targets:
        try:
            x11.close([window for window, _ in targets])
        except OSError as exc:
            return CloseOutcome(False, False, f"the window could not be asked to close: {exc}")
        said.append(f"asked {len(targets)} Codex window(s) to close")
    # Codex CLI 0.163 leaves its shared server running after the CLI quits
    # (CS-429). With no CLI open it has no client, and Codex's own command asks
    # it to stop -- observed 2026-10-19: answer 0, gone in 0.4 s, its pid file
    # removed and its queue database's log closed.
    for program in sorted({proc.path for proc in daemons}):
        try:
            result = run([program, *CLI_DAEMON_STOP_COMMAND.split()[1:]])
        except (OSError, subprocess.SubprocessError) as exc:
            return CloseOutcome(True, False, f"Codex CLI's background server could not be asked to stop: {exc}")
        if result.returncode != 0:
            return CloseOutcome(
                True, False,
                f"Codex CLI's background server did not accept `{CLI_DAEMON_STOP_COMMAND}` "
                f"(exit {result.returncode})",
            )
        said.append("asked Codex CLI's background server to stop")
    pids = tuple(sorted({pid for _, pid in targets} | {entry.pid for entry in quits} | {proc.pid for proc in daemons}))
    return CloseOutcome(True, True, "; ".join(said), pids)


class _BusctlTray(TrayMenus):  # pragma: no cover - needs a session bus
    """The tray through `busctl`, which every systemd user session has.

    The StatusNotifierWatcher lists each icon as ``bus-name@path``; the bus
    says which process owns the name, the icon's ``Menu`` property names its
    `com.canonical.dbusmenu` object, and an ``Event(id, "clicked", ...)`` is
    the click a person makes. Only labels are requested, only *Quit* is kept.
    """

    def _busctl(self, *args: str) -> object:
        import json
        import shutil

        busctl = shutil.which("busctl")
        if busctl is None:
            raise OSError("busctl is not available")
        try:
            result = subprocess.run(
                [busctl, "--user", "--json=short", *args], capture_output=True, text=True, timeout=10, check=False,
            )
        except subprocess.SubprocessError as exc:
            raise OSError(str(exc)) from exc
        if result.returncode != 0:
            raise OSError((result.stderr or "").strip() or f"busctl exit {result.returncode}")
        try:
            return json.loads(result.stdout).get("data") if result.stdout.strip() else None
        except ValueError as exc:
            raise OSError(f"busctl printed no JSON: {exc}") from exc

    def quit_entries(self, app_pids: set[int]) -> list[TrayEntry]:
        try:
            items = self._busctl(
                "get-property", "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems",
            )
        except OSError:
            return []  # no tray host on this desktop
        found = []
        for item in items or []:
            name, _, path = str(item).partition("@")
            path = path or "/StatusNotifierItem"
            try:
                pid = int(self._busctl(
                    "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                    "GetConnectionUnixProcessID", "s", name,
                )[0])
                if pid not in app_pids:
                    continue
                menu = str(self._busctl("get-property", name, path, "org.kde.StatusNotifierItem", "Menu"))
                layout = self._busctl("call", name, menu, "com.canonical.dbusmenu", "GetLayout", "--",
                                      "iias", "0", "-1", "1", "label")
            except (OSError, TypeError, ValueError, IndexError):
                continue
            for item_id in _quit_ids(layout[1] if isinstance(layout, list) and len(layout) > 1 else None):
                found.append(TrayEntry(name, menu, item_id, pid))
        return found

    def click(self, entries: Sequence[TrayEntry]) -> None:
        for entry in entries:
            self._busctl("call", entry.bus_name, entry.menu_path, "com.canonical.dbusmenu", "Event", "--",
                         "isvu", str(entry.item_id), "clicked", "i", "0", "0")


def _quit_ids(node: object) -> list[int]:
    """Ids of the *Quit* entries in a dbusmenu layout as `busctl --json` prints it."""
    if isinstance(node, dict) and "data" in node:
        node = node["data"]
    if not isinstance(node, list) or len(node) != 3:
        return []
    ident, props, children = node
    found = []
    label = props.get("label") if isinstance(props, dict) else None
    if isinstance(label, dict):
        label = label.get("data")
    if isinstance(label, str) and label.replace("_", "") in TRAY_QUIT_LABELS and isinstance(ident, int):
        found.append(ident)
    for child in children if isinstance(children, list) else []:
        found.extend(_quit_ids(child))
    return found


def _list_linux_processes() -> list[LinuxProcess]:  # pragma: no cover - needs /proc
    processes = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        base = Path(entry.path)
        try:
            stat = (base / "stat").read_text(encoding="utf-8", errors="replace")
            # `pid (comm) state ppid ...`; comm may hold spaces and parentheses.
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue  # gone, or not ours to read
        processes.append(LinuxProcess(int(entry.name), parent, name, proc_program(int(entry.name))))
    return processes


class _LibX11(X11Windows):  # pragma: no cover - needs an X server
    """The two EWMH calls asking needs, through the system's libX11 and ctypes.

    `_NET_CLIENT_LIST` on the root window lists the top-level windows,
    `_NET_WM_PID` says whose each is, and a `_NET_CLOSE_WINDOW` client message
    to the root window is what a close button sends. Errors from a window that
    went away meanwhile are ignored rather than ending this process, which is
    what Xlib's default error handler does.
    """

    _CLIENT_MESSAGE = 33
    _SUBSTRUCTURE_NOTIFY = 1 << 19
    _SUBSTRUCTURE_REDIRECT = 1 << 20

    def __init__(self) -> None:
        self._handler = None

    def _open(self):
        import ctypes
        import ctypes.util

        if not os.environ.get("DISPLAY"):
            raise OSError("no X display (DISPLAY is not set)")
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        except OSError as exc:
            raise OSError(f"libX11 is not available: {exc}") from exc
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        lib.XFree.argtypes = [ctypes.c_void_p]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._handler = handler_type(lambda _display, _event: 0)
        lib.XSetErrorHandler.argtypes = [handler_type]
        lib.XSetErrorHandler(self._handler)
        display = lib.XOpenDisplay(None)
        if not display:
            raise OSError(f"the X display {os.environ.get('DISPLAY')} could not be opened")
        return ctypes, lib, display

    @staticmethod
    def _longs(ctypes, lib, display, window: int, atom: int) -> list[int]:
        actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = lib.XGetWindowProperty(
            display, window, atom, 0, 4096, 0, 0,  # AnyPropertyType
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(count),
            ctypes.byref(after), ctypes.byref(data),
        )
        if status != 0 or not data:
            return []
        try:
            if actual_format.value != 32:
                return []
            # Format-32 data comes back as C longs, whatever their width.
            values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
            return [values[index] for index in range(count.value)]
        finally:
            lib.XFree(data)

    def windows(self) -> list[tuple[int, int]]:
        ctypes, lib, display = self._open()
        try:
            root = lib.XDefaultRootWindow(display)
            client_list = lib.XInternAtom(display, b"_NET_CLIENT_LIST", 0)
            wm_pid = lib.XInternAtom(display, b"_NET_WM_PID", 0)
            found = []
            for window in self._longs(ctypes, lib, display, root, client_list):
                pid = self._longs(ctypes, lib, display, window, wm_pid)
                if pid:
                    found.append((window, pid[0]))
            return found
        finally:
            lib.XCloseDisplay(display)

    def close(self, windows: Sequence[int]) -> None:
        ctypes, lib, display = self._open()

        class ClientMessage(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
            ]

        class Event(ctypes.Union):
            _fields_ = [("xclient", ClientMessage), ("pad", ctypes.c_long * 24)]

        try:
            root = lib.XDefaultRootWindow(display)
            close_window = lib.XInternAtom(display, b"_NET_CLOSE_WINDOW", 0)
            for window in windows:
                event = Event()
                event.xclient.type = self._CLIENT_MESSAGE
                event.xclient.window = window
                event.xclient.message_type = close_window
                event.xclient.format = 32
                # data: timestamp 0 and source 0, exactly as `wmctrl -c` sends it,
                # which is what was observed. Mutter logs "Tried to ping window
                # ... with a bad serial" for the zero timestamp and skips only
                # its are-you-responding ping; the window still closes.
                lib.XSendEvent(
                    display, root, 0, self._SUBSTRUCTURE_REDIRECT | self._SUBSTRUCTURE_NOTIFY,
                    ctypes.byref(event),
                )
            lib.XSync(display, 0)
        finally:
            lib.XCloseDisplay(display)


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
