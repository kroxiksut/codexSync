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
        return ProcessDetectorCapability(
            platform=sys.platform,
            contract_version=DETECTOR_CONTRACT_VERSION,
            supported=False,
            detail="No tested process-detector adapter is available for mutation commands on this platform",
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
        return any(os.path.basename(proc.name).lower() == name for proc in self._list_posix_all())

    def find_processes(self, process_names: list[str]) -> list[ProcessInfo]:
        """Return exact-name matches from a complete native process listing."""
        if not sys.platform.startswith("win"):
            raise RuntimeError("complete background process detection is unavailable on this platform")
        targets = {_normalize_windows_name(name) for name in process_names if name.strip()}
        if not targets:
            return []
        return [proc for proc in self._list_windows_all_complete() if _normalize_windows_name(proc.name) in targets]

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
        return [
            proc
            for proc in self._list_posix_all()
            if os.path.basename(proc.name).lower() in self._names
        ]

    def _list_posix_all(self) -> list[ProcessInfo]:
        ps = shutil.which("ps")
        if not ps:
            raise RuntimeError("ps is not available for process detection")
        result = subprocess.run(
            [ps, "-A", "-o", "pid=", "-o", "comm="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ps failed with exit code {result.returncode}")

        processes: list[ProcessInfo] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            processes.append(ProcessInfo(pid=pid, name=parts[1].strip()))
        return processes

def _normalize_windows_name(name: str) -> str:
    lowered = name.lower().strip()
    if lowered.endswith(".exe"):
        return lowered
    return f"{lowered}.exe"


def _matches_marker(proc: ProcessInfo, marker: str) -> bool:
    normalized_name = _normalize_windows_name(proc.name)
    if normalized_name == _normalize_windows_name(marker):
        return True
