from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.backup import BackupManager
from codexsync.exceptions import SafetyPreconditionError
from codexsync.models import CopyAction, SyncPlan
from codexsync.sync_engine import SyncEngine


def _sharing_violation() -> OSError:
    """The shape a cloud client or antivirus handle produces on the target."""
    return OSError(errno.EACCES, "target is held open by another process")


class _LockedTarget:
    """Fail os.replace for one destination only.

    ``os.replace`` is one shared module attribute, so patching it from
    ``codexsync.sync_engine`` also intercepts the backup manifest commit in
    ``backup.py``.  Everything but the destination under test is delegated to
    the real call.
    """

    def __init__(self, target: Path, failures: int | None) -> None:
        self._target = target
        self._failures = failures
        self._real = os.replace
        self.calls = 0

    def __call__(self, src, dst):
        if Path(dst) != self._target:
            return self._real(src, dst)
        self.calls += 1
        if self._failures is None or self.calls <= self._failures:
            raise _sharing_violation()
        return self._real(src, dst)


class ReplaceRetryTests(unittest.TestCase):
    """A destination held open for a moment must not fail the whole mutation.

    codexSync writes into a folder some other process is free to open at any
    time. On Windows that surfaces as WinError 5/32 from os.replace and used to
    abort the run, leaving a RECOVERY_REQUIRED journal for a lock that had
    already cleared by the time the user read the error.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"replace-retry-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.src = self.root / "src.txt"
        self.dst = self.root / "dst.txt"
        self.src.write_text("new-data", encoding="utf-8")
        self.dst.write_text("old-data", encoding="utf-8")
        self.plan = SyncPlan(
            to_local=[CopyAction(src=self.src, dst=self.dst, relative_path="dst.txt")],
            to_cloud=[],
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _engine(self, **kwargs) -> SyncEngine:
        manager = BackupManager(
            backup_root=self.root / "backups",
            machine_id="machine-a",
            retention_days=0,
            max_backups=0,
        )
        return SyncEngine(
            backup_manager=manager,
            temp_dir=self.root / ".tmp",
            backup_before_overwrite=True,
            fail_on_unknown=True,
            **kwargs,
        )

    def test_transient_lock_is_retried_until_it_clears(self) -> None:
        locked = _LockedTarget(self.dst, failures=2)
        with patch("codexsync.sync_engine.os.replace", side_effect=locked):
            with patch("codexsync.sync_engine.time.sleep"):
                self._engine().execute(self.plan, dry_run=False)

        self.assertEqual(locked.calls, 3)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new-data")

    def test_persistent_lock_still_fails_after_the_bounded_retries(self) -> None:
        locked = _LockedTarget(self.dst, failures=None)
        with patch("codexsync.sync_engine.os.replace", side_effect=locked):
            with patch("codexsync.sync_engine.time.sleep"):
                with self.assertRaises(OSError):
                    self._engine().execute(self.plan, dry_run=False)
        self.assertEqual(locked.calls, 5, "the retry must stay bounded")
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "old-data")

    def test_non_transient_error_is_not_retried(self) -> None:
        real_replace = os.replace

        def cross_device(src, dst):
            if Path(dst) != self.dst:
                return real_replace(src, dst)
            cross_device.calls += 1
            raise OSError(errno.EXDEV, "cross-device link")

        cross_device.calls = 0
        with patch("codexsync.sync_engine.os.replace", side_effect=cross_device):
            with patch("codexsync.sync_engine.time.sleep"):
                with self.assertRaises(OSError):
                    self._engine().execute(self.plan, dry_run=False)
        self.assertEqual(cross_device.calls, 1, "only a transient lock is worth retrying")

    def test_codex_starting_during_the_wait_stops_the_commit(self) -> None:
        """The wait must not become a hole in the process-safety guarantee."""
        checks = {"n": 0}

        def before_replace() -> None:
            checks["n"] += 1
            if checks["n"] >= 2:
                raise SafetyPreconditionError("Codex started while the target was locked")

        locked = _LockedTarget(self.dst, failures=None)
        with patch("codexsync.sync_engine.os.replace", side_effect=locked):
            with patch("codexsync.sync_engine.time.sleep"):
                with self.assertRaises(SafetyPreconditionError):
                    self._engine(before_replace_check=before_replace).execute(
                        self.plan, dry_run=False
                    )
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "old-data")


if __name__ == "__main__":
    unittest.main()
