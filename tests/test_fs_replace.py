from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.exceptions import SafetyPreconditionError
from codexsync.fs_replace import REPLACE_ATTEMPTS, is_transient_lock, replace_with_retry
from codexsync.mutation_journal import JournalState, JournalStore


def _access_denied() -> OSError:
    """What Windows raises while a cloud client still holds the target open."""
    exc = PermissionError(errno.EACCES, "Access is denied")
    exc.winerror = 5
    return exc


class _LockedTarget:
    """Fail ``os.replace`` onto one name only; every other call is real."""

    def __init__(self, name: str, failures: int | None) -> None:
        self._name = name
        self._failures = failures
        self._real = os.replace
        self.calls = 0

    def __call__(self, src, dst):
        if Path(dst).name != self._name:
            return self._real(src, dst)
        self.calls += 1
        if self._failures is None or self.calls <= self._failures:
            raise _access_denied()
        return self._real(src, dst)


class ReplaceWithRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"fs-replace-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.src = self.root / "staged.tmp"
        self.dst = self.root / "target.json"
        self.src.write_text("new", encoding="utf-8")
        self.dst.write_text("old", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_lock_that_clears_is_waited_out(self) -> None:
        locked = _LockedTarget(self.dst.name, failures=3)
        with patch("os.replace", side_effect=locked), patch("time.sleep") as sleep:
            replace_with_retry(self.src, self.dst)
        self.assertEqual(locked.calls, 4)
        self.assertEqual(sleep.call_count, 3)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")

    def test_a_lock_that_stays_fails_after_a_bounded_wait(self) -> None:
        locked = _LockedTarget(self.dst.name, failures=None)
        with patch("os.replace", side_effect=locked), patch("time.sleep"):
            with self.assertRaises(PermissionError):
                replace_with_retry(self.src, self.dst)
        self.assertEqual(locked.calls, REPLACE_ATTEMPTS)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "old")

    def test_an_error_that_is_not_a_lock_is_raised_at_once(self) -> None:
        def cross_device(src, dst):
            cross_device.calls += 1
            raise OSError(errno.EXDEV, "cross-device link")

        cross_device.calls = 0
        with patch("os.replace", side_effect=cross_device), patch("time.sleep") as sleep:
            with self.assertRaises(OSError):
                replace_with_retry(self.src, self.dst)
        self.assertEqual(cross_device.calls, 1)
        sleep.assert_not_called()

    def test_before_retry_runs_before_each_further_attempt_and_can_stop_it(self) -> None:
        checks = {"n": 0}

        def before_retry() -> None:
            checks["n"] += 1
            if checks["n"] == 2:
                raise SafetyPreconditionError("Codex started during the wait")

        locked = _LockedTarget(self.dst.name, failures=None)
        with patch("os.replace", side_effect=locked), patch("time.sleep"):
            with self.assertRaises(SafetyPreconditionError):
                replace_with_retry(self.src, self.dst, before_retry=before_retry)
        self.assertEqual(locked.calls, 2)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "old")

    def test_transient_lock_classification(self) -> None:
        self.assertTrue(is_transient_lock(_access_denied()))
        sharing = OSError(errno.EACCES, "sharing violation")
        sharing.winerror = 32
        self.assertTrue(is_transient_lock(sharing))
        not_found = OSError(errno.ENOENT, "missing")
        not_found.winerror = 2
        self.assertFalse(is_transient_lock(not_found))
        self.assertTrue(is_transient_lock(OSError(errno.EBUSY, "busy")))
        self.assertFalse(is_transient_lock(OSError(errno.EXDEV, "cross-device")))


class JournalSurvivesACloudClientTests(unittest.TestCase):
    """The reported failure: a journal transition hit `WinError 5`.

    Yandex.Disk opened the journal file it had just been told about, the
    `PREPARED -> BACKED_UP` replace failed, the attempt to close the journal as
    `FAILED` failed the same way, and the journal stayed `PREPARED` -- blocking
    every later sync until `recover` closed it.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"journal-lock-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.store = JournalStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_transition_waits_out_a_lock_on_the_journal_file(self) -> None:
        journal = self.store.begin("sessions", "a" * 64, 11)
        locked = _LockedTarget(f"{journal.operation_id}.json", failures=2)
        with patch("os.replace", side_effect=locked), patch("time.sleep"):
            moved = self.store.transition(journal, JournalState.BACKED_UP)
        self.assertEqual(moved.state, JournalState.BACKED_UP)
        self.assertEqual(self.store.load(journal.operation_id).state, JournalState.BACKED_UP)
        self.assertEqual(list(self.store.root.glob("*.tmp")), [], "no staged copy may be left behind")


if __name__ == "__main__":
    unittest.main()
