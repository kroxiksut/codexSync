"""The release tag, the package version and the changelog must name one build.

`scripts/release_meta.py` runs first in the release workflow and stops it
before anything is built or uploaded; a PyPI version number can never be
uploaded twice, so a mismatch found after the upload is a mismatch for good.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_meta", REPO_ROOT / "scripts" / "release_meta.py")
assert _spec is not None and _spec.loader is not None
meta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(meta)

REPO = "https://github.com/kroxiksut/codexSync"


def _root(version: str, changelog: str) -> tempfile.TemporaryDirectory[str]:
    folder = tempfile.TemporaryDirectory()
    root = Path(folder.name)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "codexsync"\nversion = "{version}"\n\n[project.urls]\nRepository = "{REPO}"\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return folder


class TagTests(unittest.TestCase):
    def test_the_readable_tag_is_the_pep_440_version(self) -> None:
        self.assertEqual(meta.version_from_tag("v0.2.0-alpha.1"), "0.2.0a1")
        self.assertEqual(meta.version_from_tag("v0.2.0-beta.2"), "0.2.0b2")
        self.assertEqual(meta.version_from_tag("v0.2.0-rc.1"), "0.2.0rc1")
        self.assertEqual(meta.version_from_tag("v0.2.0a1"), "0.2.0a1")
        self.assertEqual(meta.version_from_tag("v0.2.0"), "0.2.0")

    def test_anything_else_is_refused(self) -> None:
        for tag in ("latest", "v0.2.0-dev", "v0.2.0+local", "0.2.0 alpha"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                meta.version_from_tag(tag)

    def test_only_a_labelled_version_is_a_prerelease(self) -> None:
        self.assertTrue(meta.is_prerelease("0.2.0a1"))
        self.assertTrue(meta.is_prerelease("0.2.0rc1"))
        self.assertFalse(meta.is_prerelease("0.2.0"))


class CheckTests(unittest.TestCase):
    CHANGELOG = "# Changelog\n\n## [0.2.0a1] - 2026-10-08\n\nIntro.\n\n### Added\n- x\n\n## [0.1.2] - 2026-03-21\n"

    def test_a_tag_that_matches_both_files_passes(self) -> None:
        with _root("0.2.0a1", self.CHANGELOG) as folder:
            self.assertEqual(meta.check("v0.2.0-alpha.1", Path(folder)), [])

    def test_a_tag_for_another_version_is_refused(self) -> None:
        with _root("0.2.0a1", self.CHANGELOG) as folder:
            problems = meta.check("v0.2.0", Path(folder))
        self.assertTrue(any("pyproject.toml declares 0.2.0a1" in problem for problem in problems))

    def test_an_undated_or_unreleased_section_is_refused(self) -> None:
        undated = "## [Unreleased]\n\n## [0.2.0a1] - Unreleased\n\nIntro.\n"
        with _root("0.2.0a1", undated) as folder:
            problems = meta.check("v0.2.0-alpha.1", Path(folder))
        self.assertTrue(any("no release date" in problem for problem in problems))
        self.assertTrue(any("[Unreleased]" in problem for problem in problems))

    def test_a_version_without_a_section_is_refused(self) -> None:
        with _root("0.2.0a2", self.CHANGELOG) as folder:
            problems = meta.check("v0.2.0-alpha.2", Path(folder))
        self.assertTrue(any("no section for 0.2.0a2" in problem for problem in problems))


class NotesTests(unittest.TestCase):
    def test_the_body_is_the_introduction_and_a_link_to_the_rest(self) -> None:
        changelog = "## [0.2.0a1] - 2026-10-08\n\nWhy alpha.\n\n**Install.** pip.\n\n### Added\n- long list\n"
        with _root("0.2.0a1", changelog) as folder:
            notes = meta.release_notes("v0.2.0-alpha.1", Path(folder))
        self.assertIn("Why alpha.", notes)
        self.assertIn("**Install.** pip.", notes)
        self.assertNotIn("long list", notes)
        self.assertIn(f"{REPO}/blob/v0.2.0-alpha.1/CHANGELOG.md", notes)


class PypiReadmeTests(unittest.TestCase):
    def test_relative_links_and_images_are_pinned_to_the_tag(self) -> None:
        text = (
            '<img src="assets/brand/brand-mark.png"> <a href="./README.ru.md">ru</a>\n'
            "[Sync](docs/en/SYNC.md#handing-work-over) ![shot](docs/shot.png) [x](https://e.org) [top](#install)\n"
        )
        out = meta.pypi_readme(text, repo=REPO, ref="v0.2.0-alpha.1")
        self.assertIn('src="https://raw.githubusercontent.com/kroxiksut/codexSync/v0.2.0-alpha.1/assets/brand/brand-mark.png"', out)
        self.assertIn(f'href="{REPO}/blob/v0.2.0-alpha.1/README.ru.md"', out)
        self.assertIn(f"({REPO}/blob/v0.2.0-alpha.1/docs/en/SYNC.md#handing-work-over)", out)
        self.assertIn("(https://raw.githubusercontent.com/kroxiksut/codexSync/v0.2.0-alpha.1/docs/shot.png)", out)
        self.assertIn("(https://e.org)", out)
        self.assertIn("(#install)", out)

    def test_github_alerts_become_a_label(self) -> None:
        out = meta.pypi_readme("> [!IMPORTANT]\n> Windows only.\n", repo=REPO, ref="v1")
        self.assertEqual(out, "> **Important**\n> Windows only.\n")

    def test_the_real_readme_keeps_no_relative_target(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        out = meta.pypi_readme(text, repo=REPO, ref="v0.2.0-alpha.1")
        targets = re.findall(r'(?:src|href)="([^"]+)"', out) + re.findall(r"\]\(([^)\s]+)\)", out)
        self.assertTrue(targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertRegex(target, r"^(https?://|#)")
        self.assertNotIn("[!", out)


if __name__ == "__main__":
    unittest.main()
