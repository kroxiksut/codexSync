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
the result in `PROVEN_DETECTORS`. Until then macOS and Linux answer `UNKNOWN`,
and `safety.fail_on_unknown` turns that into a refusal.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
import os
import shutil
import subprocess
import sys


DETECTOR_CONTRACT_VERSION = 1

#: Platforms whose adapter has been run against a live Codex, mapping
#: ``sys.platform`` to the observation that proved it (OS build and Codex
#: version). **Filled only from `docs/dev/experiments/process-detector-macos.md`,
#: never from reading the code.** Windows is not listed here: its adapter is
#: what the project has always shipped and what CI exercises.
PROVEN_DETECTORS: dict[str, str] = {}

#: Longest name `ps -o comm=` prints on Linux before it truncates.
LINUX_COMM_LIMIT = 15


@dataclass(slots=True, frozen=True)
class ProcessInfo:
    pid: int
    name: str
    command_line: str = ""
    parent_pid: int | None = None


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
                detail="Windows tasklist and CIM process-tree adapter",
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
        the command path instead: that is how `ChatGPT.app/Contents/MacOS/` is
        told apart from the ChatGPT app of the same name.
        """
        if sys.platform.startswith("win"):
            targets = {_normalize_windows_name(name) for name in process_names if name.strip()}
            if not targets:
                return []
            return [
                proc for proc in self._list_windows_all_complete()
                if _normalize_windows_name(proc.name) in targets
            ]
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

        parents = {_normalize_windows_name(name) for name in parent_process_names if name.strip()}
        processes = self._list_windows_all_complete()
        roots = [proc for proc in processes if _normalize_windows_name(proc.name) in parents]
        if not roots:
            return [], []

        by_pid = {proc.pid: proc for proc in processes}
        children: dict[int, list[int]] = {}
        for proc in processes:
            if proc.parent_pid is None:
                continue
            children.setdefault(proc.parent_pid, []).append(proc.pid)

        root_pids = {proc.pid for proc in roots}
        seen: set[int] = set()
        queue = list(root_pids)
        descendants: list[ProcessInfo] = []
        while queue:
            pid = queue.pop(0)
            for child_pid in children.get(pid, []):
                if child_pid in seen:
                    continue
                seen.add(child_pid)
                child = by_pid.get(child_pid)
                if child:
                    descendants.append(child)
                queue.append(child_pid)
        return roots, descendants

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

    def _list_windows_all_complete(self) -> list[ProcessInfo]:
        merged: dict[int, ProcessInfo] = {}
        for proc in self._list_windows_tasklist():
            merged[proc.pid] = proc
        cim_processes = self._list_windows_cim()
        for proc in cim_processes:
            existing = merged.get(proc.pid)
            if existing is None:
                merged[proc.pid] = proc
                continue
            merged[proc.pid] = ProcessInfo(
                pid=proc.pid,
                name=proc.name or existing.name,
                parent_pid=proc.parent_pid if proc.parent_pid is not None else existing.parent_pid,
            )
        return list(merged.values())

    def _list_windows_tasklist(self) -> list[ProcessInfo]:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tasklist failed with exit code {result.returncode}")

        rows = csv.reader(io.StringIO(result.stdout))
        processes: list[ProcessInfo] = []
        for row in rows:
            if len(row) < 2:
                continue
            name = row[0].strip()
            try:
                pid = int(row[1].strip())
            except ValueError:
                continue
            processes.append(ProcessInfo(pid=pid, name=name))
        return processes

    def _list_windows_cim(self) -> list[ProcessInfo]:
        script = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"CIM process enumeration failed with exit code {result.returncode}")
        raw = result.stdout.strip()
        if not raw:
            raise RuntimeError("CIM process enumeration returned an empty snapshot")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("CIM process enumeration returned invalid JSON") from exc
        rows = payload if isinstance(payload, list) else [payload]
        processes: list[ProcessInfo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("ProcessId"))
            except (TypeError, ValueError):
                continue
            parent_raw = row.get("ParentProcessId")
            parent_pid: int | None = None
            try:
                if parent_raw is not None:
                    parent_pid = int(parent_raw)
            except (TypeError, ValueError):
                parent_pid = None
            name = str(row.get("Name") or "").strip()
            if not name:
                continue
            processes.append(ProcessInfo(pid=pid, name=name, parent_pid=parent_pid))
        return processes

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
            ProcessInfo(pid=pid, name=name, command_line=commands.get(pid, ""))
            for pid, name in sorted(names.items())
        ]

def _run_ps(ps: str, column: str) -> str:
    result = subprocess.run(
        [ps, "-A", "-o", "pid=", "-o", column],
        capture_output=True,
        text=True,
        check=False,
    )
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


def _posix_path_marker(name: str) -> bool:
    """A configured entry that names a location rather than a process."""
    return "/" in name


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
    """A path marker against a process's own path, on a segment boundary."""
    marker = marker.strip().lower().replace("\\", "/")
    haystacks = [proc.name.lower().replace("\\", "/"), proc.command_line.lower().replace("\\", "/")]
    return any(marker in value for value in haystacks if value)


def _match_posix(processes: list[ProcessInfo], wanted: list[str]) -> list[ProcessInfo]:
    names = {name.strip().lower() for name in wanted if name.strip() and not _posix_path_marker(name)}
    markers = [name.strip() for name in wanted if name.strip() and _posix_path_marker(name)]
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
    normalized_name = _normalize_windows_name(proc.name)
    if normalized_name == _normalize_windows_name(marker):
        return True
