"""`exclude_globs`, against the patterns the shipped config actually contains.

This module had no tests, and that is exactly where it broke: `**` was being
matched by `PurePath.match`, where it means the same as `*`. Every case below
that mentions a depth is a case the old behaviour got wrong on a real `.codex`.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
import unittest

from codexsync.filters import PathFilter

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "codexsync" / "config.example.toml"


class ExcludeGlobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shipped = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))["filters"]["exclude_globs"]
        self.filter = PathFilter(self.shipped)

    def test_the_shipped_globs_exclude_a_lock_folder_at_the_top_of_the_state_root(self) -> None:
        """`.codex/tmp/arg0/*` is where Codex keeps its locks on Unix."""
        self.assertTrue(self.filter.is_excluded("tmp/arg0/lock"))
        self.assertTrue(self.filter.is_excluded("tmp/anything"))

    def test_they_exclude_a_cache_at_any_depth_on_either_side_of_it(self) -> None:
        for path in ("cache/a", "a/cache/b", "a/b/cache/c/d", ".cache/x"):
            with self.subTest(path=path):
                self.assertTrue(self.filter.is_excluded(path))

    def test_a_log_is_excluded_at_the_top_as_well_as_deeper(self) -> None:
        self.assertTrue(self.filter.is_excluded("codexsync.log"))
        self.assertTrue(self.filter.is_excluded("logs/a/b/codexsync.log"))

    def test_an_ordinary_file_is_not_excluded(self) -> None:
        for path in ("skills/pack/SKILL.md", "plugins/p.json", "AGENTS.md", "sessions/2026/a.jsonl"):
            with self.subTest(path=path):
                self.assertFalse(self.filter.is_excluded(path))

    def test_a_name_that_merely_contains_a_pattern_word_is_kept(self) -> None:
        """`tmp` is a folder, not a substring: `tmpfile.md` is somebody's note."""
        self.assertFalse(self.filter.is_excluded("tmpfile.md"))
        self.assertFalse(self.filter.is_excluded("notes/cached.md"))


class PatternSyntaxTests(unittest.TestCase):
    def matches(self, pattern: str, path: str) -> bool:
        return PathFilter([pattern]).is_excluded(path)

    def test_a_double_star_segment_matches_no_segments_at_all(self) -> None:
        self.assertTrue(self.matches("**/tmp/**", "tmp/x"))

    def test_a_double_star_segment_matches_several(self) -> None:
        self.assertTrue(self.matches("**/tmp/**", "a/b/c/tmp/d/e"))

    def test_a_star_never_crosses_a_separator(self) -> None:
        self.assertTrue(self.matches("a/*.md", "a/b.md"))
        self.assertFalse(self.matches("a/*.md", "a/b/c.md"))

    def test_a_bare_name_pattern_matches_that_name_anywhere(self) -> None:
        self.assertTrue(self.matches("*.lock", "a/b/c.lock"))
        self.assertTrue(self.matches("*.lock", "c.lock"))
        self.assertFalse(self.matches("*.lock", "c.lock.txt"))

    def test_an_anchored_pattern_does_not_match_the_same_name_deeper(self) -> None:
        self.assertTrue(self.matches("logs/*.log", "logs/a.log"))
        self.assertFalse(self.matches("logs/*.log", "other/logs/a.log"))

    def test_a_backslash_path_is_matched_like_the_posix_one(self) -> None:
        self.assertTrue(self.matches("**/tmp/**", "a\\tmp\\b"))

    def test_matching_stays_case_sensitive(self) -> None:
        self.assertFalse(self.matches("**/tmp/**", "a/TMP/b"))

    def test_a_character_class_still_works(self) -> None:
        self.assertTrue(self.matches("*.[ab]", "x.a"))
        self.assertFalse(self.matches("*.[ab]", "x.c"))

    def test_an_empty_pattern_excludes_nothing(self) -> None:
        self.assertFalse(PathFilter(["", "   "]).is_excluded("anything"))

    def test_a_directory_itself_is_not_excluded_by_a_rule_about_its_contents(self) -> None:
        """`**/tmp/**` is about what is inside; the scanner only sees files anyway."""
        self.assertFalse(self.matches("**/tmp/**", "tmp"))


if __name__ == "__main__":
    unittest.main()
