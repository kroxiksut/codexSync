"""Asking Codex to quit before a sync, when `[sync] close_codex` is on (D-029).

Nothing here asks a real process anything: the process list, the Restart
Manager and `osascript` are injected. What is pinned: only the Codex app's root
process is asked; a Codex CLI in a terminal is never touched; force is never an
option; an unproven platform refuses; the sync waits for the gate to see Codex
gone and refuses with why when it is not; the watcher never asks.
"""
from __future__ import annotations

import ast

from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
import uuid

from codexsync import codex_closer
from codexsync.app import close_codex_for_sync, watch_handoff
from codexsync.codex_closer import CloseOutcome, WindowsProcess, ask_codex_to_quit
from codexsync.config_edit import create_config, set_value
from codexsync.exceptions import SafetyPreconditionError
from codexsync.safety_gate import ProcessState, SafetyDecision

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
APP = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.930.4958.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"


def _proc(pid: int, parent: int, name: str, path: str = "") -> WindowsProcess:
    return WindowsProcess(pid, parent, name, path, 133000000000000000 + pid)


#: The tree seen on 2026-10-06: the app's root, its helpers, and Codex's server.
DESKTOP = (
    _proc(4, 1, "explorer.exe", r"C:\Windows\explorer.exe"),
    _proc(100, 4, "ChatGPT.exe", APP),
    _proc(101, 100, "ChatGPT.exe", APP),
    _proc(102, 100, "ChatGPT.exe", APP),
    _proc(200, 100, "codex.exe", APP.replace("ChatGPT.exe", r"resources\codex.exe")),
    _proc(201, 100, "codex.exe", APP.replace("ChatGPT.exe", r"resources\codex.exe")),
)


class WindowsTests(unittest.TestCase):
    def ask(self, processes, answer: int = 0) -> tuple[CloseOutcome, list]:
        asked: list = []

        def shutdown(roots):
            asked.append(tuple(proc.pid for proc in roots))
            return answer

        outcome = ask_codex_to_quit("win32", list_windows_processes=lambda: processes, restart_manager=shutdown)
        return outcome, asked

    def test_only_the_apps_root_is_asked(self) -> None:
        outcome, asked = self.ask(DESKTOP)
        self.assertEqual(asked, [(100,)], "children close with their root; Codex's server is the app's")
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.pids, (100,))

    def test_a_refusal_is_reported_and_nothing_else_is_tried(self) -> None:
        outcome, asked = self.ask(DESKTOP, answer=351)
        self.assertEqual(len(asked), 1)
        self.assertTrue(outcome.asked)
        self.assertFalse(outcome.accepted)
        self.assertIn("declined", outcome.detail)

    def test_an_ordinary_chatgpt_outside_the_package_is_not_codex(self) -> None:
        other = (_proc(300, 4, "ChatGPT.exe", r"C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1\app\ChatGPT.exe"),)
        outcome, asked = self.ask(other)
        self.assertEqual(asked, [])
        self.assertFalse(outcome.asked)

    def test_a_codex_cli_in_a_terminal_is_never_closed(self) -> None:
        terminal = DESKTOP + (
            _proc(400, 4, "WindowsTerminal.exe"), _proc(401, 400, "pwsh.exe"),
            _proc(402, 401, "codex.exe", r"C:\Users\me\AppData\Roaming\npm\codex.exe"),
        )
        outcome, asked = self.ask(terminal)
        self.assertEqual(asked, [], "not even the app is asked while a terminal session runs")
        self.assertFalse(outcome.asked)
        self.assertIn("terminal", outcome.detail)

    def test_force_is_not_an_option_anywhere(self) -> None:
        # RmForceShutdown is 0x1; the call passes flags 0 and nothing can change that.
        source = Path(codex_closer.__file__).read_text(encoding="utf-8")
        self.assertIn("RmShutdown(session, 0, None)", source)
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node)
        }
        code_strings = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        ]
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse([text for text in code_strings if "taskkill" in text.lower() or "kill" == text])
        self.assertNotIn("TerminateProcess", names)
        self.assertNotIn("kill", names)


