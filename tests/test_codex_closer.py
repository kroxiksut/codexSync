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

    def test_the_cli_server_left_behind_is_named_not_stopped(self) -> None:
        daemon = (
            _proc(4, 1, "explorer.exe", r"C:\Windows\explorer.exe"),
            _proc(500, 4, "codex.exe", r"C:\Users\me\.codex\packages\app-server-daemon\releases\x\bin\codex.exe"),
        )
        outcome, asked = self.ask(daemon)
        self.assertEqual(asked, [])
        self.assertFalse(outcome.asked)
        self.assertIn("codex app-server daemon stop", outcome.detail)

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


LINUX_APP = "/usr/lib/chatgpt/ChatGPT"


def _lproc(pid: int, parent: int, name: str, path: str) -> codex_closer.LinuxProcess:
    return codex_closer.LinuxProcess(pid, parent, name, path)


#: The tree seen on 2026-10-19 (Ubuntu 26.04, Codex deb 26.1002.52244): the
#: app's root under the user's systemd, its two crash handlers beside it, its
#: helpers, and Codex's server.
LINUX_DESKTOP = (
    _lproc(1629, 1, "systemd", "/usr/lib/systemd/systemd"),
    _lproc(100, 1629, "ChatGPT", LINUX_APP),
    _lproc(101, 1629, "browser_crashpa", "/usr/lib/chatgpt/browser_crashpad_handler"),
    _lproc(102, 100, "ChatGPT", LINUX_APP),
    _lproc(103, 102, "ChatGPT", LINUX_APP),
    _lproc(200, 100, "codex", "/usr/lib/chatgpt/resources/codex"),
)


DAEMON = "/home/me/.codex/packages/app-server-daemon/releases/0.163.0-x86_64-unknown-linux-musl/bin/codex"
#: Codex CLI 0.163's server, left running after the CLI quit (2026-10-19).
LINUX_DAEMON_ONLY = (
    _lproc(1, 0, "systemd", "/usr/lib/systemd/systemd"),
    _lproc(258119, 1, "codex", DAEMON),
)


class _Display(codex_closer.X11Windows):
    def __init__(self, windows) -> None:
        self._windows = list(windows)
        self.closed: list = []

    def windows(self):
        return self._windows

    def close(self, windows) -> None:
        self.closed.append(tuple(windows))


class _Tray(codex_closer.TrayMenus):
    """No tray host unless entries are given; records clicks."""

    def __init__(self, entries=()) -> None:
        self._entries = list(entries)
        self.clicked: list = []

    def quit_entries(self, app_pids):
        return [entry for entry in self._entries if entry.pid in app_pids]

    def click(self, entries) -> None:
        self.clicked.append(tuple(entries))


