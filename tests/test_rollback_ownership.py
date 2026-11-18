"""Review of 2026-10-26: who may roll a journal back, and when it closes.

- CS-486: a rollback of another machine's journal wrote that machine's
  backup into this `.codex` (the journals folder is shared).
- CS-491: the rollback closed the journal before restoring; a refusal half-way
  left nothing open and the snapshot unprotected.
- CS-492: entries outside today's include_roots/exclude_globs were skipped
  silently, and the journal was still closed.
- CS-512: a cloud client's copy `<id> (1).json` blocked every mutation.
- CS-514: `restore` without `--from` took the newest snapshot of any machine
  and either side.
- CS-549: a dry run said what it had checked only by which button was pressed.
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from codexsync.backup import BackupManager
from codexsync.exceptions import CodexSyncError, ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.mutation_journal import JournalState
from codexsync.recovery import RecoveryAction, list_journals, resume_operation, rollback_operation
from codexsync.restore import restore_from_backup
from codexsync.safety_gate import OperationKind

try:
    from tests.test_recovery import _StoppedGate
    from tests.test_recovery_rollback import _GateFailingAt, _Sandbox, _state
except ImportError:  # collected with tests/ itself on sys.path
    from test_recovery import _StoppedGate
    from test_recovery_rollback import _GateFailingAt, _Sandbox, _state


class _CodexStartsForTheRestore(_StoppedGate):
    """Stopped for the rollback's own check, running once the restore asks."""

    def require(self, operation, *, final: bool = False):
        if operation is OperationKind.RESTORE:
            raise SafetyPreconditionError("Codex started")
        return self.check(operation, final=final)


class _Base(_Sandbox):
    def _interrupted_sync(self, files: dict[str, tuple[bytes, bytes]], *, machine: str = "machine-a",
                          side: str = "local") -> str:
        """A sync that backed up ``files`` (old bytes), replaced them (new) and died."""
        root = self.local if side == "local" else self.cloud
        manager = BackupManager(self.root / "backups", machine)
        journal = self.journals.begin(
            "sync", "a" * 64, len(files), backup_snapshot=manager.snapshot_name, machine_id=machine,
        )
        for rel, (old, _new) in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
            manager.backup_file(path, rel, side=side)
        manager.finalize()
        journal = self.journals.transition(journal, JournalState.BACKED_UP)
        journal = self.journals.transition(journal, JournalState.COMMITTING)
        for rel, (_old, new) in files.items():
            (root / rel).write_bytes(new)
        self.journals.transition(journal, JournalState.RECOVERY_REQUIRED)
        return journal.operation_id

    def _gate(self, gate=None):
        return patch("codexsync.recovery._make_safety_gate", return_value=gate or _StoppedGate())


class ForeignJournalTests(_Base):
    """CS-486."""

    def test_another_machine_s_journal_is_never_rolled_back_here(self) -> None:
        mine = self.local / "data" / "a.txt"
        operation_id = self._interrupted_sync({"data/a.txt": (b"b's old", b"b's new")}, machine="machine-b")
        mine.write_bytes(b"this machine's file")

        for dry_run in (True, False):
            with self._gate(), self.assertRaises(FailSafeError) as caught:
                rollback_operation(self.config_path, operation_id, dry_run=dry_run)
            self.assertEqual(caught.exception.code, "ROLLBACK_OTHER_MACHINE")
            self.assertEqual(caught.exception.params, {"machine": "machine-b"})
        self.assertEqual(mine.read_bytes(), b"this machine's file")
        self.assertEqual(self.journals.load(operation_id).state, JournalState.RECOVERY_REQUIRED)

        [info] = list_journals(self.config_path)
        self.assertFalse(info.own)
        self.assertFalse(info.can_rollback)
        self.assertIn("machine-b", info.rollback_refusal)
        self.assertTrue(info.can_resume, "a run of a machine that is gone must not block this one for good")

        with self._gate():
            preview = resume_operation(self.config_path, operation_id, dry_run=True)
        self.assertIn("machine-b", preview.detail)

    def test_another_machine_s_global_state_journal_is_refused_too(self) -> None:
        self.state_file.write_bytes(_state(self.root.as_posix()))
        manager = BackupManager(self.root / "backups", "machine-b")
        journal = self.journals.begin("chats", "a" * 64, 1, backup_snapshot=manager.snapshot_name, machine_id="machine-b")
        manager.backup_file(self.state_file, self.state_file.name, side="local")
        manager.finalize()
        for state in (JournalState.BACKED_UP, JournalState.COMMITTING, JournalState.RECOVERY_REQUIRED):
            journal = self.journals.transition(journal, state)
        live = _state(self.root.as_posix(), order=["p-beta", "p-alpha"])
        self.state_file.write_bytes(live)

        with self._gate(), self.assertRaises(FailSafeError) as caught:
            rollback_operation(self.config_path, journal.operation_id, dry_run=False)
        self.assertEqual(caught.exception.code, "ROLLBACK_OTHER_MACHINE")
        self.assertEqual(self.state_file.read_bytes(), live)


