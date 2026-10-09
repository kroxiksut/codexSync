"""Checking whether Codex can start, and repairing what codexSync can (D-032).

On 2026-10-09 Codex stopped starting on the machine this was built on: a sync
had asked it to rebuild its chat list, the rebuild was ended part-way, and
``backfill_state`` stayed ``running``. Every start then waited 30 s for a
rebuild nobody was doing and exited. These tests hold the check that finds
that state and the repair that puts it back.
"""
from __future__ import annotations

import sqlite3
import unittest

from codexsync.app import check_codex, list_history, refresh_thread_catalogue, repair_codex
from codexsync.codex_health import (
    CATALOGUE_MISSES_CHATS,
    CATALOGUE_REBUILD_PENDING,
    CATALOGUE_REBUILD_STUCK,
    CATALOGUE_REBUILD_UNDETERMINED,
    CATALOGUE_REBUILDING,
    CATALOGUE_UNREADABLE,
    FIX_CATALOGUE_REBUILD,
    GLOBAL_STATE_INVALID,
    GLOBAL_STATE_MISSING,
    SOURCE_AS_FOUND,
    SOURCE_OWN_BACKUP,
    ChatFiles,
    Severity,
    build_settle_plan,
    diagnose_catalogue,
    diagnose_global_state,
    measure_chat_files,
)
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.safety_gate import ProcessState
from codexsync.sqlite_audit import BackfillReading, PlacementStatus, read_backfill_state
from codexsync.thread_catalogue import BACKFILL_COMPLETE, BACKFILL_PENDING, settle_backfill

try:
    from tests.test_handoff import _Workspace
    from tests.test_thread_catalogue import KNOWN, NEW, catalogue, chat
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace
    from test_thread_catalogue import KNOWN, NEW, catalogue, chat


def reading(status: str | None, availability: PlacementStatus = PlacementStatus.AVAILABLE) -> BackfillReading:
    return BackfillReading(
        availability, "state_5.sqlite", status,
        last_watermark="sessions/2026/07/31/x.jsonl", last_success_at=None, updated_at=1791524862,
    )


def full_row(database) -> tuple:
    connection = sqlite3.connect(database)
    try:
        return tuple(connection.execute(
            "SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state WHERE id = 1"
        ).fetchone())
    finally:
        connection.close()


def set_row(database, status: str, watermark: str | None = "sessions/2026/07/31/x.jsonl") -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE backfill_state SET status = ?, last_watermark = ?, last_success_at = NULL, updated_at = 1791524862",
            (status, watermark),
        )
        connection.commit()
    finally:
        connection.close()


FILES = ChatFiles(306, 900 * 1024 * 1024)