class LinuxTests(unittest.TestCase):
    def ask(self, processes, windows, tray=None) -> tuple[CloseOutcome, _Display]:
        display = _Display(windows)
        outcome = ask_codex_to_quit(
            "linux", list_linux_processes=lambda: processes, x11=display, tray=tray or _Tray(),
        )
        return outcome, display

    def test_signed_in_the_trays_quit_is_clicked_not_the_window(self) -> None:
        # Signed in, a window close only hid Codex to its tray (2026-10-19).
        quit_entry = codex_closer.TrayEntry(":1.125", "/org/chromium/DbusMenu", 2817, 100)
        tray = _Tray([quit_entry, codex_closer.TrayEntry(":1.100", "/StatusNotifierItem", 5, 4242)])
        outcome, display = self.ask(LINUX_DESKTOP, [(0x600004, 100)], tray)
        self.assertEqual(tray.clicked, [(quit_entry,)], "only the app's own icon")
        self.assertEqual(display.closed, [])
        self.assertTrue(outcome.accepted)
        self.assertIn("tray", outcome.detail)

    def test_quit_is_found_in_a_layout_as_busctl_prints_it(self) -> None:
        # The tail of the layout Codex 26.1013 published (chat titles left out).
        layout = [0, {}, [
            {"type": "(ia{sv}av)", "data": [2813, {"label": {"type": "s", "data": "New Chat"}}, []]},
            {"type": "(ia{sv}av)", "data": [2814, {"type": {"type": "s", "data": "separator"}}, []]},
            {"type": "(ia{sv}av)", "data": [2815, {"label": {"type": "s", "data": "Open ChatGPT"}}, []]},
            {"type": "(ia{sv}av)", "data": [2817, {"label": {"type": "s", "data": "Quit"}}, []]},
        ]]
        self.assertEqual(codex_closer._quit_ids(layout), [2817])
        self.assertEqual(codex_closer._quit_ids([1, {"label": {"type": "s", "data": "_Quit"}}, []]), [1])
        self.assertEqual(codex_closer._quit_ids([1, {"label": {"type": "s", "data": "Quit soon"}}, []]), [])

    def test_the_apps_window_is_asked_to_close(self) -> None:
        outcome, display = self.ask(LINUX_DESKTOP, [(0x600004, 100), (0x800001, 4242)])
        self.assertEqual(display.closed, [(0x600004,)], "only the window whose process is the app's")
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.pids, (100,))

    def test_the_app_is_told_apart_by_the_detectors_own_marker(self) -> None:
        self.assertEqual(codex_closer.LINUX_APP_MARKERS, ("/usr/lib/chatgpt/",))

    def test_no_window_on_x11_means_nothing_is_asked(self) -> None:
        # A window drawn on Wayland itself is not listed, and cannot be asked.
        outcome, display = self.ask(LINUX_DESKTOP, [(0x800001, 4242)])
        self.assertEqual(display.closed, [])
        self.assertFalse(outcome.asked)
        self.assertIn("X11", outcome.detail)

    def test_a_display_that_cannot_be_opened_is_reported(self) -> None:
        class NoDisplay(_Display):
            def windows(self):
                raise OSError("no X display (DISPLAY is not set)")

        outcome = ask_codex_to_quit(
            "linux", list_linux_processes=lambda: LINUX_DESKTOP, x11=NoDisplay([]), tray=_Tray(),
        )
        self.assertFalse(outcome.asked)
        self.assertIn("DISPLAY", outcome.detail)

    def test_a_codex_cli_in_a_terminal_is_never_closed(self) -> None:
        terminal = LINUX_DESKTOP + (
            _lproc(400, 1629, "gnome-terminal-", "/usr/libexec/gnome-terminal-server"),
            _lproc(401, 400, "bash", "/usr/bin/bash"),
            _lproc(402, 401, "codex", "/home/me/.npm-global/lib/node_modules/@openai/codex/vendor/codex"),
        )
        outcome, display = self.ask(terminal, [(0x600004, 100)])
        self.assertEqual(display.closed, [], "not even the app is asked while a terminal session runs")
        self.assertFalse(outcome.asked)
        self.assertIn("terminal", outcome.detail)

    def ask_with_run(self, processes, windows, answer: int = 0):
        display, seen = _Display(windows), []

        def run(argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, answer, "", "")

        outcome = ask_codex_to_quit(
            "linux", list_linux_processes=lambda: processes, x11=display, run=run, tray=_Tray(),
        )
        return outcome, display, seen

    def test_the_cli_daemon_left_behind_is_asked_to_stop_by_codexs_own_command(self) -> None:
        # Codex CLI 0.163 after the CLI was quit (2026-10-19): only its server.
        outcome, display, seen = self.ask_with_run(LINUX_DAEMON_ONLY, [])
        self.assertEqual(display.closed, [])
        self.assertEqual(seen, [[DAEMON, "app-server", "daemon", "stop"]])
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.pids, (258119,))

    def test_a_daemon_that_refuses_to_stop_is_reported(self) -> None:
        outcome, _, _ = self.ask_with_run(LINUX_DAEMON_ONLY, [], answer=1)
        self.assertTrue(outcome.asked)
        self.assertFalse(outcome.accepted)
        self.assertIn("daemon stop", outcome.detail)

    def test_the_daemon_is_left_alone_while_a_cli_uses_it(self) -> None:
        with_cli = LINUX_DAEMON_ONLY + (
            _lproc(400, 1, "bash", "/usr/bin/bash"),
            _lproc(402, 400, "codex", "/home/me/.npm-global/lib/node_modules/@openai/codex/vendor/codex"),
        )
        outcome, _, seen = self.ask_with_run(with_cli, [])
        self.assertEqual(seen, [])
        self.assertFalse(outcome.asked)
        self.assertIn("terminal", outcome.detail)

    def test_the_app_and_a_left_daemon_are_both_asked(self) -> None:
        outcome, display, seen = self.ask_with_run(LINUX_DESKTOP + LINUX_DAEMON_ONLY[1:], [(0x600004, 100)])
        self.assertEqual(display.closed, [(0x600004,)])
        self.assertEqual(len(seen), 1)
        self.assertTrue(outcome.accepted)

    def test_with_only_a_daemon_no_display_is_needed(self) -> None:
        class NoDisplay(_Display):
            def windows(self):
                raise OSError("no X display (DISPLAY is not set)")

        seen = []
        outcome = ask_codex_to_quit(
            "linux", list_linux_processes=lambda: LINUX_DAEMON_ONLY, x11=NoDisplay([]), tray=_Tray(),
            run=lambda argv: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        )
        self.assertTrue(outcome.accepted)
        self.assertEqual(len(seen), 1)

    def test_a_command_codex_started_in_a_chat_is_the_apps(self) -> None:
        chat = LINUX_DESKTOP + (
            _lproc(300, 200, "codex-linux-san", "/usr/lib/chatgpt/resources/codex-linux-sandbox"),
            _lproc(301, 300, "codex", "/home/me/bin/codex"),
        )
        outcome, display = self.ask(chat, [(0x600004, 100)])
        self.assertEqual(display.closed, [(0x600004,)])
        self.assertTrue(outcome.accepted)

    def test_no_signal_is_ever_sent(self) -> None:
        # SIGTERM ends this app at once and leaves its server orphaned
        # (observed 2026-10-19); only the window's close request is a request.
        tree = ast.parse(Path(codex_closer.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("signal", imported)


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
