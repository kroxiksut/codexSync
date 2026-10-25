"""Who asks Codex to quit before a sync.

A settings-scope sync that writes without --apply still asks (CS-459); an
unattended run -- the sign-in task -- never asks, at any scope (CS-446).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.cli import main


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "src" / "codexsync" / "config.example.toml"


class SettingsSyncCloserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = REPO_ROOT / "test-sandbox" / f"settings-closer-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.path = self.root / "config.toml"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, dry_run_default: str, *extra: str) -> tuple[mock.MagicMock, mock.MagicMock]:
        text = TEMPLATE.read_text(encoding="utf-8")
        text = text.replace('scope = "full"', 'scope = "settings"', 1)
        text = text.replace("dry_run_default = true", f"dry_run_default = {dry_run_default}", 1)
        self.path.write_text(text, encoding="utf-8")
        with mock.patch("codexsync.cli.close_codex_for_sync", return_value=False) as closer, \
                mock.patch("codexsync.cli.build_context"), \
                mock.patch("codexsync.cli.run_sync") as run:
            self.assertEqual(main(["-c", str(self.path), "sync", *extra]), 0)
        return closer, run

    def test_write_by_config_default_asks_codex_to_quit(self) -> None:
        closer, run = self._run("false")
        closer.assert_called_once()
        self.assertFalse(run.call_args.kwargs["dry_run"])

    def test_dry_run_never_asks(self) -> None:
        closer, run = self._run("true")
        closer.assert_not_called()
        self.assertTrue(run.call_args.kwargs["dry_run"])
        closer, _ = self._run("false", "--dry-run")
        closer.assert_not_called()

    def test_apply_asks(self) -> None:
        closer, _ = self._run("true", "--apply")
        closer.assert_called_once()

    def test_unattended_never_asks(self) -> None:
        closer, run = self._run("true", "--apply", "--unattended")
        closer.assert_not_called()
        self.assertFalse(run.call_args.kwargs["dry_run"])


class FullSyncCloserTests(unittest.TestCase):
    """`[sync] scope = "full"`: the closer is `run_handoff(close_codex=...)` (CS-446)."""

    def setUp(self) -> None:
        self.root = REPO_ROOT / "test-sandbox" / f"full-closer-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.path = self.root / "config.toml"
        self.path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _close_codex(self, *extra: str) -> bool:
        with mock.patch("codexsync.cli.run_handoff") as handoff,                 mock.patch("codexsync.cli._print_handoff_result"):
            self.assertEqual(main(["-c", str(self.path), "sync", "--apply", *extra]), 0)
        handoff.assert_called_once()
        return handoff.call_args.kwargs["close_codex"]

    def test_unattended_run_never_asks(self) -> None:
        self.assertFalse(self._close_codex("--unattended"))

    def test_a_run_a_person_starts_may_ask(self) -> None:
        self.assertTrue(self._close_codex())


if __name__ == "__main__":
    unittest.main()
