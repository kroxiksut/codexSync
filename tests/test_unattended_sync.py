"""`sync --apply --unattended`: the sign-in task's command (CS-267, `D-016`).

Nobody is watching this run. The process gate refuses it while Codex is open
-- it goes through `build_context(enforce_safety=True)` like any sync -- and a
conflict is decided by `conflict.policy`, the person's decision made in
advance (D-027 amends D-016, which forced `manual_abort` here).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.app import build_context, with_conflict_policy
from codexsync.exceptions import ConfigError
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

    def test_an_unattended_run_keeps_the_configured_policy(self) -> None:
        with mock.patch("codexsync.app._make_safety_gate"),                 mock.patch("codexsync.app.locate_state_dirs", return_value=(self.root, self.root)),                 mock.patch("codexsync.app._build_indexes", return_value=({}, {})),                 mock.patch("codexsync.app.build_sync_plan") as plan:
            ctx = build_context(self.config, enforce_safety=False)
        self.assertEqual(ctx.config.conflict.policy, "prefer_local")
        self.assertEqual(plan.call_args.kwargs["conflict_policy"], "prefer_local", "the planner decides by it")

    def test_a_one_run_policy_replaces_only_the_policy(self) -> None:
        cfg = load_config(self.config)
        chosen = with_conflict_policy(cfg, "prefer_cloud")
        self.assertEqual(chosen.conflict.policy, "prefer_cloud")
        self.assertEqual(chosen.sync, cfg.sync, "nothing else changes")
        self.assertIs(with_conflict_policy(cfg, None), cfg)
        with self.assertRaises(ConfigError):
            with_conflict_policy(cfg, "newest")

    def test_the_cli_passes_a_one_run_policy(self) -> None:
        with mock.patch("codexsync.cli.build_context") as build, mock.patch("codexsync.cli.run_sync"):
            code = main(["-c", str(self.config), "sync", "--apply", "--conflict-policy", "prefer_cloud"])
        self.assertEqual(code, 0)
        self.assertEqual(build.call_args.kwargs["conflict_policy"], "prefer_cloud")

    def test_the_cli_flag_only_labels_the_run(self) -> None:
        with mock.patch("codexsync.cli.build_context") as build, mock.patch("codexsync.cli.run_sync") as run:
            code = main(["-c", str(self.config), "sync", "--apply", "--unattended"])
        self.assertEqual(code, 0)
        # The flag labels the run; the configured policy decides it either way (D-027).
        self.assertEqual(run.call_args.kwargs["origin"], "unattended")
        self.assertNotIn("unattended", build.call_args.kwargs)
        self.assertIs(build.call_args.kwargs["enforce_safety"], True, "the process gate still decides")

    def test_an_ordinary_sync_is_not_unattended(self) -> None:
        with mock.patch("codexsync.cli.build_context"), mock.patch("codexsync.cli.run_sync") as run:
            main(["-c", str(self.config), "sync", "--dry-run"])
        self.assertEqual(run.call_args.kwargs["origin"], "cli")


if __name__ == "__main__":
    unittest.main()
