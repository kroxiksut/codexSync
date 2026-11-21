from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from codexsync.process_detector import CodexProcessDetector, ProcessInfo, _path_marker, _path_matches
from codexsync.process_knowledge import BACKGROUND_PROCESS_NAMES
from codexsync.process_report import build_process_report
from codexsync.windows_processes import WindowsProcess


class ProcessDetectorTests(unittest.TestCase):
    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.windows_processes")
    def test_windows_matches_name_without_exe_suffix(self, inventory) -> None:
        inventory.return_value = [WindowsProcess(4242, 10, "CODEX-WINDOWS-SANDBOX.EXE", "", 0)]
        detector = CodexProcessDetector(["codex-windows-sandbox"])
        running = detector.list_running()
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].pid, 4242)
        self.assertEqual(running[0].name, "CODEX-WINDOWS-SANDBOX.EXE")

    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.windows_processes")
    def test_windows_subprocess_marker_in_codex_tree(self, inventory) -> None:
        inventory.return_value = [
            WindowsProcess(1000, 10, "Codex.exe", "", 0),
            WindowsProcess(1001, 1000, "codex-windows-sandbox.exe", "", 0),
        ]
        detector = CodexProcessDetector(["codex.exe"])
        present = detector.has_subprocess_marker(["codex.exe"], "codex-windows-sandbox")
        self.assertTrue(present)

    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.windows_processes")
    def test_windows_does_not_match_unrelated_process_by_name(self, inventory) -> None:
        inventory.return_value = [WindowsProcess(9999, 10, "python.exe", "", 0)]
        detector = CodexProcessDetector(["codex.exe"])
        running = detector.list_running()
        self.assertEqual(running, [])


#: Paths as CIM reported them on this machine on 2026-10-25 (Codex 26.1021.3705.0).
_PACKAGE = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0"
_APP = _PACKAGE + r"\app\ChatGPT.exe"
_SERVICE = _PACKAGE + r"\app\resources\codex-windows-sandbox-service.exe"
#: The ordinary ChatGPT desktop app: same publisher id, another package.
_CHATGPT = r"C:\Program Files\WindowsApps\OpenAI.ChatGPT-Desktop_1.2026.300.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
_MARKER = "/OpenAI.Codex_*/app/ChatGPT.exe"


@patch("codexsync.process_detector.sys.platform", "win32")
@patch("codexsync.process_detector.windows_processes")
class WindowsPathMarkerTests(unittest.TestCase):
    """CS-447: the desktop app is `ChatGPT.exe` from the `OpenAI.Codex_` package."""

    def _find(self, inventory, markers, *rows):
        inventory.return_value = [WindowsProcess(pid, 1, name, path, 0) for pid, name, path in rows]
        return CodexProcessDetector(["codex.exe"]).find_processes(markers)

    def test_the_desktop_app_is_found_by_its_package_path(self, inventory) -> None:
        found = self._find(inventory, [_MARKER], (10, "ChatGPT.exe", _APP))
        self.assertEqual([proc.pid for proc in found], [10])
        self.assertEqual(found[0].program, _APP)

    def test_a_marker_written_with_backslashes_means_the_same(self, inventory) -> None:
        found = self._find(inventory, [r"\OpenAI.Codex_*\app\ChatGPT.exe"], (10, "ChatGPT.exe", _APP))
        self.assertEqual([proc.pid for proc in found], [10])

    def test_the_ordinary_chatgpt_app_is_not_codex(self, inventory) -> None:
        self.assertEqual(self._find(inventory, [_MARKER], (11, "ChatGPT.exe", _CHATGPT)), [])

    def test_the_always_running_service_in_the_same_package_is_not_matched(self, inventory) -> None:
        # The package folder also holds the sandbox service, which runs from
        # boot (CS-264); a marker naming the whole folder would read RUNNING forever.
        self.assertEqual(
            self._find(inventory, [_MARKER], (12, "codex-windows-sandbox-service.exe", _SERVICE)), [],
        )

    def test_a_process_without_a_path_is_matched_by_name_only(self, inventory) -> None:
        found = self._find(inventory, [_MARKER, "codex-command-runner"],
                           (13, "ChatGPT.exe", ""), (14, "codex-command-runner.exe", ""))
        self.assertEqual([proc.pid for proc in found], [14])

    def test_has_marker_follows_the_same_rule(self, inventory) -> None:
        detector = CodexProcessDetector(["codex.exe"])
        self.assertTrue(detector.has_marker(ProcessInfo(1, "ChatGPT.exe", program=_APP), _MARKER))
        self.assertFalse(detector.has_marker(ProcessInfo(2, "ChatGPT.exe", program=_CHATGPT), _MARKER))
        self.assertFalse(detector.has_marker(ProcessInfo(3, "ChatGPT.exe"), _MARKER))

    def test_the_report_names_the_marker_and_never_the_path(self, inventory) -> None:
        inventory.return_value = [WindowsProcess(10, 1, "ChatGPT.exe", _APP, 0)]
        report = build_process_report(["codex.exe"], [_MARKER], names_from="test")
        [match] = report.matches
        self.assertEqual((match.role, match.name, match.rule, match.matched_by),
                         ("background", "ChatGPT.exe", _MARKER, "path_marker"))
        self.assertEqual(report.would_read, "RUNNING")
        self.assertNotIn("WindowsApps", json.dumps(report.as_dict()))


class PathMatchTests(unittest.TestCase):
    def test_a_marker_without_a_star_is_a_plain_substring(self) -> None:
        self.assertTrue(_path_matches("/usr/lib/chatgpt/chatgpt", "/usr/lib/chatgpt/"))
        self.assertFalse(_path_matches("/usr/lib/chatgpt-other/x", "/usr/lib/chatgpt/"))

    def test_case_and_separators_do_not_matter(self) -> None:
        self.assertTrue(_path_matches(_APP.upper(), _MARKER.lower()))

    def test_an_empty_path_matches_nothing(self) -> None:
        self.assertFalse(_path_matches("", _MARKER))


class ShippedWindowsMarkersTests(unittest.TestCase):
    """Every shipped Windows path marker names the app and nothing always running."""

    def _markers(self) -> list[str]:
        return [name for name in BACKGROUND_PROCESS_NAMES["windows"] if _path_marker(name)]

    def test_no_shipped_marker_matches_the_service_or_the_chatgpt_app(self) -> None:
        for marker in self._markers():
            self.assertFalse(_path_matches(_SERVICE, marker), marker)
            self.assertFalse(_path_matches(_CHATGPT, marker), marker)


if __name__ == "__main__":
    unittest.main()
