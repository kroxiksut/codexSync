"""CS-470: the row of a backup copy includes its copied -wal."""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from codexsync.thread_catalogue import read_snapshot_backfill_row

SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "test-sandbox"


class SnapshotRowReadsWalTests(unittest.TestCase):
    def setUp(self) -> None:
        SANDBOX_ROOT.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX_ROOT)
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _backup_with_wal(self) -> Path:
        live = self.root / "live" / "state_5.sqlite"
        live.parent.mkdir()
        backup = self.root / "backup" / "state_5.sqlite"
        backup.parent.mkdir()
        connection = sqlite3.connect(live)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY, status TEXT, "
                "last_watermark TEXT, last_success_at INTEGER, updated_at INTEGER)"
            )
            connection.execute("INSERT INTO backfill_state VALUES (1, 'complete', 'w1', 10, 10)")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("UPDATE backfill_state SET last_watermark='w2', updated_at=20 WHERE id=1")
            connection.commit()
            # Copy while the connection is open: closing would fold the log in.
            shutil.copyfile(live, backup)
            shutil.copyfile(live.with_name(live.name + "-wal"), backup.with_name(backup.name + "-wal"))
        finally:
            connection.close()
        return backup

    def test_the_logged_row_wins_and_the_copy_is_untouched(self) -> None:
        backup = self._backup_with_wal()
        before = sorted(p.name for p in backup.parent.iterdir())
        sizes = {p.name: p.read_bytes() for p in backup.parent.iterdir()}
        scratch = self.root / "temp" / "backfill-read"
        row = read_snapshot_backfill_row(backup, scratch)
        self.assertEqual(row, ("complete", "w2", 10, 20))
        self.assertEqual(sorted(p.name for p in backup.parent.iterdir()), before)
        self.assertEqual({p.name: p.read_bytes() for p in backup.parent.iterdir()}, sizes)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_an_unreadable_copy_is_no_evidence(self) -> None:
        broken = self.root / "state_5.sqlite"
        broken.write_bytes(b"not a database")
        self.assertIsNone(read_snapshot_backfill_row(broken, self.root / "scratch"))


if __name__ == "__main__":
    unittest.main()
