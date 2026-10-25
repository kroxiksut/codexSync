"""The log file is never made inside the `.codex` the locator found (CS-456)."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import unittest
from unittest import mock
import uuid

from codexsync import cli
from codexsync.config import parse_config_text


REPO_ROOT = Path(__file__).resolve().parent.parent


class LogOutsideCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = REPO_ROOT / "test-sandbox" / f"log-codex-{uuid.uuid4().hex}"
        self.home = self.root / "codex-home"
        self.home.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _cfg(self, log_file: Path) -> object:
        text = (
            "[paths]\n"
            'local_state_dir = ""\n'
            'cloud_root_dir = "sync"\n'
            'backup_dir = "backups"\n'
            'temp_dir = ".tmp"\n'
            "[logging]\n"
            f"file = {str(log_file.as_posix())!r}\n"
        ).replace("'", '"')
        return parse_config_text(text, base_dir=self.root.resolve())

    def _logged_file(self, cfg: object, located: Path) -> object:
        with mock.patch.object(cli, "locate_local_state_dir", return_value=located), \
                mock.patch.object(cli, "configure_logging") as configure:
            cli._configure_file_logging(cfg, verbose=False)
        return configure.call_args.args[0].file

    def test_log_inside_located_codex_goes_to_console_only(self) -> None:
        cfg = self._cfg(self.home / "logs" / "codexsync.log")
        self.assertIsNone(self._logged_file(cfg, self.home))
        self.assertFalse((self.home / "logs").exists())

    def test_log_inside_codex_home_when_locator_refuses(self) -> None:
        cfg = self._cfg(self.home / "logs" / "codexsync.log")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), \
                mock.patch.object(cli, "locate_local_state_dir", side_effect=RuntimeError("refused")), \
                mock.patch.object(cli, "configure_logging") as configure:
            cli._configure_file_logging(cfg, verbose=False)
        self.assertIsNone(configure.call_args.args[0].file)

    def test_log_outside_codex_is_kept(self) -> None:
        log_file = self.root / "logs" / "codexsync.log"
        cfg = self._cfg(log_file)
        self.assertEqual(Path(self._logged_file(cfg, self.home)).resolve(), log_file.resolve())


if __name__ == "__main__":
    unittest.main()
