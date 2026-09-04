from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import unittest

from codexsync.app import collect_process_snapshot
from codexsync.models import (
    AppConfig,
    BackupConfig,
    ConflictConfig,
    FiltersConfig,
    IdentityConfig,
    LoggingConfig,
    PathsConfig,
    ProcessDetectionConfig,
    SafetyConfig,
    StateConfig,
    SyncConfig,
    TargetsConfig,
)
from codexsync.process_detector import ProcessInfo


class _DetectorStub:
    def __init__(self, main: list[ProcessInfo], children: list[ProcessInfo]) -> None:
        self._main = main
        self._children = children

    def get_subprocess_tree(self, _parent_process_names: list[str]) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
        return self._main, self._children

    def has_marker(self, proc: ProcessInfo, marker_name: str) -> bool:
        return proc.name.lower().removesuffix(".exe") == marker_name.lower().removesuffix(".exe")


class ProcessSnapshotTests(unittest.TestCase):
    @patch("codexsync.runtime.sys.platform", "win32")
    def test_windows_detects_known_background_marker(self) -> None:
        cfg = AppConfig(
            identity=IdentityConfig(machine_id="machine-a"),
            paths=PathsConfig(
                workspace_root_dir=Path("D:/x"),
                local_state_dir=Path("C:/Users/user/.codex"),
                cloud_root_dir=Path("D:/x/sync"),
                backup_dir=Path("D:/x/backups"),
                temp_dir=Path("D:/x/.tmp"),
            ),
            sync=SyncConfig(),
            safety=SafetyConfig(),
            process_detection=ProcessDetectionConfig(),
            backup=BackupConfig(),
            filters=FiltersConfig(),
            targets=TargetsConfig(),
            conflict=ConflictConfig(),
            state=StateConfig(data_version=1),
            logging=LoggingConfig(),
        )
        detector = _DetectorStub(
            main=[ProcessInfo(pid=100, name="Codex.exe")],
            children=[ProcessInfo(pid=101, name="codex-windows-sandbox.exe", parent_pid=100)],
        )

        snapshot = collect_process_snapshot(cfg, detector=detector)
        self.assertTrue(snapshot.sandbox_detected)


if __name__ == "__main__":
    unittest.main()
