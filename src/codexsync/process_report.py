"""What the process check sees on this machine, safe to paste into an issue (CS-418).

The safety gate answers one question -- may codexSync write now -- and on a
platform whose detector is not proven (macOS today) it answers ``UNKNOWN``
without listing anything. That answer is right for the gate and useless to the
person who could prove the detector: they need to see what it would have seen.
This report lists it anyway, without deciding anything: which configured name
or path marker matched which process, by pid, parent pid and short name.

It never prints a full path or a command line -- on macOS and Linux those carry
the user name and arguments such as `--remote ...` -- so the output can go into
an issue as it is. A path marker that matched is reported as the marker, not
as the path it matched. Nothing is written: no config, no log file, no cache.
Proving a platform stays a decision recorded in `PROVEN_DETECTORS`, made from
the reports people send; this module never opens the gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import shutil
import sys
from typing import Iterable

from .process_detector import (
    DETECTOR_CONTRACT_VERSION,
    CodexProcessDetector,
    ProcessInfo,
    _normalize_windows_name,
    _parse_ps,
    _posix_marker_matches,
    _posix_name_matches,
    _posix_path_marker,
    _run_ps,
)

__all__ = ["ProcessMatch", "ProcessReport", "build_process_report", "os_key"]

#: What a match is: a configured Codex name, a child of one (Windows only:
#: the tree is read there), or a per-OS background marker.
MAIN = "main"
CHILD = "child"
BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class ProcessMatch:
    role: str
    pid: int
    parent_pid: int | None
    #: The process's short name -- the last path segment, never the path.
    name: str
    #: The configured entry that matched; for a child, its root's entry.
    rule: str
    #: ``name`` or ``path_marker`` (``tree`` for a child).
    matched_by: str


@dataclass(frozen=True, slots=True)
class ProcessReport:
    platform: str
    os_key: str
    contract_version: int
    #: Whether the gate trusts this platform's detector at all.
    supported: bool
    detail: str
    #: What the gate reads now: RUNNING, STOPPED or UNKNOWN.
    gate_state: str
    #: What the matches mean if the detector were trusted here.
    would_read: str
    #: Where the names came from: ``config`` or ``built-in``.
    names_from: str
    process_names: tuple[str, ...]
    background_names: tuple[str, ...]
    matches: tuple[ProcessMatch, ...]
    #: Why the processes could not be listed, if they could not.
    error: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["matches"] = [asdict(item) for item in self.matches]
        return data


def os_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def build_process_report(
    process_names: Iterable[str],
    background_names: Iterable[str],
    *,
    names_from: str,
    detector: CodexProcessDetector | None = None,
) -> ProcessReport:
    """List the matches the gate would act on, and what it reads. Reads only."""
    names = tuple(name.strip() for name in process_names if name.strip())
    markers = tuple(name.strip() for name in background_names if name.strip())
    detector = detector or CodexProcessDetector(list(names))
    capability = detector.capability()
    error: str | None = None
    try:
        matches = tuple(_matches(detector, names, markers))
    except Exception as exc:  # a listing that failed is reported, not raised
        matches = ()
        error = f"{type(exc).__name__}: {exc}"
    if error is not None:
        would_read = "UNKNOWN"
    else:
        would_read = "RUNNING" if matches else "STOPPED"
    gate_state = would_read if capability.supported else "UNKNOWN"
    return ProcessReport(
        platform=capability.platform,
        os_key=os_key(),
        contract_version=DETECTOR_CONTRACT_VERSION,
        supported=capability.supported,
        detail=capability.detail,
        gate_state=gate_state,
        would_read=would_read,
        names_from=names_from,
        process_names=names,
        background_names=markers,
        matches=matches,
        error=error,
    )


def _short(name: str) -> str:
    return os.path.basename(name.replace("\\", "/").rstrip("/")) or name


def _matches(detector: CodexProcessDetector, names: tuple[str, ...], markers: tuple[str, ...]):
    if sys.platform.startswith("win"):
        yield from _windows_matches(detector, names, markers)
        return
    parents = _posix_parents()
    for proc in detector._list_posix_all():
        parent = parents.get(proc.pid)
        found = _posix_rule(proc, names)
        if found is not None:
            yield ProcessMatch(MAIN, proc.pid, parent, _short(proc.name), *found)
            continue
        found = _posix_rule(proc, markers)
        if found is not None:
            yield ProcessMatch(BACKGROUND, proc.pid, parent, _short(proc.name), *found)


def _posix_parents() -> dict[int, int]:
    """``pid -> parent pid`` from one more `ps`, for this report only.

    The gate's own listing does without it; asking here keeps every sample the
    gate takes as cheap as it was.
    """
    ps = shutil.which("ps")
    if not ps:
        return {}
    parents: dict[int, int] = {}
    for pid, value in _parse_ps(_run_ps(ps, "ppid=")).items():
        try:
            parents[pid] = int(value)
        except ValueError:
            continue
    return parents


def _posix_rule(proc: ProcessInfo, entries: tuple[str, ...]) -> tuple[str, str] | None:
    for entry in entries:
        if _posix_path_marker(entry):
            if _posix_marker_matches(proc, entry):
                return entry, "path_marker"
        elif _posix_name_matches(proc, entry.lower()):
            return entry, "name"
    return None


def _windows_matches(detector: CodexProcessDetector, names: tuple[str, ...], markers: tuple[str, ...]):
    processes = detector._list_windows_all_complete()
    # The first entry that names a process is the rule reported for it.
    by_name: dict[str, str] = {}
    for entry in names:
        by_name.setdefault(_normalize_windows_name(entry), entry)
    by_marker: dict[str, str] = {}
    for entry in markers:
        by_marker.setdefault(_normalize_windows_name(entry), entry)
    roots: dict[int, str] = {}
    for proc in processes:
        entry = by_name.get(_normalize_windows_name(proc.name))
        if entry is not None:
            roots[proc.pid] = entry
            yield ProcessMatch(MAIN, proc.pid, proc.parent_pid, _short(proc.name), entry, "name")
    children: dict[int, list[ProcessInfo]] = {}
    for proc in processes:
        if proc.parent_pid is not None:
            children.setdefault(proc.parent_pid, []).append(proc)
    seen = set(roots)
    queue = [(pid, entry) for pid, entry in roots.items()]
    while queue:
        pid, entry = queue.pop(0)
        for child in children.get(pid, []):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            queue.append((child.pid, entry))
            yield ProcessMatch(CHILD, child.pid, child.parent_pid, _short(child.name), entry, "tree")
    for proc in processes:
        if proc.pid in seen:
            continue
        entry = by_marker.get(_normalize_windows_name(proc.name))
        if entry is not None:
            yield ProcessMatch(BACKGROUND, proc.pid, proc.parent_pid, _short(proc.name), entry, "name")
