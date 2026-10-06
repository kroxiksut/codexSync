"""Asking Codex to list chat files its catalogue misses (D-024).

Observed on a laptop on 2026-10-04: a full sync wrote 197 chats into `.codex`
and Codex showed none of them, because it lists chats from `threads` in
`state_5.sqlite` and had marked its backfill from the files `complete` in
March. The catalogue here is built with the columns that matter and the
`backfill_state` row exactly as Codex's migration creates it.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from codexsync.app import list_history, refresh_thread_catalogue, run_handoff
from codexsync.exceptions import ConfigError, SafetyPreconditionError
from codexsync.safety_gate import ProcessState
from codexsync.sqlite_audit import PlacementStatus, read_backfill_state, read_thread_placements
from codexsync.thread_catalogue import (
    BACKFILL_COMPLETE,
    BACKFILL_PENDING,
    RefreshStatus,
    build_refresh_plan,
    chat_file_id,
    find_unnamed_chat_files,
    reset_backfill,
    unnamed_digest,
)

try:
    from tests.test_handoff import _Workspace
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace

KNOWN = "01a10040-14dc-74f2-ab25-09d45aa1cd55"
NEW = "01a10041-f5a6-7552-b44c-91b193a13889"
MOVED = "019cdce5-80cb-74b3-beda-458a37b8ee6f"


def rollout_name(thread_id: str, stamp: str = "2026-10-03T13-32-42") -> str:
    return f"rollout-{stamp}-{thread_id}.jsonl"


def chat(root: Path, thread_id: str, *, archived: bool = False, records: int = 1) -> Path:
    folder = root / "archived_sessions" if archived else root / "sessions" / "2026" / "10" / "03"
    path = folder / rollout_name(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "session_meta", "payload": {"id": thread_id, "cwd": "C:/work"}}]
    rows += [{"type": "event", "n": index} for index in range(records)]
    path.write_bytes(b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows))
    return path


def catalogue(root: Path, rows: dict[str, Path], *, status: str | None = BACKFILL_COMPLETE) -> Path:
    database = root / "state_5.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL, "
            "last_watermark TEXT, last_success_at INTEGER, updated_at INTEGER NOT NULL)"
        )
        for thread_id, path in rows.items():
            connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (thread_id, str(path)))
        if status is not None:
            connection.execute(
                "INSERT INTO backfill_state VALUES (1, ?, 'sessions/x', 1773067086, 1773067086)", (status,)
            )
        connection.commit()
    finally:
        connection.close()
    return database


def backfill_row(database: Path) -> tuple:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT status, last_watermark, last_success_at FROM backfill_state WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()


class NamesTests(unittest.TestCase):
    def test_the_thread_id_comes_from_codexs_file_name(self) -> None:
        self.assertEqual(chat_file_id(rollout_name(KNOWN)), KNOWN)
        # Codex's own continuation copy carries a suffix after the id.
        self.assertEqual(chat_file_id(rollout_name(KNOWN)[:-6] + "_" + NEW + ".jsonl"), KNOWN)
        self.assertIsNone(chat_file_id("notes.jsonl"))
        self.assertIsNone(chat_file_id(f"rollout-{KNOWN}.jsonl"))


class PlanTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.codex = self.root / "codex"
        self.codex.mkdir(parents=True)

    def plan(self, asked_before: str | None = None):
        return build_refresh_plan(
            self.codex, read_thread_placements(self.codex), read_backfill_state(self.codex),
            asked_before=asked_before,
        )

    def test_a_chat_file_the_catalogue_does_not_name_is_found(self) -> None:
        known = chat(self.codex, KNOWN)
        new = chat(self.codex, NEW)
        catalogue(self.codex, {KNOWN: known})
        plan = self.plan()
        self.assertIs(plan.status, RefreshStatus.NEEDED)
        self.assertEqual(plan.unnamed, (new.relative_to(self.codex).as_posix(),))
        self.assertEqual(plan.database, "state_5.sqlite")

    def test_a_chat_moved_into_the_archive_counts_until_codex_follows_it(self) -> None:
        # D-023 moves the file; the catalogue still names the old one.
        old = self.codex / "sessions" / "2026" / "10" / "03" / rollout_name(MOVED)
        moved = chat(self.codex, MOVED, archived=True)
        catalogue(self.codex, {MOVED: old})
        self.assertEqual(self.plan().unnamed, (moved.relative_to(self.codex).as_posix(),))

    def test_codexs_own_leftover_copy_does_not_count(self) -> None:
        named = chat(self.codex, KNOWN)
        leftover = named.with_name(named.name[:-6] + "_" + NEW + ".jsonl")
        leftover.write_bytes(named.read_bytes())
        catalogue(self.codex, {KNOWN: named})
        self.assertIs(self.plan().status, RefreshStatus.NOT_NEEDED)

    def test_every_listed_chat_needs_nothing(self) -> None:
        known = chat(self.codex, KNOWN)
        catalogue(self.codex, {KNOWN: known})
        plan = self.plan()
        self.assertIs(plan.status, RefreshStatus.NOT_NEEDED)
        self.assertFalse(plan.writes)

    def test_a_backfill_codex_has_not_finished_is_left_to_codex(self) -> None:
        chat(self.codex, NEW)
        catalogue(self.codex, {}, status=BACKFILL_PENDING)
        self.assertIs(self.plan().status, RefreshStatus.ALREADY_PENDING)

    def test_the_same_files_are_not_asked_for_twice(self) -> None:
        new = chat(self.codex, NEW)
        catalogue(self.codex, {})
        digest = unnamed_digest((new.relative_to(self.codex).as_posix(),))
        self.assertIs(self.plan(asked_before=digest).status, RefreshStatus.ALREADY_ASKED)
        # A further file is a new question.
        chat(self.codex, KNOWN)
        self.assertIs(self.plan(asked_before=digest).status, RefreshStatus.NEEDED)

    def test_no_catalogue_is_built_by_codex_itself(self) -> None:
        chat(self.codex, NEW)
        self.assertIs(self.plan().status, RefreshStatus.NO_CATALOGUE)

    def test_a_catalogue_without_the_backfill_row_is_not_guessed_at(self) -> None:
        chat(self.codex, NEW)
        connection = sqlite3.connect(self.codex / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.commit()
        finally:
            connection.close()
        plan = self.plan()
        self.assertIs(plan.status, RefreshStatus.UNAVAILABLE)
        self.assertIn("NO_BACKFILL_STATE", plan.codes)

    def test_reading_creates_nothing_beside_a_wal_database(self) -> None:
        known = chat(self.codex, KNOWN)
        database = catalogue(self.codex, {KNOWN: known})
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        before = sorted(path.name for path in self.codex.iterdir())
        self.assertIs(read_backfill_state(self.codex).status, PlacementStatus.AVAILABLE)
        self.plan()
        self.assertEqual(sorted(path.name for path in self.codex.iterdir()), before)


class ResetTests(_Workspace):
    def test_the_row_goes_back_to_what_codexs_migration_creates(self) -> None:
        database = catalogue(self.root, {})
        reset_backfill(database, now=1775000000)
        self.assertEqual(backfill_row(database), (BACKFILL_PENDING, None, None))

    def test_a_status_that_moved_is_not_overwritten(self) -> None:
        database = catalogue(self.root, {}, status="running")
        with self.assertRaises(Exception):
            reset_backfill(database, now=1775000000)
        self.assertEqual(backfill_row(database)[0], "running")


class RefreshCommandTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.config, self.codex = self.machine("laptop")
        self.new = chat(self.codex, NEW)
        self.database = catalogue(self.codex, {})

    def test_a_preview_writes_nothing(self) -> None:
        before = self.database.read_bytes()
        result = refresh_thread_catalogue(self.config)
        self.assertIs(result.plan.status, RefreshStatus.NEEDED)
        self.assertFalse(result.refreshed)
        self.assertEqual(self.database.read_bytes(), before)

    def test_applying_backs_up_journals_and_asks_codex(self) -> None:
        preview = refresh_thread_catalogue(self.config)
        result = refresh_thread_catalogue(self.config, confirm_plan=preview.plan.plan_id, origin="cli")
        self.assertTrue(result.refreshed)
        self.assertEqual(backfill_row(self.database)[0], BACKFILL_PENDING)
        backups = list((self.workspace / "backups").rglob("state_5.sqlite"))
        self.assertEqual(len(backups), 1, "the catalogue is backed up before the write")
        runs = list_history(self.config, family="thread-catalogue")
        self.assertEqual([run.state for run in runs], ["COMMITTED"])
        # Codex ran its backfill and still left the file out: not asked again.
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE backfill_state SET status = 'complete'")
        connection.commit()
        connection.close()
        again = refresh_thread_catalogue(self.config)
        self.assertIs(again.plan.status, RefreshStatus.ALREADY_ASKED)

    def test_a_stale_plan_id_is_refused(self) -> None:
        refresh_thread_catalogue(self.config)
        with self.assertRaises(ConfigError):
            refresh_thread_catalogue(self.config, confirm_plan="0" * 64)
        self.assertEqual(backfill_row(self.database)[0], BACKFILL_COMPLETE)

    def test_codex_running_refuses_the_write(self) -> None:
        preview = refresh_thread_catalogue(self.config)
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            refresh_thread_catalogue(self.config, confirm_plan=preview.plan.plan_id)
        self.assertEqual(backfill_row(self.database)[0], BACKFILL_COMPLETE)


class FullSyncTests(_Workspace):
    def test_a_chat_brought_from_the_other_machine_is_announced_to_codex(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        chat(desktop_codex, NEW)
        database = catalogue(laptop_codex, {})
        run_handoff(desktop)
        loaded = run_handoff(laptop)
        self.assertEqual(loaded.new_chats_written, 1)
        self.assertEqual(loaded.chats_codex_will_list, 1)
        self.assertEqual(backfill_row(database)[0], BACKFILL_PENDING)
        # Codex not started yet: the next sync asks nothing and still says so.
        again = run_handoff(laptop)
        self.assertEqual(again.chats_codex_will_list, 1)
        self.assertEqual(again.chats_codex_ignores, 0)


if __name__ == "__main__":
    unittest.main()
