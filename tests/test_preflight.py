from __future__ import annotations

from pathlib import Path
import shutil
import textwrap
import unittest
import uuid
from unittest.mock import patch

from codexsync.app import ProcessSnapshot, run_preflight
from codexsync.process_detector import ProcessInfo


def _write_config(root: Path, *, manifest_data_version: int = 1) -> Path:
    local_state = root / "local-state"
    cloud_root = root / "cloud"
    backup_root = root / "backups"
    temp_root = root / ".tmp"
    manifest = root / "state" / "manifest.json"

    local_state.mkdir(parents=True, exist_ok=True)
    cloud_root.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)

    config_path = root / "config.toml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            [sync]
            mode = "cold"
            direction = "bidirectional"
            compare = "mtime"
            delete_policy = "never"

            [paths]
            local_state_dir = "{local_state.as_posix()}"
            cloud_root_dir = "{cloud_root.as_posix()}"
            backup_dir = "{backup_root.as_posix()}"
            temp_dir = "{temp_root.as_posix()}"

            [state]
            manifest_file = "{manifest.as_posix()}"
            data_version = {manifest_data_version}

            [targets]
            include_roots = ["sessions", "session_index.jsonl"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path


class PreflightTests(unittest.TestCase):
    def test_preflight_ok_for_healthy_environment(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"preflight-ok-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            config_path = _write_config(root)
            with patch(
                "codexsync.runtime.collect_process_snapshot",
                return_value=ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False),
            ):
                report = run_preflight(config_path)

            self.assertTrue(report.is_ok)
            self.assertFalse(report.failures)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_preflight_fails_on_manifest_version_mismatch(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"preflight-manifest-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            config_path = _write_config(root, manifest_data_version=1)
            manifest_path = root / "state" / "manifest.json"
            manifest_path.write_text(
                '{"data_version": 2, "files": {}}',
                encoding="utf-8",
            )

            with patch(
                "codexsync.runtime.collect_process_snapshot",
                return_value=ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False),
            ):
                report = run_preflight(config_path)

            self.assertFalse(report.is_ok)
            self.assertTrue(any(item.name == "manifest" and item.status == "FAIL" for item in report.checks))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_preflight_warns_on_orphan_temp_files(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"preflight-temp-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            config_path = _write_config(root)
            orphan = root / ".tmp" / "old.orphan.tmp"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text("x", encoding="utf-8")

            with patch(
                "codexsync.runtime.collect_process_snapshot",
                return_value=ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False),
            ):
                report = run_preflight(config_path)

            self.assertTrue(any(item.name == "orphan_temp_files" and item.status == "WARN" for item in report.checks))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_doctor_reports_running_codex_without_mutating(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"preflight-process-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            config_path = _write_config(root)
            snapshot = ProcessSnapshot(
                main_processes=[ProcessInfo(pid=123, name="codex.exe")],
                subprocesses=[],
                sandbox_detected=False,
            )
            with patch("codexsync.runtime.collect_process_snapshot", return_value=snapshot):
                report = run_preflight(config_path)

            self.assertTrue(report.is_ok)
            self.assertTrue(any(item.name == "codex_process" and item.status == "WARN" for item in report.checks))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
