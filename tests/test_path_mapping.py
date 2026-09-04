from __future__ import annotations

import unittest

from codexsync.path_mapping import PathMappingError, PathMappingRule, apply_path_mapping


class PathMappingTests(unittest.TestCase):
    def test_longest_segment_prefix_windows_to_posix(self) -> None:
        rules = [
            PathMappingRule("broad", "a", "b", "D:\\Work", "/work"),
            PathMappingRule("nested", "a", "b", "D:\\Work\\team", "/srv/team"),
        ]
        result = apply_path_mapping("d:\\WORK\\team\\repo", source_machine="a", target_machine="b", rules=rules)
        self.assertEqual(result.rule_id, "nested")
        self.assertEqual(result.target_path, "/srv/team/repo")

    def test_boundary_collision_is_not_a_match(self) -> None:
        rules = [PathMappingRule("work", "a", "b", "D:\\Work", "E:\\Work")]
        with self.assertRaisesRegex(PathMappingError, "NO_MAPPING"):
            apply_path_mapping("D:\\Workspace\\repo", source_machine="a", target_machine="b", rules=rules)

    def test_equal_specificity_different_targets_is_ambiguous(self) -> None:
        rules = [
            PathMappingRule("one", "a", "b", "/src", "/one"),
            PathMappingRule("two", "a", "b", "/src", "/two"),
        ]
        with self.assertRaisesRegex(PathMappingError, "AMBIGUOUS_MAPPING"):
            apply_path_mapping("/src/repo", source_machine="a", target_machine="b", rules=rules)

    def test_unc_mapping(self) -> None:
        rules = [PathMappingRule("unc", "a", "b", "\\\\server\\share", "E:\\mirror")]
        result = apply_path_mapping("\\\\SERVER\\SHARE\\repo", source_machine="a", target_machine="b", rules=rules)
        self.assertEqual(result.target_path, "E:\\mirror\\repo")


if __name__ == "__main__":
    unittest.main()
