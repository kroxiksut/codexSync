"""What the mutation envelope leaves behind when it fails, and how rollback undoes it.

Code-review findings of 2026-09-27, each one a way the recovery protection
either stranded the user or restored the wrong thing:

- CS-291: `commit_global_state` interrupted before the replace left its
  journal open, so every later mutation exited 5 for a write that never
  happened.
- CS-292: `recover rollback` closed the journal and only then found that the
  restore refused the global state and session files, releasing the block
  with nothing restored.
- CS-293: a bidirectional sync backs up both sides into one snapshot, and a
  rollback into one `--target` wrote the cloud side's old files into `.codex`.
- CS-294: a snapshot the journal names but that is gone was read as
  "nothing was overwritten".
- CS-296: a leftover `<id>.tmp` made every later transition of that journal
  fail with FileExistsError.
- CS-304 (second half): the global state is re-hashed right before the
  replace, so a file that moved since it was read is never overwritten.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import build_context, commit_global_state, run_sync
from codexsync.backup import BackupManager
from codexsync.config import load_config
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.recovery import RecoveryAction, list_journals, rollback_operation
from codexsync.safety_gate import OperationKind

try:
    from tests.test_recovery import _StoppedGate, _write_config
except ImportError:  # collected with tests/ itself on sys.path
    from test_recovery import _StoppedGate, _write_config


class _GateFailingAt(_StoppedGate):
    """Stopped until the `fail_at`-th final check, which raises (or runs `effect`)."""

    def __init__(self, fail_at: int, effect=None) -> None:
        self.fail_at = fail_at
        self.effect = effect
        self.finals = 0

    def require(self, operation, *, final: bool = False):
        if final:
            self.finals += 1
            if self.finals == self.fail_at:
                if self.effect is not None:
                    self.effect()
                else:
                    raise SafetyPreconditionError("Codex started")
        return self.check(operation, final=final)


def _state(root: str, *, order: list[str] | None = None) -> bytes:
    state = {
        "local-projects": {
            "p-alpha": {"id": "p-alpha", "name": "alpha", "rootPaths": [root + "/alpha"], "createdAt": 1, "updatedAt": 2},
            "p-beta": {"id": "p-beta", "name": "beta", "rootPaths": [root + "/beta"], "createdAt": 3, "updatedAt": 4},
        },
        "project-order": order if order is not None else ["p-alpha", "p-beta"],
        "thread-project-assignments": {},
        "app-server-project-id-by-legacy-project-id-by-host": {},
    }
    return (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8")


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"rollback-{uuid.uuid4().hex}"
        self.local = self.root / "local-state"
        self.cloud = self.root / "cloud"
        (self.local / "data").mkdir(parents=True)
        (self.cloud / "data").mkdir(parents=True)
        self.config_path = _write_config(self.root)
        self.cfg = load_config(self.config_path)
        self.journals = JournalStore(self.root / ".tmp")
        self.state_file = self.local / ".codex-global-state.json"
        self.addCleanup(shutil.rmtree, self.root, True)

    def _recovery_gate(self):
        return patch("codexsync.recovery._make_safety_gate", return_value=_StoppedGate())

    def _only_journal(self):
        [path] = list(self.journals.root.glob("*.json"))
        return self.journals.load(path.stem)


class CommitGlobalStateFailureTests(_Sandbox):
    """CS-291 and the re-hash half of CS-304."""

    def setUp(self) -> None:
        super().setUp()
        self.original = _state(self.root.as_posix())
        self.state_file.write_bytes(self.original)
        self.candidate = _state(self.root.as_posix(), order=["p-beta", "p-alpha"])

    def _commit(self, gate, original: bytes | None = None) -> int:
        return commit_global_state(
            self.cfg, gate, OperationKind.CHAT_MOVE,
            family="chats", plan_id="a" * 64, action_count=1, state_root=self.local,
            source=self.state_file, original=self.original if original is None else original,
            candidate=self.candidate,
        )

    def test_codex_starting_before_the_commit_phase_closes_the_journal(self) -> None:
        with self.assertRaises(SafetyPreconditionError):
            self._commit(_GateFailingAt(1))
        journal = self._only_journal()
        self.assertEqual(journal.state, JournalState.FAILED)
        self.assertEqual(journal.failure, "SafetyPreconditionError")
        self.assertEqual(self.journals.non_terminal(), [], "nothing was replaced, so nothing may block")
        self.assertEqual(self.state_file.read_bytes(), self.original)

    def test_codex_starting_inside_the_commit_phase_closes_it_through_recovery_required(self) -> None:
        with self.assertRaises(SafetyPreconditionError):
            self._commit(_GateFailingAt(2))
        journal = self._only_journal()
        self.assertEqual(journal.state, JournalState.FAILED)
        self.assertEqual(self.journals.non_terminal(), [])
        self.assertEqual(self.state_file.read_bytes(), self.original)
        self.assertEqual(
            [p for p in self.local.iterdir() if p.name.endswith(".tmp")], [], "the staged candidate is removed"
        )

    def test_a_backup_that_cannot_be_written_closes_the_journal(self) -> None:
        with patch.object(BackupManager, "backup_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._commit(_StoppedGate())
        self.assertEqual(self._only_journal().state, JournalState.FAILED)
        self.assertEqual(self.journals.non_terminal(), [])

    def test_a_state_that_moved_after_it_was_read_is_never_overwritten(self) -> None:
        moved = _state(self.root.as_posix(), order=["p-alpha"])

        def codex_writes() -> None:
            self.state_file.write_bytes(moved)

        with self.assertRaises(FailSafeError):
            self._commit(_GateFailingAt(2, effect=codex_writes))
        self.assertEqual(self.state_file.read_bytes(), moved, "the newer state survives")
        self.assertEqual(self._only_journal().state, JournalState.FAILED)
        self.assertEqual(self.journals.non_terminal(), [])

    def _locked_state_file(self, failures: int | None, during_wait=None):
        """`os.replace` onto the state file is refused the way a held handle refuses it."""
        real_replace = os.replace
        calls = {"n": 0}

        def replace(src, dst):
            if Path(dst) != self.state_file:
                return real_replace(src, dst)
            calls["n"] += 1
            if failures is None or calls["n"] <= failures:
                if during_wait is not None:
                    during_wait()
                exc = PermissionError(13, "Access is denied")
                exc.winerror = 5
                raise exc
            return real_replace(src, dst)

        return patch("os.replace", side_effect=replace), calls

    def test_a_lock_on_the_state_file_is_waited_out(self) -> None:
        locked, calls = self._locked_state_file(failures=2)
        with locked, patch("time.sleep"):
            self._commit(_StoppedGate())
        self.assertEqual(calls["n"], 3)
        self.assertEqual(self.state_file.read_bytes(), self.candidate)
        self.assertEqual(self._only_journal().state, JournalState.COMMITTED)

    def test_a_state_that_moves_while_a_lock_is_waited_out_is_never_overwritten(self) -> None:
        moved = _state(self.root.as_posix(), order=["p-alpha"])

        def codex_writes() -> None:
            self.state_file.write_bytes(moved)

        locked, calls = self._locked_state_file(failures=None, during_wait=codex_writes)
        with locked, patch("time.sleep"):
            with self.assertRaises(FailSafeError):
                self._commit(_StoppedGate())
        self.assertEqual(calls["n"], 1, "the retry re-reads the file before trying again")
        self.assertEqual(self.state_file.read_bytes(), moved, "the newer state survives")
        self.assertEqual(self.journals.non_terminal(), [])


class GlobalStateRollbackTests(_Sandbox):
    """CS-292: the global state goes back through `commit_global_state`."""

    def _interrupted(self, family: str, before: bytes, after: bytes) -> str:
        """A commit that replaced the file and died before closing its journal."""
        self.state_file.write_bytes(before)
        manager = BackupManager(self.root / "backups", "machine-a")
        journal = self.journals.begin(family, "a" * 64, 1, backup_snapshot=manager.snapshot_name)
        manager.backup_file(self.state_file, self.state_file.name, side="local")
        manager.finalize()
        journal = self.journals.transition(journal, JournalState.BACKED_UP)
        journal = self.journals.transition(journal, JournalState.COMMITTING)
        self.state_file.write_bytes(after)
        self.journals.transition(journal, JournalState.RECOVERY_REQUIRED)
        return journal.operation_id

    def test_rollback_puts_the_global_state_back_and_closes_the_journal(self) -> None:
        before = _state(self.root.as_posix())
        after = _state(self.root.as_posix(), order=["p-beta", "p-alpha"])
        operation_id = self._interrupted("chats", before, after)
        [info] = list_journals(self.config_path)
        self.assertTrue(info.can_rollback)
        self.assertIsNone(info.rollback_refusal)

        with self._recovery_gate():
            preview = rollback_operation(self.config_path, operation_id, dry_run=True)
        self.assertEqual(preview.action, RecoveryAction.WOULD_RECOVER)
        self.assertEqual(self.state_file.read_bytes(), after)
        self.assertEqual(len(self.journals.non_terminal()), 1)

        with self._recovery_gate():
            outcome = rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(outcome.action, RecoveryAction.ROLLED_BACK)
        self.assertEqual(outcome.restored_files, 1)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(self.journals.load(operation_id).state, JournalState.FAILED)
        self.assertEqual(self.journals.non_terminal(), [])
        families = sorted(item.family for item in list_journals(self.config_path))
        self.assertEqual(families, ["chats", "restore"], "the rollback's own write is journalled")

    def test_an_invalid_snapshot_state_is_refused_before_the_journal_is_closed(self) -> None:
        operation_id = self._interrupted("repair", b"{not json", _state(self.root.as_posix()))
        with self._recovery_gate(), self.assertRaises(FailSafeError):
            rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(self.journals.load(operation_id).state, JournalState.RECOVERY_REQUIRED)

    def test_a_cloud_target_is_refused_for_the_global_state(self) -> None:
        operation_id = self._interrupted("chats", _state(self.root.as_posix()), _state(self.root.as_posix(), order=["p-beta", "p-alpha"]))
        with self._recovery_gate(), self.assertRaises(ConfigError):
            rollback_operation(self.config_path, operation_id, target="cloud", dry_run=False)
        self.assertEqual(len(self.journals.non_terminal()), 1)

    def test_a_session_transfer_is_refused_up_front_and_the_listing_says_so(self) -> None:
        manager = BackupManager(self.root / "backups", "machine-a")
        session = self.local / "sessions" / "rollout-x.jsonl"
        session.parent.mkdir(parents=True)
        session.write_bytes(b'{"type":"session_meta"}\n')
        journal = self.journals.begin("sessions", "a" * 64, 1, backup_snapshot=manager.snapshot_name)
        manager.backup_file(session, "sessions/rollout-x.jsonl", side="local")
        manager.finalize()
        journal = self.journals.transition(journal, JournalState.BACKED_UP)
        self.journals.transition(journal, JournalState.COMMITTING)

        [info] = list_journals(self.config_path)
        self.assertFalse(info.can_rollback)
        self.assertIn("recover resume", info.rollback_refusal or "")
        self.assertTrue(info.can_resume)
        for dry_run in (True, False):
            with self._recovery_gate(), self.assertRaises(FailSafeError):
                rollback_operation(self.config_path, journal.operation_id, dry_run=dry_run)
        self.assertEqual(len(self.journals.non_terminal()), 1, "the block survives a refusal")


class SyncRollbackBySideTests(_Sandbox):
    """CS-293 and CS-294 on a real, interrupted bidirectional sync."""

    def _interrupt_bidirectional_sync(self) -> str:
        now = time.time_ns()
        old, new = now - 2_000_000_000, now - 1_000_000_000
        # a.txt: local newer -> cloud is backed up. b.txt: cloud newer -> local is backed up.
        for path, text, ns in (
            (self.local / "data" / "a.txt", "a-local-new", new),
            (self.cloud / "data" / "a.txt", "a-cloud-old", old),
            (self.local / "data" / "b.txt", "b-local-old", old),
            (self.cloud / "data" / "b.txt", "b-cloud-new", new),
        ):
            path.write_text(text, encoding="utf-8")
            os.utime(path, ns=(ns, ns))
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
        self.assertEqual((len(ctx.plan.to_local), len(ctx.plan.to_cloud)), (1, 1))
        with patch("codexsync.sync_engine.SyncEngine._replace_staged", side_effect=OSError("power cut")):
            with self.assertRaises(OSError):
                run_sync(ctx, dry_run=False)
        [pending] = self.journals.non_terminal()
        self.assertEqual(pending.state, JournalState.RECOVERY_REQUIRED)
        # Both replaces had happened before the crash.
        (self.cloud / "data" / "a.txt").write_text("a-local-new", encoding="utf-8")
        (self.local / "data" / "b.txt").write_text("b-cloud-new", encoding="utf-8")
        return pending.operation_id

    def _snapshot_dir(self, operation_id: str) -> Path:
        return self.root / "backups" / self.journals.load(operation_id).backup_snapshot

    def test_each_file_goes_back_to_the_side_it_was_backed_up_from(self) -> None:
        operation_id = self._interrupt_bidirectional_sync()
        manifest = json.loads(
            (self.root / "backups" / (self._snapshot_dir(operation_id).name + ".manifest.json")).read_text("utf-8")
        )
        self.assertEqual(
            {entry["relative_path"]: entry["side"] for entry in manifest["entries"]},
            {"data/a.txt": "cloud", "data/b.txt": "local"},
        )

        with self._recovery_gate():
            outcome = rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(outcome.action, RecoveryAction.ROLLED_BACK)
        self.assertEqual(outcome.restored_files, 2)
        self.assertEqual((self.cloud / "data" / "a.txt").read_text("utf-8"), "a-cloud-old")
        self.assertEqual((self.local / "data" / "b.txt").read_text("utf-8"), "b-local-old")
        # Nothing crossed sides: each source is exactly what it was.
        self.assertEqual((self.local / "data" / "a.txt").read_text("utf-8"), "a-local-new")
        self.assertEqual((self.cloud / "data" / "b.txt").read_text("utf-8"), "b-cloud-new")
        self.assertEqual(self.journals.non_terminal(), [])

    def test_a_target_naming_one_side_of_a_two_sided_snapshot_is_refused(self) -> None:
        operation_id = self._interrupt_bidirectional_sync()
        for target in ("local", "cloud"):
            with self.subTest(target=target), self._recovery_gate(), self.assertRaises(ConfigError):
                rollback_operation(self.config_path, operation_id, target=target, dry_run=True)
        self.assertEqual((self.local / "data" / "b.txt").read_text("utf-8"), "b-cloud-new")
        self.assertEqual(len(self.journals.non_terminal()), 1)

    def test_an_older_snapshot_without_sides_is_refused_for_a_sync(self) -> None:
        operation_id = self._interrupt_bidirectional_sync()
        manifest_path = self.root / "backups" / (self._snapshot_dir(operation_id).name + ".manifest.json")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        for entry in manifest["entries"]:
            entry.pop("side")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        [info] = list_journals(self.config_path)
        self.assertFalse(info.can_rollback)
        with self._recovery_gate(), self.assertRaises(FailSafeError):
            rollback_operation(self.config_path, operation_id, target="local", dry_run=False)
        self.assertEqual((self.local / "data" / "b.txt").read_text("utf-8"), "b-cloud-new")
        self.assertEqual(len(self.journals.non_terminal()), 1)

    def test_a_recorded_snapshot_that_is_gone_is_never_nothing_to_roll_back(self) -> None:
        """CS-294: another machine's retention may have removed it."""
        operation_id = self._interrupt_bidirectional_sync()
        snapshot = self._snapshot_dir(operation_id)
        shutil.rmtree(snapshot)
        (snapshot.parent / (snapshot.name + ".manifest.json")).unlink()

        [info] = list_journals(self.config_path)
        self.assertIs(info.backup_snapshot_present, False)
        self.assertFalse(info.can_rollback)
        self.assertTrue(info.can_resume)
        with self._recovery_gate(), self.assertRaises(FailSafeError):
            rollback_operation(self.config_path, operation_id, dry_run=False)
        self.assertEqual(self.journals.load(operation_id).state, JournalState.RECOVERY_REQUIRED)


class JournalTempFileTests(_Sandbox):
    """CS-296."""

    def test_a_leftover_temp_file_does_not_block_the_journal(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1)
        (self.journals.root / f"{journal.operation_id}.tmp").write_text("{", encoding="utf-8")
        closed = self.journals.transition(journal, JournalState.FAILED)
        self.assertEqual(self.journals.load(journal.operation_id), closed)

    def test_a_failed_write_leaves_no_temp_file_and_the_next_one_works(self) -> None:
        journal = self.journals.begin("sync", "a" * 64, 1)
        with patch("codexsync.mutation_journal.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.journals.transition(journal, JournalState.BACKED_UP)
        self.assertEqual(list(self.journals.root.glob("*.tmp")), [])
        self.assertEqual(self.journals.transition(journal, JournalState.BACKED_UP).state, JournalState.BACKED_UP)


if __name__ == "__main__":
    unittest.main()
