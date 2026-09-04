from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
import uuid
import zipfile
from unittest.mock import patch

from codexsync.backup import BackupManager
from codexsync.models import CopyAction, SyncPlan
from codexsync.sync_engine import SyncEngine


class SyncEngineTests(unittest.TestCase):
    def test_backup_before_overwrite(self) -> None:
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            src = root / "src.txt"
            dst = root / "dst.txt"
            backup_root = root / "backups"
            temp_root = root / ".tmp"

            src.write_text("new-data", encoding="utf-8")
            dst.write_text("old-data", encoding="utf-8")

            action = CopyAction(src=src, dst=dst, relative_path="dst.txt")
            plan = SyncPlan(to_local=[action], to_cloud=[])

            manager = BackupManager(
                backup_root=backup_root,
                machine_id="machine-a",
                retention_days=7,
                max_backups=0,
            )
            engine = SyncEngine(
                backup_manager=manager,
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=True,
            )
            engine.execute(plan, dry_run=False)

            self.assertEqual(dst.read_text(encoding="utf-8"), "new-data")
            backup_files = [p for p in backup_root.rglob("*") if p.is_file() and not p.name.endswith(".manifest.json")]
            self.assertEqual(len(backup_files), 1)
            self.assertEqual(backup_files[0].read_text(encoding="utf-8"), "old-data")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_backup_before_overwrite_with_zip_compression(self) -> None:
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-zip-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            src = root / "src.txt"
            dst = root / "dst.txt"
            backup_root = root / "backups"
            temp_root = root / ".tmp"

            src.write_text("new-data", encoding="utf-8")
            dst.write_text("old-data", encoding="utf-8")

            action = CopyAction(src=src, dst=dst, relative_path="dst.txt")
            plan = SyncPlan(to_local=[action], to_cloud=[])

            manager = BackupManager(
                backup_root=backup_root,
                machine_id="machine-a",
                retention_days=7,
                max_backups=0,
                compression="zip",
            )
            engine = SyncEngine(
                backup_manager=manager,
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=True,
            )
            engine.execute(plan, dry_run=False)

            self.assertEqual(dst.read_text(encoding="utf-8"), "new-data")
            backup_zips = list(backup_root.glob("*.zip"))
            self.assertEqual(len(backup_zips), 1)
            with zipfile.ZipFile(backup_zips[0], "r") as zf:
                self.assertEqual(zf.namelist(), ["dst.txt"])
                self.assertEqual(zf.read("dst.txt").decode("utf-8"), "old-data")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_staged_file_is_cleaned_when_fallback_copy_fails(self) -> None:
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-cleanup-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            src = root / "src.txt"
            dst = root / "dst.txt"
            backup_root = root / "backups"
            temp_root = root / ".tmp"

            src.write_text("new-data", encoding="utf-8")
            dst.write_text("old-data", encoding="utf-8")

            action = CopyAction(src=src, dst=dst, relative_path="dst.txt")
            plan = SyncPlan(to_local=[action], to_cloud=[])

            manager = BackupManager(
                backup_root=backup_root,
                machine_id="machine-a",
                retention_days=7,
                max_backups=0,
            )
            engine = SyncEngine(
                backup_manager=manager,
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=False,
            )

            staged = dst.parent / ".dst.txt.fixedhex.tmp"
            real_copy2 = shutil.copy2
            call_counter = {"n": 0}

            def copy2_side_effect(src_path, dst_path, *args, **kwargs):
                call_counter["n"] += 1
                if call_counter["n"] == 1:
                    return real_copy2(src_path, dst_path, *args, **kwargs)
                raise OSError("fallback copy failed")

            with patch("codexsync.sync_engine.uuid.uuid4", return_value=SimpleNamespace(hex="fixedhex")):
                with patch("codexsync.sync_engine.os.replace", side_effect=OSError("replace failed")):
                    with patch("codexsync.sync_engine.shutil.copy2", side_effect=copy2_side_effect):
                        with self.assertRaises(OSError):
                            engine.execute(plan, dry_run=False)

            self.assertFalse(staged.exists(), "staged file must be cleaned in finally")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_execute_cleans_orphaned_temp_files_before_apply(self) -> None:
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-orphans-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            backup_root = root / "backups"
            temp_root = root / ".tmp"
            temp_root.mkdir(parents=True, exist_ok=True)
            orphan = temp_root / "nested" / "leftover.tmp"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text("orphan", encoding="utf-8")

            manager = BackupManager(
                backup_root=backup_root,
                machine_id="machine-a",
                retention_days=7,
                max_backups=0,
            )
            engine = SyncEngine(
                backup_manager=manager,
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=True,
            )

            engine.execute(SyncPlan(), dry_run=False)
            self.assertFalse(orphan.exists(), "orphaned temp file should be removed on apply start")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
