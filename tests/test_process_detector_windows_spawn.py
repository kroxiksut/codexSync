"""The detector must not open a console window, and must not invent markers.

Two separate regressions live here (CS-259, CS-264). Neither was visible from a
console build or from a fixture, so both are pinned by asserting on what the
code *asks the operating system for*, never by running the real tools.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from codexsync import process_knowledge
from codexsync.process_detector import _run_console_tool


class ConsoleToolSpawnTests(unittest.TestCase):
    """`codexsync-gui.exe` is windowed: a child console is a visible window."""

    def _captured_kwargs(self) -> dict:
        completed = subprocess.CompletedProcess(["x"], 0, "", "")
        with patch("codexsync.process_detector.subprocess.run", return_value=completed) as run:
            _run_console_tool(["tasklist"], encoding="oem")
        return run.call_args.kwargs

    def test_stdin_is_never_inherited(self) -> None:
        # A windowed process has no standard handles to hand down; a console
        # tool given an invalid one can fail in ways that read as "unknown".
        self.assertIs(self._captured_kwargs()["stdin"], subprocess.DEVNULL)

    def test_output_is_captured_and_failures_are_returned(self) -> None:
        kwargs = self._captured_kwargs()
        self.assertTrue(kwargs["capture_output"])
        self.assertFalse(kwargs["check"])

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows spawn flags")
    def test_windows_asks_for_no_console_window(self) -> None:
        kwargs = self._captured_kwargs()
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        startupinfo = kwargs["startupinfo"]
        self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startupinfo.wShowWindow, subprocess.SW_HIDE)

    def test_a_platform_without_the_flags_still_runs(self) -> None:
        """The Windows adapter is exercised from macOS and Linux.

        `test_process_detector.py` patches this module's `sys.platform` to
        "win32" so the tasklist parser can be tested anywhere. Deciding on the
        platform *name* and then reaching for `subprocess.STARTUPINFO` made
        those tests crash on CI, and would crash the same way in any
        environment that calls itself Windows without offering the attributes.
        """
        completed = subprocess.CompletedProcess(["x"], 0, "", "")
        # `create=True`: on POSIX these attributes do not exist at all, which is
        # the very condition under test, so `patch.object` has to be allowed to
        # invent them before setting them to None.
        absent = dict(new=None, create=True)
        with patch("codexsync.process_detector.subprocess.run", return_value=completed) as run:
            with patch.object(subprocess, "STARTUPINFO", **absent):
                with patch.object(subprocess, "CREATE_NO_WINDOW", **absent):
                    _run_console_tool(["tasklist"], encoding="utf-8")
        kwargs = run.call_args.kwargs
        self.assertNotIn("startupinfo", kwargs)
        self.assertNotIn("creationflags", kwargs)
        # The part that is not Windows-specific still applies.
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows spawn flags")
    def test_encoding_is_the_console_code_page(self) -> None:
        # `tasklist` writes in the OEM code page; decoding it as UTF-8 with
        # errors="ignore" silently dropped bytes of any non-ASCII name.
        self.assertEqual(self._captured_kwargs()["encoding"], "oem")


class BackgroundMarkerTests(unittest.TestCase):
    """A marker that is always running is not a marker (CS-264)."""

    def test_the_always_running_sandbox_service_is_not_a_marker(self) -> None:
        # `CodexSandboxService.OpenAI.Codex` is StartMode=Auto on Windows and
        # runs from boot. Listing it made the safety gate report RUNNING
        # forever, which closed every mutation on a real machine.
        self.assertNotIn(
            "codex-windows-sandbox-service",
            process_knowledge.BACKGROUND_PROCESS_NAMES["windows"],
        )

    def test_the_shipped_template_does_not_list_it_either(self) -> None:
        from pathlib import Path

        for name in ("src/codexsync/config.example.toml", "config.example.toml"):
            text = Path(name).read_text(encoding="utf-8")
            listed = [
                line for line in text.splitlines()
                if '"codex-windows-sandbox-service"' in line and not line.lstrip().startswith("#")
            ]
            self.assertEqual(listed, [], f"{name} still lists the always-on service")


if __name__ == "__main__":
    unittest.main()
