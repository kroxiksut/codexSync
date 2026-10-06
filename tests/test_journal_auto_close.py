"""A journal that provably replaced nothing no longer blocks every later sync.

On 2026-10-06 a sync from the window stopped on `WinError 5`: Yandex.Disk held
the journal it was uploading, the `PREPARED -> BACKED_UP` write was refused,
and so was the write that would have closed it as `FAILED`. The journal stayed
`PREPARED` -- which proves nothing was replaced -- and still blocked every
later mutation until a person ran `recover`.

`JournalStore.begin` now closes such a journal itself, but only this
machine's, only before the commit phase, and only for a caller holding this
machine's operation lock. Everything else keeps blocking, now as
`RecoveryPendingError`, which names the journal so the window can open it.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.app import commit_global_state
from codexsync.cli import main
from codexsync.config import load_config
from codexsync.exceptions import FailSafeError, RecoveryPendingError
from codexsync.mutation_journal import (
    ABANDONED,
    JournalState,
    JournalStore,
    owned_by,
    snapshot_belongs_to,
)
from codexsync.recovery import list_journals
from codexsync.safety_gate import OperationKind

try:
    from tests.test_recovery import _StoppedGate, _write_config
    from tests.test_recovery_rollback import _state
except ImportError:  # collected with tests/ itself on sys.path
    from test_recovery import _StoppedGate, _write_config
    from test_recovery_rollback import _state

OWN_SNAPSHOT = "machine-a-20261006T003412Z-5221274ffced.zip"
PEER_SNAPSHOT = "krox-nout-20261006T003412Z-5221274ffced.zip"


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"journal-auto-close-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = JournalStore(self.root / ".tmp")

    def _stuck(self, state: JournalState, *, machine: str | None, snapshot: str | None) -> str:
        """A journal left open at ``state``, as an interrupted run leaves it.

        Written directly: ``begin`` would close or refuse the ones before it.
        """
        operation_id = str(uuid.uuid4())
        raw = {
            "operation_id": operation_id, "family": "sessions", "state": state.value,
            "created_at_utc": "2026-10-06T00:34:12.803179Z", "plan_hash": "a" * 64,
            "action_count": 11, "backup_snapshot": snapshot, "origin": "window",
        }
        if machine is not None:  # an older build wrote no machine at all
            raw["machine_id"] = machine
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / f"{operation_id}.json").write_text(json.dumps(raw), encoding="utf-8")
        return operation_id


class BeginClosesAbandonedJournalsTests(_Store):
    def test_own_journal_stopped_before_the_commit_phase_is_closed(self) -> None:
        for state in (JournalState.PREPARED, JournalState.BACKED_UP):
            with self.subTest(state=state):
                stuck = self._stuck(state, machine="machine-a", snapshot=OWN_SNAPSHOT)
                fresh = self.store.begin("sync", "b" * 64, 1, machine_id="machine-a")
                closed = self.store.load(stuck)
                self.assertEqual(closed.state, JournalState.FAILED)
                self.assertEqual(closed.failure, ABANDONED)
                self.assertIsNotNone(closed.finished_at_utc)
                self.assertEqual(fresh.machine_id, "machine-a")
                self.store.transition(fresh, JournalState.FAILED)

    def test_the_reported_journal_of_an_older_build_is_closed_by_its_snapshot_name(self) -> None:
        stuck = self._stuck(JournalState.PREPARED, machine=None, snapshot=OWN_SNAPSHOT)
        self.assertIsNone(self.store.load(stuck).machine_id)
        self.store.begin("sessions", "b" * 64, 1, machine_id="machine-a")
        self.assertEqual(self.store.load(stuck).state, JournalState.FAILED)

    def test_a_journal_that_entered_the_commit_phase_still_blocks(self) -> None:
        for state in (JournalState.COMMITTING, JournalState.RECOVERY_REQUIRED):
            with self.subTest(state=state):
                stuck = self._stuck(state, machine="machine-a", snapshot=OWN_SNAPSHOT)
                with self.assertRaises(RecoveryPendingError) as caught:
                    self.store.begin("sync", "b" * 64, 1, machine_id="machine-a")
                self.assertEqual(caught.exception.details["operation_id"], stuck)
                self.assertEqual(caught.exception.details["state"], state.value)
                self.assertEqual(caught.exception.code, "RECOVERY_PENDING")
                self.assertEqual(self.store.load(stuck).state, state, "left for a person")
                (self.store.root / f"{stuck}.json").unlink()

    def test_another_machines_journal_is_never_closed_here(self) -> None:
        recorded = self._stuck(JournalState.PREPARED, machine="krox-nout", snapshot=PEER_SNAPSHOT)
        with self.assertRaises(RecoveryPendingError) as caught:
            self.store.begin("sync", "b" * 64, 1, machine_id="machine-a")
        self.assertEqual(caught.exception.details["machine"], "krox-nout")
        self.assertEqual(self.store.load(recorded).state, JournalState.PREPARED)

    def test_an_older_journal_proving_no_owner_is_never_closed(self) -> None:
        for snapshot in (PEER_SNAPSHOT, None, "machine-a-not-a-snapshot"):
            with self.subTest(snapshot=snapshot):
                stuck = self._stuck(JournalState.PREPARED, machine=None, snapshot=snapshot)
                with self.assertRaises(RecoveryPendingError):
                    self.store.begin("sync", "b" * 64, 1, machine_id="machine-a")
                self.assertEqual(self.store.load(stuck).state, JournalState.PREPARED)
                (self.store.root / f"{stuck}.json").unlink()

    def test_begin_without_a_machine_closes_nothing(self) -> None:
        stuck = self._stuck(JournalState.PREPARED, machine="machine-a", snapshot=OWN_SNAPSHOT)
        with self.assertRaises(FailSafeError):
            self.store.begin("sync", "b" * 64, 1)
        self.assertEqual(self.store.load(stuck).state, JournalState.PREPARED)

    def test_an_unreadable_journal_still_blocks(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / f"{uuid.uuid4()}.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(FailSafeError):
            self.store.begin("sync", "b" * 64, 1, machine_id="machine-a")

    def test_ownership_rules(self) -> None:
        self.assertTrue(snapshot_belongs_to(OWN_SNAPSHOT, "machine-a"))
        self.assertTrue(snapshot_belongs_to(OWN_SNAPSHOT.removesuffix(".zip"), "machine-a"))
        self.assertFalse(snapshot_belongs_to(OWN_SNAPSHOT, "machine"), "a prefix is not a machine")
        self.assertFalse(snapshot_belongs_to(PEER_SNAPSHOT, "machine-a"))
        self.assertFalse(snapshot_belongs_to(None, "machine-a"))
        stuck = self._stuck(JournalState.PREPARED, machine="krox-nout", snapshot=OWN_SNAPSHOT)
        journal = self.store.load(stuck)
        self.assertFalse(owned_by(journal, "machine-a"), "a recorded machine outranks the snapshot name")
        self.assertTrue(owned_by(journal, "krox-nout"))


class EnvelopeAndListingTests(_Store):
    def setUp(self) -> None:
        super().setUp()
        self.local = self.root / "local-state"
        (self.local / "data").mkdir(parents=True)
        (self.root / "cloud" / "data").mkdir(parents=True)
        self.config_path = _write_config(self.root)
        self.cfg = load_config(self.config_path)
        self.store = JournalStore(self.cfg.paths.temp_dir)

    def test_a_mutation_runs_past_an_abandoned_journal_of_this_machine(self) -> None:
        stuck = self._stuck(JournalState.PREPARED, machine=None, snapshot=OWN_SNAPSHOT)
        state_file = self.local / ".codex-global-state.json"
        original = _state(self.root.as_posix())
        candidate = _state(self.root.as_posix(), order=["p-beta", "p-alpha"])
        state_file.write_bytes(original)
        commit_global_state(
            self.cfg, _StoppedGate(), OperationKind.CHAT_MOVE,
            family="chats", plan_id="c" * 64, action_count=1, state_root=self.local,
            source=state_file, original=original, candidate=candidate,
        )
        self.assertEqual(state_file.read_bytes(), candidate)
        self.assertEqual(self.store.load(stuck).failure, ABANDONED)
        self.assertEqual(self.store.non_terminal(), [])

    def test_listing_says_which_journal_closes_itself(self) -> None:
        own = self._stuck(JournalState.PREPARED, machine="machine-a", snapshot=OWN_SNAPSHOT)
        peer = self._stuck(JournalState.PREPARED, machine="krox-nout", snapshot=PEER_SNAPSHOT)
        committing = self._stuck(JournalState.COMMITTING, machine="machine-a", snapshot=OWN_SNAPSHOT)
        by_id = {item.operation_id: item for item in list_journals(self.config_path)}
        self.assertTrue(by_id[own].own and by_id[own].closes_itself)
        self.assertFalse(by_id[peer].own or by_id[peer].closes_itself)
        self.assertEqual(by_id[peer].machine_id, "krox-nout")
        self.assertTrue(by_id[committing].own)
        self.assertFalse(by_id[committing].closes_itself)

    def test_recover_list_prints_open_journals_and_what_closes_them(self) -> None:
        own = self._stuck(JournalState.PREPARED, machine="machine-a", snapshot=OWN_SNAPSHOT)
        peer = self._stuck(JournalState.BACKED_UP, machine="krox-nout", snapshot=PEER_SNAPSHOT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["-c", str(self.config_path), "recover", "list"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn(own, text)
        self.assertIn(peer, text)
        self.assertIn("closes it by itself", text)
        self.assertIn("another machine's run", text)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["-c", str(self.config_path), "recover", "list", "--json"])
        rows = {row["operation_id"]: row for row in json.loads(out.getvalue())}
        self.assertTrue(rows[own]["closes_itself"])
        self.assertEqual(rows[peer]["machine_id"], "krox-nout")

    def test_recover_list_with_nothing_open(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["-c", str(self.config_path), "recover", "list"])
        self.assertEqual(code, 0)
        self.assertIn("No open journals", out.getvalue())


if __name__ == "__main__":
    unittest.main()
