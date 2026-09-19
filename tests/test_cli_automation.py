"""`codexsync automation`, the deprecated `guardian scheduler`, and the GUI exe's dispatch.

No test here reaches the operating system's scheduler: every app function the
CLI calls is patched where the CLI imported it (`codexsync.cli.<name>`).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

from codexsync.app import AutomationRun, AutomationView
from codexsync.cli import build_parser, main
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.system_scheduler import SchedulerStatus

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"
ENTRYPOINT = REPO_ROOT / "scripts" / "pyinstaller_gui_entrypoint.py"

ARGV = ("C:/Python/pythonw.exe", "-m", "codexsync", "-c", "C:/cfg/config.toml", "guardian", "snapshot", "--once")


def _view(**overrides: object) -> AutomationView:
    values: dict[str, object] = dict(
        enabled=True,
        mode="guardian_snapshot",
        interval_seconds=300,
        run_at_login=True,
        startup_delay_seconds=15,
        jitter_seconds=30,
        argv=ARGV,
        ignored=("jitter_seconds",),
        reports_run_times=True,
        status=SchedulerStatus(
            installed=True,
            enabled=True,
            last_run_utc="2026-09-13T10:00:00Z",
            next_run_utc="2026-09-13T10:05:00Z",
            last_result=0,
            detail="",
            definition_matches=True,
        ),
        status_error=None,
    )
    values.update(overrides)
    return AutomationView(**values)  # type: ignore[arg-type]


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition(": ")
        if sep:
            fields.setdefault(key, value)
    return fields


class AutomationParserTests(unittest.TestCase):
    def test_the_group_has_exactly_the_four_commands(self) -> None:
        parser = build_parser()
        for command in ("status", "apply", "remove", "run"):
            args = parser.parse_args(["automation", command])
            self.assertEqual(args.automation_command, command)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["automation", "install"])


class AutomationStatusTests(unittest.TestCase):
    def test_status_prints_config_argv_and_os_task_and_exits_zero(self) -> None:
        with mock.patch("codexsync.cli.automation_status", return_value=_view()) as status:
            code, out, _ = _run_cli(["-c", "cfg.toml", "automation", "status"])

        self.assertEqual(code, 0)
        status.assert_called_once_with(Path("cfg.toml"))
        fields = _fields(out)
        self.assertEqual(fields["enabled"], "yes")
        self.assertEqual(fields["mode"], "guardian_snapshot")
        self.assertEqual(fields["interval_seconds"], "300")
        self.assertEqual(fields["run_at_login"], "yes")
        self.assertEqual(fields["startup_delay_seconds"], "15")
        self.assertEqual(fields["jitter_seconds"], "30")
        self.assertEqual(json.loads(fields["argv"]), list(ARGV))
        self.assertEqual(fields["ignored_settings"], "jitter_seconds")
        self.assertEqual(fields["installed"], "yes")
        self.assertEqual(fields["definition_matches"], "yes")
        self.assertEqual(fields["last_run_utc"], "2026-09-13T10:00:00Z")
        self.assertEqual(fields["next_run_utc"], "2026-09-13T10:05:00Z")
        self.assertEqual(fields["last_result"], "0")
        self.assertNotIn("automation apply", out)

    def test_a_task_that_drifted_from_the_config_points_to_apply(self) -> None:
        drifted = _view(status=SchedulerStatus(installed=True, enabled=True, definition_matches=False))
        with mock.patch("codexsync.cli.automation_status", return_value=drifted):
            code, out, _ = _run_cli(["automation", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(_fields(out)["definition_matches"], "no")
        self.assertIn("codexsync automation apply", out)

    def test_enabled_but_not_installed_points_to_apply(self) -> None:
        missing = _view(status=SchedulerStatus(installed=False, detail="Scheduled task is not installed"))
        with mock.patch("codexsync.cli.automation_status", return_value=missing):
            code, out, _ = _run_cli(["automation", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(_fields(out)["installed"], "no")
        self.assertIn("codexsync automation apply", out)

    def test_a_platform_without_run_times_says_so_instead_of_never(self) -> None:
        view = _view(reports_run_times=False, ignored=())
        with mock.patch("codexsync.cli.automation_status", return_value=view):
            _, out, _ = _run_cli(["automation", "status"])

        fields = _fields(out)
        self.assertEqual(fields["ignored_settings"], "(none)")
        self.assertIn("not reported", fields["last_run_utc"])

    def test_an_unreachable_scheduler_is_reported_not_raised(self) -> None:
        view = _view(status=None, status_error="schtasks.exe not found")
        with mock.patch("codexsync.cli.automation_status", return_value=view):
            code, out, _ = _run_cli(["automation", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(_fields(out)["status_error"], "schtasks.exe not found")

    def test_a_bad_config_is_exit_four(self) -> None:
        with mock.patch("codexsync.cli.automation_status", side_effect=ConfigError("bad")):
            with self.assertLogs("codexsync.cli", level="ERROR"):
                code, _, _ = _run_cli(["automation", "status"])
        self.assertEqual(code, 4)


class AutomationApplyRemoveTests(unittest.TestCase):
    def test_apply_prints_the_resulting_status(self) -> None:
        with mock.patch("codexsync.cli.apply_automation", return_value=_view()) as apply:
            code, out, _ = _run_cli(["-c", "cfg.toml", "automation", "apply"])

        self.assertEqual(code, 0)
        apply.assert_called_once_with(Path("cfg.toml"))
        self.assertIn("installed or updated", out)
        self.assertEqual(_fields(out)["installed"], "yes")

    def test_apply_with_scheduler_disabled_reports_removal(self) -> None:
        disabled = _view(enabled=False, status=SchedulerStatus(installed=False))
        with mock.patch("codexsync.cli.apply_automation", return_value=disabled):
            code, out, _ = _run_cli(["automation", "apply"])

        self.assertEqual(code, 0)
        self.assertIn("enabled = false", out)
        self.assertNotIn("codexsync automation apply", out)

    def test_a_refused_apply_is_a_safe_abort(self) -> None:
        with mock.patch("codexsync.cli.apply_automation", side_effect=FailSafeError("not updated")):
            with self.assertLogs("codexsync.cli", level="ERROR"):
                code, _, _ = _run_cli(["automation", "apply"])
        self.assertEqual(code, 5)

    def test_remove_leaves_the_config_and_says_apply_would_reinstall(self) -> None:
        for existed, message in ((True, "Scheduled task removed."), (False, "No scheduled task")):
            with self.subTest(existed=existed):
                with mock.patch("codexsync.cli.remove_automation", return_value=existed) as remove:
                    code, out, _ = _run_cli(["-c", "cfg.toml", "automation", "remove"])
                self.assertEqual(code, 0)
                remove.assert_called_once_with(Path("cfg.toml"))
                self.assertIn(message, out)
                self.assertIn("enabled = true", out)
                self.assertIn("automation apply", out)

    def test_a_refused_remove_is_a_safe_abort(self) -> None:
        with mock.patch("codexsync.cli.remove_automation", side_effect=FailSafeError("not removed")):
            with self.assertLogs("codexsync.cli", level="ERROR"):
                code, _, _ = _run_cli(["automation", "remove"])
        self.assertEqual(code, 5)


class AutomationRunTests(unittest.TestCase):
    def test_exit_code_follows_the_job_status(self) -> None:
        cases = (
            ("guardian_snapshot", "COMMITTED", 0),
            ("guardian_snapshot", "UNCHANGED", 0),
            ("guardian_snapshot", "BUSY", 0),
            ("guardian_snapshot", "QUARANTINED", 2),
            ("guardian_snapshot", "FAILED", 5),
            ("preflight", "PASSED", 0),
            ("preflight", "FAILED", 5),
            ("sync_dry_run", "DRY_RUN_FINISHED", 0),
            ("guardian_snapshot", "SOMETHING_NEW", 5),
        )
        for mode, status, expected in cases:
            with self.subTest(mode=mode, status=status):
                result = AutomationRun(mode, status, "why" if status == "FAILED" else "")
                with mock.patch("codexsync.cli.run_automation_job", return_value=result) as run:
                    code, out, _ = _run_cli(["-c", "cfg.toml", "automation", "run"])
                self.assertEqual(code, expected)
                run.assert_called_once_with(Path("cfg.toml"))
                fields = _fields(out)
                self.assertEqual(fields["mode"], mode)
                if status == "FAILED":
                    self.assertEqual(fields["detail"], "why")

    def test_busy_is_labelled_like_guardian_snapshot(self) -> None:
        with mock.patch("codexsync.cli.run_automation_job", return_value=AutomationRun("guardian_snapshot", "BUSY")):
            _, out, _ = _run_cli(["automation", "run"])
        self.assertIn("Automation run: SKIPPED_ACTIVE_GUARDIAN", out)

    def test_actions_are_printed_for_a_dry_run(self) -> None:
        result = AutomationRun("sync_dry_run", "DRY_RUN_FINISHED", actions=7)
        with mock.patch("codexsync.cli.run_automation_job", return_value=result):
            _, out, _ = _run_cli(["automation", "run"])
        self.assertEqual(_fields(out)["actions"], "7")

    def test_codex_running_during_a_dry_run_sync_is_exit_three(self) -> None:
        with mock.patch("codexsync.cli.run_automation_job", side_effect=SafetyPreconditionError("running")):
            with self.assertLogs("codexsync.cli", level="ERROR"):
                code, _, _ = _run_cli(["automation", "run"])
        self.assertEqual(code, 3)


class DeprecatedGuardianSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"cli-guardian-scheduler-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_still_writes_its_templates_and_warns_once(self) -> None:
        output = self.root / "out"
        code, out, err = _run_cli([
            "-c", str(self.root / "config.toml"),
            "guardian", "scheduler",
            "--platform", "macos",
            "--output-dir", str(output),
            "--log-dir", str(self.root / "logs"),
        ])

        self.assertEqual(code, 0)
        self.assertIn("templates written: 1", out)
        self.assertEqual([path.name for path in output.iterdir()], ["io.codexsync.guardian.plist"])
        warnings = [line for line in err.splitlines() if "DEPRECATED" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("codexsync automation apply", warnings[0])

    def test_its_help_says_it_is_deprecated(self) -> None:
        parser = build_parser()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(["guardian", "--help"])
        self.assertIn("DEPRECATED", out.getvalue())


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("codexsync_gui_entrypoint_under_test", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuiExeDispatchTests(unittest.TestCase):
    """The windowed exe is also what the scheduled task runs; it must route correctly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.entry = _load_entrypoint()

    def _dispatch(self, argv: list[str]) -> str:
        calls: list[tuple[str, list[str]]] = []

        def cli(args: list[str]) -> int:
            calls.append(("cli", args))
            return 5

        def gui(args: list[str]) -> int:
            calls.append(("gui", args))
            return 0

        code = self.entry.main(argv, cli_main=cli, gui_main=gui)
        self.assertEqual(len(calls), 1)
        target, passed = calls[0]
        self.assertEqual(passed, argv)
        self.assertEqual(code, 5 if target == "cli" else 0)
        return target

    def test_cli_argv_routes_to_the_cli(self) -> None:
        for argv in (
            ["-c", "C:/cfg/config.toml", "guardian", "snapshot", "--once"],
            ["--config", "validate", "validate"],  # the first `validate` is the config value
            ["--config=x.toml", "automation", "status"],
            ["-v", "doctor"],
            ["init-config"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self._dispatch(argv), "cli")

    def test_gui_argv_routes_to_the_window(self) -> None:
        for argv in ([], ["-c", "x.toml"], ["--config", "x.toml"], ["-c", "sync"], ["--config=validate"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._dispatch(argv), "gui")

    def test_every_cli_command_name_is_recognised_from_the_parser(self) -> None:
        parser = build_parser()
        commands = next(
            action.choices for action in parser._actions if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        self.assertIn("automation", commands)
        for name in commands:
            with self.subTest(command=name):
                self.assertTrue(self.entry.is_cli_invocation(["-c", "x.toml", name]))
        self.assertFalse(self.entry.is_cli_invocation(["-c", "x.toml", "no-such-command"]))

    def test_an_exception_in_a_cli_run_becomes_an_exit_code_not_a_dialog(self) -> None:
        def broken(args: list[str]) -> int:
            raise RuntimeError("boom")

        with contextlib.redirect_stderr(io.StringIO()):
            code = self.entry.main(["validate"], cli_main=broken, gui_main=lambda args: 0)
        self.assertEqual(code, 1)

    def test_missing_standard_streams_are_pointed_at_the_null_device(self) -> None:
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            self.entry.ensure_standard_streams()
            try:
                self.assertIsNotNone(sys.stdout)
                self.assertIsNotNone(sys.stderr)
                print("goes nowhere")
                print("goes nowhere", file=sys.stderr)
            finally:
                sys.stdout.close()
                sys.stderr.close()

    def test_existing_streams_are_left_alone(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            self.entry.ensure_standard_streams()
            self.assertIs(sys.stdout, out)
            self.assertIs(sys.stderr, err)


if __name__ == "__main__":
    unittest.main()
