"""CS-463: a destination that appears after the backup phase is never overwritten."""

from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.backup import BackupManager
from codexsync.exceptions import FailSafeError
from codexsync.models import CopyAction, SyncPlan
from codexsync.sync_engine import SyncEngine


class LateArrivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"late-arrival-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _engine(self, after_backup) -> SyncEngine:
        manager = BackupManager(
            backup_root=self.root / "backups", machine_id="machine-a",
            retention_days=7, max_backups=0,
        )
        return SyncEngine(
            backup_manager=manager, temp_dir=self.root / ".tmp",
            backup_before_overwrite=True, fail_on_unknown=True, after_backup=after_backup,
        )

    def test_a_file_delivered_after_the_backup_is_kept(self) -> None:
        src = self.root / "src.txt"
        dst = self.root / "mirror" / "dst.txt"
        src.write_text("ours", encoding="utf-8")
        plan = SyncPlan(to_local=[], to_cloud=[CopyAction(src=src, dst=dst, relative_path="dst.txt")])

        def cloud_client_delivers() -> None:
            dst.write_text("delivered", encoding="utf-8")

        with self.assertRaises(FailSafeError):
            self._engine(cloud_client_delivers).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "delivered")
        self.assertEqual([p.name for p in dst.parent.iterdir()], ["dst.txt"])

    def test_an_absent_destination_is_still_written(self) -> None:
        src = self.root / "src.txt"
        dst = self.root / "mirror" / "dst.txt"
        src.write_text("ours", encoding="utf-8")
        plan = SyncPlan(to_local=[], to_cloud=[CopyAction(src=src, dst=dst, relative_path="dst.txt")])
        self._engine(None).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "ours")


if __name__ == "__main__":
    unittest.main()
