"""`doctor --report` carries nothing of the user's (CS-393).

The report exists so an issue can say something about a state without the
state: versions, formats, gates, check statuses and counts. These tests plant
private values everywhere a report could pick them up -- the machine name,
paths with a user name in them, a project name, a chat's working folder and
first message -- and fail if any one of them reaches the output.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.cli import main
from codexsync.config_edit import create_config
from codexsync.issue_report import REPORT_FORMAT, build_issue_report
from codexsync.process_detector import ProcessDetectorCapability
from codexsync.process_report import build_process_report

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
CHAT = "019ce072-1068-7270-ade9-d4cdff67902e"
PRIVATE = ("alice-secret", "machine-zeta", "Secret Project Orion", "first message about orion", "orion-folder")


class _Detector:
    def capability(self) -> ProcessDetectorCapability:
        return ProcessDetectorCapability("darwin", 1, False, "the ps adapter")

    def _list_posix_all(self):
        return []

    def _list_windows_all_complete(self):
        return []


class IssueReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"issue-report-{uuid.uuid4().hex[:8]}" / "alice-secret"
        self.codex = self.root / "codex"
        day = self.codex / "sessions" / "2026" / "03" / "12"
        day.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root.parent, True)
        records = [
            {"type": "session_meta", "timestamp": "2026-03-12T13:08:17Z",
             "payload": {"id": CHAT, "cwd": "C:/Users/alice-secret/orion-folder", "timestamp": "2026-03-12T13:08:17Z"}},
            {"type": "event", "timestamp": "2026-03-12T13:09:00Z", "text": "first message about orion"},
        ]
        (day / f"rollout-2026-03-12T13-08-17-{CHAT}.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8",
        )
        (self.codex / ".codex-global-state.json").write_text(json.dumps({
            "local-projects": {"p": {"id": "p", "name": "Secret Project Orion",
                                     "rootPaths": ["C:/Users/alice-secret/orion-folder"], "createdAt": 1, "updatedAt": 2}},
            "project-order": ["p"],
            "thread-project-assignments": {},
            "app-server-project-id-by-legacy-project-id-by-host": {},
        }), encoding="utf-8")
        (self.codex / "models_cache.json").write_text(json.dumps({"client_version": "0.163.0"}), encoding="utf-8")
        self.config = self.root / "cfg" / "config.toml"
        create_config(
            self.config, machine_id="machine-zeta", local_state_dir=self.codex.as_posix(),
            workspace_root_dir=(self.root / "workspace").as_posix(),
        )
        fake = build_process_report(["codex"], [], names_from="config", detector=_Detector())
        for target, value in (
            ("codexsync.runtime.collect_process_snapshot", None),
            ("codexsync.issue_report.build_process_report", fake),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_nothing_private_reaches_the_report(self) -> None:
        report = build_issue_report(self.config)
        text = json.dumps(report)
        for private in PRIVATE:
            self.assertNotIn(private, text, private)
        self.assertNotIn(str(self.root), text)

    def test_it_says_what_an_issue_needs(self) -> None:
        report = build_issue_report(self.config)
        self.assertEqual(report["format"], REPORT_FORMAT)
        self.assertEqual(report["codex_version"], "0.163.0")
        self.assertEqual(report["global_state"]["projects"], 1)
        self.assertEqual(report["global_state"]["schema"], "electron-v2")
        self.assertEqual(report["sessions"]["files"], 1)
        self.assertEqual(report["sessions"]["record_formats"], {"legacy": 1})
        self.assertIn("codex_compat", report["checks"])
        self.assertTrue(report["formats"]["compatible"])
        self.assertEqual(report["process"]["gate_state"], "UNKNOWN")
        self.assertIn("layouts", report["gates"])

    def test_the_command_prints_it(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("codexsync.cli.configure_logging"):
            self.assertEqual(main(["-c", str(self.config), "doctor", "--report"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["format"], REPORT_FORMAT)
        for private in PRIVATE:
            self.assertNotIn(private, out.getvalue())


if __name__ == "__main__":
    unittest.main()
