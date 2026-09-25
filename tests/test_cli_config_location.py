"""Which config the command line opens when it was not told which.

The window has always looked in several places; the command line's `-c`
defaulted to the literal `config.toml`, so it only ever looked in the current
directory. Both shells now resolve through `config_locations.choose_config_path`,
and the window's choice reaches the terminal through the pointer file.

What these tests pin besides the order: nothing found means *no* config --
never a location made up on the user's behalf (CS-268) -- and the pointer
carries a path, is written only for a real file outside the temp directory, and
is never read from the real per-user location during a test.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.cli import _require_config_path, _resolve_config_path, main
from codexsync.config_locations import (
    CONFIG_NAME,
    choose_config_path,
    read_config_pointer,
    write_config_pointer,
)
from codexsync.exceptions import ConfigError
from codexsync.exit_codes import ExitCode

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class ResolveConfigPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"cli-config-location-{uuid.uuid4().hex}"
        (self.root / "here").mkdir(parents=True)
        (self.root / "beside").mkdir()
        (self.root / "chosen").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.here = self.root / "here" / CONFIG_NAME
        self.beside = self.root / "beside" / CONFIG_NAME
        self.chosen = self.root / "chosen" / CONFIG_NAME

    def _choice(self, explicit=None, remembered=None, **kwargs):
        return choose_config_path(explicit, remembered, cwd=self.root / "here", **kwargs)

    def test_an_explicit_path_wins_even_when_it_does_not_exist_yet(self) -> None:
        """Naming a path is a request, including a request to create it."""
        self.here.write_text("", encoding="utf-8")
        wanted = self.root / "somewhere" / "other.toml"
        choice = self._choice(wanted)
        self.assertEqual(choice.path, wanted)
        self.assertEqual(choice.source, "explicit")
        self.assertFalse(choice.exists)

    def test_the_config_the_window_opened_comes_first(self) -> None:
        self.here.write_text("", encoding="utf-8")
        self.chosen.write_text("", encoding="utf-8")
        choice = self._choice(remembered=self.chosen)
        self.assertEqual(choice.path, self.chosen)
        self.assertEqual(choice.source, "remembered")

    def test_a_config_beside_the_executable_is_found(self) -> None:
        """A downloaded exe with its config next to it, started from anywhere."""
        self.beside.write_text("", encoding="utf-8")
        choice = self._choice(executable_dir=self.root / "beside")
        self.assertEqual(choice.path, self.beside)
        self.assertEqual(choice.source, "executable")

    def test_nothing_anywhere_is_no_config(self) -> None:
        choice = self._choice()
        self.assertIsNone(choice.path)
        self.assertEqual(choice.source, "none")

    def test_the_cli_reads_the_pointer_and_resolves_through_the_same_rule(self) -> None:
        self.chosen.write_text("", encoding="utf-8")
        with mock.patch("codexsync.cli.read_config_pointer", return_value=self.chosen):
            resolved = _resolve_config_path(None)
        self.assertEqual(resolved.path, self.chosen)
        self.assertEqual(resolved.source, "remembered")
        with mock.patch("codexsync.cli.choose_config_path", wraps=choose_config_path) as chooser:
            _resolve_config_path("somewhere/config.toml")
        self.assertEqual(chooser.call_args.args[0], "somewhere/config.toml")

    def test_no_config_is_a_config_error_that_says_how_to_name_one(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            _require_config_path(self._choice())
        self.assertIn("-c", str(caught.exception))

    def test_a_command_without_any_config_exits_4(self) -> None:
        empty = self._choice()
        with mock.patch("codexsync.cli._resolve_config_path", return_value=empty):
            with mock.patch("codexsync.cli.configure_logging"):
                code = main(["validate"])
        self.assertEqual(code, int(ExitCode.BAD_INPUT))

    def test_a_config_found_elsewhere_is_announced_after_logging_is_ready(self) -> None:
        """The notice must survive: it used to be written before logging existed."""
        from codexsync.cli import _announce_config_choice

        self.chosen.write_text("", encoding="utf-8")
        choice = self._choice(remembered=self.chosen)
        with self.assertLogs("codexsync.cli", level="INFO") as captured:
            _announce_config_choice(choice)
        self.assertIn(str(self.chosen), chr(10).join(captured.output))

    def test_a_config_in_this_folder_is_not_announced(self) -> None:
        from codexsync.cli import _announce_config_choice

        self.here.write_text("", encoding="utf-8")
        choice = self._choice()
        logger = __import__("logging").getLogger("codexsync.cli")
        with mock.patch.object(logger, "info") as info:
            _announce_config_choice(choice)
        info.assert_not_called()


class PointerTests(unittest.TestCase):
    """Every test names its own pointer file; the real one is never touched."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"config-pointer-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.pointer = self.root / "pointer" / "config-path.txt"
        self.config = self.root / "workspace" / CONFIG_NAME
        self.config.parent.mkdir()
        self.config.write_text("", encoding="utf-8")

    def test_a_written_pointer_is_read_back_as_the_absolute_path(self) -> None:
        self.assertTrue(write_config_pointer(self.config, self.pointer))
        self.assertEqual(read_config_pointer(self.pointer), self.config.resolve())
        self.assertEqual(
            self.pointer.read_text(encoding="utf-8").strip(), str(self.config.resolve()),
            "the pointer holds the path and nothing else",
        )

    def test_a_file_that_does_not_exist_is_not_recorded(self) -> None:
        self.assertFalse(write_config_pointer(self.root / "missing.toml", self.pointer))
        self.assertFalse(self.pointer.exists())

    def test_a_config_in_the_temp_directory_is_not_recorded(self) -> None:
        """A scratch config must never become the default (CS-266)."""
        with mock.patch("codexsync.config_locations.is_under_temp", return_value=True):
            self.assertFalse(write_config_pointer(self.config, self.pointer))
        self.assertFalse(self.pointer.exists())

    def test_a_broken_pointer_is_no_pointer(self) -> None:
        self.assertIsNone(read_config_pointer(self.pointer), "missing")
        self.pointer.parent.mkdir(parents=True)
        for content in ("", "relative/config.toml", "C:/a\nC:/b"):
            self.pointer.write_text(content, encoding="utf-8")
            self.assertIsNone(read_config_pointer(self.pointer), repr(content))

    def test_no_staging_file_is_left_beside_the_pointer(self) -> None:
        write_config_pointer(self.config, self.pointer)
        self.assertEqual([entry.name for entry in self.pointer.parent.iterdir()], [self.pointer.name])


if __name__ == "__main__":
    unittest.main()
