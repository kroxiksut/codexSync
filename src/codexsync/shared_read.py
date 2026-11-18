"""Open a file another program may replace while it is being read (CS-500).

On Windows `open()` shares a file for reading and writing but not for
deletion, and a rename over a file counts as deleting it. Codex saves
`.codex-global-state.json` by replacing it, and Guardian reads it three times
on every change and once a minute, so each reading was a moment in which that
save could fail. Here the file is opened with ``FILE_SHARE_DELETE`` as well.

What that buys depends on how the writer renames (measured 2026-10-26,
Windows 10 19045, NTFS): a POSIX-semantics rename (``FileRenameInfoEx``, what
Rust's `std::fs::rename` does) goes through with this open and fails with
`WinError 32` against a plain `open()`; a classic `MoveFileExW` (Python's
`os.replace`) fails with `WinError 5` against either, so for such a writer only
a short reading helps. The reader then sees the file change and reports the
reading as unstable.

Elsewhere `open()` never stands in the way of a rename, and is used as it is.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80

_create_file = None


def shares_deletion() -> bool:
    """Whether this platform needs, and gets, the Windows open."""
    return os.name == "nt"


def open_shared(path: Path | str) -> BinaryIO:
    """``path`` opened for binary reading without blocking a rename over it."""
    if not shares_deletion():
        return open(path, "rb")
    import ctypes
    import msvcrt

    global _create_file
    if _create_file is None:
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel.CreateFileW
        function.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        function.restype = wintypes.HANDLE
        _create_file = function
    handle = _create_file(
        str(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        # OSError maps the Windows code to FileNotFoundError and the rest.
        raise OSError(None, ctypes.FormatError(code).strip(), str(path), code)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise
    return os.fdopen(descriptor, "rb")


def read_shared(path: Path | str) -> bytes:
    """The whole file, read with `open_shared`."""
    with open_shared(path) as handle:
        return handle.read()
