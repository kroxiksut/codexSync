from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import time
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

            staged = dst.parent / ".dst.txt.fixedhex.codexsync.tmp"
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

    def test_apply_succeeds_when_temp_dir_is_on_another_volume(self) -> None:
        """A destination on another drive than `paths.temp_dir` (CS-253).

        Found by a live `sync --apply`: staging happened in `temp_dir` beside
        the cloud copy on `D:`, every destination was in `.codex` on `C:`, and
        `os.replace` across volumes is `WinError 17` — so nothing could ever be
        written into `.codex` on that ordinary layout, while the suite stayed
        green because a sandbox is one volume. The replace here refuses exactly
        for anything staged under `temp_dir`, which is what another volume
        means, and the apply must still go through.
        """
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-volumes-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            src = root / "src.txt"
            dst = root / "state" / "dst.txt"
            dst.parent.mkdir(parents=True, exist_ok=True)
            temp_root = root / ".tmp"
            temp_root.mkdir(parents=True, exist_ok=True)

            src.write_text("new-data", encoding="utf-8")
            dst.write_text("old-data", encoding="utf-8")

            plan = SyncPlan(
                to_local=[CopyAction(src=src, dst=dst, relative_path="dst.txt")], to_cloud=[]
            )
            engine = SyncEngine(
                backup_manager=BackupManager(
                    backup_root=root / "backups",
                    machine_id="machine-a",
                    retention_days=7,
                    max_backups=0,
                ),
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=True,
            )

            real_replace = os.replace
            crossings: list[str] = []

            def replace_across_volumes(source, target, *args, **kwargs):
                if temp_root.resolve() in Path(source).resolve().parents:
                    crossings.append(str(source))
                    error = OSError(errno.EXDEV, "Invalid cross-device link")
                    error.winerror = 17  # ERROR_NOT_SAME_DEVICE
                    raise error
                return real_replace(source, target, *args, **kwargs)

            with patch("codexsync.sync_engine.os.replace", side_effect=replace_across_volumes):
                engine.execute(plan, dry_run=False)

            self.assertEqual(crossings, [], "nothing may be staged on the other volume")
            self.assertEqual(dst.read_text(encoding="utf-8"), "new-data")
            self.assertEqual(
                [path.name for path in dst.parent.iterdir()],
                ["dst.txt"],
                "the staged sibling must be gone",
            )
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_stale_staging_siblings_are_swept_and_fresh_ones_kept(self) -> None:
        """Only a folder this run writes into, only this module's own names.

        A run killed mid-staging leaves its sibling behind, and nothing else
        looks there — `orphan_temp_files` only ever watched `temp_dir`. The age
        limit is what keeps a second machine's in-flight staging file in a
        shared cloud folder safe, so both halves are checked here.
        """
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-stale-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            src = root / "src.txt"
            dst = root / "state" / "dst.txt"
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("new-data", encoding="utf-8")
            dst.write_text("old-data", encoding="utf-8")

            stale = dst.parent / ".dst.txt.deadbeef.codexsync.tmp"
            stale.write_text("half a payload", encoding="utf-8")
            in_flight = dst.parent / ".other.txt.cafe1234.codexsync.tmp"
            in_flight.write_text("another machine is writing this", encoding="utf-8")
            untouched = dst.parent / "notes.tmp"
            untouched.write_text("a user file", encoding="utf-8")

            clock = {"now": time.time()}
            engine = SyncEngine(
                backup_manager=BackupManager(
                    backup_root=root / "backups",
                    machine_id="machine-a",
                    retention_days=7,
                    max_backups=0,
                ),
                temp_dir=root / ".tmp",
                backup_before_overwrite=True,
                fail_on_unknown=True,
                now=lambda: clock["now"],
            )

            engine.execute(
                SyncPlan(to_local=[CopyAction(src=src, dst=dst, relative_path="dst.txt")]),
                dry_run=False,
            )
            self.assertTrue(stale.exists(), "a sibling staged a moment ago is not an orphan")
            self.assertTrue(in_flight.exists())

            clock["now"] += 2 * 3600
            src.write_text("newer-data", encoding="utf-8")
            engine.execute(
                SyncPlan(to_local=[CopyAction(src=src, dst=dst, relative_path="dst.txt")]),
                dry_run=False,
            )
            self.assertFalse(stale.exists(), "an hour-old staging sibling is an orphan")
            self.assertFalse(in_flight.exists())
            self.assertTrue(untouched.exists(), "only this module's own names are swept")
            self.assertEqual(dst.read_text(encoding="utf-8"), "newer-data")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_execute_removes_an_orphaned_staging_directory(self) -> None:
        """`restore` extracts a snapshot into one, and a crash leaves it behind.

        Sweeping only `*.tmp` files meant such a directory stayed for good — one
        from 2026-09-05 was still in the live temp directory when this was found.
        """
        tmp_root = Path.cwd() / "test-sandbox"
        tmp_root.mkdir(parents=True, exist_ok=True)
        case_dir = tmp_root / f"sync-engine-stagedir-{uuid.uuid4().hex}"
        case_dir.mkdir(parents=True, exist_ok=False)
        try:
            root = case_dir
            temp_root = root / ".tmp"
            orphan = temp_root / ".codexsync-restore-19a99270"
            orphan.mkdir(parents=True, exist_ok=True)
            (orphan / "payload.bin").write_bytes(b"extracted")
            mine = temp_root / "keep-me"
            mine.mkdir(parents=True, exist_ok=True)

            clock = {"now": time.time()}
            engine = SyncEngine(
                backup_manager=BackupManager(
                    backup_root=root / "backups",
                    machine_id="machine-a",
                    retention_days=7,
                    max_backups=0,
                ),
                temp_dir=temp_root,
                backup_before_overwrite=True,
                fail_on_unknown=True,
                now=lambda: clock["now"],
            )
            engine.execute(SyncPlan(), dry_run=False)
            self.assertTrue(orphan.exists(), "a directory a running restore owns stays")

            clock["now"] += 2 * 3600
            engine.execute(SyncPlan(), dry_run=False)
            self.assertFalse(orphan.exists(), "an old staging directory is an orphan")
            self.assertTrue(mine.exists(), "only staging directories are removed")
        finally:
            shutil.rmtree(case_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
