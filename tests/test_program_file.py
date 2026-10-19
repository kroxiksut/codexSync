"""A frozen build remembers the file that starts it, not a temporary mount (CS-420).

Inside an AppImage `sys.executable` lies under `/tmp/.mount_*`, which is gone
once the program exits: a scheduled task pointing there fails at its first
run. The AppImage runtime names the real file in `$APPIMAGE`. These tests use
a stand-in environment and `sys.executable`; no AppImage and no `systemctl`.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

from codexsync.config_locations import frozen_executable_dir
from codexsync.program_file import frozen_program
from codexsync.system_scheduler import job_command

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class _Frozen(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"program-file-{uuid.uuid4().hex[:8]}"
        self.mount = self.root / "tmp" / ".mount_codexsAbC" / "usr" / "bin"
        self.mount.mkdir(parents=True)
        self.inner = self.mount / "codexsync"
        self.inner.write_bytes(b"")
        self.home = self.root / "home" / "Applications"
        self.home.mkdir(parents=True)
        self.appimage = self.home / "CodexSync-x86_64.AppImage"
        self.appimage.write_bytes(b"")
        self.addCleanup(shutil.rmtree, self.root, True)

    def frozen(self, *, platform: str = "linux", appimage: str | None = None, writable: bool = True):
        import os

        env = {key: value for key, value in os.environ.items() if key != "APPIMAGE"}
        if appimage is not None:
            env["APPIMAGE"] = appimage
        return [
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(self.inner)),
            mock.patch("codexsync.program_file.sys.platform", platform),
            mock.patch("codexsync.config_locations.sys.platform", platform),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("codexsync.config_locations.os.access", return_value=writable),
        ]

    def run_with(self, patches, call):
        import contextlib

        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return call()


class FrozenProgramTests(_Frozen):
    def test_from_source_there_is_no_program_file(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(frozen_program())
            self.assertIsNone(frozen_executable_dir())

    def test_inside_an_appimage_the_appimage_is_the_program(self) -> None:
        result = self.run_with(self.frozen(appimage=str(self.appimage)), frozen_program)
        self.assertEqual(result, self.appimage)

    def test_the_folder_searched_beside_it_is_the_appimages_own(self) -> None:
        result = self.run_with(self.frozen(appimage=str(self.appimage)), frozen_executable_dir)
        self.assertEqual(result, self.home)

    def test_an_appimage_variable_naming_no_file_is_not_believed(self) -> None:
        missing = str(self.home / "gone.AppImage")
        result = self.run_with(self.frozen(appimage=missing), frozen_program)
        self.assertEqual(result, self.inner.resolve())

    def test_without_an_appimage_the_executable_is_the_program(self) -> None:
        result = self.run_with(self.frozen(), frozen_program)
        self.assertEqual(result, self.inner.resolve())

    def test_a_folder_this_user_cannot_write_is_not_searched(self) -> None:
        # A package installs into a folder only root may change: a config there
        # could never be saved.
        result = self.run_with(self.frozen(appimage=str(self.appimage), writable=False), frozen_executable_dir)
        self.assertIsNone(result)

    def test_windows_ignores_the_appimage_variable(self) -> None:
        result = self.run_with(self.frozen(platform="win32", appimage=str(self.appimage)), frozen_program)
        self.assertEqual(result, self.inner.resolve())


class ScheduledTaskTests(_Frozen):
    def test_a_task_installed_from_an_appimage_starts_the_appimage(self) -> None:
        command = self.run_with(self.frozen(appimage=str(self.appimage)), lambda: job_command(platform="linux"))
        self.assertEqual(command, [str(self.appimage)])
        self.assertNotIn(".mount_", " ".join(command))

    def test_an_explicit_executable_still_wins(self) -> None:
        other = self.root / "elsewhere" / "codexsync"
        command = self.run_with(
            self.frozen(appimage=str(self.appimage)), lambda: job_command(other, frozen=True, platform="linux"),
        )
        self.assertEqual(command, [str(other)])


if __name__ == "__main__":
    unittest.main()