class RollbackKeepsTheJournalOpenTests(_Base):
    """CS-491."""

    def test_codex_starting_before_the_restore_leaves_the_journal_open(self) -> None:
        target = self.local / "data" / "a.txt"
        operation_id = self._interrupted_sync({"data/a.txt": (b"old", b"half-applied")})

        with self._gate(_CodexStartsForTheRestore()), self.assertRaises(SafetyPreconditionError):
            rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(target.read_bytes(), b"half-applied")
        self.assertEqual(self.journals.load(operation_id).state, JournalState.RECOVERY_REQUIRED)
        self.assertEqual([j.operation_id for j in self.journals.non_terminal()], [operation_id])

        # The rollback can simply be run again once Codex is closed.
        with self._gate():
            outcome = rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(outcome.action, RecoveryAction.ROLLED_BACK)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(self.journals.non_terminal(), [])

    def test_codex_starting_inside_the_global_state_envelope_leaves_the_journal_open(self) -> None:
        before = _state(self.root.as_posix())
        after = _state(self.root.as_posix(), order=["p-beta", "p-alpha"])
        self.state_file.write_bytes(before)
        manager = BackupManager(self.root / "backups", "machine-a")
        journal = self.journals.begin("chats", "a" * 64, 1, backup_snapshot=manager.snapshot_name, machine_id="machine-a")
        manager.backup_file(self.state_file, self.state_file.name, side="local")
        manager.finalize()
        for state in (JournalState.BACKED_UP, JournalState.COMMITTING):
            journal = self.journals.transition(journal, state)
        self.state_file.write_bytes(after)
        journal = self.journals.transition(journal, JournalState.RECOVERY_REQUIRED)

        with self._gate(_GateFailingAt(1)), self.assertRaises(SafetyPreconditionError):
            rollback_operation(self.config_path, journal.operation_id, dry_run=False)
        self.assertEqual(self.state_file.read_bytes(), after)
        self.assertEqual(self.journals.load(journal.operation_id).state, JournalState.RECOVERY_REQUIRED)
        self.assertEqual([j.operation_id for j in self.journals.non_terminal()], [journal.operation_id])


class RollbackTakesEveryEntryTests(_Base):
    """CS-492."""

    def test_entries_outside_today_s_include_roots_are_restored_too(self) -> None:
        operation_id = self._interrupted_sync({
            "data/a.txt": (b"old a", b"new a"),
            "rules/default.rules": (b"old rules", b"new rules"),
        })
        # The config includes only `data`: `rules` was included when the sync ran.
        with self._gate():
            outcome = rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(outcome.restored_files, 2)
        self.assertEqual((self.local / "data" / "a.txt").read_bytes(), b"old a")
        self.assertEqual((self.local / "rules" / "default.rules").read_bytes(), b"old rules")


class JournalCopyTests(_Base):
    """CS-512."""

    def _copy(self, operation_id: str, state: JournalState) -> None:
        original = self.journals.root / f"{operation_id}.json"
        raw = json.loads(original.read_text(encoding="utf-8"))
        raw["state"] = state.value
        (self.journals.root / f"{operation_id} (1).json").write_text(json.dumps(raw), encoding="utf-8")

    def test_a_stale_copy_of_a_closed_journal_blocks_nothing(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1, machine_id="machine-a")
        self._copy(journal.operation_id, JournalState.BACKED_UP)
        self.journals.transition(journal, JournalState.FAILED)
        # Back to COMMITTING in the copy: older than the final FAILED either way.
        self._copy(journal.operation_id, JournalState.COMMITTING)

        self.assertEqual(self.journals.non_terminal(), [])
        self.journals.begin("sync", "b" * 64, 1, machine_id="machine-a")
        self.assertNotIn(f"{journal.operation_id} (1)", [info.operation_id for info in list_journals(self.config_path)])

    def test_a_copy_whose_original_is_gone_says_so(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1, machine_id="machine-a")
        journal = self.journals.transition(journal, JournalState.BACKED_UP)
        journal = self.journals.transition(journal, JournalState.COMMITTING)
        self._copy(journal.operation_id, JournalState.COMMITTING)
        (self.journals.root / f"{journal.operation_id}.json").unlink()

        with self.assertRaises(FailSafeError) as caught:
            self.journals.non_terminal()
        self.assertEqual(caught.exception.code, "JOURNAL_COPY")
        self.assertEqual(caught.exception.params["operation_id"], journal.operation_id)
        [info] = list_journals(self.config_path)
        self.assertFalse(info.readable)
        self.assertEqual(info.copy_of, journal.operation_id)


