from __future__ import annotations

from unittest.mock import patch
import unittest

from codexsync.process_detector import CodexProcessDetector


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


if __name__ == "__main__":
    unittest.main()
