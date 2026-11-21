from __future__ import annotations

import ctypes
from pathlib import Path
from unittest.mock import Mock, patch
import os
import sys
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
from codexsync.process_detector import CodexProcessDetector, ProcessInfo
from codexsync.process_report import build_process_report
from codexsync.runtime import _make_safety_gate
from codexsync.safety_gate import OperationKind, ProcessState
from codexsync.windows_processes import windows_processes


class ProcessSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AppConfig(
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

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_windows_detects_known_background_marker(self) -> None:
        processes = [
            ProcessInfo(pid=100, name="Codex.exe"),
            ProcessInfo(pid=101, name="codex-windows-sandbox.exe", parent_pid=100),
        ]
        with patch.object(CodexProcessDetector, "_list_windows_all_complete", return_value=processes):
            snapshot = collect_process_snapshot(self.cfg)
        self.assertTrue(snapshot.sandbox_detected)

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_each_final_gate_check_takes_one_fresh_inventory(self) -> None:
        idle = ProcessInfo(pid=100, name="explorer.exe")
        desktop = ProcessInfo(
            pid=101, name="ChatGPT.exe", parent_pid=100,
            program=r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64__test\app\ChatGPT.exe",
        )
        gate = _make_safety_gate(self.cfg)
        with patch.object(CodexProcessDetector, "_list_windows_all_complete",
                          side_effect=[[idle], [idle, desktop], [idle, desktop]]) as enumerate_processes:
            first = gate.check(OperationKind.SYNC, final=True)
            second = gate.check(OperationKind.SYNC, final=True)
        self.assertTrue(first.allowed)
        self.assertEqual(second.process_state, ProcessState.RUNNING)
        self.assertFalse(second.allowed)
        self.assertEqual(enumerate_processes.call_count, 2)

    @unittest.skipUnless(sys.platform == "win32", "requires the native Windows process APIs")
    def test_windows_samples_do_not_spawn_console_tools(self) -> None:
        self.cfg.process_detection.process_names = [Path(sys.executable).name]
        gate = _make_safety_gate(self.cfg)
        with patch("codexsync.process_detector.subprocess.run", side_effect=AssertionError("console tools")):
            decision = gate.check(OperationKind.SYNC, final=True)
            snapshot = collect_process_snapshot(self.cfg)
        self.assertEqual(decision.process_state, ProcessState.RUNNING)
        interpreter = next(proc for proc in snapshot.main_processes if proc.pid == os.getpid())
        self.assertTrue(any(os.path.samefile(interpreter.program, exe)
                            for exe in (sys.executable, sys._base_executable)))

    def _kernel32(self, name="ChatGPT.exe"):
        def first(_snapshot, entry):
            entry._obj.th32ProcessID = 100
            entry._obj.th32ParentProcessID = 99
            entry._obj.szExeFile = name
            return True

        kernel = Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 123
        kernel.Process32FirstW.side_effect = first
        kernel.Process32NextW.return_value = False
        kernel.OpenProcess.return_value = 456
        kernel.QueryFullProcessImageNameW.return_value = False
        return kernel

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_a_partial_windows_inventory_blocks_writes(self) -> None:
        kernel = self._kernel32()
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                patch.object(ctypes, "get_last_error", return_value=31, create=True):
            decision = _make_safety_gate(self.cfg).check(OperationKind.SYNC, final=True)
        self.assertEqual(decision.process_state, ProcessState.UNKNOWN)
        self.assertFalse(decision.allowed)
        self.assertIn("Process32NextW", decision.reason)
        kernel.CloseHandle.assert_called_once_with(123)

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_a_failed_path_query_is_benign_only_if_the_process_exited(self) -> None:
        def exit_status(_process, code):
            code._obj.value = status
            return True

        for status, expected in [(259, ProcessState.UNKNOWN), (0, ProcessState.STOPPED)]:
            with self.subTest(exit_status=status):
                kernel = self._kernel32()
                kernel.GetExitCodeProcess.side_effect = exit_status
                with patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                        patch.object(ctypes, "get_last_error", side_effect=[18, 18, 31], create=True):
                    decision = _make_safety_gate(self.cfg).check(OperationKind.SYNC, final=True)
                self.assertEqual(decision.process_state, expected)
                self.assertEqual(decision.allowed, expected is ProcessState.STOPPED)
                kernel.CloseHandle.assert_called_with(456)

    def test_reused_pid_with_the_same_name_uses_the_replacement_parent(self) -> None:
        kernel = self._kernel32()
        fill_entry = kernel.Process32FirstW.side_effect

        def fill_with_parent(snapshot, entry):
            fill_entry(snapshot, entry)
            if kernel.Process32FirstW.call_count > 1:
                entry._obj.th32ParentProcessID = 77
            return True

        def image_path(_handle, _flags, path, _size):
            path.value = r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64__test\app\ChatGPT.exe"
            return True

        kernel.Process32FirstW.side_effect = fill_with_parent
        kernel.QueryFullProcessImageNameW.side_effect = image_path
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                patch.object(ctypes, "get_last_error", return_value=18, create=True):
            [proc] = windows_processes()
        self.assertEqual(proc.parent_pid, 77)
        self.assertIn("OpenAI.Codex_", proc.path)

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_protected_chatgpt_is_unknown_but_unrelated_protected_processes_do_not_block(self) -> None:
        for name, expected in [("ChatGPT.exe", ProcessState.UNKNOWN), ("lsass.exe", ProcessState.STOPPED)]:
            for denied_at_open in (True, False):
                with self.subTest(name=name, denied_at_open=denied_at_open):
                    kernel = self._kernel32(name)
                    if denied_at_open:
                        kernel.OpenProcess.return_value = None
                    errors = [18, 5] if denied_at_open else [18, 18, 5]
                    with patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                            patch.object(ctypes, "get_last_error", side_effect=errors, create=True):
                        decision = _make_safety_gate(self.cfg).check(OperationKind.SYNC, final=True)
                    self.assertEqual(decision.process_state, expected)
                    self.assertEqual(decision.allowed, expected is ProcessState.STOPPED)
                    if name == "ChatGPT.exe":
                        self.assertIn("access denied", decision.reason)

    @patch("codexsync.runtime.sys.platform", "win32")
    def test_an_unreadable_path_cannot_rule_out_a_custom_marker(self) -> None:
        examples = [
            (r"\private\ChatGPT.exe", "ChatGPT.exe", r"C:\private\ChatGPT.exe"),
            (r"\private\ChatGPT.exe", "bridge.exe", r"C:\private\ChatGPT.exe-tools\bridge.exe"),
            ("/custom/codex/", "bridge.exe", r"C:\custom\codex\bridge.exe"),
        ]
        for marker, name, path in examples:
            with self.subTest(marker=marker, name=name):
                self.cfg.process_detection.background_process_names["windows"] = [marker]
                process = ProcessInfo(100, name, program_access_denied=True)
                with patch.object(CodexProcessDetector, "_list_windows_all_complete", return_value=[process]):
                    decision = _make_safety_gate(self.cfg).check(OperationKind.SYNC, final=True)
                    report = build_process_report([], [marker], names_from="test")
                self.assertEqual(decision.process_state, ProcessState.UNKNOWN)
                self.assertFalse(decision.allowed)
                self.assertEqual(report.gate_state, "UNKNOWN")
                process = ProcessInfo(100, name, program=path)
                with patch.object(CodexProcessDetector, "_list_windows_all_complete", return_value=[process]):
                    decision = _make_safety_gate(self.cfg).check(OperationKind.SYNC, final=True)
                self.assertEqual(decision.process_state, ProcessState.RUNNING)


if __name__ == "__main__":
    unittest.main()