class OutcomeSaysWhatWasCheckedTests(_Base):
    """CS-549."""

    def test_each_dry_run_names_what_the_apply_would_do(self) -> None:
        operation_id = self._interrupted_sync({"data/a.txt": (b"old", b"new")})
        with self._gate():
            resume = resume_operation(self.config_path, operation_id, dry_run=True)
            rollback = rollback_operation(self.config_path, operation_id, dry_run=True)
        self.assertEqual(resume.would, RecoveryAction.RETRY_ALLOWED)
        self.assertTrue(resume.snapshot_kept)
        self.assertEqual(rollback.would, RecoveryAction.ROLLED_BACK)
        [info] = list_journals(self.config_path)
        self.assertTrue(info.backup_needed)

    def test_a_rollback_with_nothing_replaced_says_it_read_no_snapshot(self) -> None:
        unreplaced = self.journals.begin(
            "sync", "b" * 64, 1, backup_snapshot="machine-a-20260101T000000Z-0123456789ab", machine_id="machine-a",
        )
        unreplaced = self.journals.transition(unreplaced, JournalState.BACKED_UP)
        [info] = list_journals(self.config_path)
        self.assertFalse(info.backup_needed)
        with self._gate():
            no_op = rollback_operation(self.config_path, unreplaced.operation_id, dry_run=True)
        self.assertEqual(no_op.would, RecoveryAction.NOTHING_TO_ROLL_BACK)

    def test_a_resume_does_not_claim_a_backup_that_is_gone(self) -> None:
        operation_id = self._interrupted_sync({"data/a.txt": (b"old", b"new")})
        snapshot = self.journals.load(operation_id).backup_snapshot
        for path in (self.root / "backups").iterdir():
            if path.name.startswith(snapshot):
                if path.is_dir():
                    import shutil

                    shutil.rmtree(path)
                else:
                    path.unlink()
        with self._gate():
            outcome = resume_operation(self.config_path, operation_id, dry_run=False)
        self.assertFalse(outcome.snapshot_kept)
        self.assertNotIn("is kept", outcome.detail)


class RestoreWithoutANameTests(_Base):
    """CS-514."""

    def _snapshot(self, machine: str, side: str, rel: str, content: bytes, age: int) -> str:
        root = self.local if side == "local" else self.cloud
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manager = BackupManager(self.root / "backups", machine)
        manager.backup_file(path, rel, side=side)
        manager.finalize()
        stamp = time.time() - age
        os.utime(self.root / "backups" / manager.snapshot_name, (stamp, stamp))
        return manager.snapshot_name

    def test_only_this_machine_s_snapshots_of_the_target_side_are_taken(self) -> None:
        own = self._snapshot("machine-a", "local", "data/a.txt", b"own local", age=300)
        self._snapshot("machine-a", "cloud", "data/a.txt", b"own cloud", age=200)
        self._snapshot("machine-b", "local", "data/a.txt", b"b's local", age=100)
        (self.local / "data" / "a.txt").write_bytes(b"current")

        with patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
            result = restore_from_backup(self.config_path, None, "local", dry_run=False)
        self.assertEqual(result.snapshot_name, own)
        self.assertEqual((self.local / "data" / "a.txt").read_bytes(), b"own local")

    def test_no_own_snapshot_of_the_side_is_a_refusal_with_a_code(self) -> None:
        self._snapshot("machine-b", "local", "data/a.txt", b"b's local", age=100)
        with patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
            with self.assertRaises(ConfigError) as caught:
                restore_from_backup(self.config_path, None, "local", dry_run=True)
        self.assertIsInstance(caught.exception, CodexSyncError)
        self.assertEqual(caught.exception.code, "NO_OWN_BACKUP_FOR_TARGET")
