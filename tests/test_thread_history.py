"""Reading Codex 0.163's history store, `thread_history_<n>.sqlite` (CS-426).

Only which threads have a projection is read, strictly read-only: no file may
appear beside the database, and every way of not reading one that exists is
INDETERMINATE -- a replacing write is refused on it -- never ABSENT.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.sqlite_audit import PlacementStatus, read_projected_threads

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class ProjectedThreadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"thread-history-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _store(self, name: str, ddl: str, rows=()) -> Path:
        path = self.root / name
        connection = sqlite3.connect(path)
        try:
            connection.execute(ddl)
            for row in rows:
                connection.execute("INSERT INTO thread_history_projection_state VALUES (?, ?, ?)", row)
            connection.commit()
        finally:
            connection.close()
        return path

    def test_no_store_constrains_nothing(self) -> None:
        self.assertEqual(read_projected_threads(self.root)[:2], (PlacementStatus.ABSENT, frozenset()))

    def test_the_projected_threads_are_read_and_nothing_is_created(self) -> None:
        # The columns Codex 0.163.0-alpha.10.2 has (seen on the laptop).
        self._store(
            "thread_history_1.sqlite",
            "CREATE TABLE thread_history_projection_state "
            "(thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER)",
            [("019ce7a8", 115115, 58), ("019d0457", 149088, 95)],
        )
        before = sorted(p.name for p in self.root.iterdir())
        status, ids, codes = read_projected_threads(self.root)
        self.assertEqual(status, PlacementStatus.AVAILABLE)
        self.assertEqual(ids, frozenset({"019ce7a8", "019d0457"}))
        self.assertEqual(codes, ())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_a_store_of_an_unknown_shape_is_not_taken_for_none(self) -> None:
        self._store("thread_history_1.sqlite", "CREATE TABLE other (x INTEGER)")
        status, _, codes = read_projected_threads(self.root)
        self.assertEqual(status, PlacementStatus.INDETERMINATE)
        self.assertEqual(codes, ("HISTORY_SHAPE_UNKNOWN",))

    def test_two_stores_are_not_merged(self) -> None:
        ddl = "CREATE TABLE thread_history_projection_state (thread_id TEXT, a INTEGER, b INTEGER)"
        self._store("thread_history_1.sqlite", ddl)
        self._store("thread_history_2.sqlite", ddl)
        self.assertEqual(read_projected_threads(self.root)[2], ("SEVERAL_HISTORY_STORES",))


if __name__ == "__main__":
    unittest.main()
