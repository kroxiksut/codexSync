"""Codex CLI's left-behind app-server daemon (CS-429).

Codex CLI 0.163 starts a shared app-server under `<CODEX_HOME>/packages/
app-server-daemon/` and leaves it running after the CLI quits (observed on
Linux, 2026-10-19). It is Codex, so a write still waits for it; what is pinned
here is that a refusal and `doctor` say it is that server and name the command
that stops it -- and that the decision itself does not change.
"""
from __future__ import annotations

import unittest
from unittest import mock

from codexsync.exceptions import SafetyPreconditionError
from codexsync.process_detector import ProcessInfo
from codexsync.process_knowledge import CLI_DAEMON_STOP_COMMAND, is_cli_daemon_path
from codexsync.runtime import ProcessSnapshot, only_cli_daemon
from codexsync.safety_gate import OperationKind, ProcessState, SafetyGate

DAEMON = "/home/me/.codex/packages/app-server-daemon/releases/0.163.0-x86_64-unknown-linux-musl/bin/codex"


def _gate(state: ProcessState, daemon_only: bool) -> SafetyGate:
    return SafetyGate(
        lambda: state, describe=lambda: "codex (pid 258119)", cli_daemon_only=lambda: daemon_only,
        stable_window_seconds=0.0, monotonic=lambda: 0.0, sleep=lambda _s: None,
    )


class RecognitionTests(unittest.TestCase):
    def test_the_daemon_is_known_by_its_path_on_every_system(self) -> None:
        self.assertTrue(is_cli_daemon_path(DAEMON))
        self.assertTrue(is_cli_daemon_path(r"C:\Users\me\.codex\packages\app-server-daemon\releases\x\bin\codex.exe"))
        self.assertFalse(is_cli_daemon_path("/usr/lib/chatgpt/resources/codex"))

    def test_only_the_daemon_left_is_said_so(self) -> None:
        daemon = ProcessInfo(258119, "codex", f"{DAEMON} app-server --listen unix:// --managed-daemon", program=DAEMON)
        app_server = ProcessInfo(28047, "codex", "/usr/lib/chatgpt/resources/codex app-server",
                                 program="/usr/lib/chatgpt/resources/codex")
        self.assertTrue(only_cli_daemon(ProcessSnapshot([daemon], [], False)))
        self.assertFalse(only_cli_daemon(ProcessSnapshot([daemon, app_server], [], False)))
        self.assertFalse(only_cli_daemon(ProcessSnapshot([daemon], [], True)), "a sandbox at work is more than the server")
        self.assertFalse(only_cli_daemon(ProcessSnapshot([], [], False)))


class GateTests(unittest.TestCase):
    def test_the_refusal_names_the_server_and_its_stop_command(self) -> None:
        with self.assertRaises(SafetyPreconditionError) as caught:
            _gate(ProcessState.RUNNING, True).require(OperationKind.SYNC)
        self.assertEqual(caught.exception.code, "CODEX_CLI_DAEMON")
        self.assertEqual(caught.exception.params, {"command": CLI_DAEMON_STOP_COMMAND})
        self.assertIn(CLI_DAEMON_STOP_COMMAND, str(caught.exception))

    def test_the_decision_does_not_change(self) -> None:
        decision = _gate(ProcessState.RUNNING, True).check(OperationKind.SYNC)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.cli_daemon_only)
        self.assertFalse(_gate(ProcessState.STOPPED, True).check(OperationKind.SYNC).cli_daemon_only)

    def test_anything_else_running_is_the_ordinary_refusal(self) -> None:
        with self.assertRaises(SafetyPreconditionError) as caught:
            _gate(ProcessState.RUNNING, False).require(OperationKind.SYNC)
        self.assertEqual(caught.exception.code, "CODEX_RUNNING")

    def test_a_failing_hook_is_not_a_daemon(self) -> None:
        def broken() -> bool:
            raise RuntimeError("boom")

        gate = SafetyGate(lambda: ProcessState.RUNNING, cli_daemon_only=broken,
                          monotonic=lambda: 0.0, sleep=lambda _s: None)
        self.assertFalse(gate.check(OperationKind.SYNC).cli_daemon_only)


class DoctorTests(unittest.TestCase):
    def test_doctor_says_how_to_stop_it(self) -> None:
        from codexsync import preflight

        with mock.patch.object(preflight, "_make_safety_gate", return_value=_gate(ProcessState.RUNNING, True)):
            result = preflight._check_process_state(mock.Mock(), OperationKind.DOCTOR)
        codes = [part[0] for part in result.parts]
        self.assertIn("codex_process.cli_daemon", codes)
        self.assertIn(CLI_DAEMON_STOP_COMMAND, result.details)


if __name__ == "__main__":
    unittest.main()
