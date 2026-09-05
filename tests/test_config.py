from __future__ import annotations

from pathlib import Path
import os
import shutil
import textwrap
import unittest
import uuid

from codexsync.config import load_config
from codexsync.exceptions import ConfigError


class ConfigTests(unittest.TestCase):
    def test_workspace_root_substitution(self) -> None:
        if os.name == "nt":
            workspace_root = "D:/codexSync"
            local_state = "C:/Users/user/.codex_test"
        else:
            workspace_root = "/tmp/codexSync"
            local_state = "/tmp/.codex_test"

        # load_config resolves every path, so the expectations must be resolved
        # too: on macOS /tmp is a symlink to /private/tmp.
        resolved_workspace = Path(workspace_root).resolve()
        expected_workspace = str(resolved_workspace)
        expected_cloud = str(resolved_workspace / "sync")
        expected_backup = str(resolved_workspace / "backups")
        expected_temp = str(resolved_workspace / ".tmp")
        expected_manifest = str(resolved_workspace / "state" / "manifest.json")
        expected_log = str(resolved_workspace / "logs" / "codexsync.log")

        root = Path.cwd() / "test-sandbox" / f"config-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [identity]
                    machine_id = "machine-a"

                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    workspace_root_dir = "{workspace_root}"
                    local_state_dir = "{local_state}"
                    cloud_root_dir = "${{workspace_root}}/sync"
                    backup_dir = "${{workspace_root}}/backups"
                    temp_dir = "${{workspace_root}}/.tmp"

                    [state]
                    manifest_file = "${{workspace_root}}/state/manifest.json"
                    data_version = 1

                    [logging]
                    file = "${{workspace_root}}/logs/codexsync.log"
                    format = "text"
                    """
                ).strip().format(workspace_root=workspace_root, local_state=local_state)
                + "\n",
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertEqual(str(cfg.paths.workspace_root_dir), expected_workspace)
            self.assertEqual(str(cfg.paths.cloud_root_dir), expected_cloud)
            self.assertEqual(str(cfg.paths.backup_dir), expected_backup)
            self.assertEqual(str(cfg.paths.temp_dir), expected_temp)
            assert cfg.state.manifest_file is not None
            self.assertEqual(str(cfg.state.manifest_file), expected_manifest)
            assert cfg.logging.file is not None
            self.assertEqual(str(cfg.logging.file), expected_log)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_background_process_names_by_os(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-bg-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"

                    [process_detection]
                    process_names = ["Codex.EXE", "CODEX", "codex"]
                    terminate_confirmation_mode = "CONSOLE"

                    [process_detection.background_process_names]
                    windows = ["codex-windows-sandbox", "codex-gpu-helper.exe"]
                    macos = ["codex-macos-helper"]
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertEqual(cfg.process_detection.process_names, ["codex.exe", "codex"])
            self.assertEqual(cfg.process_detection.terminate_confirmation_mode, "console")
            self.assertEqual(
                cfg.process_detection.background_process_names["windows"],
                ["codex-windows-sandbox", "codex-gpu-helper.exe"],
            )
            self.assertEqual(
                cfg.process_detection.background_process_names["macos"],
                ["codex-macos-helper"],
            )
            self.assertEqual(cfg.process_detection.background_process_names["linux"], [])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_sync_mode_values_are_normalized_and_validated(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-sync-mode-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"
                    equal_mtime_action = "PREFER_LOCAL"
                    session_mode = "LAST_DATE_ONLY"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertEqual(cfg.sync.equal_mtime_action, "prefer_local")
            self.assertEqual(cfg.sync.session_mode, "last_date_only")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_invalid_equal_mtime_action_fails(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-eq-mtime-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"
                    equal_mtime_action = "unknown"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "sync.equal_mtime_action"):
                load_config(cfg_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_invalid_session_mode_fails(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-session-mode-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"
                    session_mode = "latest_only"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "sync.session_mode"):
                load_config(cfg_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_backup_compression_zip_is_supported(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-backup-zip-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"

                    [backup]
                    compression = "zip"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertEqual(cfg.backup.compression, "zip")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_logging_rotation_settings_are_loaded(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-logging-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"

                    [logging]
                    archive_mode = "TEXT"
                    max_file_size_mb = 20
                    retention_days = 14
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            cfg = load_config(cfg_path)
            self.assertEqual(cfg.logging.archive_mode, "text")
            self.assertEqual(cfg.logging.max_file_size_mb, 20)
            self.assertEqual(cfg.logging.retention_days, 14)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_compare_mtime_hash_fallback_is_supported(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-compare-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "MTIME_HASH_FALLBACK"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.sync.compare, "mtime_hash_fallback")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_invalid_logging_archive_mode_fails(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-log-archive-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"

                    [logging]
                    archive_mode = "rar"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "logging.archive_mode"):
                load_config(cfg_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_invalid_logging_max_file_size_fails(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"config-log-size-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            cfg_path = root / "config.toml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    [sync]
                    mode = "cold"
                    direction = "bidirectional"
                    compare = "mtime"
                    delete_policy = "never"

                    [paths]
                    cloud_root_dir = "sync"
                    backup_dir = "backups"
                    temp_dir = ".tmp"

                    [logging]
                    max_file_size_mb = 0
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "logging.max_file_size_mb"):
                load_config(cfg_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
