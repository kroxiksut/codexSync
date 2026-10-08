"""Tasks the 0.1 scheduler scripts installed are found and named, never touched.

0.1 shipped `scripts/scheduler/` to register a task (`codexSyncSync`, or a
LaunchAgent `com.codexsync.sync`) that ran `codexsync sync` on a timer. Under
0.2 that is a settings-only sync -- chats are carried by `handoff sync` -- so
the task keeps running, carries no chat, and nothing said so. No test here
runs PowerShell or touches the real `~/Library`: the runner and the home
folder are injected.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import shutil
import unittest
import uuid

from codexsync.automation import automation_status
from codexsync.cli import print_automation_status
from codexsync.config_edit import create_config
from codexsync.system_scheduler import LegacyTask, find_01_tasks

try:
    from tests.test_automation import FakeScheduler
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_automation import FakeScheduler

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class WindowsTests(unittest.TestCase):
    def find(self, stdout: str, returncode: int = 0) -> tuple[LegacyTask, ...]:
        self.argv: list[str] = []

        def run(argv: list[str]) -> _Result:
            self.argv = argv
            return _Result(stdout, returncode)

        return find_01_tasks("windows", run=run)

    def test_none_installed(self) -> None:
        self.assertEqual(self.find(""), ())

    def test_the_default_task_is_named_with_its_removal(self) -> None:
        found = self.find(json.dumps({"TaskPath": "\\", "TaskName": "codexSyncSync"}))
        self.assertEqual(found, (LegacyTask("\\codexSyncSync", 'schtasks /Delete /TN "\\codexSyncSync" /F'),))
        script = self.argv[-1]
        self.assertIn("codexSyncSync", script)
        self.assertIn("run-codexsync", script, "a renamed task is found by the script it runs")
        self.assertNotIn("Unregister", script, "finding never removes")

    def test_several_are_all_named(self) -> None:
        found = self.find(json.dumps([
            {"TaskPath": "\\", "TaskName": "codexSyncSync"},
            {"TaskPath": "\\Mine\\", "TaskName": "nightly sync"},
        ]))
        self.assertEqual([task.name for task in found], ["\\Mine\\nightly sync", "\\codexSyncSync"])

    def test_a_query_that_fails_says_nothing_rather_than_raising(self) -> None:
        self.assertEqual(self.find("access denied", returncode=1), ())
        self.assertEqual(self.find("not json"), ())

    def test_linux_had_no_scripts(self) -> None:
        self.assertEqual(find_01_tasks("linux", run=lambda argv: self.fail("nothing to ask")), ())


class LaunchdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = SANDBOX / f"legacy-home-{uuid.uuid4().hex[:8]}"
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.home, True)

    def agent(self, file_name: str, label: str, program: str) -> Path:
        path = self.agents / file_name
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/bash", program]}))
        return path

    def test_the_default_label_and_a_renamed_one_are_found_and_others_left_alone(self) -> None:
        default = self.agent("com.codexsync.sync.plist", "com.codexsync.sync", "/x/run-codexsync.sh")
        renamed = self.agent("me.sync.plist", "me.sync", "/Users/me/codexSync/scripts/scheduler/macos/run-codexsync.sh")
        self.agent("com.other.plist", "com.other", "/usr/local/bin/other")
        found = find_01_tasks("darwin", home=self.home)
        self.assertEqual([task.name for task in found], [str(default), str(renamed)])
        self.assertIn('gui/$(id -u)/me.sync', found[1].remove_command)
        self.assertTrue(default.exists() and renamed.exists(), "finding never removes")

    def test_no_launch_agents_folder(self) -> None:
        self.assertEqual(find_01_tasks("darwin", home=self.home / "nobody"), ())


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"legacy-status-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(
            self.config, machine_id="laptop", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"),
        )
        self.adapters = dict(
            scheduler=FakeScheduler(), login_scheduler=FakeScheduler(),
            backup_scheduler=FakeScheduler(), handoff_scheduler=FakeScheduler(),
        )

    def test_injected_adapters_never_bring_a_real_query(self) -> None:
        self.assertEqual(automation_status(self.config, **self.adapters).legacy_tasks, ())

    def test_found_tasks_are_reported_and_printed_with_their_removal(self) -> None:
        task = LegacyTask("\\codexSyncSync", 'schtasks /Delete /TN "\\codexSyncSync" /F')
        view = automation_status(self.config, **self.adapters, find_legacy=lambda: (task,))
        self.assertEqual(view.legacy_tasks, (task,))
        out = io.StringIO()
        with redirect_stdout(out):
            print_automation_status(view)
        text = out.getvalue()
        self.assertIn("Task left by codexSync 0.1: \\codexSyncSync", text)
        self.assertIn('schtasks /Delete /TN "\\codexSyncSync" /F', text)
        self.assertIn("never chats", text)


if __name__ == "__main__":
    unittest.main()
