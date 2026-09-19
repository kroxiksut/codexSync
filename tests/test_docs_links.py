"""The documentation is two languages of one set of pages, and its links resolve.

Nothing else notices a dead link: GitHub renders it without complaint, and a path
printed by a refusal (`docs/dev/experiments/...`) only turns out to lead nowhere
when a user follows it. Moving the developer documents under `docs/dev/` touched
five modules, a test, a script and the changelog, which is exactly the kind of
change that leaves one reference behind.
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
LANGUAGES = ("en", "ru", "zh")

#: Tracked Markdown whose links are checked. Local `*.ru.md` working notes are
#: gitignored and absent in CI, so they are deliberately not listed.
ROOT_PAGES = ("README.md", "README.ru.md", "README.zh.md", "CONTRIBUTING.md", "CHANGELOG.md")

FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HTML_REF = re.compile(r"""(?:src|href)=["']([^"']+)["']""")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
DOCS_PATH_IN_CODE = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")


def _pages() -> list[Path]:
    pages = [REPO_ROOT / name for name in ROOT_PAGES]
    pages.extend(sorted(DOCS.rglob("*.md")))
    return [page for page in pages if page.is_file()]


def _without_code(text: str) -> str:
    return CODE_SPAN.sub("", FENCE.sub("", text))


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for heading in HEADING.findall(FENCE.sub("", page.read_text(encoding="utf-8"))):
        slug = _slug(heading)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(page: Path) -> list[str]:
    text = _without_code(page.read_text(encoding="utf-8"))
    return MD_LINK.findall(text) + HTML_REF.findall(text)


def _link_problems(pages: list[Path], *, root: Path) -> list[str]:
    problems: list[str] = []
    for page in pages:
        for link in _links(page):
            if re.match(r"^[a-z][a-z0-9+.-]*:", link) or link.startswith("//"):
                continue
            path_part, _, anchor = link.partition("#")
            target = (page.parent / path_part).resolve() if path_part else page
            where = f"{page.relative_to(root)} -> {link}"
            if not target.exists():
                problems.append(f"{where}: no such file")
            elif anchor and target.suffix == ".md" and anchor not in _anchors(target):
                problems.append(f"{where}: no such heading")
    return problems


class DocsLanguageParityTests(unittest.TestCase):
    def test_every_language_has_the_same_pages(self) -> None:
        names = {lang: sorted(p.name for p in (DOCS / lang).glob("*.md")) for lang in LANGUAGES}
        self.assertTrue(names["en"], "docs/en holds no pages")
        for lang in LANGUAGES[1:]:
            self.assertEqual(names["en"], names[lang], f"docs/en and docs/{lang} differ")

    def test_every_page_links_to_its_counterpart_near_the_top(self) -> None:
        for lang in LANGUAGES:
            for page in (DOCS / lang).glob("*.md"):
                head = "\n".join(page.read_text(encoding="utf-8").splitlines()[:5])
                for other in LANGUAGES:
                    if other != lang:
                        self.assertIn(
                            f"](../{other}/{page.name})", head,
                            f"{page.relative_to(REPO_ROOT)} has no switch to ../{other}/{page.name}",
                        )

    def test_root_readmes_link_to_each_other(self) -> None:
        """Each root README offers the other languages, or one of them is a dead end."""
        names = {"en": "README.md", "ru": "README.ru.md", "zh": "README.zh.md"}
        for language, name in names.items():
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            for other, other_name in names.items():
                if other != language:
                    self.assertIn(f'href="./{other_name}"', text, f"{name} has no switch to {other_name}")

    def test_every_language_has_the_same_screenshots(self) -> None:
        shots = {lang: sorted(p.name for p in (DOCS / "screenshots" / lang).glob("*.png")) for lang in LANGUAGES}
        self.assertTrue(shots["en"], "docs/screenshots/en holds no screenshots")
        for lang in LANGUAGES[1:]:
            self.assertEqual(shots["en"], shots[lang])


class DocsLinkTests(unittest.TestCase):
    def test_slug_matches_github(self) -> None:
        self.assertEqual(_slug("Moving a project's files"), "moving-a-projects-files")
        self.assertEqual(_slug("`[[path_mappings]]`"), "path_mappings")
        self.assertEqual(_slug("Что пока не подтверждено"), "что-пока-не-подтверждено")
        self.assertEqual(_slug("C. Windows `.exe`"), "c-windows-exe")

    def test_a_dead_link_and_a_dead_anchor_are_caught(self) -> None:
        """A check that never fires proves nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "PAGE.md"
            page.write_text(
                "# Title\n\n[ok](#title) [gone](MISSING.md) [nowhere](#no-such-heading)\n"
                "`[in code](MISSING2.md)`\n",
                encoding="utf-8",
            )
            problems = _link_problems([page], root=Path(tmp))
        self.assertEqual(2, len(problems), problems)
        self.assertIn("MISSING.md: no such file", problems[0])
        self.assertIn("#no-such-heading: no such heading", problems[1])

    def test_relative_links_and_anchors_resolve(self) -> None:
        self.assertEqual([], _link_problems(_pages(), root=REPO_ROOT))

    def test_docs_paths_named_in_code_exist(self) -> None:
        problems: list[str] = []
        for root in ("src", "scripts", "tests"):
            for source in (REPO_ROOT / root).rglob("*.py"):
                for path in DOCS_PATH_IN_CODE.findall(source.read_text(encoding="utf-8")):
                    if not (REPO_ROOT / path).is_file():
                        problems.append(f"{source.relative_to(REPO_ROOT)}: {path}")
        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
