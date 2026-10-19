"""`codex processes`: what the process check sees, safe to paste into an issue (CS-418).

A Mac owner had nothing to run: `-v` printed a snapshot only inside
`plan`/`sync`/`restore` (a full config needed), its sentences named
`codex.exe`, the background markers -- where `ChatGPT.app/Contents/MacOS/`
lives -- were never shown, and on an unproven platform the gate reads
`UNKNOWN` without listing anything. These tests hold the report to its
promises: it lists what it would act on, it says which rule matched, it never
shows a path or a command line, it needs no config, and it creates nothing.
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
from codexsync.config_locations import ConfigChoice
from codexsync.process_detector import ProcessDetectorCapability, ProcessInfo
from codexsync.process_report import build_process_report

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
#: What `ps` shows for the macOS app and a CLI under a user's home.
APP = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
CLI = "/Users/alice/.nvm/versions/node/v22/bin/codex"


class _Detector:
    def __init__(self, *, supported: bool, posix=(), windows=(), fail: bool = False) -> None:
        self._supported = supported
        self._posix = list(posix)
        self._windows = list(windows)
        self._fail = fail

    def capability(self) -> ProcessDetectorCapability:
        return ProcessDetectorCapability("darwin", 1, self._supported, "the ps adapter")

    def _list_posix_all(self):
        if self._fail:
            raise RuntimeError("ps is not available for process detection")
        return self._posix

    def _list_windows_all_complete(self):
        return self._windows


class MacTests(unittest.TestCase):
    def report(self, processes, *, supported=False):
        with mock.patch("codexsync.process_report.sys.platform", "darwin"), \
                mock.patch("codexsync.process_report._posix_parents", return_value={501: 1, 777: 501}):
            return build_process_report(
                ["codex.exe", "codex", "codex-app-server"],
                ["ChatGPT.app/Contents/MacOS/", "codex-app-server", "codex-execve-wrapper"],
                names_from="built-in",
                detector=_Detector(supported=supported, posix=processes),
            )

    def test_an_open_app_is_listed_although_the_gate_reads_unknown(self) -> None:
        report = self.report([
            ProcessInfo(501, APP, command_line=APP + " --remote alice@host"),
            ProcessInfo(777, CLI, command_line=CLI + " exec --secret-arg"),
            ProcessInfo(900, "/usr/sbin/cron", command_line="/usr/sbin/cron"),
        ])
        self.assertEqual((report.gate_state, report.would_read), ("UNKNOWN", "RUNNING"))
        app, cli = report.matches
        self.assertEqual((app.role, app.rule, app.matched_by), ("background", "ChatGPT.app/Contents/MacOS/", "path_marker"))
        self.assertEqual((app.name, app.parent_pid), ("ChatGPT", 1))
        self.assertEqual((cli.role, cli.rule, cli.matched_by, cli.name, cli.parent_pid), ("main", "codex", "name", "codex", 501))

    def test_no_path_and_no_command_line_reaches_the_output(self) -> None:
        report = self.report([
            ProcessInfo(501, APP, command_line=APP + " --remote alice@host"),
            ProcessInfo(777, CLI, command_line=CLI + " exec --secret-arg"),
        ])
        text = json.dumps(report.as_dict())
        for private in ("alice", "/Users/", "--remote", "--secret-arg", "/Applications/"):
            self.assertNotIn(private, text)

    def test_a_closed_codex_would_read_stopped(self) -> None:
        report = self.report([ProcessInfo(900, "/usr/sbin/cron", command_line="/usr/sbin/cron")])
        self.assertEqual((report.gate_state, report.would_read, report.matches), ("UNKNOWN", "STOPPED", ()))

    def test_a_proven_platform_reads_what_it_sees(self) -> None:
        report = self.report([ProcessInfo(777, CLI, command_line=CLI)], supported=True)
        self.assertEqual(report.gate_state, "RUNNING")

    def test_a_listing_that_fails_is_reported_not_raised(self) -> None:
        with mock.patch("codexsync.process_report.sys.platform", "darwin"), \
                mock.patch("codexsync.process_report._posix_parents", return_value={}):
            report = build_process_report(
                ["codex"], [], names_from="built-in", detector=_Detector(supported=True, fail=True),
            )
        self.assertEqual((report.gate_state, report.would_read), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("ps is not available", report.error)


class WindowsTests(unittest.TestCase):
    def test_main_child_and_background_with_the_first_rule(self) -> None:
        processes = [
            ProcessInfo(10, "codex.exe", parent_pid=1),
            ProcessInfo(11, "node.exe", parent_pid=10),
            ProcessInfo(12, "codex-windows-sandbox.exe", parent_pid=11),
            ProcessInfo(20, "codex-command-runner.exe", parent_pid=4),
            ProcessInfo(30, "explorer.exe", parent_pid=4),
        ]
        with mock.patch("codexsync.process_report.sys.platform", "win32"):
            report = build_process_report(
                ["codex.exe", "codex"], ["codex-windows-sandbox", "codex-command-runner"],
                names_from="config", detector=_Detector(supported=True, windows=processes),
            )
        roles = [(item.role, item.pid, item.rule) for item in report.matches]
        self.assertEqual(roles, [
            ("main", 10, "codex.exe"),
            ("child", 11, "codex.exe"),
            ("child", 12, "codex.exe"),
            ("background", 20, "codex-command-runner"),
        ])
        self.assertEqual(report.gate_state, "RUNNING")


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"process-report-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_main(self, argv):
        out = io.StringIO()
        fake = build_process_report(["codex"], [], names_from="built-in", detector=_Detector(supported=False))
        with contextlib.redirect_stdout(out), \
                mock.patch("codexsync.cli._resolve_config_path", return_value=ConfigChoice(None, "none", False)), \
                mock.patch("codexsync.cli.configure_logging") as logging_setup, \
                mock.patch("codexsync.app.build_process_report", return_value=fake) as built:
            code = main(argv)
        return code, out.getvalue(), logging_setup, built

    def test_it_needs_no_config_and_uses_the_names_this_version_knows(self) -> None:
        code, text, _, built = self.run_main(["codex", "processes"])
        self.assertEqual(code, 0)
        self.assertEqual(built.call_args.kwargs["names_from"], "built-in")
        self.assertIn("the safety gate reads: UNKNOWN", text)
        self.assertIn("can be pasted into an issue", text)

    def test_json_is_the_whole_report(self) -> None:
        code, text, _, _ = self.run_main(["codex", "processes", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual(data["gate_state"], "UNKNOWN")
        self.assertIn("matches", data)

    def test_it_writes_no_log_file_and_nothing_else(self) -> None:
        before = sorted(path.name for path in self.root.iterdir())
        _, _, logging_setup, _ = self.run_main(["codex", "processes"])
        for call in logging_setup.call_args_list:
            self.assertIsNone(call.args[0].file, "no log file is configured")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_a_config_supplies_its_own_names(self) -> None:
        from codexsync.config_edit import create_config

        config = self.root / "cfg" / "config.toml"
        create_config(
            config, machine_id="mac", local_state_dir=(self.root / "codex").as_posix(),
            workspace_root_dir=(self.root / "workspace").as_posix(),
        )
        fake = build_process_report(["codex"], [], names_from="config", detector=_Detector(supported=False))
        with contextlib.redirect_stdout(io.StringIO()), mock.patch("codexsync.cli.configure_logging"), \
                mock.patch("codexsync.app.build_process_report", return_value=fake) as built:
            self.assertEqual(main(["-c", str(config), "codex", "processes"]), 0)
        self.assertEqual(built.call_args.kwargs["names_from"], "config")


if __name__ == "__main__":
    unittest.main()
