"""The Linux menu entry for an install no package made (CS-399).

Everything runs against a sandbox data home given through the environment, on
every platform: the module decides "Linux" from the `platform` it is told, so
the Windows and macOS runners check the same writes.
"""
from __future__ import annotations

import configparser
import hashlib
from pathlib import Path, PurePosixPath
import shutil
import unittest
import uuid

from codexsync import desktop_shortcut as shortcut
from codexsync.exceptions import ConfigError

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class MenuEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"shortcut-{uuid.uuid4().hex[:8]}"
        self.data = self.root / "data"
        self.system = self.root / "system"
        self.bin = self.root / "venv" / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "python").write_bytes(b"")
        self.program = self.bin / "codexsync-gui"
        self.program.write_bytes(b"#!/bin/sh\n")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.env = {"XDG_DATA_HOME": str(self.data), "XDG_DATA_DIRS": str(self.system), "PATH": ""}

    def call(self, function, **kwargs):
        options = {"env": self.env, "platform": "linux"}
        if function is not shortcut.remove_shortcut:
            options.update(executable=str(self.bin / "python"), frozen=False)
        options.update(kwargs)
        return function(**options)

    def entry(self) -> configparser.SectionProxy:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.data / "applications" / "codexsync.desktop", encoding="utf-8")
        return parser["Desktop Entry"]

    def test_install_writes_the_entry_and_icon_into_the_users_folder(self) -> None:
        status = self.call(shortcut.install_shortcut)
        self.assertEqual(status.entry, self.data / "applications" / "codexsync.desktop")
        self.assertEqual(status.icon, self.data / "icons" / "hicolor" / "256x256" / "apps" / "codexsync.png")
        self.assertTrue(status.ours)
        self.assertTrue(status.program_exists)
        entry = self.entry()
        self.assertEqual(entry["TryExec"], self.program.as_posix())
        self.assertEqual(entry["Icon"], "codexsync")
        self.assertEqual(entry["X-CodexSync-Shortcut"], "true")
        self.assertEqual(status.icon.read_bytes(), (shortcut.PACKAGED / "codexsync.png").read_bytes())
        self.assertEqual(entry["X-CodexSync-Icon-SHA256"], hashlib.sha256(status.icon.read_bytes()).hexdigest())
        self.assertEqual(sorted(p.name for p in (self.data / "applications").iterdir()), ["codexsync.desktop"])

    def test_the_entry_keeps_what_the_package_entry_says(self) -> None:
        self.call(shortcut.install_shortcut)
        packaged = configparser.ConfigParser(interpolation=None)
        packaged.optionxform = str
        packaged.read(shortcut.PACKAGED / "codexsync.desktop", encoding="utf-8")
        written = self.entry()
        for key, value in packaged["Desktop Entry"].items():
            if key != "Exec":
                self.assertEqual(written[key], value, key)

    def test_an_appimage_entry_starts_the_appimage_file(self) -> None:
        image = self.root / "CodexSync.AppImage"
        image.write_bytes(b"")
        self.env["APPIMAGE"] = str(image)
        status = self.call(shortcut.install_shortcut, frozen=True, executable="/tmp/.mount_x/usr/lib/codexsync-gui")
        self.assertEqual(status.program, image)

    def test_install_again_updates_its_own_entry(self) -> None:
        self.call(shortcut.install_shortcut)
        moved = self.root / "other" / "bin"
        moved.mkdir(parents=True)
        (moved / "codexsync-gui").write_bytes(b"")
        status = self.call(shortcut.install_shortcut, executable=str(moved / "python"))
        self.assertEqual(status.program, moved / "codexsync-gui")

    def test_a_program_that_is_gone_is_reported(self) -> None:
        self.call(shortcut.install_shortcut)
        self.program.unlink()
        status = self.call(shortcut.shortcut_status)
        self.assertTrue(status.ours)
        self.assertFalse(status.program_exists)

    def test_an_entry_codexsync_did_not_write_is_never_replaced_or_removed(self) -> None:
        foreign = self.data / "applications" / "codexsync.desktop"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("[Desktop Entry]\nType=Application\nName=Mine\nExec=mine\n", encoding="utf-8")
        for function in (shortcut.install_shortcut, shortcut.remove_shortcut):
            with self.subTest(function=function.__name__):
                with self.assertRaises(ConfigError) as caught:
                    self.call(function)
                self.assertEqual(caught.exception.code, "SHORTCUT_NOT_OURS")
                self.assertIn("Name=Mine", foreign.read_text(encoding="utf-8"))

    def test_nothing_is_written_when_a_package_installed_the_entry(self) -> None:
        packaged = self.system / "applications" / "codexsync.desktop"
        packaged.parent.mkdir(parents=True)
        packaged.write_text("[Desktop Entry]\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            self.call(shortcut.install_shortcut)
        self.assertEqual(caught.exception.code, "SHORTCUT_FROM_PACKAGE")
        self.assertFalse(self.data.exists())

    def test_no_window_program_is_refused(self) -> None:
        self.program.unlink()
        with self.assertRaises(ConfigError) as caught:
            self.call(shortcut.install_shortcut)
        self.assertEqual(caught.exception.code, "SHORTCUT_NO_PROGRAM")
        self.assertFalse(self.data.exists())

    def test_away_from_linux_nothing_is_written(self) -> None:
        self.assertFalse(self.call(shortcut.shortcut_status, platform="win32").supported)
        for function in (shortcut.install_shortcut, shortcut.remove_shortcut):
            with self.assertRaises(ConfigError) as caught:
                self.call(function, platform="darwin")
            self.assertEqual(caught.exception.code, "SHORTCUT_LINUX_ONLY")
        self.assertFalse(self.data.exists())

    def test_remove_takes_its_entry_and_icon_and_nothing_else(self) -> None:
        self.call(shortcut.install_shortcut)
        neighbour = self.data / "applications" / "other.desktop"
        neighbour.write_text("[Desktop Entry]\n", encoding="utf-8")
        status = self.call(shortcut.remove_shortcut)
        self.assertFalse(status.present)
        self.assertFalse(status.icon.exists())
        self.assertTrue(neighbour.exists())
        self.assertFalse(self.call(shortcut.remove_shortcut).present, "removing twice is not an error")

    def test_an_icon_someone_replaced_is_kept(self) -> None:
        status = self.call(shortcut.install_shortcut)
        status.icon.write_bytes(b"someone else's icon")
        self.call(shortcut.remove_shortcut)
        self.assertEqual(status.icon.read_bytes(), b"someone else's icon")

    def test_a_relative_data_home_is_ignored_as_the_spec_says(self) -> None:
        self.assertEqual(shortcut.data_home({"XDG_DATA_HOME": "relative"}), Path.home() / ".local" / "share")


class ExecQuotingTests(unittest.TestCase):
    def test_a_plain_path_is_written_as_it_is(self) -> None:
        self.assertEqual(shortcut.exec_value(PurePosixPath("/home/u/.local/bin/codexsync-gui")),
                         "/home/u/.local/bin/codexsync-gui")

    def test_a_space_quotes_the_path(self) -> None:
        self.assertEqual(shortcut.exec_value(PurePosixPath("/home/u/my apps/codexsync-gui")),
                         '"/home/u/my apps/codexsync-gui"')

    def test_reserved_characters_are_escaped_twice(self) -> None:
        # Quoting escapes $ and " with a backslash, % is doubled, and the
        # value's own string escape then doubles each backslash.
        self.assertEqual(shortcut.exec_value(PurePosixPath('/x/a$b%c"d/codexsync-gui')),
                         '"/x/a\\\\$b%%c\\\\"d/codexsync-gui"')


if __name__ == "__main__":
    unittest.main()
