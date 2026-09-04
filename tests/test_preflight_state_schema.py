"""Diagnostics for the two failures that were previously invisible.

Guardian quarantined every real state file because its schema was not
recognised, so no snapshot was ever committed and `latest-good` never existed.
Nothing reported that. `doctor` passed, and the only symptom was an exit code
from a command the user had no reason to run.

These checks make both conditions visible before they matter.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import ProcessSnapshot, run_preflight


def _snapshot():
    return ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False)


ELECTRON_STATE = {
    "local-projects": {"p1": {"id": "p1", "name": "n", "rootPaths": ["C:/a"]}},
    "project-order": ["p1"],
    "thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "p1"}},
    "app-server-project-id-by-legacy-project-id-by-host": {"local:host": {"p1": "as1"}},
}


class PreflightStateSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"preflight-schema-{uuid.uuid4().hex}"
        self.local = self.root / "local-state"
        self.guardian = self.root / "guardian"
        for path in (self.local, self.root / "cloud", self.root / "backups", self.root / ".tmp"):
            path.mkdir(parents=True, exist_ok=True)
        self.config_path = self._write_config()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_config(self) -> Path:
        path = self.root / "config.toml"
        path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                session_mode = "all"

                [paths]
                local_state_dir = "{self.local.as_posix()}"
                cloud_root_dir = "{(self.root / 'cloud').as_posix()}"
                backup_dir = "{(self.root / 'backups').as_posix()}"
                temp_dir = "{(self.root / '.tmp').as_posix()}"

                [guardian]
                root_dir = "{self.guardian.as_posix()}"

                [semantic]
                root_dir = "{(self.root / 'semantic').as_posix()}"

                [targets]
                include_roots = ["sessions"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _state(self, payload) -> None:
        target = self.local / ".codex-global-state.json"
        if isinstance(payload, (bytes, bytearray)):
            target.write_bytes(payload)
        else:
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _run(self):
        with patch("codexsync.runtime.collect_process_snapshot", return_value=_snapshot()):
            return run_preflight(self.config_path)

    def _check(self, report, name):
        return next(item for item in report.checks if item.name == name)

    # --- schema recognition ------------------------------------------------

    def test_a_recognised_state_passes_and_names_its_schema(self) -> None:
        self._state(ELECTRON_STATE)
        check = self._check(self._run(), "global_state_schema")
        self.assertEqual(check.status, "PASS")
        self.assertIn("electron-v2", check.details)

    def test_an_unrecognised_state_fails_loudly(self) -> None:
        """This is the condition that made Guardian inert without saying so."""
        self._state({"local-projects": {}, "project-order": [],
                     "thread-project-assignments": {"t1": {"unknownShape": True}}})
        report = self._run()
        check = self._check(report, "global_state_schema")
        self.assertEqual(check.status, "FAIL")
        self.assertIn("quarantine", check.details.lower())
        self.assertFalse(report.is_ok, "an unrecognised schema must fail preflight")

    def test_a_missing_state_file_warns_rather_than_fails(self) -> None:
        check = self._check(self._run(), "global_state_schema")
        self.assertEqual(check.status, "WARN")

    # --- restorable snapshot -----------------------------------------------

    def test_no_latest_good_pointer_is_reported(self) -> None:
        self._state(ELECTRON_STATE)
        check = self._check(self._run(), "guardian_latest_good")
        self.assertEqual(check.status, "WARN")
        self.assertIn("guardian snapshot", check.details)

    def test_a_pointer_to_a_missing_snapshot_fails(self) -> None:
        self._state(ELECTRON_STATE)
        pointer = self.guardian / "latest-good" / "machine-a.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"snapshot_id": "gone"}), encoding="utf-8")
        check = self._check(self._run(), "guardian_latest_good")
        self.assertEqual(check.status, "FAIL")

    def test_a_committed_snapshot_passes(self) -> None:
        self._state(ELECTRON_STATE)
        snapshot = self.guardian / "snapshots" / "machine-a" / "snap-1"
        snapshot.mkdir(parents=True)
        (snapshot / "COMMITTED").write_text("{}", encoding="utf-8")
        pointer = self.guardian / "latest-good" / "machine-a.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"snapshot_id": "snap-1"}), encoding="utf-8")
        check = self._check(self._run(), "guardian_latest_good")
        self.assertEqual(check.status, "PASS")
        self.assertIn("snap-1", check.details)


if __name__ == "__main__":
    unittest.main()