class DiagnoseTests(unittest.TestCase):
    def codes(self, status, process, **kwargs):
        return [(item.code, item.severity, item.fix) for item in diagnose_catalogue(reading(status), process, FILES, **kwargs)]

    def test_a_rebuild_marked_running_with_codex_closed_is_why_codex_does_not_start(self) -> None:
        self.assertEqual(
            self.codes("running", ProcessState.STOPPED),
            [(CATALOGUE_REBUILD_STUCK, Severity.BROKEN, FIX_CATALOGUE_REBUILD)],
        )

    def test_a_rebuild_while_codex_runs_is_left_to_codex(self) -> None:
        self.assertEqual(self.codes("running", ProcessState.RUNNING), [(CATALOGUE_REBUILDING, Severity.NOTE, None)])
        self.assertEqual(self.codes("pending", ProcessState.RUNNING), [(CATALOGUE_REBUILDING, Severity.NOTE, None)])

    def test_an_undetermined_process_never_counts_as_closed(self) -> None:
        self.assertEqual(
            self.codes("running", ProcessState.UNKNOWN),
            [(CATALOGUE_REBUILD_UNDETERMINED, Severity.WARNING, None)],
        )

    def test_a_pending_rebuild_is_a_warning_with_its_size(self) -> None:
        found = diagnose_catalogue(reading("pending"), ProcessState.STOPPED, FILES)
        self.assertEqual([item.code for item in found], [CATALOGUE_REBUILD_PENDING])
        self.assertEqual(found[0].counts, {"chats": 306, "megabytes": 900})
        self.assertEqual(found[0].fix, FIX_CATALOGUE_REBUILD)

    def test_a_complete_catalogue_reports_only_what_it_misses(self) -> None:
        self.assertEqual(self.codes(BACKFILL_COMPLETE, ProcessState.STOPPED), [])
        found = diagnose_catalogue(reading(BACKFILL_COMPLETE), ProcessState.STOPPED, FILES, unnamed=3)
        self.assertEqual([(item.code, item.command) for item in found], [(CATALOGUE_MISSES_CHATS, "sessions catalogue")])

    def test_no_catalogue_is_nothing_and_an_unreadable_one_is_said(self) -> None:
        self.assertEqual(diagnose_catalogue(reading(None, PlacementStatus.ABSENT), ProcessState.STOPPED, FILES), [])
        found = diagnose_catalogue(reading(None, PlacementStatus.INDETERMINATE), ProcessState.STOPPED, FILES)
        self.assertEqual([item.code for item in found], [CATALOGUE_UNREADABLE])

    def test_the_global_state(self) -> None:
        self.assertEqual([item.code for item in diagnose_global_state(present=False, valid=None)], [GLOBAL_STATE_MISSING])
        self.assertEqual([item.code for item in diagnose_global_state(present=True, valid=False)], [GLOBAL_STATE_INVALID])
        self.assertEqual(diagnose_global_state(present=True, valid=True), [])


class SettlePlanTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.codex = self.root / "codex"
        self.codex.mkdir(parents=True)
        self.database = catalogue(self.codex, {})

    def test_the_row_as_found_is_kept_except_status_and_watermark_and_the_id_is_stable(self) -> None:
        set_row(self.database, "running")
        first = build_settle_plan(self.codex, read_backfill_state(self.codex))
        second = build_settle_plan(self.codex, read_backfill_state(self.codex))
        self.assertTrue(first.writes)
        self.assertEqual(first.plan_id, second.plan_id, "a preview and its apply hash the same")
        self.assertEqual(first.source, SOURCE_AS_FOUND)
        self.assertEqual(first.target, (BACKFILL_COMPLETE, None, 1791524862, 1791524862))

    def test_codexsyncs_own_copy_of_the_row_wins_when_it_says_complete(self) -> None:
        set_row(self.database, "running")
        own = ("machine-a-20261009T003258Z-768894632e07", (BACKFILL_COMPLETE, None, 1773067086, 1773067086))
        plan = build_settle_plan(self.codex, read_backfill_state(self.codex), own_backup=own)
        self.assertEqual(plan.source, SOURCE_OWN_BACKUP)
        self.assertEqual(plan.target, own[1])
        self.assertEqual(plan.snapshot, own[0])

    def test_a_complete_rebuild_needs_nothing(self) -> None:
        plan = build_settle_plan(self.codex, read_backfill_state(self.codex))
        self.assertFalse(plan.writes)

    def test_the_write_refuses_a_row_codex_moved(self) -> None:
        set_row(self.database, "running")
        plan = build_settle_plan(self.codex, read_backfill_state(self.codex))
        set_row(self.database, "running", watermark="sessions/2026/08/01/y.jsonl")
        with self.assertRaises(FailSafeError):
            settle_backfill(self.database, expected=plan.current, target=plan.target)
        self.assertEqual(full_row(self.database)[0], "running")

    def test_the_write_only_ever_completes(self) -> None:
        set_row(self.database, "running")
        plan = build_settle_plan(self.codex, read_backfill_state(self.codex))
        with self.assertRaises(FailSafeError):
            settle_backfill(self.database, expected=plan.current, target=(BACKFILL_PENDING, None, None, 1))

    def test_measuring_counts_chat_files_only(self) -> None:
        chat(self.codex, KNOWN)
        chat(self.codex, NEW, archived=True)
        (self.codex / "sessions" / "notes.txt").write_text("not a chat", encoding="utf-8")
        files = measure_chat_files(self.codex)
        self.assertEqual(files.count, 2)
        self.assertGreater(files.size, 0)


class CheckAndRepairTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.config, self.codex = self.machine("desktop")
        chat(self.codex, KNOWN)
        self.database = catalogue(self.codex, {})

    def test_a_check_finds_the_stuck_rebuild_and_writes_nothing(self) -> None:
        set_row(self.database, "running")
        before = sorted((path.name, path.stat().st_size) for path in self.codex.iterdir())
        health = check_codex(self.config)
        self.assertTrue(health.broken)
        self.assertIn(CATALOGUE_REBUILD_STUCK, [item.code for item in health.findings])
        self.assertTrue(health.plan.writes)
        self.assertEqual(sorted((path.name, path.stat().st_size) for path in self.codex.iterdir()), before)

    def test_a_healthy_codex_has_no_broken_finding(self) -> None:
        health = check_codex(self.config)
        self.assertFalse(health.broken)
        self.assertFalse(health.plan.writes)

    def test_a_missing_global_state_is_reported(self) -> None:
        self.assertIn(GLOBAL_STATE_MISSING, [item.code for item in check_codex(self.config).findings])

    def test_repairing_backs_up_journals_and_completes_the_rebuild(self) -> None:
        set_row(self.database, "running")
        preview = repair_codex(self.config)
        self.assertEqual(full_row(self.database)[0], "running", "a preview writes nothing")
        done = repair_codex(self.config, confirm_plan=preview.plan.plan_id, origin="cli")
        self.assertTrue(done.repaired)
        self.assertEqual(full_row(self.database), (BACKFILL_COMPLETE, None, 1791524862, 1791524862))
        self.assertTrue(list((self.workspace / "backups" / done.snapshot).rglob("state_5.sqlite")))
        self.assertEqual([run.state for run in list_history(self.config, family="codex-repair")], ["COMMITTED"])
        self.assertFalse(check_codex(self.config).broken)

    def test_codex_running_refuses_the_repair(self) -> None:
        set_row(self.database, "running")
        preview = repair_codex(self.config)
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            repair_codex(self.config, confirm_plan=preview.plan.plan_id)
        self.assertEqual(full_row(self.database)[0], "running")

    def test_a_stale_plan_id_is_refused(self) -> None:
        set_row(self.database, "running")
        preview = repair_codex(self.config)
        set_row(self.database, "running", watermark="sessions/2026/08/01/y.jsonl")
        with self.assertRaises(ConfigError):
            repair_codex(self.config, confirm_plan=preview.plan.plan_id)
        self.assertEqual(full_row(self.database)[0], "running")

    def test_the_row_codexsyncs_own_rebuild_request_replaced_is_put_back_exactly(self) -> None:
        # The 2026-10-09 sequence: codexSync asks for a rebuild (its backup holds
        # the `complete` row), Codex takes it up and is ended part-way.
        chat(self.codex, NEW)
        asked = refresh_thread_catalogue(self.config)
        refresh_thread_catalogue(self.config, confirm_plan=asked.plan.plan_id)
        self.assertEqual(full_row(self.database)[0], BACKFILL_PENDING)
        set_row(self.database, "running")
        preview = repair_codex(self.config)
        self.assertEqual(preview.plan.source, SOURCE_OWN_BACKUP)
        repair_codex(self.config, confirm_plan=preview.plan.plan_id)
        self.assertEqual(full_row(self.database), (BACKFILL_COMPLETE, "sessions/x", 1773067086, 1773067086))


if __name__ == "__main__":
    unittest.main()
