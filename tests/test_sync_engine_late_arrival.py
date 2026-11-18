"""A destination that appears (CS-463) or changes (CS-493) after the backup phase is never overwritten."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.backup import BackupManager
from codexsync.exceptions import FailSafeError
from codexsync.filters import PathFilter
from codexsync.models import CopyAction, DeleteAction, SyncPlan
from codexsync.planner import build_sync_plan
from codexsync.scanner import scan_tree
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

    def _existing(self) -> tuple[Path, Path, SyncPlan]:
        src = self.root / "src.txt"
        dst = self.root / "mirror" / "dst.txt"
        src.write_text("ours", encoding="utf-8")
        dst.parent.mkdir(parents=True)
        dst.write_text("before", encoding="utf-8")
        return src, dst, SyncPlan(to_local=[], to_cloud=[CopyAction(src=src, dst=dst, relative_path="dst.txt")])

    def test_a_destination_edited_after_the_backup_is_kept(self) -> None:
        _src, dst, plan = self._existing()

        def cloud_client_writes_in_place() -> None:
            with dst.open("a", encoding="utf-8") as handle:
                handle.write(" + edit from the other machine")

        with self.assertRaises(FailSafeError):
            self._engine(cloud_client_writes_in_place).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "before + edit from the other machine")

    def test_a_destination_replaced_after_the_backup_is_kept_even_at_the_same_size(self) -> None:
        _src, dst, plan = self._existing()
        stat = dst.stat()

        def cloud_client_replaces() -> None:
            other = dst.with_name("incoming.part")
            other.write_text("BEFORE", encoding="utf-8")
            os.utime(other, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(other, dst)

        with self.assertRaises(FailSafeError):
            self._engine(cloud_client_replaces).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "BEFORE")

    def test_a_destination_replaced_after_the_plan_is_kept(self) -> None:
        local = self.root / "local"
        cloud = self.root / "cloud"
        (local / "settings").mkdir(parents=True)
        (cloud / "settings").mkdir(parents=True)
        src = local / "settings" / "value.txt"
        dst = cloud / "settings" / "value.txt"
        src.write_text("ours!!", encoding="utf-8")
        dst.write_text("before", encoding="utf-8")
        os.utime(src, ns=(src.stat().st_atime_ns, dst.stat().st_mtime_ns + 2_000_000_000))
        path_filter = PathFilter([])
        plan = build_sync_plan(
            scan_tree(local, ["settings"], path_filter),
            scan_tree(cloud, ["settings"], path_filter),
            local,
            cloud,
        )
        self.assertEqual(len(plan.to_cloud), 1)

        planned = dst.stat()
        incoming = dst.with_name("incoming.part")
        incoming.write_text("newer!", encoding="utf-8")
        os.utime(incoming, ns=(planned.st_atime_ns, planned.st_mtime_ns))
        os.replace(incoming, dst)

        with self.assertRaises(FailSafeError):
            self._engine(None).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "newer!")

    def test_a_destination_created_after_the_plan_is_kept(self) -> None:
        local = self.root / "local"
        cloud = self.root / "cloud"
        (local / "settings").mkdir(parents=True)
        cloud.mkdir(parents=True)
        src = local / "settings" / "value.txt"
        dst = cloud / "settings" / "value.txt"
        src.write_text("ours", encoding="utf-8")
        path_filter = PathFilter([])
        plan = build_sync_plan(
            scan_tree(local, ["settings"], path_filter),
            scan_tree(cloud, ["settings"], path_filter),
            local,
            cloud,
        )

        dst.parent.mkdir(parents=True)
        dst.write_text("arrived after planning", encoding="utf-8")

        with self.assertRaises(FailSafeError):
            self._engine(None).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "arrived after planning")

    def test_a_file_edited_after_its_backup_is_not_deleted(self) -> None:
        doomed = self.root / "mirror" / "old.txt"
        doomed.parent.mkdir(parents=True)
        doomed.write_text("old", encoding="utf-8")
        plan = SyncPlan(deletions=[DeleteAction(path=doomed, relative_path="old.txt", side="cloud")])

        def other_machine_edits() -> None:
            doomed.write_text("edited elsewhere", encoding="utf-8")

        with self.assertRaises(FailSafeError):
            self._engine(other_machine_edits).execute(plan, dry_run=False)
        self.assertEqual(doomed.read_text(encoding="utf-8"), "edited elsewhere")

    def test_an_unchanged_destination_is_replaced(self) -> None:
        _src, dst, plan = self._existing()
        self._engine(None).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "ours")

    def test_an_absent_destination_is_still_written(self) -> None:
        src = self.root / "src.txt"
        dst = self.root / "mirror" / "dst.txt"
        src.write_text("ours", encoding="utf-8")
        plan = SyncPlan(to_local=[], to_cloud=[CopyAction(src=src, dst=dst, relative_path="dst.txt")])
        self._engine(None).execute(plan, dry_run=False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "ours")


if __name__ == "__main__":
    unittest.main()
