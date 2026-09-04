from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from codexsync.backup import BackupManager
from codexsync.config import load_config, require_guardian_identity
from codexsync.exceptions import ConfigError
from codexsync.guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_PAYLOAD_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GuardianSnapshot,
    build_snapshot_id,
)


class GuardianModelsTests(unittest.TestCase):
    def test_default_root_is_local_to_the_config_not_codex_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._write_config(root, guardian_root=None, machine_id=None)

            cfg = load_config(config_path)

            self.assertEqual(cfg.guardian.root_dir, (root / "guardian").resolve())
            self.assertFalse(cfg.guardian.root_dir.exists())

    def test_explicit_guardian_config_requires_machine_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self._write_config(root, guardian_root=root / "guardian", machine_id=None)

            with self.assertRaisesRegex(ConfigError, "identity.machine_id"):
                load_config(config_path)

    def test_guardian_operation_rejects_empty_machine_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = load_config(self._write_config(root, guardian_root=None, machine_id=None))

            with self.assertRaisesRegex(ConfigError, "identity.machine_id"):
                require_guardian_identity(cfg)

    def test_root_inside_state_or_protected_roots_fails_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_state = root / ".codex"
            backup = root / "backups"
            cloud = root / "sync"
            staging = root / ".tmp"
            cases = {
                "state": local_state / "guardian",
                "backup": backup,
                "backup-child": backup / "guardian",
                "cloud": cloud / "guardian",
                "temp-parent": root,
                "temp": staging,
            }
            for name, guardian_root in cases.items():
                with self.subTest(name=name):
                    config_path = self._write_config(
                        root,
                        guardian_root=guardian_root,
                        machine_id="machine-a",
                        local_state=local_state,
                        backup=backup,
                        cloud=cloud,
                        temp_dir=staging,
                    )
                    with self.assertRaisesRegex(ConfigError, "guardian.root_dir"):
                        load_config(config_path)

    def test_symlink_escape_into_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            local_state = root / ".codex"
            local_state.mkdir()
            alias = root / "state-alias"
            try:
                alias.symlink_to(local_state, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")
            try:
                config_path = self._write_config(
                    root,
                    guardian_root=alias / "guardian",
                    machine_id="machine-a",
                    local_state=local_state,
                )
                with self.assertRaisesRegex(ConfigError, "paths.local_state_dir"):
                    load_config(config_path)
            finally:
                if alias.exists() or alias.is_symlink():
                    alias.unlink()

    def test_unicode_root_and_missing_parent_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            guardian_root = root / "данные" / "守护者"
            config_path = self._write_config(root, guardian_root=guardian_root, machine_id="машина 1")

            cfg = load_config(config_path)

            self.assertEqual(cfg.guardian.root_dir, guardian_root.resolve())
            self.assertFalse(guardian_root.exists())

    def test_snapshot_id_is_unique_at_same_timestamp_and_snapshot_paths_are_fixed(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
        digest = "a" * 64
        first_id = build_snapshot_id(
            digest,
            operation_id=UUID("11111111-1111-1111-1111-111111111111"),
            now=now,
        )
        second_id = build_snapshot_id(
            digest,
            operation_id=UUID("22222222-2222-2222-2222-222222222222"),
            now=now,
        )
        snapshot = GuardianSnapshot(
            root_dir=Path("guardian-root"),
            machine_id="machine-a",
            snapshot_id=first_id,
            generation=1,
        )

        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.endswith("-aaaaaaaaaaaa"))
        self.assertEqual(
            snapshot.directory,
            Path("guardian-root") / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a" / first_id,
        )
        self.assertEqual(snapshot.payload_path.name, GUARDIAN_PAYLOAD_NAME)
        self.assertEqual(snapshot.manifest_path.name, GUARDIAN_MANIFEST_NAME)
        self.assertEqual(snapshot.committed_path.name, GUARDIAN_COMMITTED_NAME)

    def test_backup_and_guardian_share_machine_id_normalization(self) -> None:
        manager = BackupManager(Path("backups"), " machine / a ")

        self.assertTrue(manager._snapshot_path.name.startswith("machine-a-"))

    @staticmethod
    def _write_config(
        root: Path,
        *,
        guardian_root: Path | None,
        machine_id: str | None,
        local_state: Path | None = None,
        cloud: Path | None = None,
        backup: Path | None = None,
        temp_dir: Path | None = None,
    ) -> Path:
        cloud = cloud or root / "sync"
        backup = backup or root / "backups"
        temp_dir = temp_dir or root / ".tmp"
        lines = [
            "[sync]",
            'mode = "cold"',
            'direction = "bidirectional"',
            'compare = "mtime"',
            'delete_policy = "never"',
            "",
            "[paths]",
            f'cloud_root_dir = "{cloud.as_posix()}"',
            f'backup_dir = "{backup.as_posix()}"',
            f'temp_dir = "{temp_dir.as_posix()}"',
        ]
        if local_state is not None:
            lines.append(f'local_state_dir = "{local_state.as_posix()}"')
        if machine_id is not None:
            lines.extend(["", "[identity]", f'machine_id = "{machine_id}"'])
        if guardian_root is not None:
            lines.extend(["", "[guardian]", f'root_dir = "{guardian_root.as_posix()}"'])
        config_path = root / "config.toml"
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config_path


if __name__ == "__main__":
    unittest.main()
