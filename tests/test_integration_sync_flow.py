from __future__ import annotations

import os
from pathlib import Path
import shutil
import textwrap
import time
import unittest
import uuid
from unittest.mock import patch

from codexsync.app import build_context, restore_from_backup, run_sync


class _StoppedGate:
    def require(self, *_args, **_kwargs):
        return None


def _set_mtime_ns(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


class IntegrationSyncFlowTests(unittest.TestCase):
    def test_plan_dry_run_apply_restore_with_backup_pruning(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"integration-flow-{uuid.uuid4().hex}"
        local_state = root / "local-state"
        cloud_root = root / "cloud"
        backup_root = root / "backups"
        temp_root = root / ".tmp"
        state_root = root / "state"
        config_path = root / "config.toml"

        try:
            local_state.mkdir(parents=True, exist_ok=True)
            cloud_root.mkdir(parents=True, exist_ok=True)
            backup_root.mkdir(parents=True, exist_ok=True)
            temp_root.mkdir(parents=True, exist_ok=True)
            state_root.mkdir(parents=True, exist_ok=True)

            local_file = local_state / "data" / "a.json"
            cloud_file = cloud_root / "data" / "a.json"
            local_file.parent.mkdir(parents=True, exist_ok=True)
            cloud_file.parent.mkdir(parents=True, exist_ok=True)

            cloud_file.write_text("v1", encoding="utf-8")
            local_file.write_text("v2", encoding="utf-8")
            now_ns = time.time_ns()
            _set_mtime_ns(cloud_file, now_ns - 2_000_000_000)
            _set_mtime_ns(local_file, now_ns - 1_000_000_000)

            config_path.write_text(
                textwrap.dedent(
                    f"""
                    [identity]
                    machine_id = "machine-a"

                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"
                    dry_run_default = true

                    [safety]
                    require_codex_stopped = false
                    fail_on_unknown = true

                    [paths]
                    workspace_root_dir = "{root.as_posix()}"
                    local_state_dir = "{local_state.as_posix()}"
                    cloud_root_dir = "{cloud_root.as_posix()}"
                    backup_dir = "{backup_root.as_posix()}"
                    temp_dir = "{temp_root.as_posix()}"

                    [targets]
                    include_roots = ["data"]

                    [backup]
                    backup_before_overwrite = true
                    retention_days = 30
                    max_backups = 1
                    compression = "zip"

                    [state]
                    manifest_file = "{(state_root / "manifest.json").as_posix()}"
                    data_version = 1
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            # 1) Plan should detect local -> cloud update.
            with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
                ctx = build_context(config_path, enforce_safety=True)
            self.assertEqual(len(ctx.plan.to_cloud), 1)
            self.assertEqual(len(ctx.plan.to_local), 0)

            # 2) Dry-run should not change files.
            run_sync(ctx, dry_run=True)
            self.assertEqual(cloud_file.read_text(encoding="utf-8"), "v1")

            # 3) Apply should sync and create backup snapshot with old cloud content.
            with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
                ctx = build_context(config_path, enforce_safety=True)
            run_sync(ctx, dry_run=False)
            self.assertEqual(cloud_file.read_text(encoding="utf-8"), "v2")
            backup_zips = sorted(backup_root.glob("*.zip"))
            self.assertEqual(len(backup_zips), 1)

            # 4) Another apply should create another snapshot; max_backups=1 prunes older snapshot.
            time.sleep(1.1)  # ensure unique snapshot timestamp granularity
            local_file.write_text("v3", encoding="utf-8")
            _set_mtime_ns(local_file, time.time_ns())
            with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
                ctx = build_context(config_path, enforce_safety=True)
            run_sync(ctx, dry_run=False)
            self.assertEqual(cloud_file.read_text(encoding="utf-8"), "v3")
            backup_zips = sorted(backup_root.glob("*.zip"))
            self.assertEqual(len(backup_zips), 1, "old snapshot should be pruned by max_backups=1")

            # 5) Restore latest backup to local (latest snapshot contains previous cloud version = v2).
            local_file.write_text("broken", encoding="utf-8")
            with patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
                result = restore_from_backup(
                    config_path=config_path,
                    snapshot_name=None,
                    target="local",
                    dry_run=False,
                )
            self.assertTrue(result.snapshot_name.endswith(".zip"))
            self.assertEqual(local_file.read_text(encoding="utf-8"), "v2")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
