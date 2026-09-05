"""A read-only SQLite audit must not create a file inside the state directory.

This is the project's hardest rule and the one place it was being broken
without anything noticing. Opening a WAL database creates `-wal` and `-shm`
beside it when they are absent, and SQLite does that in C -- below
`tests/test_guardian_state_isolation.py`, which can only see Python-level
calls. Measured before the fix: a plain `mode=ro` open of a cleanly closed
database created both files in the state root, on every `doctor`, every
`sessions scan` and every apply.

So these tests do not watch calls. They list the directory before and after,
which is the only way to see a write made by a library.

Four shapes are the whole decision, and what separates them is whether the
write-ahead log holds frames, not whether the file exists.

* No `-wal`, or one truncated to zero bytes: nothing is pending, the main file
  is already the whole database, and `immutable=1` reads it without building or
  rebuilding a shared index. The empty case is a cleanly closed Codex, and it
  is not a detail -- opening it normally *rebuilds* the stale `-shm`, which on
  a real machine moved that file's timestamp on every single `doctor`.
* A `-wal` with frames and a `-shm` beside it: the running case. Both files
  already exist and nothing is created; the shared index's timestamp moves,
  which is what every reader including Codex does to it.
* A `-wal` with frames and no `-shm`: a crashed writer, where opening would
  create one. Refused -- and refused as `INDETERMINATE` rather than `ABSENT`,
  because those two mean opposite things to a caller deciding whether it may
  write.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.sqlite_audit import (
    PlacementStatus,
    WAL_WITHOUT_SHARED_INDEX,
    audit_sqlite,
    read_thread_placements,
)


class SidecarCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"sqlite-nothing-{uuid.uuid4().hex}"
        (self.root / "sessions").mkdir(parents=True)
        self.rollout = self.root / "sessions" / "a.jsonl"
        self.rollout.write_bytes(b"{}\n")
        self.database = self.root / "state_1.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER)"
        )
        connection.execute("INSERT INTO threads VALUES ('t1', ?, 0)", (str(self.rollout),))
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _listing(self) -> set[str]:
        return {path.name for path in self.root.iterdir()}

    def test_a_cleanly_closed_database_has_no_sidecars_to_begin_with(self) -> None:
        """The premise: this is what the state root looks like with Codex shut."""
        self.assertNotIn("state_1.sqlite-wal", self._listing())
        self.assertNotIn("state_1.sqlite-shm", self._listing())

    def test_reading_placements_creates_nothing(self) -> None:
        before = self._listing()
        result = read_thread_placements(self.root)
        self.assertEqual(self._listing() - before, set(), "no file may appear in the state root")
        self.assertIs(result.status, PlacementStatus.AVAILABLE)
        self.assertEqual(result.placement_of("t1"), "sessions/a.jsonl")

    def test_auditing_creates_nothing(self) -> None:
        before = self._listing()
        reports = audit_sqlite(self.root)
        self.assertEqual(self._listing() - before, set())
        self.assertEqual(len(reports), 1)

    def test_the_database_bytes_are_untouched(self) -> None:
        before = self.database.read_bytes()
        read_thread_placements(self.root)
        audit_sqlite(self.root)
        self.assertEqual(self.database.read_bytes(), before)

    def test_a_running_writer_leaves_both_sidecars_and_still_creates_nothing(self) -> None:
        """The normal case: Codex holds the database open."""
        writer = sqlite3.connect(self.database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO threads VALUES ('t2', ?, 0)", (str(self.rollout),))
        writer.commit()
        try:
            self.assertIn("state_1.sqlite-shm", self._listing())
            before = self._listing()
            result = read_thread_placements(self.root)
            self.assertEqual(self._listing() - before, set())
            self.assertIs(result.status, PlacementStatus.AVAILABLE)
        finally:
            writer.close()

    def test_a_wal_without_a_shared_index_is_refused_rather_than_written(self) -> None:
        (self.root / "state_1.sqlite-wal").write_bytes(b"x" * 64)
        before = self._listing()

        result = read_thread_placements(self.root)

        self.assertEqual(self._listing() - before, set(), "refusing must not write either")
        self.assertIs(result.status, PlacementStatus.INDETERMINATE)
        self.assertIn(WAL_WITHOUT_SHARED_INDEX, result.codes)

    def test_that_refusal_is_never_reported_as_an_absent_catalogue(self) -> None:
        """ABSENT constrains nothing; INDETERMINATE blocks. Collapsing them writes."""
        (self.root / "state_1.sqlite-wal").write_bytes(b"x" * 64)
        self.assertIsNot(read_thread_placements(self.root).status, PlacementStatus.ABSENT)

    def test_the_audit_reports_the_same_refusal(self) -> None:
        (self.root / "state_1.sqlite-wal").write_bytes(b"x" * 64)
        before = self._listing()

        reports = audit_sqlite(self.root)

        self.assertEqual(self._listing() - before, set())
        self.assertEqual(reports[0].status, "INDETERMINATE")
        self.assertIn(WAL_WITHOUT_SHARED_INDEX, reports[0].codes)

    def test_the_listing_guard_itself_would_catch_a_planted_file(self) -> None:
        """A check that never fires proves nothing."""
        before = self._listing()
        (self.root / "planted").write_bytes(b"")
        self.assertEqual(self._listing() - before, {"planted"})

    def test_a_stale_shared_index_is_not_rewritten(self) -> None:
        """A cleanly closed Codex leaves an empty -wal and a stale -shm.

        Opening that normally rebuilds the shared index, and rebuilding it
        writes: measured on a real machine as the -shm mtime moving on every
        `doctor`. An empty write-ahead log holds no frames, so the main file is
        already the whole database and nothing has to be rebuilt to read it.
        """
        wal = self.root / "state_1.sqlite-wal"
        shm = self.root / "state_1.sqlite-shm"
        wal.write_bytes(b"")
        shm.write_bytes(b"stale")
        before = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (self.database, wal, shm)
        }

        result = read_thread_placements(self.root)
        audit_sqlite(self.root)

        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (self.database, wal, shm)
        }
        self.assertEqual(after, before, "no byte and no timestamp may move")
        self.assertIs(result.status, PlacementStatus.AVAILABLE)
        self.assertEqual(result.placement_of("t1"), "sessions/a.jsonl")

    def test_a_wal_with_frames_and_no_shared_index_is_still_refused(self) -> None:
        """Empty is the exemption; a log with content is not."""
        (self.root / "state_1.sqlite-wal").write_bytes(b"x" * 64)
        before = self._listing()

        result = read_thread_placements(self.root)

        self.assertEqual(self._listing() - before, set())
        self.assertIs(result.status, PlacementStatus.INDETERMINATE)
        self.assertIn(WAL_WITHOUT_SHARED_INDEX, result.codes)


if __name__ == "__main__":
    unittest.main()
