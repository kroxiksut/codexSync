"""`sync --apply --unattended`: the sign-in task's command (CS-267, `D-016`).

Nobody is watching this run, so nothing that needs a person may be decided by
it. The process gate already refuses it while Codex is open -- it goes through
`build_context(enforce_safety=True)` like any sync -- and this pins the other
half: a conflict stops it before a write whatever `conflict.policy` says.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.app import unattended_config
from codexsync.cli import main
from codexsync.config import load_config
from codexsync.config_edit import create_config, set_value

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class UnattendedSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"unattended-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(
            self.config, machine_id="laptop", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"),
        )
        text = set_value(self.config.read_text(encoding="utf-8"), "conflict", "policy", "prefer_local")
        self.config.write_text(text, encoding="utf-8")
        # `main` would open the config's log file inside the sandbox and keep
        # the handle, which the sandbox check then reports as a leak.
        logging_patch = mock.patch("codexsync.cli.configure_logging")
        logging_patch.start()
        self.addCleanup(logging_patch.stop)

    def test_every_conflict_policy_becomes_manual_abort(self) -> None:
        cfg = load_config(self.config)
        self.assertEqual(cfg.conflict.policy, "prefer_local")
        self.assertEqual(unattended_config(cfg).conflict.policy, "manual_abort")
        self.assertEqual(unattended_config(cfg).sync, cfg.sync, "nothing else changes")

    def test_the_cli_flag_reaches_the_planner(self) -> None:
        with mock.patch("codexsync.cli.build_context") as build, mock.patch("codexsync.cli.run_sync"):
            code = main(["-c", str(self.config), "sync", "--apply", "--unattended"])
        self.assertEqual(code, 0)
        self.assertIs(build.call_args.kwargs["unattended"], True)
        self.assertIs(build.call_args.kwargs["enforce_safety"], True, "the process gate still decides")

    def test_an_ordinary_sync_is_not_unattended(self) -> None:
        with mock.patch("codexsync.cli.build_context") as build, mock.patch("codexsync.cli.run_sync"):
            main(["-c", str(self.config), "sync", "--dry-run"])
        self.assertIs(build.call_args.kwargs["unattended"], False)


if __name__ == "__main__":
    unittest.main()
