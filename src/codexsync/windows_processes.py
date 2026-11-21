"""A fresh Windows process inventory for the detector and the quit request."""
from __future__ import annotations

from collections.abc import Collection
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass


TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
MAX_SNAPSHOT_ROUNDS = 4


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


@dataclass(frozen=True, slots=True)
class WindowsProcess:
    pid: int
    parent_pid: int
    name: str
    path: str
    #: Creation time as a FILETIME integer for the Restart Manager.
    started: int
    path_access_denied: bool = False


def windows_processes(
    *, inspect_names: Collection[str] | None = None, include_start_time: bool = False,
) -> list[WindowsProcess]:
    """Read names, parent PIDs and image paths from the same process identities.

    A system process may deny path access and a process may exit between the
    snapshot and OpenProcess. In both cases keep its snapshot name for exact
    name matching. An incomplete inventory or unexpected API failure raises.
    The quit request also reads creation times along the candidates' parent chains.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = kernel32.Process32FirstW.argtypes
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    if include_start_time:
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel32.GetProcessTimes.restype = wintypes.BOOL

    with ExitStack() as close_handles:
        entries, handles, denied = _verified_snapshot(kernel32, inspect_names, include_start_time, close_handles)
        return _with_image_paths(kernel32, entries, handles, denied, inspect_names, include_start_time)


def _verified_snapshot(
    kernel32: ctypes.WinDLL, inspect_names: Collection[str] | None, include_ancestors: bool, close_handles: ExitStack,
) -> tuple[list[tuple[int, int, str]], dict[int, int], set[int]]:
    # Windows retains a PID while a handle holds its process object alive.
    # Open each handle before the snapshot we use for that PID's name and
    # parent. A new PID that appears in that snapshot needs the same treatment.
    # https://devblogs.microsoft.com/oldnewthing/20110107-00/?p=11803
    entries = _process_entries(kernel32)
    handles: dict[int, int] = {}
    for _ in range(MAX_SNAPSHOT_ROUNDS):
        denied = set()
        opened = False
        for pid in _inspection_pids(entries, inspect_names, include_ancestors) - handles.keys():
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                handles[pid] = handle
                close_handles.callback(kernel32.CloseHandle, handle)
                opened = True
                continue
            error = ctypes.get_last_error()
            if error == ERROR_ACCESS_DENIED:
                denied.add(pid)
            elif error != ERROR_INVALID_PARAMETER:
                raise OSError(error, "OpenProcess failed")
        if not opened:
            return entries, handles, denied
        entries = _process_entries(kernel32)
    raise OSError("Windows process list kept changing while its identities were verified")


def _inspection_pids(
    entries: list[tuple[int, int, str]], names: Collection[str] | None, include_ancestors: bool,
) -> set[int]:
    pids = {pid for pid, _, name in entries if pid and (names is None or name.lower() in names)}
    if include_ancestors and names is not None:
        parents = {pid: parent for pid, parent, _ in entries if pid}
        pending = list(pids)
        while pending:
            parent = parents[pending.pop()]
            if parent in parents and parent not in pids:
                pids.add(parent)
                pending.append(parent)
    return pids


def _process_entries(kernel32: ctypes.WinDLL) -> list[tuple[int, int, str]]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    entries: list[tuple[int, int, str]] = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Process32FirstW failed")
        while True:
            name = entry.szExeFile.strip()
            if not name:
                raise OSError("Toolhelp returned a process without a name")
            entries.append((entry.th32ProcessID, entry.th32ParentProcessID, name))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != ERROR_NO_MORE_FILES:
                    raise OSError(error, "Process32NextW failed")
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return entries


def _with_image_paths(
    kernel32: ctypes.WinDLL, entries: list[tuple[int, int, str]], handles: dict[int, int],
    denied: set[int], inspect_names: Collection[str] | None, include_start_time: bool,
) -> list[WindowsProcess]:
    # 32,767 UTF-16 units plus the terminator is the Win32 path limit.
    path_buffer = ctypes.create_unicode_buffer(32768)
    processes = []
    for pid, parent, name in entries:
        path, started = "", 0
        access_denied = pid in denied
        handle = handles.get(pid)
        if handle and (inspect_names is None or name.lower() in inspect_names):
            size = wintypes.DWORD(len(path_buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, path_buffer, ctypes.byref(size)):
                path = path_buffer.value
            else:
                error = ctypes.get_last_error()
                # Windows can report a failed query as an image unloads.
                code = wintypes.DWORD(STILL_ACTIVE)
                read_exit_code = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                exited = read_exit_code and code.value != STILL_ACTIVE
                if not exited:
                    if error != ERROR_ACCESS_DENIED:
                        raise OSError(error, "QueryFullProcessImageNameW failed")
                    access_denied = True
        if handle and include_start_time:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        processes.append(WindowsProcess(pid, parent, name, path, started, access_denied))
    return processes
