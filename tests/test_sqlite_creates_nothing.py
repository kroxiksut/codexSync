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
import time
import unittest
import uuid

from codexsync.sqlite_audit import (
    PlacementStatus,
    WAL_WITHOUT_SHARED_INDEX,
    audit_sqlite,
    read_thread_placements,
)


#: SQLite's own sidecars of the test database, as it names them.
_SIDECARS = frozenset(f"state_1.sqlite{suffix}" for suffix in ("-wal", "-shm", "-journal"))


def _foreign_sidecar_copy(name: str) -> bool:
    """`STATE_1.SQLITE-SHM.tmp`: a sidecar's name in another case, plus `.tmp`."""
    if not name.endswith(".tmp"):
        return False
    stem = name[: -len(".tmp")]
    return stem not in _SIDECARS and stem.lower() in _SIDECARS


class ForeignCopyRuleTests(unittest.TestCase):
    """The rule above sets aside exactly one shape and nothing SQLite makes."""

    def test_only_another_program_s_copy_is_set_aside(self) -> None:
        self.assertTrue(_foreign_sidecar_copy("STATE_1.SQLITE-SHM.tmp"))
        self.assertTrue(_foreign_sidecar_copy("STATE_1.SQLITE-WAL.tmp"))
        for name in (
            "state_1.sqlite-shm", "state_1.sqlite-wal", "state_1.sqlite-journal",
            "STATE_1.SQLITE-SHM", "state_1.sqlite-shm.tmp", ".state_1.sqlite.ab12.codexsync.tmp", "planted",
        ):
            with self.subTest(name=name):
                self.assertFalse(_foreign_sidecar_copy(name))


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
        # The premise of every test here is a cleanly closed database. SQLite
        # removes its `-wal`/`-shm` on the last close, but under a loaded suite
        # on Windows they can linger a moment; a listing taken then would count
        # their disappearance or reappearance as the reader's doing.
        for _ in range(40):
            if not any((self.root / f"state_1.sqlite{suffix}").exists() for suffix in ("-wal", "-shm")):
                break
            time.sleep(0.05)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _wal_without_shared_index(self) -> None:
        """A log with frames and no `-shm`, whatever the closing in `setUp` left.

        SQLite removes `-shm` when the last connection closes, but under load on
        Windows the file can still be there a moment later; the case under test
        is its absence, so it is made absent rather than assumed.
        """
        shm = self.root / "state_1.sqlite-shm"
        for _ in range(50):
            try:
                shm.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.05)
        (self.root / "state_1.sqlite-wal").write_bytes(b"x" * 64)

    def _listing(self) -> set[str]:
        """What is in the state root, less another program's copy of a sidecar.

        A file client or a virus scanner on this machine copies the `-shm` that
        `setUp` briefly creates, as `STATE_1.SQLITE-SHM.tmp`, and the copy can
        land between the two listings (CS-427: three runs in a fortnight, a
        different test each time). SQLite never makes such a name -- its
        sidecars are exactly `-wal`, `-shm` and `-journal`, in the case they
        were opened with, and never end in `.tmp` -- and codexSync's own
        staging names start with a dot. Only that shape is set aside, and it is
        named in the output so a run that saw it says so; every real sidecar
        still counts.
        """
        names = {path.name for path in self.root.iterdir()}
        foreign = {name for name in names if _foreign_sidecar_copy(name)}
        if foreign:
            print(f"set aside another program's sidecar copy: {sorted(foreign)}")
        return names - foreign

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
        self._wal_without_shared_index()
        before = self._listing()

        result = read_thread_placements(self.root)

        self.assertEqual(self._listing() - before, set(), "refusing must not write either")
        self.assertIs(result.status, PlacementStatus.INDETERMINATE)
        self.assertIn(WAL_WITHOUT_SHARED_INDEX, result.codes)

    def test_that_refusal_is_never_reported_as_an_absent_catalogue(self) -> None:
        """ABSENT constrains nothing; INDETERMINATE blocks. Collapsing them writes."""
        self._wal_without_shared_index()
        self.assertIsNot(read_thread_placements(self.root).status, PlacementStatus.ABSENT)

    def test_the_audit_reports_the_same_refusal(self) -> None:
        self._wal_without_shared_index()
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
        self._wal_without_shared_index()
        before = self._listing()

        result = read_thread_placements(self.root)

        self.assertEqual(self._listing() - before, set())
        self.assertIs(result.status, PlacementStatus.INDETERMINATE)
        self.assertIn(WAL_WITHOUT_SHARED_INDEX, result.codes)


if __name__ == "__main__":
    unittest.main()
