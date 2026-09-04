from __future__ import annotations

import os
from pathlib import Path
import shutil
import textwrap
import time
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import build_context, run_sync
from codexsync.exceptions import FailSafeError
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.recovery import RecoveryAction, resume_operation, rollback_operation
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


def _write_config(root: Path) -> Path:
    config_path = root / "config.toml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            [identity]
            machine_id = "machine-a"

            [sync]
            mode = "cold"
            direction = "bidirectional"
            compare = "mtime"
            dry_run_default = false

            [paths]
            local_state_dir = "{(root / 'local-state').as_posix()}"
            cloud_root_dir = "{(root / 'cloud').as_posix()}"
            backup_dir = "{(root / 'backups').as_posix()}"
            temp_dir = "{(root / '.tmp').as_posix()}"

            [guardian]
            root_dir = "{(root / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(root / 'semantic').as_posix()}"

            [targets]
            include_roots = ["data"]

            [backup]
            backup_before_overwrite = true
            compression = "none"

            [state]
            manifest_file = "{(root / 'state' / 'manifest.json').as_posix()}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"recovery-{uuid.uuid4().hex}"
        (self.root / "local-state" / "data").mkdir(parents=True)
        (self.root / "cloud" / "data").mkdir(parents=True)
        self.local_file = self.root / "local-state" / "data" / "a.txt"
        self.cloud_file = self.root / "cloud" / "data" / "a.txt"
        self.config_path = _write_config(self.root)
        self.journals = JournalStore(self.root / ".tmp")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _interrupt_sync_during_commit(self) -> str:
        """Run a sync that dies after the backup, mid commit, like a power cut."""
        self.local_file.write_text("new", encoding="utf-8")
        self.cloud_file.write_text("old", encoding="utf-8")
        # Local strictly newer, so the plan copies local -> cloud and the cloud
        # file is the one backed up before being replaced.
        now_ns = time.time_ns()
        os.utime(self.cloud_file, ns=(now_ns - 2_000_000_000, now_ns - 2_000_000_000))
        os.utime(self.local_file, ns=(now_ns - 1_000_000_000, now_ns - 1_000_000_000))

        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
        with patch(
            "codexsync.sync_engine.SyncEngine._replace_staged",
            side_effect=OSError("simulated power cut during commit"),
        ):
            with self.assertRaises(OSError):
                run_sync(ctx, dry_run=False)

        pending = self.journals.non_terminal()
        self.assertEqual(len(pending), 1, "the interrupted operation must leave one open journal")
        self.assertEqual(pending[0].state, JournalState.RECOVERY_REQUIRED)
        self.assertIsNotNone(pending[0].backup_snapshot)
        return pending[0].operation_id

    def test_interrupted_mutation_blocks_the_next_one(self) -> None:
        self._interrupt_sync_during_commit()
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
            with self.assertRaises(FailSafeError):
                run_sync(ctx, dry_run=False)

    def test_resume_closes_the_journal_and_unblocks_a_rerun(self) -> None:
        operation_id = self._interrupt_sync_during_commit()

        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            outcome = resume_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(outcome.action, RecoveryAction.RETRY_ALLOWED)
        self.assertEqual(self.journals.non_terminal(), [])
        self.assertEqual(self.journals.load(operation_id).state, JournalState.FAILED)

        # The interrupted command now runs to completion.
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
            run_sync(ctx, dry_run=False)
        self.assertEqual(self.cloud_file.read_text(encoding="utf-8"), "new")

    def test_resume_dry_run_changes_nothing(self) -> None:
        operation_id = self._interrupt_sync_during_commit()
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            outcome = resume_operation(self.config_path, operation_id, dry_run=True)
        self.assertEqual(outcome.action, RecoveryAction.WOULD_RECOVER)
        self.assertEqual(self.journals.load(operation_id).state, JournalState.RECOVERY_REQUIRED)
        self.assertEqual(len(self.journals.non_terminal()), 1)

    def test_rollback_restores_the_operations_own_snapshot(self) -> None:
        operation_id = self._interrupt_sync_during_commit()
        # Simulate the destination having been replaced before the crash.
        self.cloud_file.write_text("half-applied", encoding="utf-8")

        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()), \
                patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
            outcome = rollback_operation(
                self.config_path, operation_id, target="cloud", dry_run=False
            )
        self.assertEqual(outcome.action, RecoveryAction.ROLLED_BACK)
        self.assertEqual(self.cloud_file.read_text(encoding="utf-8"), "old")
        self.assertEqual(self.journals.load(operation_id).state, JournalState.FAILED)

    def test_rollback_dry_run_leaves_state_and_journal_untouched(self) -> None:
        operation_id = self._interrupt_sync_during_commit()
        self.cloud_file.write_text("half-applied", encoding="utf-8")

        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            outcome = rollback_operation(
                self.config_path, operation_id, target="cloud", dry_run=True
            )
        self.assertEqual(outcome.action, RecoveryAction.WOULD_RECOVER)
        self.assertEqual(self.cloud_file.read_text(encoding="utf-8"), "half-applied")
        self.assertEqual(len(self.journals.non_terminal()), 1)

    def test_rollback_without_a_recorded_snapshot_refuses(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1)
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            with self.assertRaises(FailSafeError):
                rollback_operation(
                    self.config_path, journal.operation_id, target="local", dry_run=False
                )
        self.assertEqual(len(self.journals.non_terminal()), 1, "the block must survive a refusal")

    def test_rollback_is_a_no_op_when_no_destination_was_replaced(self) -> None:
        # A journal that names a snapshot which was never created: the crash
        # happened before the backup was stamped, so nothing can have been
        # overwritten.
        journal = self.journals.begin(
            "sync", "a" * 64, 1, backup_snapshot="machine-a-20260101T000000Z-deadbeef"
        )
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            outcome = rollback_operation(
                self.config_path, journal.operation_id, target="cloud", dry_run=False
            )
        self.assertEqual(outcome.action, RecoveryAction.NOTHING_TO_ROLL_BACK)
        self.assertEqual(self.journals.non_terminal(), [])

    def test_recovering_a_terminal_operation_is_refused(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1)
        self.journals.transition(journal, JournalState.FAILED)
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            with self.assertRaises(FailSafeError):
                resume_operation(self.config_path, journal.operation_id, dry_run=False)


if __name__ == "__main__":
    unittest.main()
