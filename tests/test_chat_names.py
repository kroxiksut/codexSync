"""Chat names between machines (D-025).

On 2026-10-04 the laptop showed the carried chats under their projects, but
each under its first message: the name lives only in `threads.name`, never in
the chat file. The catalogue here carries the columns that matter.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import unittest

from codexsync.app import list_history, run_handoff, sync_chat_names
from codexsync.chat_names import (
    NamesPublication,
    build_name_plan,
    publication_from_names,
    read_names_board,
    write_names_publication,
)
from codexsync.exceptions import ConfigError, SafetyPreconditionError
from codexsync.safety_gate import ProcessState
from codexsync.sqlite_audit import PlacementStatus, ThreadNames, read_thread_names

try:
    from tests.test_handoff import _Workspace
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace

A = "01a07b26-013d-7470-a5ae-9d69c9958949"
B = "01a10041-f5a6-7552-b44c-91b193a13889"
C = "019cdce5-80cb-74b3-beda-458a37b8ee6f"


def catalogue(root: Path, rows: dict[str, tuple[str | None, str]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "state_5.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER NOT NULL, "
            "title TEXT NOT NULL, name TEXT)"
        )
        connection.execute(
            "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL, "
            "last_watermark TEXT, last_success_at INTEGER, updated_at INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO backfill_state VALUES (1, 'complete', NULL, 1, 1)")
        for thread_id, (name, title) in rows.items():
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, 0, ?, ?)", (thread_id, f"x/{thread_id}.jsonl", title, name)
            )
        connection.commit()
    finally:
        connection.close()
    return database


def names_in(database: Path) -> dict[str, str | None]:
    connection = sqlite3.connect(database)
    try:
        return dict(connection.execute("SELECT id, name FROM threads"))
    finally:
        connection.close()


def local(rows: dict[str, tuple[str | None, str]]) -> ThreadNames:
    return ThreadNames(PlacementStatus.AVAILABLE, "state_5.sqlite", rows)


class PlanTests(unittest.TestCase):
    def test_an_unnamed_chat_takes_the_other_machines_name(self) -> None:
        plan = build_name_plan(
            local({A: (None, "ok, where did we stop?")}),
            [NamesPublication("machine-a", {A: "Decomposition 0.9+"})],
        )
        self.assertEqual([(item.thread_id, item.name) for item in plan.changes], [(A, "Decomposition 0.9+")])

    def test_the_first_message_fallback_counts_as_unnamed(self) -> None:
        plan = build_name_plan(
            local({A: ("ok, where did we stop?", "ok, where did we stop?"), B: ("", "hi")}),
            [NamesPublication("machine-a", {A: "Decomposition", B: "Greeting"})],
        )
        self.assertEqual(len(plan.changes), 2)

    def test_a_name_given_here_is_never_replaced(self) -> None:
        plan = build_name_plan(
            local({A: ("Mine", "first message")}),
            [NamesPublication("machine-a", {A: "Theirs"})],
        )
        self.assertEqual(plan.changes, ())
        self.assertEqual(plan.kept, 1)

    def test_two_machines_disagreeing_leave_the_chat_alone(self) -> None:
        plan = build_name_plan(
            local({A: (None, "first")}),
            [NamesPublication("machine-a", {A: "One"}), NamesPublication("machine-c", {A: "Two"})],
        )
        self.assertEqual((plan.changes, plan.ambiguous), ((), 1))

    def test_a_chat_codex_has_not_listed_yet_waits(self) -> None:
        plan = build_name_plan(local({}), [NamesPublication("machine-a", {C: "Later"})])
        self.assertEqual((plan.changes, plan.waiting), ((), 1))

    def test_only_a_real_name_is_published(self) -> None:
        publication = publication_from_names(
            local({A: ("Decomposition", "first"), B: ("hi", "hi"), C: (None, "x")}), "machine-a",
        )
        self.assertEqual(publication.names, {A: "Decomposition"})


class BoardTests(_Workspace):
    def test_a_tampered_file_is_not_believed(self) -> None:
        root = self.root / "chat-names"
        path = write_names_publication(root, NamesPublication("machine-a", {A: "Name"}))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["names"][A] = "Other"
        path.write_text(json.dumps(raw), encoding="utf-8")
        board = read_names_board(root)
        self.assertEqual(board.publications, {})
        self.assertIn("machine-a.json", board.unreadable)


class SyncNamesTests(_Workspace):
    def test_names_travel_from_one_machine_to_the_other(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        catalogue(desktop_codex, {A: ("Decomposition 0.9+", "ok, where did we stop?")})
        laptop_db = catalogue(laptop_codex, {A: (None, "ok, where did we stop?")})

        published = sync_chat_names(desktop, confirm_plan=sync_chat_names(desktop).plan.plan_id)
        self.assertTrue(published.published)

        preview = sync_chat_names(laptop)
        self.assertEqual(len(preview.plan.changes), 1)
        self.assertEqual(names_in(laptop_db)[A], None, "a preview writes nothing")
        result = sync_chat_names(laptop, confirm_plan=preview.plan.plan_id, origin="cli")
        self.assertEqual(result.written, 1)
        self.assertEqual(names_in(laptop_db)[A], "Decomposition 0.9+")
        self.assertEqual([run.state for run in list_history(laptop, family="chat-names")], ["COMMITTED"])
        self.assertEqual(len(list((self.workspace / "backups").rglob("state_5.sqlite"))), 1)
        # Nothing left to do the second time.
        self.assertFalse(sync_chat_names(laptop).plan.writes)

    def test_a_stale_plan_or_a_running_codex_writes_nothing(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        catalogue(desktop_codex, {A: ("Name", "first")})
        laptop_db = catalogue(laptop_codex, {A: (None, "first")})
        sync_chat_names(desktop, confirm_plan=sync_chat_names(desktop).plan.plan_id)
        with self.assertRaises(ConfigError):
            sync_chat_names(laptop, confirm_plan="0" * 64)
        preview = sync_chat_names(laptop)
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            sync_chat_names(laptop, confirm_plan=preview.plan.plan_id)
        self.assertIsNone(names_in(laptop_db)[A])

    def test_a_full_sync_carries_the_names(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        catalogue(desktop_codex, {A: ("Decomposition 0.9+", "first")})
        laptop_db = catalogue(laptop_codex, {A: ("first", "first")})
        run_handoff(desktop)
        loaded = run_handoff(laptop)
        self.assertEqual(loaded.chat_names_set, 1)
        self.assertEqual(names_in(laptop_db)[A], "Decomposition 0.9+")

    def test_reading_names_creates_nothing_beside_the_database(self) -> None:
        _, codex = self.machine("laptop")
        database = catalogue(codex, {A: ("Name", "first")})
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        before = sorted(path.name for path in codex.iterdir())
        self.assertIs(read_thread_names(codex).status, PlacementStatus.AVAILABLE)
        self.assertEqual(sorted(path.name for path in codex.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
