"""The sync history (CS-272): what a journal records about a run, and how it is read back.

A history needs no store of its own -- it is the operation journals, read
newest first -- so what is tested here is that the journal carries enough to
describe a run (counts per direction, who started it, when it ended, what
stopped it), that a journal written before those fields existed still loads
and still gates, and that reading the history writes nothing.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import build_context, run_sync
from codexsync.cli import main
from codexsync.exceptions import ConflictError, FailSafeError
from codexsync.mutation_journal import JournalState, JournalStore, MutationJournal
from codexsync.recovery import list_history

from tests.test_recovery import _StoppedGate, _tree, _write_config


class JournalFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"history-journal-{uuid.uuid4().hex}"
        self.store = JournalStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_counts_and_origin_round_trip_and_finish_is_stamped_once_terminal(self) -> None:
        journal = self.store.begin(
            "sync", "a" * 64, 3, counts={"to_cloud": 2, "to_local": 1, "deletions": 0}, origin="window"
        )
        self.assertIsNone(journal.finished_at_utc)
        journal = self.store.transition(journal, JournalState.BACKED_UP)
        self.assertIsNone(journal.finished_at_utc, "a run that has not ended has no end time")
        journal = self.store.transition(journal, JournalState.COMMITTING)
        journal = self.store.transition(journal, JournalState.COMMITTED)

        loaded = self.store.load(journal.operation_id)
        self.assertEqual(loaded, journal)
        self.assertEqual(dict(loaded.counts), {"to_cloud": 2, "to_local": 1, "deletions": 0})
        self.assertEqual(loaded.origin, "window")
        self.assertIsNotNone(loaded.finished_at_utc)
        self.assertGreaterEqual(loaded.finished_at_utc, loaded.created_at_utc)
        self.assertIsNone(loaded.failure)

    def test_failure_records_the_exception_class_never_its_message(self) -> None:
        journal = self.store.begin("sync", "a" * 64, 1)
        secret = "C:/Users/someone/private/project/notes.txt"
        self.store.transition(journal, JournalState.FAILED, failure=ConflictError(secret))

        raw = (self.store.root / f"{journal.operation_id}.json").read_text(encoding="utf-8")
        self.assertNotIn("private", raw, "a journal is payload-free: no message, no path")
        self.assertEqual(self.store.load(journal.operation_id).failure, "ConflictError")

    def test_recovery_required_keeps_its_reason_when_later_closed(self) -> None:
        journal = self.store.begin("sync", "a" * 64, 1)
        journal = self.store.transition(journal, JournalState.BACKED_UP)
        journal = self.store.transition(journal, JournalState.COMMITTING)
        journal = self.store.transition(journal, JournalState.RECOVERY_REQUIRED, failure=OSError("x"))
        self.assertIsNone(journal.finished_at_utc)
        closed = self.store.transition(journal, JournalState.FAILED)
        self.assertEqual(closed.failure, "OSError")
        self.assertIsNotNone(closed.finished_at_utc)

    def test_a_journal_from_an_older_build_loads_and_still_gates(self) -> None:
        self.store.root.mkdir(parents=True)
        operation_id = str(uuid.uuid4())
        (self.store.root / f"{operation_id}.json").write_text(json.dumps({
            "operation_id": operation_id, "family": "sync", "state": "COMMITTING",
            "created_at_utc": "2026-09-19T14:28:34.164361Z", "plan_hash": "a" * 64,
            "action_count": 159, "backup_snapshot": None,
        }), encoding="utf-8")

        loaded = self.store.load(operation_id)
        self.assertIsNone(loaded.counts)
        self.assertIsNone(loaded.origin)
        self.assertIsNone(loaded.finished_at_utc)
        self.assertIsNone(loaded.failure)
        with self.assertRaises(FailSafeError):
            self.store.begin("sync", "b" * 64, 1)

    def test_a_malformed_descriptive_field_is_dropped_not_a_reason_to_block(self) -> None:
        self.store.root.mkdir(parents=True)
        operation_id = str(uuid.uuid4())
        (self.store.root / f"{operation_id}.json").write_text(json.dumps({
            "operation_id": operation_id, "family": "sync", "state": "COMMITTED",
            "created_at_utc": "2026-09-19T14:28:34.164361Z", "plan_hash": "a" * 64,
            "action_count": 2, "backup_snapshot": None,
            "counts": {"to_cloud": "two", "to_local": 1, "deletions": True},
            "origin": 7, "finished_at_utc": "", "failure": ["x"],
        }), encoding="utf-8")

        loaded = self.store.load(operation_id)
        self.assertEqual(dict(loaded.counts), {"to_local": 1})
        self.assertIsNone(loaded.origin)
        self.assertIsNone(loaded.finished_at_utc)
        self.assertIsNone(loaded.failure)
        self.assertEqual(self.store.non_terminal(), [], "the journal is readable, so nothing is blocked")


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"history-{uuid.uuid4().hex}"
        (self.root / "local-state" / "data").mkdir(parents=True)
        (self.root / "cloud" / "data").mkdir(parents=True)
        self.config_path = _write_config(self.root)
        self.journals = JournalStore(self.root / ".tmp")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _journal(self, family: str, state: JournalState, created: str) -> MutationJournal:
        written = MutationJournal(str(uuid.uuid4()), family, state, created, "a" * 64, 1)
        self.journals.write(written)
        return written

    def test_newest_first_by_time_alone_and_filtered_by_family(self) -> None:
        old = self._journal("sync", JournalState.COMMITTED, "2026-01-01T00:00:00.000000Z")
        interrupted = self._journal("sync", JournalState.RECOVERY_REQUIRED, "2026-01-02T00:00:00.000000Z")
        sessions = self._journal("sessions", JournalState.COMMITTED, "2026-01-03T00:00:00.000000Z")
        new = self._journal("sync", JournalState.FAILED, "2026-01-04T00:00:00.000000Z")

        self.assertEqual(
            [item.operation_id for item in list_history(self.config_path, family="sync")],
            # Unlike the recovery listing, an open journal is not put first.
            [new.operation_id, interrupted.operation_id, old.operation_id],
        )
        self.assertEqual(
            [item.operation_id for item in list_history(self.config_path)],
            [new.operation_id, sessions.operation_id, interrupted.operation_id, old.operation_id],
        )
        self.assertEqual(
            [item.operation_id for item in list_history(self.config_path, family="sync", limit=2)],
            [new.operation_id, interrupted.operation_id],
        )

    def test_reading_the_history_writes_nothing(self) -> None:
        self._journal("sync", JournalState.COMMITTED, "2026-01-01T00:00:00.000000Z")
        before = _tree(self.root)
        list_history(self.config_path, family="sync")
        self.assertEqual(_tree(self.root), before)

    def test_no_journals_is_an_empty_history_and_creates_nothing(self) -> None:
        self.assertEqual(list_history(self.config_path), [])
        self.assertFalse((self.root / ".tmp").exists())

    def test_a_real_run_records_its_counts_origin_and_end(self) -> None:
        local = self.root / "local-state" / "data" / "a.txt"
        local.write_text("new", encoding="utf-8")
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
        run_sync(ctx, dry_run=False, origin="window")

        (run,) = list_history(self.config_path, family="sync")
        self.assertEqual(run.state, "COMMITTED")
        self.assertEqual(dict(run.counts), {"to_cloud": 1, "to_local": 0, "deletions": 0})
        self.assertEqual(run.origin, "window")
        self.assertIsNotNone(run.finished_at_utc)
        self.assertIsNone(run.failure)

    def test_a_dry_run_leaves_no_history(self) -> None:
        (self.root / "local-state" / "data" / "a.txt").write_text("new", encoding="utf-8")
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
        run_sync(ctx, dry_run=True, origin="window")
        self.assertEqual(list_history(self.config_path), [])

    def test_a_failed_run_names_what_stopped_it(self) -> None:
        (self.root / "local-state" / "data" / "a.txt").write_text("new", encoding="utf-8")
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config_path, enforce_safety=True)
        with patch(
            "codexsync.sync_engine.SyncEngine._replace_staged",
            side_effect=PermissionError("C:/secret/path is locked"),
        ):
            with self.assertRaises(PermissionError):
                run_sync(ctx, dry_run=False, origin="cli")

        (run,) = list_history(self.config_path, family="sync")
        self.assertEqual(run.state, "RECOVERY_REQUIRED")
        self.assertEqual(run.failure, "PermissionError")
        self.assertEqual(run.origin, "cli")

    def test_cli_history_prints_runs_and_json(self) -> None:
        written = self.journals.begin(
            "sync", "a" * 64, 2, counts={"to_cloud": 2, "to_local": 0, "deletions": 0}, origin="unattended"
        )
        self.journals.transition(written, JournalState.FAILED, failure=ConflictError("x"))
        self._journal("sessions", JournalState.COMMITTED, "2026-01-01T00:00:00.000000Z")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["-c", str(self.config_path), "history"]), 0)
        text = out.getvalue()
        self.assertIn("FAILED (ConflictError)", text)
        self.assertIn("origin=unattended", text)
        self.assertIn("to_cloud=2", text)
        self.assertNotIn("sessions", text, "the default family is sync")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["-c", str(self.config_path), "history", "--family", "all", "--json"]), 0)
        records = json.loads(out.getvalue())
        self.assertEqual([record["family"] for record in records], ["sync", "sessions"])
        self.assertEqual(records[0]["counts"], {"deletions": 0, "to_cloud": 2, "to_local": 0})


if __name__ == "__main__":
    unittest.main()
