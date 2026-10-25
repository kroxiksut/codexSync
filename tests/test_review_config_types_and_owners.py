"""Config review fixes: non-string choices (CS-457) and owned paths apart (CS-455)."""
from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.config import parse_config_text
from codexsync.exceptions import ConfigError


SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox" / "unused-review-config"

BASE = (
    "[paths]\n"
    'cloud_root_dir = "sync"\n'
    'backup_dir = "backups"\n'
    'temp_dir = ".tmp"\n'
)


def _parse(text: str) -> object:
    return parse_config_text(text, base_dir=SANDBOX)


class NonStringChoiceTests(unittest.TestCase):
    def test_list_or_table_choice_is_a_config_error(self) -> None:
        for section, key in (
            ("sync", "direction"),
            ("sync", "delete_policy"),
            ("sync", "mode"),
            ("conflict", "policy"),
        ):
            for value in ("[1, 2]", "{ a = 1 }"):
                with self.subTest(key=f"{section}.{key}", value=value):
                    with self.assertRaisesRegex(ConfigError, f"{section}.{key} must be a string"):
                        _parse(BASE + f"[{section}]\n{key} = {value}\n")


class OwnedPathsApartTests(unittest.TestCase):
    def test_semantic_root_inside_mirror_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "semantic.root_dir must not overlap paths.cloud_root_dir"):
            _parse(BASE + '[semantic]\nroot_dir = "sync/semantic"\n')

    def test_log_file_inside_mirror_or_backups_is_refused(self) -> None:
        for folder, field in (("sync", "paths.cloud_root_dir"), ("backups", "paths.backup_dir"), (".tmp", "paths.temp_dir")):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(ConfigError, f"{field} and logging.file must not overlap"):
                    _parse(BASE + f'[logging]\nfile = "{folder}/codexsync.log"\n')

    def test_log_file_inside_state_copies_is_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "logging.file and state_backup.root_dir must not overlap"):
            _parse(BASE + '[logging]\nfile = "copies/codexsync.log"\n[state_backup]\nroot_dir = "copies"\n')


if __name__ == "__main__":
    unittest.main()
