from __future__ import annotations

import json
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
from codexsync.mutation_journal import JournalState, JournalStore, MutationJournal
from codexsync.recovery import JournalInfo, RecoveryAction, list_journals, resume_operation, rollback_operation
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

    def test_listing_shows_an_interrupted_sync_with_both_exits_and_its_snapshot(self) -> None:
        operation_id = self._interrupt_sync_during_commit()

        [info] = list_journals(self.config_path)

        self.assertEqual(info.operation_id, operation_id)
        self.assertEqual(info.family, "sync")
        self.assertEqual(info.state, JournalState.RECOVERY_REQUIRED.value)
        self.assertEqual(info.action_count, 1)
        self.assertTrue(info.readable)
        self.assertFalse(info.terminal)
        self.assertTrue(info.can_resume)
        self.assertTrue(info.can_rollback)
        self.assertEqual(info.backup_snapshot, self.journals.load(operation_id).backup_snapshot)
        self.assertTrue(info.backup_snapshot_present)

    def test_recovering_a_terminal_operation_is_refused(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1)
        self.journals.transition(journal, JournalState.FAILED)
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            with self.assertRaises(FailSafeError):
                resume_operation(self.config_path, journal.operation_id, dry_run=False)


def _tree(root: Path) -> dict[str, tuple[bool, int, int]]:
    """Names, sizes and mtimes under ``root``; parent mtimes catch a created-then-removed file."""
    if not root.exists():
        return {}
    result = {".": (True, 0, root.stat().st_mtime_ns)}
    for path in root.rglob("*"):
        stat_result = path.stat()
        result[path.relative_to(root).as_posix()] = (
            path.is_dir(),
            0 if path.is_dir() else stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    return result


class JournalListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"journal-list-{uuid.uuid4().hex}"
        (self.root / "local-state" / "data").mkdir(parents=True)
        (self.root / "cloud" / "data").mkdir(parents=True)
        self.config_path = _write_config(self.root)
        self.journals = JournalStore(self.root / ".tmp")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _by_id(self) -> dict[str, JournalInfo]:
        return {item.operation_id: item for item in list_journals(self.config_path)}

    def test_no_journal_directory_lists_nothing_and_creates_nothing(self) -> None:
        before = _tree(self.root)

        self.assertEqual(list_journals(self.config_path), [])

        self.assertEqual(_tree(self.root), before)
        self.assertFalse((self.root / ".tmp").exists())
        self.assertFalse((self.root / "backups").exists())

    def test_rollback_is_offered_only_when_a_snapshot_is_recorded(self) -> None:
        closed = self.journals.begin("sync", "a" * 64, 1)
        self.journals.transition(closed, JournalState.FAILED)
        named = self.journals.begin("restore", "b" * 64, 2, backup_snapshot="machine-a-20260101T000000Z-deadbeef0000")

        listed = self._by_id()

        self.assertTrue(listed[named.operation_id].can_resume)
        self.assertTrue(listed[named.operation_id].can_rollback)
        # Recorded but never created: rollback stays legal and closes as NOTHING_TO_ROLL_BACK.
        self.assertIs(listed[named.operation_id].backup_snapshot_present, False)
        self.assertTrue(listed[closed.operation_id].terminal)
        self.assertIsNone(listed[closed.operation_id].backup_snapshot_present)
        self.assertFalse(listed[closed.operation_id].can_resume, "a terminal journal has no exit")
        self.assertFalse(listed[closed.operation_id].can_rollback)

        self.journals.transition(named, JournalState.FAILED)
        unnamed = self.journals.begin("sync", "c" * 64, 1)
        info = self._by_id()[unnamed.operation_id]
        self.assertTrue(info.can_resume)
        self.assertFalse(info.can_rollback, "rollback refuses without a recorded snapshot")

    def test_unreadable_journals_are_listed_as_evidence_instead_of_failing(self) -> None:
        good = self.journals.begin("sync", "a" * 64, 3)
        self.journals.transition(good, JournalState.FAILED)
        broken_id = str(uuid.uuid4())
        (self.journals.root / f"{broken_id}.json").write_text("{not json", encoding="utf-8")
        impostor_id = str(uuid.uuid4())
        (self.journals.root / f"{impostor_id}.json").write_text(
            json.dumps({
                "operation_id": str(uuid.uuid4()), "family": "restore", "state": "COMMITTING",
                "created_at_utc": "2026-01-01T00:00:00.000000Z", "plan_hash": "a" * 64,
                "action_count": 4, "backup_snapshot": None,
            }),
            encoding="utf-8",
        )
        with self.assertRaises(FailSafeError):
            self.journals.non_terminal()

        listed = self._by_id()

        self.assertTrue(listed[good.operation_id].readable)
        for operation_id in (broken_id, impostor_id):
            with self.subTest(operation_id=operation_id):
                info = listed[operation_id]
                self.assertFalse(info.readable)
                self.assertIsNone(info.state)
                self.assertFalse(info.terminal, "an unreadable journal still blocks begin()")
                self.assertFalse(info.can_resume)
                self.assertFalse(info.can_rollback)
        self.assertEqual(listed[impostor_id].family, "restore")
        self.assertEqual(listed[impostor_id].action_count, 4)

        # The listing's flags agree with what recover actually does.
        with patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate()):
            for operation_id in (broken_id, impostor_id):
                with self.assertRaises(FailSafeError):
                    resume_operation(self.config_path, operation_id, dry_run=True)

    def test_non_terminal_first_then_newest_first(self) -> None:
        # Explicit timestamps: consecutive begin() calls can share a clock tick
        # on Windows, which would make the expected order a coin toss.
        def journal(state: JournalState, created: str) -> MutationJournal:
            written = MutationJournal(str(uuid.uuid4()), "sync", state, created, "a" * 64, 1)
            self.journals.write(written)
            return written

        oldest = journal(JournalState.FAILED, "2026-01-01T00:00:00.000000Z")
        old_open = journal(JournalState.PREPARED, "2026-01-02T00:00:00.000000Z")
        newest = journal(JournalState.COMMITTED, "2026-01-04T00:00:00.000000Z")
        new_open = journal(JournalState.RECOVERY_REQUIRED, "2026-01-03T00:00:00.000000Z")
        broken_id = str(uuid.uuid4())
        (self.journals.root / f"{broken_id}.json").write_text("", encoding="utf-8")

        order = [item.operation_id for item in list_journals(self.config_path)]

        self.assertEqual(
            order,
            [new_open.operation_id, old_open.operation_id, broken_id, newest.operation_id, oldest.operation_id],
        )

    def test_listing_creates_nothing(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1, backup_snapshot="machine-a-20260101T000000Z-deadbeef0000")
        (self.journals.root / f"{uuid.uuid4()}.json").write_text("{", encoding="utf-8")
        before = _tree(self.root)

        listed = list_journals(self.config_path)

        self.assertEqual(len(listed), 2)
        self.assertEqual(_tree(self.root), before)
        self.assertEqual(self.journals.load(journal.operation_id).state, JournalState.PREPARED)
        self.assertFalse((self.root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