class MacTests(unittest.TestCase):
    def test_unproven_until_observed(self) -> None:
        def run(argv):
            self.fail("nothing is asked on a platform where asking was never observed")

        outcome = ask_codex_to_quit("darwin", run=run)
        self.assertFalse(outcome.asked)
        self.assertIn("not been observed", outcome.detail)

    def test_once_proven_it_asks_the_app_by_its_bundle_id(self) -> None:
        seen: list = []

        def run(argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.dict(codex_closer.PROVEN_CLOSERS, {"darwin": "observed"}):
            outcome = ask_codex_to_quit("darwin", run=run)
        self.assertTrue(outcome.accepted)
        self.assertEqual(seen, [["osascript", "-e", 'tell application id "com.openai.codex" to quit']])


class _Gate:
    def __init__(self, states) -> None:
        self.states = list(states)

    def check(self, operation, *, final=False):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SafetyDecision(operation, state, state is ProcessState.STOPPED, "test")

    def require(self, operation, *, final=False):
        return self.check(operation, final=final)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"closer-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(
            self.config, machine_id="desk", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"),
        )

    def turn_on(self) -> None:
        self.config.write_text(
            set_value(self.config.read_text(encoding="utf-8"), "sync", "close_codex", True), encoding="utf-8",
        )

    def close(self, states, outcome=CloseOutcome(True, True, "agreed", (100,)), wait_seconds=10.0):
        asked: list = []
        clock = [0.0]
        gate = _Gate(states)
        with mock.patch("codexsync.app._make_safety_gate", return_value=gate):
            result = close_codex_for_sync(
                self.config, ask=lambda: (asked.append(1), outcome)[1],
                monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                wait_seconds=wait_seconds,
            )
        return result, asked

    def test_off_by_default_nothing_is_asked(self) -> None:
        result, asked = self.close([ProcessState.RUNNING])
        self.assertIsNone(result)
        self.assertEqual(asked, [])

    def test_on_and_closed_already_nothing_is_asked(self) -> None:
        self.turn_on()
        result, asked = self.close([ProcessState.STOPPED])
        self.assertIsNone(result)
        self.assertEqual(asked, [])

    def test_on_and_open_it_asks_once_and_waits_for_the_gate(self) -> None:
        self.turn_on()
        result, asked = self.close([ProcessState.RUNNING, ProcessState.RUNNING, ProcessState.RUNNING, ProcessState.STOPPED])
        self.assertEqual(asked, [1])
        self.assertTrue(result.accepted)

    def test_a_refusal_stops_the_sync_with_why(self) -> None:
        self.turn_on()
        with self.assertRaises(SafetyPreconditionError) as raised:
            self.close([ProcessState.RUNNING], outcome=CloseOutcome(True, False, "the application declined to close"))
        self.assertIn("declined", str(raised.exception))

    def test_still_running_after_the_wait_stops_the_sync(self) -> None:
        self.turn_on()
        with self.assertRaises(SafetyPreconditionError) as raised:
            self.close([ProcessState.RUNNING], wait_seconds=5.0)
        self.assertIn("tray", str(raised.exception))

    def test_an_undetermined_state_is_never_taken_for_open(self) -> None:
        # UNKNOWN is neither: nothing is asked, and the gate refuses the write as always.
        self.turn_on()
        result, asked = self.close([ProcessState.UNKNOWN])
        self.assertIsNone(result)
        self.assertEqual(asked, [])

    def test_the_watcher_never_asks(self) -> None:
        self.turn_on()
        calls: list = []
        with mock.patch("codexsync.app.run_handoff", side_effect=lambda path, **kw: calls.append(kw) or mock.MagicMock()):
            watch_handoff(
                self.config, probe=lambda: ProcessState.STOPPED, notifier=lambda *a, **k: None,
                sleep=lambda s: None, should_stop=lambda: True,
            )
        self.assertEqual(calls, [{"close_codex": False}])


if __name__ == "__main__":
    unittest.main()
