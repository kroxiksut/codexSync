from __future__ import annotations

import json
from pathlib import Path
import shutil
import textwrap
import unittest
import uuid
from unittest.mock import patch

from codexsync.app import ProcessSnapshot, run_preflight
from codexsync.process_detector import ProcessInfo

NEWLINE = b"\n"


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

    def test_preflight_warns_on_an_orphan_staging_directory(self) -> None:
        """A directory `restore` extracted a snapshot into is an orphan too.

        The check counted files only, so an empty staging directory from a
        crashed run was reported as "no orphan temp files" indefinitely.
        """
        root = Path.cwd() / "test-sandbox" / f"preflight-stagedir-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            config_path = _write_config(root)
            (root / ".tmp" / ".codexsync-restore-19a99270").mkdir(parents=True, exist_ok=True)

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


class SessionIndexCheckTests(unittest.TestCase):
    """The index check reports the journal; it never judges its shape.

    A repeated id and a session with no line at all are what an append/update
    journal looks like -- on a real machine, 191 records for 151 sessions -- so
    neither may become a warning. What is a warning is a record that will not
    parse or two readings of a repeated id disagreeing.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"preflight-index-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=False)
        self.config_path = _write_config(self.root)
        self.index = self.root / "local-state" / "session_index.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rows: list[dict]) -> None:
        self.index.write_bytes(
            b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + NEWLINE for row in rows)
        )

    def _check(self):
        with patch(
            "codexsync.runtime.collect_process_snapshot",
            return_value=ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False),
        ):
            report = run_preflight(self.config_path)
        return next(item for item in report.checks if item.name == "session_index")

    def test_an_absent_index_is_not_an_absent_set_of_sessions(self) -> None:
        check = self._check()
        self.assertEqual(check.status, "PASS")
        self.assertIn("No session_index.jsonl", check.details)

    def test_a_repeated_id_is_the_journal_working_and_not_a_warning(self) -> None:
        self._write([
            {"id": "s1", "thread_name": "one", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": "s1", "thread_name": "one renamed", "updated_at": "2026-01-02T00:00:00Z"},
            {"id": "s2", "thread_name": "two", "updated_at": "2026-01-01T00:00:00Z"},
        ])
        check = self._check()
        self.assertEqual(check.status, "PASS")
        self.assertIn("records=3", check.details)
        self.assertIn("sessions=2", check.details)

    def test_a_clock_that_ran_backwards_is_reported(self) -> None:
        """The two readings disagree exactly here, and that has to be visible."""
        self._write([
            {"id": "s1", "thread_name": "later", "updated_at": "2026-01-02T00:00:00Z"},
            {"id": "s1", "thread_name": "earlier", "updated_at": "2026-01-01T00:00:00Z"},
        ])
        check = self._check()
        self.assertEqual(check.status, "WARN")
        self.assertIn("REDUCTION_AMBIGUOUS", check.details)

    def test_a_thread_name_never_reaches_the_report(self) -> None:
        self._write([{"id": "s1", "thread_name": "a private title", "updated_at": "2026-01-01T00:00:00Z"}])
        self.assertNotIn("a private title", self._check().details)


if __name__ == "__main__":
    unittest.main()
