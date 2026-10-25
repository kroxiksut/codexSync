"""`config unset` / `config mapping` on a broken file exit 4, not with a traceback (CS-458)."""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.cli import main
from codexsync.config_edit import change_path_mappings, remove_config_value
from codexsync.exceptions import ConfigError


SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class BrokenTomlEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"edit-broken-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.path = self.root / "config.toml"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_invalid_toml_is_a_config_error(self) -> None:
        self._write("[sync\nmode = \n")
        with self.assertRaises(ConfigError):
            remove_config_value(self.path, "sync.direction")
        with self.assertRaises(ConfigError):
            change_path_mappings(self.path, remove="x")
        self.assertEqual(main(["-c", str(self.path), "config", "unset", "sync.direction"]), 4)

    def test_section_that_is_not_a_table_is_a_config_error(self) -> None:
        self._write("logging = 5\npath_mappings = 3\n")
        with self.assertRaises(ConfigError):
            remove_config_value(self.path, "logging.file")
        with self.assertRaises(ConfigError):
            change_path_mappings(self.path, remove="x")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "logging = 5\npath_mappings = 3\n")


if __name__ == "__main__":
    unittest.main()
