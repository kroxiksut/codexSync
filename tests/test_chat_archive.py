"""Codex's catalogue follows a chat moved into or out of the archive (D-035).

A sync carries an archive move as a file move (D-023); the row in
`state_5.sqlite` kept naming the old path with the old flag, so Codex listed
the chat where it was and found no file. Observed shape, Linux laptop
2026-10-10: Codex keeps an active chat at `sessions/<y>/<m>/<d>/<name>`, an
archived one flat at `archived_sessions/<name>`, and its own rows hold
`archived_at` equal to the file's mtime in seconds.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import unittest

from codexsync.app import follow_chat_archive, list_history, run_handoff
from codexsync.chat_archive import build_archive_plan
from codexsync.exceptions import ConfigError, SafetyPreconditionError
from codexsync.safety_gate import ProcessState
from codexsync.sqlite_audit import PlacementStatus, read_thread_archive_rows

try:
    from tests.test_handoff import _Workspace
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace

A = "019ee475-8e28-7b80-8a89-3712d624b86e"
B = "019ee489-7ba4-7dd2-99fb-9575960ea277"
NAME_A = f"rollout-2026-06-20T17-56-14-{A}.jsonl"
NAME_B = f"rollout-2026-06-20T18-18-00-{B}.jsonl"


def catalogue(root: Path, rows: dict[str, tuple[str, int]], *, extended: bool = False) -> Path:
    """``rows``: thread id -> (path relative to the state root, archived)."""
    root.mkdir(parents=True, exist_ok=True)
    database = root / "state_5.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER NOT NULL, "
            "archived_at INTEGER, title TEXT NOT NULL, name TEXT)"
        )
        connection.execute(
            "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL, "
            "last_watermark TEXT, last_success_at INTEGER, updated_at INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO backfill_state VALUES (1, 'complete', NULL, 1, 1)")
        for thread_id, (relative, archived) in rows.items():
            stored = str(root.resolve() / Path(relative))
            if extended and os.name == "nt":
                stored = "\\\\?\\" + stored
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, 'first', NULL)",
                (thread_id, stored, archived, 7 if archived else None),
            )
        connection.commit()
    finally:
        connection.close()
    return database


def rows_in(database: Path) -> dict[str, tuple[str, int, int | None]]:
    connection = sqlite3.connect(database)
    try:
        return {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
            "SELECT id, rollout_path, archived, archived_at FROM threads"
        )}
    finally:
        connection.close()


def chat_file(root: Path, relative: str) -> Path:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    os.utime(path, (1791607034, 1791607034))
    return path


class FollowTests(_Workspace):
    def test_a_chat_archived_elsewhere_is_archived_in_codexs_list(self) -> None:
        laptop, codex = self.machine("laptop")
        database = catalogue(codex, {A: (f"sessions/2026/06/20/{NAME_A}", 0)})
        # What the session transfer did: the file is where the other machine keeps it.
        chat_file(codex, f"archived_sessions/{NAME_A}")

        preview = follow_chat_archive(laptop)
        self.assertEqual((preview.plan.archived, preview.plan.restored), (1, 0))
        self.assertEqual(rows_in(database)[A][1], 0, "a preview writes nothing")
        done = follow_chat_archive(laptop, confirm_plan=preview.plan.plan_id, origin="cli")
        self.assertEqual(done.written, 1)
        path, archived, archived_at = rows_in(database)[A]
        self.assertEqual(Path(path), codex.resolve() / "archived_sessions" / NAME_A)
        self.assertEqual((archived, archived_at), (1, 1791607034))
        self.assertEqual([run.state for run in list_history(laptop, family="chat-archive")], ["COMMITTED"])
        self.assertEqual(len(list((self.workspace / "backups").rglob("state_5.sqlite"))), 1)
        self.assertFalse(follow_chat_archive(laptop).plan.writes)

    def test_a_chat_brought_back_elsewhere_is_active_again(self) -> None:
        laptop, codex = self.machine("laptop")
        database = catalogue(codex, {A: (f"archived_sessions/{NAME_A}", 1)}, extended=True)
        chat_file(codex, f"sessions/2026/06/20/{NAME_A}")
        preview = follow_chat_archive(laptop)
        follow_chat_archive(laptop, confirm_plan=preview.plan.plan_id)
        path, archived, archived_at = rows_in(database)[A]
        self.assertEqual((archived, archived_at), (0, None))
        self.assertTrue(path.endswith(os.path.join("sessions", "2026", "06", "20", NAME_A)))
        if os.name == "nt":
            self.assertTrue(path.startswith("\\\\?\\"), "the row keeps the form Codex wrote it in")

    def test_only_a_moved_file_is_followed(self) -> None:
        laptop, codex = self.machine("laptop")
        catalogue(codex, {
            A: (f"sessions/2026/06/20/{NAME_A}", 0),  # in place: nothing to do
            B: (f"sessions/2026/06/20/{NAME_B}", 0),  # gone, not moved: nothing to point at
        })
        chat_file(codex, f"sessions/2026/06/20/{NAME_A}")
        self.assertFalse(follow_chat_archive(laptop).plan.writes)

    def test_a_file_another_row_names_is_not_taken(self) -> None:
        _, codex = self.machine("laptop")
        catalogue(codex, {
            A: (f"sessions/2026/06/20/{NAME_A}", 0),
            B: (f"archived_sessions/{NAME_A}", 1),
        })
        chat_file(codex, f"archived_sessions/{NAME_A}")
        plan = build_archive_plan(read_thread_archive_rows(codex), codex)
        self.assertEqual((plan.writes, plan.unresolved), (False, 1))

    def test_a_stale_plan_or_a_running_codex_writes_nothing(self) -> None:
        laptop, codex = self.machine("laptop")
        database = catalogue(codex, {A: (f"sessions/2026/06/20/{NAME_A}", 0)})
        chat_file(codex, f"archived_sessions/{NAME_A}")
        with self.assertRaises(ConfigError):
            follow_chat_archive(laptop, confirm_plan="0" * 64)
        preview = follow_chat_archive(laptop)
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            follow_chat_archive(laptop, confirm_plan=preview.plan.plan_id)
        self.assertEqual(rows_in(database)[A][1], 0)

    def test_a_full_sync_follows_the_move(self) -> None:
        laptop, codex = self.machine("laptop")
        database = catalogue(codex, {A: (f"sessions/2026/06/20/{NAME_A}", 0)})
        chat_file(codex, f"archived_sessions/{NAME_A}")
        result = run_handoff(laptop)
        self.assertEqual((result.chats_archived, result.chats_restored), (1, 0))
        self.assertEqual(rows_in(database)[A][1], 1)

    def test_reading_creates_nothing_beside_the_database(self) -> None:
        _, codex = self.machine("laptop")
        database = catalogue(codex, {A: (f"sessions/2026/06/20/{NAME_A}", 0)})
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        before = sorted(path.name for path in codex.iterdir())
        self.assertIs(read_thread_archive_rows(codex).status, PlacementStatus.AVAILABLE)
        self.assertEqual(sorted(path.name for path in codex.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
