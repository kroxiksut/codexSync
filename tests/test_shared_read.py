"""Reading Codex's state never stops Codex replacing it (CS-500).

On Windows a file opened by `open()` cannot be renamed over: Codex saves
`.codex-global-state.json` by a replace, and every Guardian reading was a
moment in which that save could fail. The replace here is the POSIX-semantics
rename (Rust's `std::fs::rename`); a classic `MoveFileExW` fails against any
open handle, so no way of opening helps it (see `shared_read`).
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.shared_read import open_shared, read_shared, shares_deletion
from codexsync.stable_reader import SourceMissingError, SourceUnstableError, StableReader

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class SharedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"shared-read-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = self.root / ".codex-global-state.json"
        self.state.write_bytes(b'{"a": 1}')

    def replace_with(self, payload: bytes) -> None:
        """Replace the state as a writer using POSIX rename semantics does."""
        staged = self.root / ".staged"
        staged.write_bytes(payload)
        if shares_deletion():
            _posix_rename(staged, self.state)
        else:
            os.replace(staged, self.state)

    def test_a_replace_goes_through_while_the_file_is_open(self) -> None:
        with open_shared(self.state) as handle:
            self.replace_with(b'{"a": 2}')
            self.assertEqual(handle.read(), b'{"a": 1}')
        self.assertEqual(self.state.read_bytes(), b'{"a": 2}')

    @unittest.skipUnless(shares_deletion(), "only Windows blocks a rename over an open file")
    def test_the_plain_open_is_what_blocked_the_replace(self) -> None:
        # The premise: without FILE_SHARE_DELETE the same replace is refused.
        with self.state.open("rb"):
            with self.assertRaises(OSError):
                self.replace_with(b'{"a": 2}')
        self.assertEqual(self.state.read_bytes(), b'{"a": 1}')

    def test_a_missing_file_is_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_shared(self.root / "missing.json")

    def test_guardian_reports_the_replace_as_an_unstable_reading(self) -> None:
        reader = StableReader(self.state, max_bytes=1 << 20)
        real = open_shared

        def open_then_replace(path):
            handle = real(path)
            self.replace_with(b'{"a": 22}')
            return handle

        with patch("codexsync.stable_reader.open_shared", side_effect=open_then_replace):
            with self.assertRaises(SourceUnstableError):
                reader.read_once()
        self.assertEqual(self.state.read_bytes(), b'{"a": 22}')
        self.assertEqual(reader.read_once().observation.payload, b'{"a": 22}')

    def test_a_missing_state_is_still_source_missing(self) -> None:
        with self.assertRaises(SourceMissingError):
            StableReader(self.root / "none.json", max_bytes=10).read_once()


def _posix_rename(source: Path, target: Path) -> None:
    """`FileRenameInfoEx` with POSIX semantics, as Rust's `std::fs::rename` does."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    delete, share_all, open_existing, backup_semantics = 0x10000, 0x7, 3, 0x02000000
    handle = kernel.CreateFileW(str(source.resolve()), delete, share_all, None, open_existing, backup_semantics, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    name = str(target.resolve())

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * (len(name) + 1)),
        ]

    replace_if_exists, posix_semantics, file_rename_info_ex = 0x1, 0x2, 22
    info = RenameInfo(replace_if_exists | posix_semantics, None, len(name) * 2, name)
    try:
        if not kernel.SetFileInformationByHandle(handle, file_rename_info_ex, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
