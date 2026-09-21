"""A remembered config path is a hint, not an instruction (CS-266, CS-261).

A GUI smoke run once left its own throwaway config remembered in the real
`QSettings`. The scratch directory survived, so every later start opened it:
the window ran against an empty fake `.codex` and the template's process lists,
and reported "Codex is open" and a bare `SourceMissingError` for a whole
session. Neither message could be argued with, because neither named a path.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
import uuid

from codexsync.config_locations import choose_config_path, is_under_temp

SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "test-sandbox"


class TempRememberedPathTests(unittest.TestCase):
    def setUp(self) -> None:
        # The stale path has to be genuinely inside the OS temp directory, and
        # the "real" config genuinely outside it -- that distinction is the
        # whole subject here, so neither side may be a `tmp_path`.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scratch = self.tmp / "scratchpad" / "smoke"
        self.scratch.mkdir(parents=True)
        (self.scratch / "config.toml").write_text("# throwaway\n", encoding="utf-8")
        self.project = SANDBOX_ROOT / f"config-choice-{uuid.uuid4().hex[:8]}"
        self.project.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.project, True)

    def test_a_config_in_the_temp_directory_is_recognised(self) -> None:
        self.assertTrue(is_under_temp(self.scratch / "config.toml"))

    def test_an_ordinary_config_is_not(self) -> None:
        self.assertFalse(is_under_temp(Path.home() / "config.toml"))

    def test_a_remembered_temp_config_is_not_opened(self) -> None:
        real = self.project / "config.toml"
        real.write_text("# the user's\n", encoding="utf-8")
        choice = choose_config_path(None, self.scratch / "config.toml", cwd=self.project)
        self.assertEqual(choice.path, real)
        self.assertEqual(choice.source, "cwd")

    def test_the_rejected_path_is_reported_not_swallowed(self) -> None:
        # The window shows it. Dropping a remembered path silently would swap
        # one unexplained config for another.
        stale = self.scratch / "config.toml"
        choice = choose_config_path(None, stale, cwd=self.project)
        self.assertEqual(choice.rejected_remembered, stale)

    def test_nothing_is_rejected_when_nothing_was_remembered(self) -> None:
        choice = choose_config_path(None, None, cwd=self.project)
        self.assertIsNone(choice.rejected_remembered)

    def test_an_explicit_path_in_temp_still_wins(self) -> None:
        # `-c` is a request, not a hint: naming a file is deliberate.
        stale = self.scratch / "config.toml"
        choice = choose_config_path(stale, None, cwd=self.project)
        self.assertEqual(choice.path, stale)
        self.assertEqual(choice.source, "explicit")

    def test_an_ordinary_remembered_path_is_still_preferred(self) -> None:
        remembered = self.project / "elsewhere.toml"
        remembered.write_text("# remembered\n", encoding="utf-8")
        choice = choose_config_path(None, remembered, cwd=self.project)
        self.assertEqual(choice.source, "remembered")
        self.assertIsNone(choice.rejected_remembered)


if __name__ == "__main__":
    unittest.main()
