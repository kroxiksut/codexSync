from __future__ import annotations

import json
from unittest.mock import patch
import unittest

from codexsync.process_detector import CodexProcessDetector, ProcessInfo, _path_marker, _path_matches
from codexsync.process_knowledge import BACKGROUND_PROCESS_NAMES
from codexsync.process_report import build_process_report


class _Result:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


class ProcessDetectorTests(unittest.TestCase):
    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.subprocess.run")
    def test_windows_matches_name_without_exe_suffix(self, run_mock) -> None:
        run_mock.side_effect = [
            _Result(returncode=0, stdout='"CODEX-WINDOWS-SANDBOX.EXE","4242","Console","1","10,000 K"\n'),
            _Result(returncode=0, stdout="[]"),
        ]
        detector = CodexProcessDetector(["codex-windows-sandbox"])
        running = detector.list_running()
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].pid, 4242)
        self.assertEqual(running[0].name, "CODEX-WINDOWS-SANDBOX.EXE")

    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.subprocess.run")
    def test_windows_subprocess_marker_in_codex_tree(self, run_mock) -> None:
        run_mock.side_effect = [
            _Result(returncode=0, stdout=""),
            _Result(
                returncode=0,
                stdout=(
                    '[{"ProcessId":1000,"ParentProcessId":10,"Name":"Codex.exe"},'
                    '{"ProcessId":1001,"ParentProcessId":1000,"Name":"codex-windows-sandbox.exe"}]'
                ),
            ),
        ]
        detector = CodexProcessDetector(["codex.exe"])
        present = detector.has_subprocess_marker(["codex.exe"], "codex-windows-sandbox")
        self.assertTrue(present)

    @patch("codexsync.process_detector.sys.platform", "win32")
    @patch("codexsync.process_detector.subprocess.run")
    def test_windows_does_not_match_unrelated_process_by_name(self, run_mock) -> None:
        run_mock.side_effect = [
            _Result(returncode=0, stdout='"python.exe","9999","Console","1","10,000 K"\n'),
            _Result(returncode=0, stdout='[{"ProcessId":9999,"Name":"python.exe"}]'),
        ]
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


def _cim(*rows: tuple[int, str, str | None]) -> str:
    return json.dumps([
        {"ProcessId": pid, "ParentProcessId": 1, "Name": name, "ExecutablePath": path}
        for pid, name, path in rows
    ])


@patch("codexsync.process_detector.sys.platform", "win32")
@patch("codexsync.process_detector.subprocess.run")
class WindowsPathMarkerTests(unittest.TestCase):
    """CS-447: the desktop app is `ChatGPT.exe` from the `OpenAI.Codex_` package."""

    def _find(self, run_mock, markers, *rows):
        run_mock.side_effect = [_Result(0, ""), _Result(0, _cim(*rows))]
        return CodexProcessDetector(["codex.exe"]).find_processes(markers)

    def test_the_desktop_app_is_found_by_its_package_path(self, run_mock) -> None:
        found = self._find(run_mock, [_MARKER], (10, "ChatGPT.exe", _APP))
        self.assertEqual([proc.pid for proc in found], [10])
        self.assertEqual(found[0].program, _APP)

    def test_a_marker_written_with_backslashes_means_the_same(self, run_mock) -> None:
        found = self._find(run_mock, [r"\OpenAI.Codex_*\app\ChatGPT.exe"], (10, "ChatGPT.exe", _APP))
        self.assertEqual([proc.pid for proc in found], [10])

    def test_the_ordinary_chatgpt_app_is_not_codex(self, run_mock) -> None:
        self.assertEqual(self._find(run_mock, [_MARKER], (11, "ChatGPT.exe", _CHATGPT)), [])

    def test_the_always_running_service_in_the_same_package_is_not_matched(self, run_mock) -> None:
        # The package folder also holds the sandbox service, which runs from
        # boot (CS-264); a marker naming the whole folder would read RUNNING forever.
        self.assertEqual(
            self._find(run_mock, [_MARKER], (12, "codex-windows-sandbox-service.exe", _SERVICE)), [],
        )

    def test_a_process_without_a_path_is_matched_by_name_only(self, run_mock) -> None:
        found = self._find(run_mock, [_MARKER, "codex-command-runner"],
                           (13, "ChatGPT.exe", None), (14, "codex-command-runner.exe", None))
        self.assertEqual([proc.pid for proc in found], [14])

    def test_the_path_survives_the_merge_with_tasklist(self, run_mock) -> None:
        run_mock.side_effect = [
            _Result(0, '"ChatGPT.exe","10","Console","1","10,000 K"\n'),
            _Result(0, _cim((10, "ChatGPT.exe", _APP))),
        ]
        [proc] = CodexProcessDetector(["codex.exe"])._list_windows_all_complete()
        self.assertEqual(proc.program, _APP)

    def test_the_cim_listing_asks_for_the_path(self, run_mock) -> None:
        run_mock.side_effect = [_Result(0, ""), _Result(0, "[]")]
        CodexProcessDetector(["codex.exe"])._list_windows_all_complete()
        script = run_mock.call_args_list[1].args[0][-1]
        self.assertIn("ExecutablePath", script)

    def test_has_marker_follows_the_same_rule(self, run_mock) -> None:
        detector = CodexProcessDetector(["codex.exe"])
        self.assertTrue(detector.has_marker(ProcessInfo(1, "ChatGPT.exe", program=_APP), _MARKER))
        self.assertFalse(detector.has_marker(ProcessInfo(2, "ChatGPT.exe", program=_CHATGPT), _MARKER))
        self.assertFalse(detector.has_marker(ProcessInfo(3, "ChatGPT.exe"), _MARKER))

    def test_the_report_names_the_marker_and_never_the_path(self, run_mock) -> None:
        run_mock.side_effect = [_Result(0, ""), _Result(0, _cim((10, "ChatGPT.exe", _APP)))]
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
