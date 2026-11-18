"""Probe SQLite's standard byte-range read lock without opening SQLite.

Windows probes with a separate read-only handle, immediately releasing each
shared lock. POSIX asks the kernel about conflicting locks without acquiring
or releasing any: closing/acquiring POSIX locks could disturb another SQLite
connection in this process. Linux's OFD query also sees same-process locks;
Darwin's F_GETLK can see other processes only. A database already opened by
this process is refused before opening it: merely closing a raw descriptor
could release that process's SQLite locks on POSIX.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

from .shared_read import open_shared


_PENDING_BYTE = 0x40000000


def check_source_read_lock(database: Path) -> None:
    """Raise OSError when SQLite's normal reader would encounter a lock.

    This is a momentary probe, not a lock held through the observation. The
    capture's byte and metadata checks independently reject concurrent writes.
    """
    if os.name != "nt":
        _refuse_same_process_handle(database)
    with open_shared(database) as source:
        if os.name == "nt":
            _windows_probe(source.fileno())
        elif sys.platform in ("linux", "darwin"):
            _posix_probe(source.fileno())
        else:
            raise OSError("SQLite lock probing is unsupported on this platform")


def _refuse_same_process_handle(database: Path) -> None:
    stat = database.stat()
    descriptors = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
    # If the descriptor inventory cannot be read, fail closed. It is not safe
    # to close an uncoordinated database descriptor in an embedding process.
    for name in os.listdir(descriptors):
        try:
            current = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        if (current.st_dev, current.st_ino) == (stat.st_dev, stat.st_ino):
            raise OSError("SQLite source is already open in this process")


def _windows_probe(descriptor: int) -> None:
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel.LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                     ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    unlock = kernel.UnlockFileEx
    unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    unlock.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    for offset, length in ((_PENDING_BYTE, 1), (_PENDING_BYTE + 2, 510)):
        overlapped = Overlapped(Offset=offset)
        if not lock(handle, 1, 0, length, 0, ctypes.byref(overlapped)):
            raise OSError("SQLite source is locked")
        if not unlock(handle, 0, length, 0, ctypes.byref(overlapped)):
            raise OSError("SQLite source lock could not be released")


def _posix_probe(descriptor: int) -> None:
    import fcntl

    class Flock(ctypes.Structure):
        if sys.platform == "darwin":
            _fields_ = [("start", ctypes.c_longlong), ("length", ctypes.c_longlong),
                        ("pid", ctypes.c_int), ("kind", ctypes.c_short), ("whence", ctypes.c_short)]
        else:
            _fields_ = [("kind", ctypes.c_short), ("whence", ctypes.c_short),
                        ("start", ctypes.c_longlong), ("length", ctypes.c_longlong), ("pid", ctypes.c_int)]

    command = getattr(fcntl, "F_OFD_GETLK", fcntl.F_GETLK)
    for offset, length in ((_PENDING_BYTE, 1), (_PENDING_BYTE + 2, 510)):
        request = Flock(kind=fcntl.F_RDLCK, whence=os.SEEK_SET, start=offset, length=length, pid=0)
        result = fcntl.fcntl(descriptor, command, bytes(request))
        if Flock.from_buffer_copy(result).kind != fcntl.F_UNLCK:
            raise OSError("SQLite source is locked")
