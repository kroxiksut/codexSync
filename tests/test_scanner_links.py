"""CS-432: the settings scanner never follows a junction or symlink."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.filters import PathFilter
from codexsync.scanner import scan_tree

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class ScannerLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"scanner-links-{uuid.uuid4().hex[:8]}"
        self.codex = self.root / ".codex"
        (self.codex / "skills" / "mine").mkdir(parents=True)
        (self.codex / "skills" / "mine" / "SKILL.md").write_text("ours", encoding="utf-8")
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()
        (self.elsewhere / "not-ours.md").write_text("theirs", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _scan(self) -> dict:
        return scan_tree(self.codex, ["skills"], PathFilter([]))

    def test_a_junction_is_not_followed(self) -> None:
        if os.name != "nt":
            self.skipTest("junctions exist only on Windows")
        import _winapi

        link = self.codex / "skills" / "linked"
        _winapi.CreateJunction(str(self.elsewhere), str(link))
        self.addCleanup(os.rmdir, link)  # the junction itself, never its target
        found = self._scan()
        self.assertIn("skills/mine/SKILL.md", found)
        self.assertFalse([rel for rel in found if rel.startswith("skills/linked")])

    def test_a_symlink_is_not_followed(self) -> None:
        dir_link = self.codex / "skills" / "dirlink"
        file_link = self.codex / "skills" / "file.md"
        try:
            dir_link.symlink_to(self.elsewhere, target_is_directory=True)
            file_link.symlink_to(self.elsewhere / "not-ours.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        found = self._scan()
        self.assertEqual(sorted(found), ["skills/mine/SKILL.md"])


if __name__ == "__main__":
    unittest.main()
