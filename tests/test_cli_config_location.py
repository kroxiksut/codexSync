"""Which config the command line opens when it was not told which.

The window has always looked in several places; the command line's `-c`
defaulted to the literal `config.toml`, so it only ever looked in the current
directory. A machine set up through the window -- config in the per-user
location, or beside a downloaded exe -- answered every terminal command with
"Config file not found: config.toml" while the window on the same machine
opened that config without being asked. Both shells now resolve through
`config_locations.choose_config_path`, and these tests pin the order.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.cli import _resolve_config_path
from codexsync.config_locations import CONFIG_NAME, choose_config_path

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class ResolveConfigPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"cli-config-location-{uuid.uuid4().hex}"
        (self.root / "here").mkdir(parents=True)
        (self.root / "beside").mkdir()
        (self.root / "user").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.here = self.root / "here" / CONFIG_NAME
        self.beside = self.root / "beside" / CONFIG_NAME
        self.user = self.root / "user" / CONFIG_NAME

    def _choice(self, explicit=None, **kwargs):
        return choose_config_path(
            explicit, None, cwd=self.root / "here", user_path=self.user, **kwargs
        )

    def test_an_explicit_path_wins_even_when_it_does_not_exist_yet(self) -> None:
        """Naming a path is a request, including a request to create it."""
        self.here.write_text("", encoding="utf-8")
        wanted = self.root / "somewhere" / "other.toml"
        choice = self._choice(wanted)
        self.assertEqual(choice.path, wanted)
        self.assertEqual(choice.source, "explicit")
        self.assertFalse(choice.exists)

    def test_this_directory_comes_before_the_per_user_location(self) -> None:
        self.here.write_text("", encoding="utf-8")
        self.user.write_text("", encoding="utf-8")
        choice = self._choice()
        self.assertEqual(choice.path, self.here)
        self.assertEqual(choice.source, "cwd")

    def test_a_config_beside_the_executable_is_found(self) -> None:
        """A downloaded exe with its config next to it, started from anywhere."""
        self.beside.write_text("", encoding="utf-8")
        self.user.write_text("", encoding="utf-8")
        choice = self._choice(executable_dir=self.root / "beside")
        self.assertEqual(choice.path, self.beside)
        self.assertEqual(choice.source, "executable")

    def test_the_per_user_config_is_found_from_any_directory(self) -> None:
        """The case that used to fail: set up in the window, run in a terminal."""
        self.user.write_text("", encoding="utf-8")
        choice = self._choice()
        self.assertEqual(choice.path, self.user)
        self.assertEqual(choice.source, "user")

    def test_nothing_anywhere_proposes_the_per_user_path(self) -> None:
        choice = self._choice()
        self.assertEqual(choice.path, self.user)
        self.assertEqual(choice.source, "new")
        self.assertFalse(choice.exists)

    def test_the_cli_resolves_through_the_same_rule(self) -> None:
        self.user.write_text("", encoding="utf-8")
        with mock.patch("codexsync.cli.choose_config_path") as chooser:
            chooser.return_value = choose_config_path(None, None, cwd=self.root / "here", user_path=self.user)
            resolved = _resolve_config_path(None)
        self.assertEqual(resolved.path, self.user)
        self.assertEqual(resolved.source, "user")
        # And an explicit path is passed straight through to the same function.
        with mock.patch("codexsync.cli.choose_config_path", wraps=choose_config_path) as chooser:
            _resolve_config_path("somewhere/config.toml")
        self.assertEqual(chooser.call_args.args[0], "somewhere/config.toml")

    def test_a_config_found_elsewhere_is_announced_after_logging_is_ready(self) -> None:
        """The notice must survive: it used to be written before logging existed."""
        import logging

        from codexsync.cli import _announce_config_choice

        self.user.write_text("", encoding="utf-8")
        choice = choose_config_path(None, None, cwd=self.root / "here", user_path=self.user)
        with self.assertLogs("codexsync.cli", level="INFO") as captured:
            _announce_config_choice(choice)
        self.assertIn(str(self.user), chr(10).join(captured.output))

    def test_a_config_in_this_folder_is_not_announced(self) -> None:
        from codexsync.cli import _announce_config_choice

        self.here.write_text("", encoding="utf-8")
        choice = choose_config_path(None, None, cwd=self.root / "here", user_path=self.user)
        logger = __import__("logging").getLogger("codexsync.cli")
        with mock.patch.object(logger, "info") as info:
            _announce_config_choice(choice)
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
