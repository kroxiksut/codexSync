from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import textwrap
import unittest
import uuid
import zipfile
from unittest.mock import patch

from codexsync.app import restore_from_backup
from codexsync.backup import BackupManager
from codexsync.restore import BackupSnapshotInfo, _verify_backup_snapshot, list_backup_snapshots


class _StoppedGate:
    def require(self, *_args, **_kwargs):
        return None


class RestoreTests(unittest.TestCase):
    def test_restore_from_latest_snapshot_to_local(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"restore-{uuid.uuid4().hex}"
        local_state = root / "local-state"
        cloud_root = root / "cloud"
        backup_root = root / "backups"
        temp_root = root / ".tmp"
        config_path = root / "config.toml"

        local_state.mkdir(parents=True, exist_ok=True)
        cloud_root.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        temp_root.mkdir(parents=True, exist_ok=True)

        snapshot_old = backup_root / "machine-a-20260101T000000Z"
        snapshot_new = backup_root / "machine-a-20260101T000100Z"
        (snapshot_old / "data").mkdir(parents=True, exist_ok=True)
        (snapshot_new / "data").mkdir(parents=True, exist_ok=True)
        (snapshot_old / "data" / "a.txt").write_text("old", encoding="utf-8")
        (snapshot_new / "data" / "a.txt").write_text("new", encoding="utf-8")

        config_path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                direction = "bidirectional"
                compare = "mtime"
                delete_policy = "never"

                [safety]
                require_codex_stopped = false
                fail_on_unknown = true

                [paths]
                workspace_root_dir = "{root.as_posix()}"
                local_state_dir = "{local_state.as_posix()}"
                cloud_root_dir = "{cloud_root.as_posix()}"
                backup_dir = "{backup_root.as_posix()}"
                temp_dir = "{temp_root.as_posix()}"

                [targets]
                include_roots = ["data"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        try:
            with patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
                result = restore_from_backup(
                    config_path=config_path,
                    snapshot_name=snapshot_new.name,
                    target="local",
                    dry_run=False,
                    allow_legacy_snapshot=True,
                )

            restored = local_state / "data" / "a.txt"
            self.assertTrue(restored.exists())
            self.assertEqual(restored.read_text(encoding="utf-8"), "new")
            self.assertEqual(result.snapshot_name, snapshot_new.name)
            self.assertEqual(result.target, "local")
            self.assertEqual(result.restored_files, 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_restore_from_zip_snapshot_to_local(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"restore-zip-{uuid.uuid4().hex}"
        local_state = root / "local-state"
        cloud_root = root / "cloud"
        backup_root = root / "backups"
        temp_root = root / ".tmp"
        config_path = root / "config.toml"

        local_state.mkdir(parents=True, exist_ok=True)
        cloud_root.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        temp_root.mkdir(parents=True, exist_ok=True)

        snapshot_zip = backup_root / "machine-a-20260101T000200Z.zip"
        with zipfile.ZipFile(snapshot_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("data/a.txt", "zip-new")

        config_path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                direction = "bidirectional"
                compare = "mtime"
                delete_policy = "never"

                [safety]
                require_codex_stopped = false
                fail_on_unknown = true

                [paths]
                workspace_root_dir = "{root.as_posix()}"
                local_state_dir = "{local_state.as_posix()}"
                cloud_root_dir = "{cloud_root.as_posix()}"
                backup_dir = "{backup_root.as_posix()}"
                temp_dir = "{temp_root.as_posix()}"

                [backup]
                compression = "zip"

                [targets]
                include_roots = ["data"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

        try:
            with patch("codexsync.restore._make_safety_gate", return_value=_StoppedGate()):
                result = restore_from_backup(
                    config_path=config_path,
                    snapshot_name=snapshot_zip.name,
                    target="local",
                    dry_run=False,
                    allow_legacy_snapshot=True,
                )

            restored = local_state / "data" / "a.txt"
            self.assertTrue(restored.exists())
            self.assertEqual(restored.read_text(encoding="utf-8"), "zip-new")
            self.assertEqual(result.snapshot_name, snapshot_zip.name)
            self.assertEqual(result.target, "local")
            self.assertEqual(result.restored_files, 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _tree(root: Path) -> dict[str, tuple[bool, int, int]]:
    """Names, sizes and mtimes of everything under ``root``, including ``root``.

    Directory mtimes are part of it on purpose: creating and then removing a
    file still moves its parent's mtime, so a listing that leaves a probe file
    behind *or* cleans one up is caught.
    """
    if not root.exists():
        return {}
    result = {".": (True, 0, root.stat().st_mtime_ns)}
    for path in root.rglob("*"):
        stat_result = path.stat()
        result[path.relative_to(root).as_posix()] = (
            path.is_dir(),
            0 if path.is_dir() else stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    return result


class BackupSnapshotListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"backup-list-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.backup_root = self.root / "backups"
        self.temp_root = self.root / ".tmp"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                direction = "bidirectional"
                compare = "mtime"

                [paths]
                local_state_dir = "{(self.root / 'local-state').as_posix()}"
                cloud_root_dir = "{(self.root / 'cloud').as_posix()}"
                backup_dir = "{self.backup_root.as_posix()}"
                temp_dir = "{self.temp_root.as_posix()}"

                [guardian]
                root_dir = "{(self.root / 'guardian').as_posix()}"

                [semantic]
                root_dir = "{(self.root / 'semantic').as_posix()}"

                [targets]
                include_roots = ["data"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _backup(self, files: dict[str, bytes], *, compression: str = "none") -> str:
        """Produce a snapshot exactly the way a mutation does: backup, then finalize."""
        manager = BackupManager(self.backup_root, "machine-a", compression=compression)
        for relative, payload in files.items():
            source = self.root / "source" / uuid.uuid4().hex / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
            manager.backup_file(source, relative)
        self.assertIsNotNone(manager.finalize())
        return manager.snapshot_name

    def _by_name(self) -> dict[str, BackupSnapshotInfo]:
        return {item.name: item for item in list_backup_snapshots(self.config_path)}

    def test_committed_directory_and_zip_snapshots_are_described_from_their_manifests(self) -> None:
        plain = self._backup({"data/a.txt": b"alpha", "data/b.txt": b"bee"})
        zipped = self._backup({"data/c.txt": b"sea"}, compression="zip")

        listed = self._by_name()

        self.assertEqual(set(listed), {plain, zipped})
        self.assertEqual(listed[plain].entries, 2)
        self.assertEqual(listed[plain].total_bytes, 8)
        self.assertFalse(listed[plain].compressed)
        self.assertEqual(listed[zipped].entries, 1)
        self.assertEqual(listed[zipped].total_bytes, 3)
        self.assertTrue(listed[zipped].compressed)
        for name, info in listed.items():
            self.assertTrue(info.committed)
            self.assertFalse(info.legacy)
            self.assertIsNone(info.problem)
            self.assertEqual(info.machine, "machine-a")
            self.assertRegex(info.created_utc or "", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertRegex(info.modified_utc, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            # "committed" in the listing must mean restore accepts the snapshot.
            _verify_backup_snapshot(self.backup_root / name, allow_legacy_snapshot=False)

    def test_legacy_uncommitted_damaged_and_mismatched_snapshots_are_reported_not_hidden(self) -> None:
        legacy = self.backup_root / "machine-a-20260101T000000Z"
        (legacy / "data").mkdir(parents=True)
        (legacy / "data" / "a.txt").write_text("old", encoding="utf-8")

        uncommitted = self._backup({"data/a.txt": b"u"})
        manifest = self.backup_root / f"{uncommitted}.manifest.json"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["committed"] = False
        manifest.write_text(json.dumps(raw), encoding="utf-8")

        damaged = self._backup({"data/a.txt": b"d"})
        (self.backup_root / f"{damaged}.manifest.json").write_text("{", encoding="utf-8")

        donor = self._backup({"data/a.txt": b"donor"})
        mismatched = self._backup({"data/a.txt": b"m"})
        shutil.copyfile(
            self.backup_root / f"{donor}.manifest.json",
            self.backup_root / f"{mismatched}.manifest.json",
        )

        listed = self._by_name()

        self.assertTrue(listed[legacy.name].legacy)
        self.assertFalse(listed[legacy.name].committed)
        self.assertIsNone(listed[legacy.name].problem)
        self.assertIsNone(listed[legacy.name].entries)
        # Pre-suffix names do not match the current naming, so nothing is guessed.
        self.assertIsNone(listed[legacy.name].machine)
        self.assertIsNone(listed[legacy.name].created_utc)

        for name, fragment in ((uncommitted, "uncommitted"), (damaged, "invalid"), (mismatched, "does not match")):
            with self.subTest(name=name):
                info = listed[name]
                self.assertFalse(info.committed)
                self.assertFalse(info.legacy)
                self.assertIsNone(info.entries)
                self.assertIsNone(info.total_bytes)
                self.assertIn(fragment, info.problem or "")
        self.assertTrue(listed[donor].committed)

    def test_newest_first_by_name_timestamp_falling_back_to_mtime(self) -> None:
        names = {
            "machine-a-20260102T000000Z-aaaaaaaaaaaa": None,
            "machine-a-20260101T000000Z-bbbbbbbbbbbb": None,
            "hand-made-newest": datetime(2026, 1, 3, tzinfo=timezone.utc),
            "hand-made-oldest": datetime(2025, 6, 1, tzinfo=timezone.utc),
        }
        for name, mtime in names.items():
            (self.backup_root / name).mkdir(parents=True)
            if mtime is not None:
                os.utime(self.backup_root / name, (mtime.timestamp(), mtime.timestamp()))

        listed = [item.name for item in list_backup_snapshots(self.config_path)]

        self.assertEqual(
            listed,
            [
                "hand-made-newest",
                "machine-a-20260102T000000Z-aaaaaaaaaaaa",
                "machine-a-20260101T000000Z-bbbbbbbbbbbb",
                "hand-made-oldest",
            ],
        )

    def test_manifest_and_temporary_files_are_not_snapshots(self) -> None:
        self.backup_root.mkdir(parents=True)
        (self.backup_root / "orphan.manifest.json").write_text("{}", encoding="utf-8")
        (self.backup_root / "half.manifest.json.tmp").write_text("{", encoding="utf-8")
        (self.backup_root / "staging.tmp").mkdir()
        (self.backup_root / "notes.txt").write_text("not a snapshot", encoding="utf-8")

        self.assertEqual(list_backup_snapshots(self.config_path), [])

    def test_missing_backup_dir_lists_nothing_and_creates_nothing(self) -> None:
        before = _tree(self.root)

        self.assertEqual(list_backup_snapshots(self.config_path), [])

        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.backup_root.exists())
        self.assertFalse(self.temp_root.exists())

    def test_listing_creates_nothing_and_never_hashes_a_payload(self) -> None:
        self._backup({"data/a.txt": b"alpha" * 1000})
        self._backup({"data/b.txt": b"beta"}, compression="zip")
        before = _tree(self.root)

        forbidden = AssertionError("the listing must not read snapshot payloads")
        with patch("codexsync.restore._snapshot_hash_inventory", side_effect=forbidden), \
                patch("codexsync.restore._hash_file", side_effect=forbidden), \
                patch("codexsync.restore.zipfile.ZipFile", side_effect=forbidden):
            listed = list_backup_snapshots(self.config_path)

        self.assertEqual(len(listed), 2)
        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.temp_root.exists())


if __name__ == "__main__":
    unittest.main()
