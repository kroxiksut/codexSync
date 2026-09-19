"""Tests for installing the periodic safe job into the OS scheduler.

No test here may run ``schtasks``, ``launchctl``, ``systemctl`` or
``powershell``: every adapter gets a fake ``run``, and ``subprocess.run`` in the
module under test is patched to fail loudly, so a missed injection is an error
instead of a task registered on the developer's machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import itertools
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import uuid
from xml.etree import ElementTree

from codexsync import system_scheduler as ss
from codexsync.cli import build_parser
from codexsync.system_scheduler import (
    JOB_MODES,
    JobDefinition,
    LaunchdScheduler,
    ScheduledJob,
    SchedulerError,
    SystemdUserScheduler,
    WindowsTaskScheduler,
    job_arguments,
    job_command,
    system_scheduler,
)


SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "test-sandbox"
TASK_NAME = "\\CodexSync\\CodexSync Job"


@dataclass
class FakeResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRun:
    """Records every argv and answers from a responder."""

    def __init__(self, responder=None) -> None:
        self.calls: list[list[str]] = []
        self._responder = responder or (lambda argv: FakeResult())

    def __call__(self, argv: list[str]) -> FakeResult:
        self.calls.append(list(argv))
        return self._responder(list(argv))


def split_windows_arguments(command_line: str) -> list[str]:
    """``CommandLineToArgvW`` rules for every argument after the program name."""
    args: list[str] = []
    i, n = 0, len(command_line)
    while True:
        while i < n and command_line[i] in " \t":
            i += 1
        if i >= n:
            return args
        current: list[str] = []
        in_quotes = False
        while i < n:
            char = command_line[i]
            if char == "\\":
                j = i
                while j < n and command_line[j] == "\\":
                    j += 1
                count = j - i
                if j < n and command_line[j] == '"':
                    current.append("\\" * (count // 2))
                    if count % 2:
                        current.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    current.append("\\" * count)
                    i = j
                continue
            if char == '"':
                if in_quotes and i + 1 < n and command_line[i + 1] == '"':
                    current.append('"')
                    i += 2
                    continue
                in_quotes = not in_quotes
                i += 1
                continue
            if char in " \t" and not in_quotes:
                break
            current.append(char)
            i += 1
        args.append("".join(current))


class SandboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX_ROOT / f"system-scheduler-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        guard = mock.patch.object(
            ss.subprocess, "run", side_effect=AssertionError("a test reached the real subprocess.run")
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.program = self.root / "Program Files" / "Python 3.12" / "pythonw.exe"
        self.config = self.root / "My Config" / "config.toml"
        self.logs = self.root / "logs"
        self.command = [str(self.program), "-m", "codexsync"]

    def definition(self, job: ScheduledJob) -> JobDefinition:
        return JobDefinition(job, tuple(self.command), self.config, self.logs)


# --------------------------------------------------------------------------
# Job model
# --------------------------------------------------------------------------

class ScheduledJobTests(SandboxCase):
    def test_only_three_modes_exist_and_anything_else_is_rejected(self) -> None:
        self.assertEqual(set(JOB_MODES), {"guardian_snapshot", "preflight", "sync_dry_run"})
        for mode in ("sync", "sync_apply", "restore", "repair", "", "SYNC_DRY_RUN"):
            with self.assertRaises(ValueError, msg=mode):
                ScheduledJob(mode, 60, True)

    def test_numeric_limits(self) -> None:
        ScheduledJob("preflight", 60, False)
        for kwargs in (
            {"interval_seconds": 59},
            {"interval_seconds": True},
            {"interval_seconds": 60.0},
            {"startup_delay_seconds": -1},
            {"jitter_seconds": -1},
            {"jitter_seconds": False},
        ):
            values = {"mode": "preflight", "interval_seconds": 60, "run_at_login": True, **kwargs}
            with self.assertRaises(ValueError, msg=kwargs):
                ScheduledJob(**values)
        with self.assertRaises(ValueError):
            ScheduledJob("preflight", 60, 1)  # type: ignore[arg-type]

    def test_job_arguments_are_exact(self) -> None:
        cfg = str(self.config)
        self.assertEqual(job_arguments("guardian_snapshot", self.config), ["-c", cfg, "guardian", "snapshot", "--once"])
        self.assertEqual(job_arguments("preflight", self.config), ["-c", cfg, "preflight", "--for", "sync"])
        self.assertEqual(job_arguments("sync_dry_run", self.config), ["-c", cfg, "sync", "--dry-run"])
        with self.assertRaises(ValueError):
            job_arguments("preflight", Path("config.toml"))
        with self.assertRaises(ValueError):
            job_arguments("restore", self.config)

    def test_no_mode_can_express_a_mutation(self) -> None:
        forbidden = {
            "--apply", "--confirm-plan", "--force", "restore", "repair-projects", "recover",
            "sessions", "chats", "init-config", "apply", "move", "rollback", "resume",
        }
        parser = build_parser()
        for mode in JOB_MODES:
            args = job_arguments(mode, self.config)
            self.assertFalse(forbidden & set(args), (mode, args))
            parsed = parser.parse_args(args)
            if parsed.command == "sync":
                # The CLI's mutually exclusive group makes this a dry run for good.
                self.assertTrue(parsed.dry_run)
                self.assertFalse(parsed.apply)
            else:
                self.assertIn(parsed.command, {"guardian", "preflight"})

    def test_definition_requires_absolute_paths(self) -> None:
        job = ScheduledJob("preflight", 60, True)
        with self.assertRaises(ValueError):
            JobDefinition(job, ("pythonw.exe", "-m", "codexsync"), self.config, self.logs)
        with self.assertRaises(ValueError):
            JobDefinition(job, (), self.config, self.logs)
        with self.assertRaises(ValueError):
            JobDefinition(job, tuple(self.command), self.config, Path("logs"))
        with self.assertRaises(ValueError):
            JobDefinition(job, tuple(self.command), Path("config.toml"), self.logs)


class JobCommandTests(SandboxCase):
    def test_frozen_build_is_its_own_cli(self) -> None:
        exe = self.root / "codexsync.exe"
        self.assertEqual(job_command(exe, frozen=True, platform="win32"), [str(exe)])

    def test_windows_prefers_pythonw_next_to_the_interpreter(self) -> None:
        bin_dir = self.root / "venv" / "Scripts"
        bin_dir.mkdir(parents=True)
        python = bin_dir / "python.exe"
        python.write_bytes(b"")
        self.assertEqual(job_command(python, frozen=False, platform="win32"), [str(python), "-m", "codexsync"])
        (bin_dir / "pythonw.exe").write_bytes(b"")
        self.assertEqual(
            job_command(python, frozen=False, platform="win32"),
            [str(bin_dir / "pythonw.exe"), "-m", "codexsync"],
        )

    def test_posix_uses_the_interpreter_itself(self) -> None:
        python = self.root / "bin" / "python3"
        (self.root / "bin").mkdir()
        (self.root / "bin" / "pythonw.exe").write_bytes(b"")
        self.assertEqual(job_command(python, frozen=False, platform="darwin"), [str(python), "-m", "codexsync"])

    def test_defaults_come_from_the_running_interpreter(self) -> None:
        command = job_command()
        self.assertTrue(command)
        if not getattr(sys, "frozen", False):
            self.assertEqual(command[1:], ["-m", "codexsync"])


class FactoryTests(SandboxCase):
    def test_picks_adapter_by_platform(self) -> None:
        run = FakeRun()
        self.assertIsInstance(system_scheduler("win32", run=run, temp_dir=self.root), WindowsTaskScheduler)
        self.assertIsInstance(system_scheduler("darwin", run=run, home=self.root, uid=501), LaunchdScheduler)
        self.assertIsInstance(system_scheduler("linux", run=run, home=self.root), SystemdUserScheduler)
        with self.assertRaises(ValueError):
            system_scheduler("sunos5")

    def test_default_run_hides_the_console_and_decodes_the_console_code_page(self) -> None:
        with mock.patch.object(ss.subprocess, "run", return_value=FakeResult()) as fake:
            ss._default_run(["tool", "/x"])
        (argv,), kwargs = fake.call_args
        self.assertEqual(argv, ["tool", "/x"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(kwargs["encoding"], "oem")
        else:
            self.assertNotIn("creationflags", kwargs)

    def test_a_tool_that_cannot_start_is_a_scheduler_error(self) -> None:
        def missing(argv):
            raise FileNotFoundError(argv[0])

        adapter = WindowsTaskScheduler(run=missing, temp_dir=self.root, schtasks="schtasks")
        with self.assertRaises(SchedulerError):
            adapter.status()


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

class WindowsQuotingTests(unittest.TestCase):
    CASES = [
        "plain",
        "",
        r"C:\Program Files\Python\pythonw.exe",
        "C:\\dir with space\\",
        "C:\\trailing\\",
        'a"b',
        'a\\"b',
        'say "hi" there',
        "\\\\server\\share\\dir with space\\",
        "tab\there",
        'C:\\a\\\\"q',
        "ends with two\\\\",
    ]

    def test_round_trips_through_a_command_line_splitter(self) -> None:
        line = " ".join(ss.quote_windows_argument(item) for item in self.CASES)
        self.assertEqual(split_windows_arguments(line), self.CASES)

    def test_agrees_with_list2cmdline_on_what_the_program_receives(self) -> None:
        for item in self.CASES:
            ours = split_windows_arguments(ss.quote_windows_argument(item))
            theirs = split_windows_arguments(subprocess.list2cmdline([item]))
            self.assertEqual(ours, theirs, item)

    @unittest.skipUnless(sys.platform == "win32", "CommandLineToArgvW is Windows-only")
    def test_round_trips_through_command_line_to_argv_w(self) -> None:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        line = "program.exe " + " ".join(ss.quote_windows_argument(item) for item in self.CASES)
        count = ctypes.c_int()
        argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
        try:
            parsed = [argv[i] for i in range(count.value)]
        finally:
            kernel32.LocalFree(argv)
        self.assertEqual(parsed[1:], self.CASES)

    def test_a_quoted_trailing_backslash_does_not_swallow_the_next_argument(self) -> None:
        # Escaping only the quote (scheduler._windows_quote) turns this into
        # one argument ending in '" next'.
        value = "C:\\dir with space\\"
        self.assertEqual(ss.quote_windows_argument(value), '"C:\\dir with space\\\\"')
        self.assertEqual(split_windows_arguments(ss.quote_windows_argument(value) + " next"), [value, "next"])


class WindowsRenderTests(SandboxCase):
    NOW = datetime(2026, 9, 13, 14, 30, 5, 123456)

    def adapter(self, run=None) -> WindowsTaskScheduler:
        return WindowsTaskScheduler(
            run=run or FakeRun(),
            user_id="DESKTOP\\krox",
            now=lambda: self.NOW,
            temp_dir=self.root,
            protected_roots=[self.root / ".codex"],
            schtasks="schtasks.exe",
            powershell="powershell.exe",
        )

    def parse_rendered(self, job: ScheduledJob) -> ElementTree.Element:
        payload = self.adapter().render(self.definition(job))["codexsync-job.xml"]
        path = self.root / f"rendered-{uuid.uuid4().hex}.xml"
        path.write_bytes(payload)
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xff\xfe"), "UTF-16 LE BOM expected")
        # Well-formed as bytes, honouring the declared UTF-16 encoding.
        ElementTree.fromstring(data)
        return ss.parse_task_xml(data.decode("utf-16"))

    def test_every_trigger_combination(self) -> None:
        for login, delay, jitter, interval in itertools.product((True, False), (0, 45), (0, 30), (60, 90, 3600)):
            with self.subTest(login=login, delay=delay, jitter=jitter, interval=interval):
                root = self.parse_rendered(ScheduledJob("guardian_snapshot", interval, login, delay, jitter))
                triggers = list(root.find("Triggers"))
                self.assertEqual(len(triggers), 1)
                trigger = triggers[0]
                self.assertEqual(trigger.tag, "LogonTrigger" if login else "TimeTrigger")
                expected_interval = {60: "PT1M", 90: "PT90S", 3600: "PT60M"}[interval]
                self.assertEqual(trigger.findtext("Repetition/Interval"), expected_interval)
                self.assertEqual(trigger.findtext("Repetition/StopAtDurationEnd"), "false")
                self.assertIsNone(trigger.find("Repetition/Duration"))
                if login:
                    self.assertEqual(trigger.findtext("UserId"), "DESKTOP\\krox")
                    self.assertEqual(trigger.findtext("Delay"), "PT45S" if delay else None)
                    # Task Scheduler's schema rejects RandomDelay on a LogonTrigger.
                    self.assertIsNone(trigger.find("RandomDelay"))
                    self.assertIsNone(trigger.find("StartBoundary"))
                else:
                    self.assertEqual(trigger.findtext("StartBoundary"), "2026-09-13T14:30:05")
                    self.assertEqual(trigger.findtext("RandomDelay"), "PT30S" if jitter else None)
                    self.assertIsNone(trigger.find("Delay"))
                ignored = self.adapter().ignored_settings(ScheduledJob("guardian_snapshot", interval, login))
                self.assertEqual(ignored, ("jitter_seconds",) if login else ("startup_delay_seconds",))

    def test_principal_settings_and_action(self) -> None:
        root = self.parse_rendered(ScheduledJob("sync_dry_run", 300, True))
        self.assertEqual(root.findtext("Principals/Principal/LogonType"), "InteractiveToken")
        self.assertEqual(root.findtext("Principals/Principal/RunLevel"), "LeastPrivilege")
        self.assertEqual(root.findtext("Principals/Principal/UserId"), "DESKTOP\\krox")
        settings = root.find("Settings")
        self.assertEqual(settings.findtext("MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(settings.findtext("StartWhenAvailable"), "true")
        self.assertEqual(settings.findtext("DisallowStartIfOnBatteries"), "false")
        self.assertEqual(settings.findtext("StopIfGoingOnBatteries"), "false")
        self.assertEqual(settings.findtext("ExecutionTimeLimit"), "PT10M")
        self.assertEqual(settings.findtext("Hidden"), "false")
        actions = list(root.find("Actions"))
        self.assertEqual(len(actions), 1)
        self.assertEqual(split_windows_arguments(actions[0].findtext("Command")), [str(self.program)])
        self.assertEqual(
            split_windows_arguments(actions[0].findtext("Arguments")),
            ["-m", "codexsync", "-c", str(self.config), "sync", "--dry-run"],
        )
        text = ElementTree.tostring(root, encoding="unicode")
        for marker in ("S-1-5-18", "SYSTEM", "HighestAvailable", "ServiceAccount", "--apply"):
            self.assertNotIn(marker, text)

    def test_xml_special_characters_are_escaped(self) -> None:
        config = self.root / "R&D <team>" / "config.toml"
        definition = JobDefinition(ScheduledJob("preflight", 60, False), tuple(self.command), config, self.logs)
        root = ss.parse_task_xml(ss.render_task_xml(definition, user_id="D\\u&v", now=self.NOW))
        arguments = split_windows_arguments(root.findtext("Actions/Exec/Arguments"))
        self.assertIn(str(config), arguments)

    def test_task_scheduler_environment_expansion_is_refused(self) -> None:
        config = self.root / "%USERPROFILE%" / "config.toml"
        definition = JobDefinition(ScheduledJob("preflight", 60, False), tuple(self.command), config, self.logs)
        with self.assertRaises(ValueError):
            ss.render_task_xml(definition, user_id="D\\u", now=self.NOW)


def _windows_rewrite(xml_text: str, *, sid: str = "S-1-5-21-1-2-3-1001") -> str:
    """Imitate how Task Scheduler hands a registered definition back.

    Elements reordered, defaults dropped, principal user turned into a SID,
    minutes written as seconds, a different StartBoundary, the lying UTF-16
    declaration and ``\\r\\r\\n`` line ends of ``schtasks /Query /XML``.
    """
    root = ss.parse_task_xml(xml_text)
    settings = root.find("Settings")
    for name in ("Hidden", "Enabled", "MultipleInstancesPolicy"):
        element = settings.find(name)
        if element is not None:
            settings.remove(element)
    principal = root.find("Principals/Principal")
    principal.find("UserId").text = sid
    for trigger in root.find("Triggers"):
        for child in list(trigger):
            trigger.remove(child)
            trigger.append(child)  # reversed-ish order
        interval = trigger.find("Repetition/Interval")
        interval.text = f"PT{ss._duration_seconds(interval.text)}S"
        boundary = trigger.find("StartBoundary")
        if boundary is not None:
            boundary.text = "2020-01-01T00:00:00"
        enabled = trigger.find("Enabled")
        trigger.remove(enabled)
    task = ElementTree.Element("Task", {"version": "1.4", "xmlns": ss._TASK_NAMESPACE})
    for name in ("RegistrationInfo", "Principals", "Settings", "Triggers", "Actions"):
        task.append(root.find(name))
    body = ElementTree.tostring(task, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-16"?>\n' + body).replace("\n", "\r\r\n")


class WindowsAdapterTests(WindowsRenderTests):
    def test_install_writes_utf16_xml_to_temp_and_removes_it(self) -> None:
        seen: dict[str, bytes] = {}

        def responder(argv):
            path = Path(argv[argv.index("/XML") + 1])
            seen["path"] = str(path)
            seen["payload"] = path.read_bytes()
            return FakeResult()

        run = FakeRun(responder)
        job = ScheduledJob("guardian_snapshot", 60, True, 30, 0)
        self.adapter(run).install(job, command=self.command, config_path=self.config, log_dir=self.logs)
        self.assertEqual(run.calls, [["schtasks.exe", "/Create", "/XML", seen["path"], "/TN", TASK_NAME, "/F"]])
        path = Path(seen["path"])
        self.assertEqual(path.parent, self.root)
        self.assertFalse(path.exists(), "temporary task XML must be removed")
        self.assertTrue(seen["payload"].startswith(b"\xff\xfe"))
        self.assertIn("LogonTrigger", seen["payload"].decode("utf-16"))

    def test_install_failure_raises_and_still_removes_the_temp_file(self) -> None:
        paths: list[Path] = []

        def responder(argv):
            paths.append(Path(argv[argv.index("/XML") + 1]))
            return FakeResult(1, "", "ОШИБКА: отказано в доступе.")

        with self.assertRaises(SchedulerError):
            self.adapter(FakeRun(responder)).install(
                ScheduledJob("preflight", 60, False), command=self.command, config_path=self.config, log_dir=self.logs
            )
        self.assertFalse(paths[0].exists())

    def test_install_refuses_a_temp_dir_inside_codex_state(self) -> None:
        run = FakeRun()
        adapter = WindowsTaskScheduler(run=run, user_id="D\\u", temp_dir=self.root / ".codex" / "tmp",
                                       protected_roots=[self.root / ".codex"], schtasks="schtasks.exe")
        with self.assertRaises(SchedulerError):
            adapter.install(ScheduledJob("preflight", 60, False), command=self.command,
                            config_path=self.config, log_dir=self.logs)
        self.assertEqual(run.calls, [])
        self.assertFalse((self.root / ".codex").exists())

    def test_remove(self) -> None:
        run = FakeRun(lambda argv: FakeResult(0))
        adapter = self.adapter(run)
        self.assertTrue(adapter.remove())
        self.assertEqual(run.calls[0], ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"])
        # Then the folder install created, and only if nothing else lives in it.
        self.assertEqual(run.calls[1], adapter._remove_empty_folder_argv())
        self.assertIn("-eq 0", run.calls[1][-1])
        self.assertEqual(len(run.calls), 2)

        run = FakeRun(lambda argv: FakeResult(1, "", "ОШИБКА: Не удается найти указанный файл."))
        self.assertFalse(self.adapter(run).remove())
        self.assertEqual(run.calls[1], ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML"])

        def still_there(argv):
            return FakeResult(1, "", "denied") if "/Delete" in argv else FakeResult(0, "<Task/>")

        with self.assertRaises(SchedulerError):
            self.adapter(FakeRun(still_there)).remove()

    def test_status_not_installed_does_not_ask_powershell(self) -> None:
        run = FakeRun(lambda argv: FakeResult(1, "", "ОШИБКА"))
        status = self.adapter(run).status(self.definition(ScheduledJob("preflight", 60, True)))
        self.assertFalse(status.installed)
        self.assertIsNone(status.definition_matches)
        self.assertEqual(len(run.calls), 1)

    def installed_responder(self, xml_text: str, info: FakeResult):
        def responder(argv):
            if argv[0] == "schtasks.exe":
                return FakeResult(0, xml_text)
            return info
        return responder

    def test_status_compares_a_rewritten_definition_semantically(self) -> None:
        job = ScheduledJob("guardian_snapshot", 120, False, 0, 15)
        definition = self.definition(job)
        stored = _windows_rewrite(ss.render_task_xml(definition, user_id="DESKTOP\\krox", now=self.NOW))
        info = FakeResult(0, '{"LastRunTime":"\\/Date(1757750400000)\\/","NextRunTime":"\\/Date(1757750520000)\\/","LastTaskResult":0}\r\n')
        run = FakeRun(self.installed_responder(stored, info))
        adapter = self.adapter(run)

        status = adapter.status(definition)
        self.assertTrue(status.installed)
        self.assertTrue(status.enabled)
        self.assertTrue(status.definition_matches, status.detail)
        self.assertEqual(status.last_run_utc, "2025-09-13T08:00:00Z")
        self.assertEqual(status.next_run_utc, "2025-09-13T08:02:00Z")
        self.assertEqual(status.last_result, 0)
        self.assertEqual(run.calls[0], ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/XML"])
        self.assertEqual(run.calls[1], [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-ScheduledTaskInfo -TaskPath '\\CodexSync\\' -TaskName 'CodexSync Job'"
            " | Select-Object LastRunTime,NextRunTime,LastTaskResult | ConvertTo-Json -Compress",
        ])

        self.assertIsNone(adapter.status().definition_matches)
        for other in (
            self.definition(ScheduledJob("guardian_snapshot", 180, False, 0, 15)),
            self.definition(ScheduledJob("guardian_snapshot", 120, False, 0, 0)),
            self.definition(ScheduledJob("guardian_snapshot", 120, True, 0, 15)),
            self.definition(ScheduledJob("preflight", 120, False, 0, 15)),
            JobDefinition(job, tuple(self.command), self.root / "other.toml", self.logs),
        ):
            with self.subTest(other=other):
                changed = adapter.status(other)
                self.assertFalse(changed.definition_matches)
                self.assertIn("differs", changed.detail)

    def test_status_reports_disabled_and_never_run(self) -> None:
        definition = self.definition(ScheduledJob("preflight", 60, True, 10))
        stored = ss.render_task_xml(definition, user_id="DESKTOP\\krox", now=self.NOW).replace(
            "<Enabled>true</Enabled>\r\n    <ExecutionTimeLimit>", "<Enabled>false</Enabled>\r\n    <ExecutionTimeLimit>"
        )
        info = FakeResult(0, '{"LastRunTime":"\\/Date(943920000000)\\/","NextRunTime":null,"LastTaskResult":267011}')
        status = self.adapter(FakeRun(self.installed_responder(stored, info))).status(definition)
        self.assertTrue(status.installed)
        self.assertFalse(status.enabled)
        self.assertTrue(status.definition_matches, "disabling is reported as enabled=False, not as drift")
        self.assertIsNone(status.last_run_utc)
        self.assertIsNone(status.next_run_utc)
        self.assertIsNone(status.last_result)
        self.assertIn("has not run", status.detail)

    def test_status_survives_a_powershell_failure(self) -> None:
        definition = self.definition(ScheduledJob("preflight", 60, True))
        stored = ss.render_task_xml(definition, user_id="DESKTOP\\krox", now=self.NOW)
        info = FakeResult(1, "", "Get-ScheduledTaskInfo : Доступ запрещен.")
        status = self.adapter(FakeRun(self.installed_responder(stored, info))).status(definition)
        self.assertTrue(status.installed)
        self.assertTrue(status.enabled)
        self.assertTrue(status.definition_matches)
        self.assertIsNone(status.last_run_utc)
        self.assertIsNone(status.last_result)
        self.assertIn("run times unavailable", status.detail)

        garbage = self.adapter(FakeRun(self.installed_responder(stored, FakeResult(0, "not json")))).status()
        self.assertTrue(garbage.installed)
        self.assertIn("run times unavailable", garbage.detail)

    def test_status_mentions_settings_the_trigger_cannot_carry(self) -> None:
        definition = self.definition(ScheduledJob("preflight", 60, True, 0, 20))
        stored = ss.render_task_xml(definition, user_id="DESKTOP\\krox", now=self.NOW)
        status = self.adapter(FakeRun(self.installed_responder(stored, FakeResult(1)))).status(definition)
        self.assertIn("jitter_seconds", status.detail)


class TaskInfoParsingTests(unittest.TestCase):
    def test_powershell_51_dates(self) -> None:
        raw = '{"LastRunTime":"\\/Date(1757750400000)\\/","NextRunTime":"\\/Date(1757750460000)\\/","LastTaskResult":5}'
        self.assertEqual(ss.parse_task_info(raw), ("2025-09-13T08:00:00Z", "2025-09-13T08:01:00Z", 5, ""))

    def test_iso_dates(self) -> None:
        raw = json.dumps({
            "LastRunTime": "2025-09-13T11:00:00.1234567+03:00",
            "NextRunTime": "2025-09-13T08:01:00Z",
            "LastTaskResult": 2147942402,
        })
        self.assertEqual(ss.parse_task_info(raw), ("2025-09-13T08:00:00Z", "2025-09-13T08:01:00Z", 2147942402, ""))

    def test_naive_iso_is_local_time(self) -> None:
        local = datetime(2025, 9, 13, 11, 0, 0).astimezone()
        self.assertEqual(ss.parse_task_time("2025-09-13T11:00:00"), ss._utc_text(local))

    def test_never_run_and_sentinel_dates(self) -> None:
        raw = '{"LastRunTime":"\\/Date(943920000000)\\/","NextRunTime":"0001-01-01T00:00:00","LastTaskResult":267011}'
        last, next_run, result, note = ss.parse_task_info(raw)
        self.assertEqual((last, next_run, result), (None, None, None))
        self.assertIn("not run", note)
        self.assertIsNone(ss.parse_task_time("/Date(-62135596800000)/"))
        self.assertIsNone(ss.parse_task_time(None))
        self.assertIsNone(ss.parse_task_time(""))
        self.assertEqual(
            ss.parse_task_time({"value": "/Date(1757750400000)/", "DateTime": "13 September 2025"}),
            "2025-09-13T08:00:00Z",
        )

    def test_running_is_not_an_exit_code(self) -> None:
        _, _, result, note = ss.parse_task_info('{"LastRunTime":null,"NextRunTime":null,"LastTaskResult":267009}')
        self.assertIsNone(result)
        self.assertIn("running", note)

    def test_durations(self) -> None:
        self.assertEqual(ss.task_duration(60), "PT1M")
        self.assertEqual(ss.task_duration(61), "PT61S")
        for text, seconds in (("PT1M", 60), ("PT60S", 60), ("PT1H", 3600), ("P1DT1S", 86401), ("PT10M", 600)):
            self.assertEqual(ss._duration_seconds(text), seconds)


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------

class LaunchdTests(SandboxCase):
    SERVICE = "gui/501/io.codexsync.job"

    def adapter(self, run) -> LaunchdScheduler:
        return LaunchdScheduler(run=run, home=self.root / "home", uid=501,
                                protected_roots=[self.root / "home" / ".codex"], launchctl="launchctl")

    def test_install_writes_plist_and_bootstraps(self) -> None:
        run = FakeRun(lambda argv: FakeResult(113 if argv[1] == "bootout" else 0))
        adapter = self.adapter(run)
        job = ScheduledJob("guardian_snapshot", 120, True, 10, 5)
        adapter.install(job, command=self.command, config_path=self.config, log_dir=self.logs)
        plist_path = self.root / "home" / "Library" / "LaunchAgents" / "io.codexsync.job.plist"
        self.assertEqual(run.calls, [
            ["launchctl", "bootout", self.SERVICE],
            ["launchctl", "bootstrap", "gui/501", str(plist_path)],
        ])
        data = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(data, {
            "Label": "io.codexsync.job",
            "ProgramArguments": [*self.command, "-c", str(self.config), "guardian", "snapshot", "--once"],
            "StartInterval": 120,
            "RunAtLoad": True,
            "StandardOutPath": str(self.logs / "codexsync-job.out.log"),
            "StandardErrorPath": str(self.logs / "codexsync-job.err.log"),
            "ProcessType": "Background",
        })
        self.assertTrue(self.logs.is_dir())
        self.assertEqual([p.name for p in plist_path.parent.iterdir()], [plist_path.name])

        adapter.install(ScheduledJob("preflight", 60, False), command=self.command,
                        config_path=self.config, log_dir=self.logs)
        again = plistlib.loads(plist_path.read_bytes())
        self.assertEqual((again["RunAtLoad"], again["StartInterval"]), (False, 60))
        self.assertEqual(adapter.ignored_settings(job), ("startup_delay_seconds", "jitter_seconds"))

    def test_bootstrap_failure_raises(self) -> None:
        run = FakeRun(lambda argv: FakeResult(5, "", "Bootstrap failed: 5: Input/output error"))
        with self.assertRaises(SchedulerError):
            self.adapter(run).install(ScheduledJob("preflight", 60, False), command=self.command,
                                      config_path=self.config, log_dir=self.logs)

    def test_install_refuses_a_log_dir_inside_codex_state(self) -> None:
        run = FakeRun()
        with self.assertRaises(SchedulerError):
            self.adapter(run).install(ScheduledJob("preflight", 60, False), command=self.command,
                                      config_path=self.config, log_dir=self.root / "home" / ".codex" / "logs")
        self.assertEqual(run.calls, [])
        self.assertFalse((self.root / "home").exists())

    def test_remove(self) -> None:
        run = FakeRun()
        adapter = self.adapter(run)
        adapter.install(ScheduledJob("preflight", 60, False), command=self.command,
                        config_path=self.config, log_dir=self.logs)
        run.calls.clear()
        self.assertTrue(adapter.remove())
        self.assertEqual(run.calls, [["launchctl", "bootout", self.SERVICE]])
        self.assertFalse(adapter.plist_path.exists())
        self.assertFalse(self.adapter(FakeRun(lambda argv: FakeResult(3))).remove())

    def test_status(self) -> None:
        missing = self.adapter(FakeRun(lambda argv: FakeResult(113))).status()
        self.assertFalse(missing.installed)

        job = ScheduledJob("guardian_snapshot", 60, True, 0, 9)
        definition = self.definition(job)
        printed = "gui/501/io.codexsync.job = {\n\tstate = not running\n\truns = 4\n\tlast exit code = 3\n}\n"
        run = FakeRun(lambda argv: FakeResult(0, printed) if argv[1] == "print" else FakeResult())
        adapter = self.adapter(run)
        adapter.install(job, command=self.command, config_path=self.config, log_dir=self.logs)
        status = adapter.status(definition)
        self.assertEqual(run.calls[-1], ["launchctl", "print", self.SERVICE])
        self.assertTrue(status.installed)
        self.assertTrue(status.enabled)
        self.assertEqual(status.last_result, 3)
        self.assertTrue(status.definition_matches)
        self.assertIsNone(status.last_run_utc)
        self.assertIsNone(status.next_run_utc)
        self.assertIn("does not report run times", status.detail)
        self.assertIn("jitter_seconds", status.detail)
        self.assertFalse(adapter.status(self.definition(ScheduledJob("guardian_snapshot", 90, True))).definition_matches)

        never = self.adapter(FakeRun(lambda argv: FakeResult(0, "\tlast exit code = (never exited)\n"))).status()
        self.assertIsNone(never.last_result)
        unloaded = self.adapter(FakeRun(lambda argv: FakeResult(113, "", "Could not find service"))).status()
        self.assertTrue(unloaded.installed)
        self.assertFalse(unloaded.enabled)


# --------------------------------------------------------------------------
# Linux
# --------------------------------------------------------------------------

class SystemdTests(SandboxCase):
    def adapter(self, run) -> SystemdUserScheduler:
        return SystemdUserScheduler(run=run, home=self.root / "home",
                                    protected_roots=[self.root / "home" / ".codex"], systemctl="systemctl")

    def unit_dir(self) -> Path:
        return self.root / "home" / ".config" / "systemd" / "user"

    def test_install_writes_units_and_starts_the_timer(self) -> None:
        run = FakeRun()
        job = ScheduledJob("sync_dry_run", 300, True, 20, 7)
        self.adapter(run).install(job, command=self.command, config_path=self.config, log_dir=self.logs)
        self.assertEqual(run.calls, [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "codexsync-job.timer"],
            ["systemctl", "--user", "restart", "codexsync-job.timer"],
        ])
        service = (self.unit_dir() / "codexsync-job.service").read_text(encoding="utf-8")
        timer = (self.unit_dir() / "codexsync-job.timer").read_text(encoding="utf-8")
        exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
        expected_words = [ss.quote_systemd_argument(item) for item in
                          [*self.command, "-c", str(self.config), "sync", "--dry-run"]]
        self.assertEqual(exec_start, "ExecStart=" + " ".join(expected_words))
        self.assertIn("Type=oneshot", service)
        self.assertIn(f"StandardOutput=append:{self.logs / 'codexsync-job.out.log'}", service)
        self.assertIn("OnActiveSec=20s", timer)
        self.assertIn("OnUnitActiveSec=300s", timer)
        self.assertIn("RandomizedDelaySec=7s", timer)
        self.assertIn("Persistent=false", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertTrue(self.logs.is_dir())
        self.assertEqual(sorted(p.name for p in self.unit_dir().iterdir()),
                         ["codexsync-job.service", "codexsync-job.timer"])

    def test_timer_semantics_per_combination(self) -> None:
        adapter = self.adapter(FakeRun())
        for login, delay, jitter in itertools.product((True, False), (0, 45), (0, 30)):
            job = ScheduledJob("preflight", 600, login, delay, jitter)
            timer = adapter.render(self.definition(job))["codexsync-job.timer"].decode("utf-8").splitlines()
            with self.subTest(login=login, delay=delay, jitter=jitter):
                first = f"OnActiveSec={max(delay, 1)}s" if login else "OnActiveSec=600s"
                self.assertIn(first, timer)
                self.assertEqual(sum(line.startswith("OnActiveSec=") for line in timer), 1)
                self.assertEqual("RandomizedDelaySec=30s" in timer, bool(jitter))
                self.assertEqual(adapter.ignored_settings(job), () if login else ("startup_delay_seconds",))

    def test_systemd_quoting(self) -> None:
        self.assertEqual(ss.quote_systemd_argument("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(ss.quote_systemd_argument("/home/a b/config.toml"), '"/home/a b/config.toml"')
        self.assertEqual(ss.quote_systemd_argument("/data/50%/c"), '"/data/50%%/c"')
        self.assertEqual(ss.quote_systemd_argument("/x/$HOME"), '"/x/$$HOME"')
        self.assertEqual(ss.quote_systemd_argument('/x/a"b\\c'), '"/x/a\\"b\\\\c"')
        self.assertEqual(ss.quote_systemd_argument("/x/it's"), "\"/x/it's\"")
        with self.assertRaises(ValueError):
            ss.quote_systemd_argument("a\nb")

    def test_remove(self) -> None:
        run = FakeRun()
        adapter = self.adapter(run)
        self.assertFalse(adapter.remove())
        self.assertEqual(run.calls, [])
        adapter.install(ScheduledJob("preflight", 60, False), command=self.command,
                        config_path=self.config, log_dir=self.logs)
        run.calls.clear()
        self.assertTrue(adapter.remove())
        self.assertEqual(run.calls, [
            ["systemctl", "--user", "disable", "--now", "codexsync-job.timer"],
            ["systemctl", "--user", "daemon-reload"],
        ])
        self.assertEqual(list(self.unit_dir().iterdir()), [])

    def test_status(self) -> None:
        self.assertFalse(self.adapter(FakeRun()).status().installed)
        job = ScheduledJob("preflight", 60, True)
        definition = self.definition(job)

        def responder(argv):
            if "codexsync-job.timer" in argv and "show" in argv:
                return FakeResult(0, "ActiveState=active\nUnitFileState=enabled\n"
                                     "LastTriggerUSec=Sat 2025-09-13 08:00:00 UTC\nNextElapseUSecRealtime=@1757750460\n")
            if "codexsync-job.service" in argv:
                return FakeResult(0, "ExecMainStatus=2\nExecMainExitTimestampMonotonic=123456\n")
            return FakeResult()

        run = FakeRun(responder)
        adapter = self.adapter(run)
        adapter.install(job, command=self.command, config_path=self.config, log_dir=self.logs)
        run.calls.clear()
        status = adapter.status(definition)
        self.assertEqual(run.calls, [
            ["systemctl", "--user", "show", "codexsync-job.timer",
             "--property=ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime"],
            ["systemctl", "--user", "show", "codexsync-job.service",
             "--property=ExecMainStatus,ExecMainExitTimestampMonotonic"],
        ])
        self.assertTrue(status.installed)
        self.assertTrue(status.enabled)
        self.assertEqual(status.last_run_utc, "2025-09-13T08:00:00Z")
        self.assertEqual(status.next_run_utc, "2025-09-13T08:01:00Z")
        self.assertEqual(status.last_result, 2)
        self.assertTrue(status.definition_matches)
        self.assertFalse(adapter.status(self.definition(ScheduledJob("preflight", 60, False))).definition_matches)

        def never_ran(argv):
            if "codexsync-job.timer" in argv:
                return FakeResult(0, "ActiveState=inactive\nUnitFileState=disabled\nLastTriggerUSec=n/a\n"
                                     "NextElapseUSecRealtime=\n")
            return FakeResult(0, "ExecMainStatus=0\nExecMainExitTimestampMonotonic=0\n")

        idle = self.adapter(FakeRun(never_ran)).status()
        self.assertFalse(idle.enabled)
        self.assertIsNone(idle.last_run_utc)
        self.assertIsNone(idle.next_run_utc)
        self.assertIsNone(idle.last_result)


if __name__ == "__main__":
    unittest.main()
