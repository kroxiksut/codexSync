from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.sqlite_audit import (
    PlacementStatus,
    SQLiteRole,
    audit_sqlite,
    discover_sqlite_sets,
    read_thread_placements,
)


class SQLiteAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"sqlite-audit-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_thread_catalog_is_audited_read_only(self) -> None:
        database = self.root / "state_9.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER NOT NULL)")
        connection.execute("INSERT INTO threads VALUES (?, ?)", ("private-thread-id", 0))
        connection.commit()
        connection.close()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}

        reports = audit_sqlite(self.root, cold=True)

        after = {path.name: path.read_bytes() for path in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].role, SQLiteRole.THREAD_CATALOG)
        self.assertEqual(reports[0].status, "PASS")
        self.assertNotIn("private-thread-id", repr(reports[0]))

    def test_discovers_sidecars_and_sqlite_subdirectory(self) -> None:
        database = self.root / "state_1.sqlite"
        database.write_bytes(b"not sqlite")
        database.with_name(database.name + "-wal").write_bytes(b"wal")
        sqlite_dir = self.root / "sqlite"
        sqlite_dir.mkdir()
        (sqlite_dir / "codex-dev.db").write_bytes(b"db")
        sets = discover_sqlite_sets(self.root)
        self.assertEqual(len(sets), 2)
        state_set = next(item for item in sets if item.database.relative_path == "state_1.sqlite")
        self.assertEqual(len(state_set.sidecars), 1)


class ThreadPlacementTests(unittest.TestCase):
    """Where the runtime says each thread's file lives.

    This is what stops a transfer from writing a branch the runtime will never
    look at. On a real machine every session on disk has a row here, so being
    listed is the normal case and being absent is the blocking one.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"placements-{uuid.uuid4().hex}"
        (self.root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _catalogue(self, rows) -> Path:
        database = self.root / "state_9.sqlite"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
            "archived INTEGER NOT NULL, title TEXT, first_user_message TEXT)"
        )
        for thread_id, rollout, archived in rows:
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                (thread_id, rollout, archived, "a private title", "a private message"),
            )
        connection.commit()
        connection.close()
        return database

    def test_no_catalogue_is_absent_not_indeterminate(self) -> None:
        """Nothing to disagree with is not the same as unable to look."""
        placements = read_thread_placements(self.root)
        self.assertIs(placements.status, PlacementStatus.ABSENT)
        self.assertEqual(placements.by_session, {})

    def test_a_rollout_path_is_returned_relative_to_the_state_root(self) -> None:
        target = self.root / "sessions" / "a.jsonl"
        target.write_bytes(b"{}\n")
        self._catalogue([("thread-a", str(target), 0)])
        placements = read_thread_placements(self.root)
        self.assertIs(placements.status, PlacementStatus.AVAILABLE)
        self.assertTrue(placements.knows("thread-a"))
        self.assertEqual(placements.placement_of("thread-a"), "sessions/a.jsonl")

    def test_a_windows_extended_length_path_names_the_same_file(self) -> None:
        """The runtime writes plenty of these; keeping the prefix loses them."""
        target = self.root / "sessions" / "b.jsonl"
        target.write_bytes(b"{}\n")
        extended = "\\\\?\\" + str(target.resolve())
        self._catalogue([("thread-b", extended, 0)])
        placements = read_thread_placements(self.root)
        self.assertEqual(placements.placement_of("thread-b"), "sessions/b.jsonl")
        self.assertNotIn("ROLLOUT_PATH_OUTSIDE_STATE_ROOT", placements.codes)

    def test_a_path_outside_the_state_root_is_unknown_not_invented(self) -> None:
        self._catalogue([("thread-c", str(self.root.parent / "elsewhere.jsonl"), 0)])
        placements = read_thread_placements(self.root)
        self.assertTrue(placements.knows("thread-c"))
        self.assertIsNone(placements.placement_of("thread-c"))
        self.assertIn("ROLLOUT_PATH_OUTSIDE_STATE_ROOT", placements.codes)

    def test_the_archived_flag_is_carried(self) -> None:
        target = self.root / "sessions" / "d.jsonl"
        target.write_bytes(b"{}\n")
        self._catalogue([("thread-d", str(target), 1)])
        self.assertIn("thread-d", read_thread_placements(self.root).archived)

    def test_a_database_without_the_columns_is_not_a_catalogue(self) -> None:
        database = self.root / "state_8.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)")
        connection.commit()
        connection.close()
        self.assertIs(read_thread_placements(self.root).status, PlacementStatus.ABSENT)

    def test_reading_leaves_the_database_untouched(self) -> None:
        target = self.root / "sessions" / "e.jsonl"
        target.write_bytes(b"{}\n")
        database = self._catalogue([("thread-e", str(target), 0)])
        before = database.stat().st_size, database.stat().st_mtime_ns
        read_thread_placements(self.root)
        self.assertEqual((database.stat().st_size, database.stat().st_mtime_ns), before)
        self.assertFalse((self.root / "state_9.sqlite-wal").exists())
        self.assertFalse((self.root / "state_9.sqlite-journal").exists())


if __name__ == "__main__":
    unittest.main()
