"""The 0.1 Windows task wrapper must not kill Python on its first stderr line."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scheduler" / "windows" / "run-codexsync.ps1"


class RunWrapperTests(unittest.TestCase):
    def test_native_call_is_not_under_stop(self) -> None:
        """CS-450: `Stop` + `*>>` turned a stderr line into NativeCommandError."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("*>>", text)
        call = text.index("& $PythonExe")
        before = text[:call]
        last = re.findall(r'\$ErrorActionPreference = "(\w+)"', before)[-1]
        self.assertEqual(last, "Continue")
        self.assertIn("2>&1", text[call:text.index("\n", call)])
        self.assertIn("exit $exitCode", text)


if __name__ == "__main__":
    unittest.main()
