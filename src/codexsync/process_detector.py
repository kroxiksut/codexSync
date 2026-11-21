"""Is Codex running? Answered per platform, and never guessed.

Windows is the tested path. The POSIX one exists because the same question has
to be answerable on a Mac, where the desktop build was renamed `ChatGPT.app` in
July 2026 while keeping the bundle id `com.openai.codex` -- a machine whose
`process_names` still says `codex` sees only the bundled `codex app-server`
child, and would read a running Codex as stopped.

Two things about POSIX matching are not details:

* **A name is a whole basename, never a substring.** `ChatGPT` alone would
  match the ordinary ChatGPT desktop app, which has nothing to do with Codex,
  so the app itself is recognised by a *path* marker
  (`ChatGPT.app/Contents/MacOS/`) and never by its bare name.
* **Linux truncates.** `ps -o comm=` gives at most 15 characters there, so
  `codex-linux-sandbox` arrives as `codex-linux-san` and a configured full name
  must match its own truncation too.

`capability()` stays the gate. Reporting a platform as supported means a
mutation may proceed on the strength of this listing, and a parser nobody has
run against a live Codex could report "stopped" while it is open -- the one
mistake this project cannot make. So a platform counts as supported only after
someone has run `docs/dev/experiments/process-detector-macos.md` on it and recorded
the result in `PROVEN_DETECTORS`. Until then such a platform answers `UNKNOWN`,
and `safety.fail_on_unknown` turns that into a refusal. Linux was observed on
2026-10-09; macOS has not been, and stays closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence

from .process_knowledge import WINDOWS_APP_NAME, WINDOWS_CODEX_APP_MARKER
from .windows_processes import windows_processes


DETECTOR_CONTRACT_VERSION = 1


#: Upper bound for one process listing; past it the sample fails closed.
CONSOLE_TOOL_TIMEOUT_SECONDS = 30


def _run_console_tool(argv: list[str], *, encoding: str) -> subprocess.CompletedProcess[str]:
    """Run a console helper without ever showing a console window.

    The window is not a cosmetic detail. `codexsync-gui.exe` is built windowed,
    so the process owns no console. Before the native Windows adapter, a
    process check could flash as many as eighteen console windows (CS-259).

    Three things make that stop, and each is needed on its own: `CREATE_NO_WINDOW`
    for the console the child would allocate, `STARTF_USESHOWWINDOW` for a child
    that asks for a window anyway, and an explicit `stdin` -- a windowed process
    has no standard handles to inherit, and a console tool handed an invalid one
    can fail in ways that read here as "process state unknown".

    `system_scheduler._default_run` has done this since it was written; the
    detector simply never did.

    Whether the flags exist is asked of `subprocess`, not of `sys.platform`:
    `STARTUPINFO` and `CREATE_NO_WINDOW` are defined only in the Windows build
    of the module, and this module's platform string is patched by tests that
    exercise the Windows adapter from macOS and Linux. Reading the name and
    then assuming the attributes crashed exactly there -- and would crash the
    same way in any environment that reports Windows without offering them.
    """
    kwargs: dict[str, object] = {}
    startupinfo_class = getattr(subprocess, "STARTUPINFO", None)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if startupinfo_class is not None and no_window is not None:
        startupinfo = startupinfo_class()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = no_window
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="ignore",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=CONSOLE_TOOL_TIMEOUT_SECONDS,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        # A listing that did not finish is a failed sample, i.e. UNKNOWN.
        raise RuntimeError(f"{argv[0]} did not finish within {CONSOLE_TOOL_TIMEOUT_SECONDS}s") from exc

#: Platforms whose adapter has been run against a live Codex, mapping
#: ``sys.platform`` to the observation that proved it (OS build and Codex
#: version). **Filled only from `docs/dev/experiments/process-detector-macos.md`,
#: never from reading the code.** Windows is not listed here: its adapter is
#: what the project has always shipped and what CI exercises.
PROVEN_DETECTORS: dict[str, str] = {
    "linux": (
        "Ubuntu 26.04 LTS (kernel 7.0.0-38), Codex desktop deb `chatgpt` 26.1002.52244: "
        "idle, a chat command in the sandbox, and quit with that command still running "
        "-- it outlived the app as `codex` and read as running until it ended; "
        "observed 2026-10-09"
    ),
}

#: Longest name `ps -o comm=` prints on Linux before it truncates.
LINUX_COMM_LIMIT = 15


@dataclass(slots=True, frozen=True)
class ProcessInfo:
    pid: int
    name: str
    command_line: str = ""
    parent_pid: int | None = None
    #: The program's own path, where the system says it exactly (`/proc` on
    #: Linux, `QueryFullProcessImageNameW` on Windows); empty elsewhere or where the
    #: system would not say, and then on POSIX the command line's first word
    #: stands in.
    program: str = ""
    program_access_denied: bool = False


@dataclass(slots=True, frozen=True)
class ProcessDetectorCapability:
    platform: str
    contract_version: int
    supported: bool
    detail: str


class CodexProcessDetector:
    def __init__(self, process_names: list[str]) -> None:
        self._names = {n.lower().strip() for n in process_names if n.strip()}
        self._windows_names = {_normalize_windows_name(n) for n in self._names}

    def capability(self) -> ProcessDetectorCapability:
        if sys.platform.startswith("win"):
            return ProcessDetectorCapability(
                platform="windows",
                contract_version=DETECTOR_CONTRACT_VERSION,
                supported=True,
                detail="Native Windows process snapshot and image paths",
            )
        proof = PROVEN_DETECTORS.get(sys.platform)
        if proof:
            return ProcessDetectorCapability(
                platform=sys.platform,
                contract_version=DETECTOR_CONTRACT_VERSION,
                supported=True,
                detail=f"ps adapter, confirmed against a live Codex: {proof}",
            )
        return ProcessDetectorCapability(
            platform=sys.platform,
            contract_version=DETECTOR_CONTRACT_VERSION,
            supported=False,
            detail=(
                "The ps adapter for this platform has not been run against a live Codex "
                "(docs/dev/experiments/process-detector-macos.md); mutations stay closed"
            ),
        )

    def is_running(self) -> bool:
        return bool(self.list_running())

    def list_running(self) -> list[ProcessInfo]:
        if not self._names:
            return []
        if sys.platform.startswith("win"):
            return self._list_windows()
        return self._list_posix()

    def has_process(self, process_name: str) -> bool:
        name = process_name.lower().strip()
        if not name:
            return False
        if sys.platform.startswith("win"):
            target = _normalize_windows_name(name)
            return any(_normalize_windows_name(proc.name) == target for proc in self._list_windows_all())
        return any(_posix_name_matches(proc, name) for proc in self._list_posix_all())

    def find_processes(self, process_names: list[str]) -> list[ProcessInfo]:
        """Exact matches for these names in a complete native process listing.

        A name containing a separator is a path marker and is matched against
        the program's path instead: that is how `ChatGPT.app/Contents/MacOS/` is
        told apart from the ChatGPT app of the same name, and how the Windows
        desktop app (`ChatGPT.exe` from the `OpenAI.Codex_` package) is told
        apart from the ordinary ChatGPT app and from the package's own
        always-running service (CS-447).
        """
        if sys.platform.startswith("win"):
            return _match_windows(self._list_windows_all_complete(), process_names)
        return _match_posix(self._list_posix_all(), process_names)

    def has_subprocess_marker(self, parent_process_names: list[str], marker_name: str) -> bool:
        if not sys.platform.startswith("win"):
            return False
        marker = marker_name.lower().strip()
        if not marker:
            return False
        parents = {_normalize_windows_name(name) for name in parent_process_names if name.strip()}
        if not parents:
            return False
        processes = self._list_windows_all()
        by_pid = {proc.pid: proc for proc in processes}
        children: dict[int, list[int]] = {}
        for proc in processes:
            if proc.parent_pid is None:
                continue
            children.setdefault(proc.parent_pid, []).append(proc.pid)
        root_pids = [proc.pid for proc in processes if _normalize_windows_name(proc.name) in parents]
        seen: set[int] = set()
        queue = list(root_pids)
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            proc = by_pid.get(pid)
            if proc and _matches_marker(proc, marker):
                return True
            queue.extend(children.get(pid, []))
        return False

    def get_subprocess_tree(self, parent_process_names: list[str]) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
        if not sys.platform.startswith("win"):
            roots = self.list_running()
            return roots, []

        return _windows_subprocess_tree(self._list_windows_all_complete(), parent_process_names)

    def sample_matches(
        self, process_names: list[str], markers: list[str],
    ) -> tuple[list[ProcessInfo], list[ProcessInfo], list[ProcessInfo]]:
        """Read main processes, children and background processes for one gate sample."""
        if sys.platform.startswith("win"):
            processes = self._list_windows_all_complete(inspect_names=_windows_path_candidates(process_names, markers))
            main, children = _windows_subprocess_tree(processes, process_names)
            background = _match_windows(processes, markers)
            if not main and not background:
                _require_windows_marker_visibility(processes, markers)
            return main, children, background
        main, children = self.get_subprocess_tree(process_names)
        return main, children, self.find_processes(markers) if markers else []

    def has_marker(self, proc: ProcessInfo, marker_name: str) -> bool:
        marker = marker_name.lower().strip()
        if not marker:
            return False
        return _matches_marker(proc, marker)

    def _list_windows(self) -> list[ProcessInfo]:
        return [proc for proc in self._list_windows_all() if _normalize_windows_name(proc.name) in self._windows_names]

    def _list_windows_all(self) -> list[ProcessInfo]:
        """Compatibility helper for read-only callers that do not need a tree."""
        return self._list_windows_all_complete()

    def _list_windows_all_complete(self, *, inspect_names: set[str] | None = None) -> list[ProcessInfo]:
        return [
            ProcessInfo(
                proc.pid, proc.name, parent_pid=proc.parent_pid,
                program=proc.path, program_access_denied=proc.path_access_denied,
            )
            for proc in windows_processes(inspect_names=inspect_names)
        ]

    def _list_posix(self) -> list[ProcessInfo]:
        return _match_posix(self._list_posix_all(), sorted(self._names))

    def _list_posix_all(self) -> list[ProcessInfo]:
        """Every process, with both its short name and its full command.

        Two listings keyed by pid rather than one with both columns: `comm` on
        macOS is a full path that contains spaces ("ChatGPT Helper (Renderer)"),
        so a single line carrying both fields could not be split back apart.
        """
        ps = shutil.which("ps")
        if not ps:
            raise RuntimeError("ps is not available for process detection")
        names = _parse_ps(_run_ps(ps, "comm="))
        commands = _parse_ps(_run_ps(ps, "command="))
        return [
            ProcessInfo(pid=pid, name=name, command_line=commands.get(pid, ""), program=proc_program(pid))
            for pid, name in sorted(names.items())
        ]


def proc_program(pid: int) -> str:
    """The program a Linux process runs, from `/proc`: its executable, else its first argument.

    `ps -o command=` joins the arguments with spaces, so it cannot say where
    the program's path ends -- and a path marker found among the *arguments*
    (`grep -r x /usr/lib/chatgpt/`, a shell whose command mentions it) read a
    stopped Codex as running for as long as that process lived (2026-10-19).
    `/proc/<pid>/cmdline` separates the arguments with NUL. Empty where there
    is no `/proc` (macOS, whose `comm` is the full path anyway) or the process
    is gone or not this user's.
    """
    if not sys.platform.startswith("linux"):
        return ""
    base = f"/proc/{pid}"
    try:
        return os.readlink(f"{base}/exe")
    except OSError:
        pass
    try:
        with open(f"{base}/cmdline", "rb") as handle:
            return handle.read().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        return ""

def _run_ps(ps: str, column: str) -> str:
    result = _run_console_tool([ps, "-A", "-o", "pid=", "-o", column], encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"ps failed with exit code {result.returncode}")
    return result.stdout


def _parse_ps(output: str) -> dict[int, str]:
    """``pid -> the rest of the line``. The rest may hold spaces and brackets."""
    rows: dict[int, str] = {}
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows[pid] = parts[1].strip()
    return rows


def _path_marker(name: str) -> bool:
    """A configured entry that names a location rather than a process."""
    return "/" in name or "\\" in name


def _path_matches(path: str, marker: str) -> bool:
    """One path marker against one program path.

    Both sides are compared lower-case, with `/` for every separator, and the
    marker may sit anywhere in the path. A `*` in the marker stands for any run
    of characters: that is how the Windows marker skips the version in the
    package folder (`/OpenAI.Codex_*/app/ChatGPT.exe`) while naming one program
    of that package and not the rest -- the same folder holds the sandbox
    service, which runs from boot whether Codex is open or not (CS-264).
    """
    path = path.strip().lower().replace("\\", "/")
    marker = marker.strip().lower().replace("\\", "/")
    if not path or not marker:
        return False
    if "*" not in marker:
        return marker in path
    return fnmatch.fnmatchcase(path, f"*{marker}*")


def _posix_name_matches(proc: ProcessInfo, target: str) -> bool:
    """One configured name against one process, by whole basename.

    On Linux the listing is cut to 15 characters, so a configured
    `codex-linux-sandbox` has to match the `codex-linux-san` that arrives. The
    comparison is still a whole name: a truncation is compared with the
    target's own truncation, never with a prefix of arbitrary length.
    """
    actual = os.path.basename(proc.name).strip().lower()
    if not actual:
        return False
    if actual == target:
        return True
    if sys.platform.startswith("linux") and len(target) > LINUX_COMM_LIMIT:
        return actual == target[:LINUX_COMM_LIMIT]
    return False


def _posix_marker_matches(proc: ProcessInfo, marker: str) -> bool:
    """A path marker against a process's own path -- never against its arguments.

    The path is the name where `comm` is a full path (macOS), and the program
    `/proc` names (Linux), or else the command line's first word.
    """
    program = proc.program or (proc.command_line.split(maxsplit=1) or [""])[0]
    return any(_path_matches(value, marker) for value in (proc.name, program) if value)


def _windows_marker_matches(proc: ProcessInfo, marker: str) -> bool:
    """A path marker against the program's path as Windows reports it, nothing else.

    No command line stands in for a missing path: a process whose path the
    system would not give is matched by name only, never by its arguments.
    """
    if _windows_codex_app_marker(marker) and proc.name.lower() != WINDOWS_APP_NAME:
        return False
    return _path_matches(proc.program, marker)


def _match_windows(processes: list[ProcessInfo], wanted: list[str]) -> list[ProcessInfo]:
    names = {_normalize_windows_name(name) for name in wanted if name.strip() and not _path_marker(name)}
    markers = [name.strip() for name in wanted if name.strip() and _path_marker(name)]
    if not names and not markers:
        return []
    return [
        proc for proc in processes
        if _normalize_windows_name(proc.name) in names
        or any(_windows_marker_matches(proc, marker) for marker in markers)
    ]


def _windows_codex_app_marker(marker: str) -> bool:
    return marker.strip().lower().replace("\\", "/") == WINDOWS_CODEX_APP_MARKER.lower()


def _windows_path_candidates(names: Sequence[str], markers: Sequence[str]) -> set[str] | None:
    """Keep unrelated process churn out of the common case of the shipped marker."""
    candidates: set[str] = set()
    for marker in markers:
        if _windows_codex_app_marker(marker):
            candidates.add(WINDOWS_APP_NAME)
        elif _path_marker(marker):
            return None  # An arbitrary pattern can match any process path.
    for name in (*names, *markers):
        if name.strip() and not _path_marker(name):
            normalized = _normalize_windows_name(name)
            candidates.update((normalized, normalized.removesuffix(".exe")))
    return candidates


def _require_windows_marker_visibility(processes: list[ProcessInfo], markers: Sequence[str]) -> None:
    """Refuse to report STOPPED if a marker could match an unreadable path."""
    # The shipped marker requires a known basename. A custom substring or
    # wildcard can also match a directory segment, even if it contains `.exe`.
    candidates = _windows_path_candidates((), [marker for marker in markers if _path_marker(marker)])
    for proc in processes:
        if proc.program_access_denied and (candidates is None or proc.name.lower() in candidates):
            raise RuntimeError(f"Windows image path access denied for {proc.name} (pid {proc.pid})")


def _windows_subprocess_tree(
    processes: list[ProcessInfo], parent_process_names: list[str],
) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
    parents = {_normalize_windows_name(name) for name in parent_process_names if name.strip()}
    roots = [proc for proc in processes if _normalize_windows_name(proc.name) in parents]
    if not roots:
        return [], []

    children: dict[int, list[ProcessInfo]] = {}
    for proc in processes:
        if proc.parent_pid is not None:
            children.setdefault(proc.parent_pid, []).append(proc)

    root_pids = {proc.pid for proc in roots}
    seen: set[int] = set()
    queue = list(root_pids)
    descendants: list[ProcessInfo] = []
    while queue:
        pid = queue.pop(0)
        for child in children.get(pid, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            descendants.append(child)
            queue.append(child.pid)
    return roots, descendants


def _match_posix(processes: list[ProcessInfo], wanted: list[str]) -> list[ProcessInfo]:
    names = {name.strip().lower() for name in wanted if name.strip() and not _path_marker(name)}
    markers = [name.strip() for name in wanted if name.strip() and _path_marker(name)]
    found: list[ProcessInfo] = []
    for proc in processes:
        if any(_posix_name_matches(proc, name) for name in names):
            found.append(proc)
        elif any(_posix_marker_matches(proc, marker) for marker in markers):
            found.append(proc)
    return found


def _normalize_windows_name(name: str) -> str:
    lowered = name.lower().strip()
    if lowered.endswith(".exe"):
        return lowered
    return f"{lowered}.exe"


def _matches_marker(proc: ProcessInfo, marker: str) -> bool:
    if _path_marker(marker):
        return _windows_marker_matches(proc, marker)
    normalized_name = _normalize_windows_name(proc.name)
    if normalized_name == _normalize_windows_name(marker):
        return True
