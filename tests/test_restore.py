from __future__ import annotations

from pathlib import Path
import shutil
import textwrap
import unittest
import uuid
import zipfile
from unittest.mock import patch

from codexsync.app import restore_from_backup


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


if __name__ == "__main__":
    unittest.main()
