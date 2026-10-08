"""What a release tag means, checked before anything is built or published.

    python scripts/release_meta.py check --tag v0.2.0-alpha.1
    python scripts/release_meta.py outputs --tag v0.2.0-alpha.1 >> "$GITHUB_OUTPUT"
    python scripts/release_meta.py notes --tag v0.2.0-alpha.1 --out notes.md
    python scripts/release_meta.py pypi-readme --tag v0.2.0-alpha.1

The tag is written the way people read it (`v0.2.0-alpha.1`); the package
version is its PEP 440 form (`0.2.0a1`), which is what `pyproject.toml`,
PyPI and `codexsync --version` carry. PEP 440 accepts the tag's spelling as
another way of writing the same version, so the two never name different
builds -- and `check` refuses a tag whose version is not the one
`pyproject.toml` declares, or that `CHANGELOG.md` has no dated section for.

`notes` is the GitHub release body: the section's introduction (everything
before its first `###`) and a link to the full list, because the 0.2 section
alone is longer than anyone reads on a release page.

`pypi-readme` rewrites `README.md` *in the build checkout only*: PyPI shows the
file without the repository around it, so every relative link and image is
pinned to the tag on GitHub, and GitHub's `> [!NOTE]` alerts, which PyPI shows
literally, become a bold label. Run it in CI before `python -m build`; never
commit its output.

Standard library only, like the package.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent

_TAG = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<label>a|alpha|b|beta|rc|c|pre|preview)[-_.]?(?P<number>\d*))?$",
    re.IGNORECASE,
)
_LABELS = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "rc": "rc", "c": "rc", "pre": "rc", "preview": "rc"}
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>.+))?$", re.MULTILINE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALERT = re.compile(r"^> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.MULTILINE)
_MD_LINK = re.compile(r"(?P<bang>!?)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)\)")
_HTML_ATTR = re.compile(r"(?P<attr>\b(?:src|href))=\"(?P<target>[^\"]+)\"")


def version_from_tag(tag: str) -> str:
    """`v0.2.0-alpha.1` -> `0.2.0a1`; `v0.2.0` -> `0.2.0`. Anything else is refused."""
    match = _TAG.match(tag.strip())
    if match is None:
        raise ValueError(f"Tag {tag!r} is not a release tag (expected v<major>.<minor>.<patch>[-alpha.N|-beta.N|-rc.N])")
    version = match.group("release")
    label = match.group("label")
    if label:
        version += _LABELS[label.lower()] + (match.group("number") or "0")
    return version


def is_prerelease(version: str) -> bool:
    return re.search(r"(a|b|rc)\d+$", version) is not None


def declared_version(root: Path = REPO_ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def repository_url(root: Path = REPO_ROOT) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["urls"]["Repository"]).rstrip("/")


def changelog_section(text: str, version: str) -> tuple[str | None, str]:
    """The date and body of `## [version]`; `ValueError` when there is none."""
    headings = list(_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if heading.group("version") == version:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return heading.group("date"), text[heading.end():end].strip("\n")
    raise ValueError(f"CHANGELOG.md has no section for {version}")


def check(tag: str, root: Path = REPO_ROOT) -> list[str]:
    problems: list[str] = []
    try:
        version = version_from_tag(tag)
    except ValueError as error:
        return [str(error)]
    declared = declared_version(root)
    if version != declared:
        problems.append(f"Tag {tag} means version {version}, but pyproject.toml declares {declared}")
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    try:
        date, _body = changelog_section(text, version)
    except ValueError as error:
        problems.append(str(error))
    else:
        if date is None or not _DATE.match(date.strip()):
            problems.append(f"CHANGELOG.md section {version} has no release date (found {date!r})")
    if re.search(r"^## \[Unreleased\]", text, re.MULTILINE | re.IGNORECASE):
        problems.append("CHANGELOG.md still has an [Unreleased] heading")
    return problems


def release_notes(tag: str, root: Path = REPO_ROOT) -> str:
    version = version_from_tag(tag)
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    _date, body = changelog_section(text, version)
    intro = body.split("\n### ", 1)[0].strip()
    link = f"{repository_url(root)}/blob/{tag}/CHANGELOG.md"
    return f"{intro}\n\nEvery change in this release: [CHANGELOG.md]({link}).\n"


def _absolute(target: str, *, image: bool, repo: str, ref: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith(("#", "//")):
        return target
    path = target[2:] if target.startswith("./") else target.lstrip("/")
    if image:
        owner_repo = repo.split("github.com/", 1)[1]
        return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"
    return f"{repo}/blob/{ref}/{path}"


def pypi_readme(text: str, *, repo: str, ref: str) -> str:
    text = _ALERT.sub(lambda match: f"> **{match.group(1).capitalize()}**", text)

    def markdown(match: re.Match[str]) -> str:
        image = bool(match.group("bang"))
        target = _absolute(match.group("target"), image=image, repo=repo, ref=ref)
        return f"{match.group('bang')}[{match.group('text')}]({target})"

    def html(match: re.Match[str]) -> str:
        image = match.group("attr") == "src"
        return f'{match.group("attr")}="{_absolute(match.group("target"), image=image, repo=repo, ref=ref)}"'

    return _HTML_ATTR.sub(html, _MD_LINK.sub(markdown, text))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check", "outputs", "notes", "pypi-readme"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if args.command == "check":
        problems = check(args.tag)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        if not problems:
            print(f"{args.tag} -> {version_from_tag(args.tag)}: pyproject.toml and CHANGELOG.md agree")
        return 1 if problems else 0
    if args.command == "outputs":
        version = version_from_tag(args.tag)
        print(f"version={version}")
        print(f"prerelease={'true' if is_prerelease(version) else 'false'}")
        return 0
    if args.command == "notes":
        notes = release_notes(args.tag)
        if args.out is None:
            sys.stdout.buffer.write(notes.encode("utf-8"))
        else:
            args.out.write_text(notes, encoding="utf-8")
        return 0
    readme = REPO_ROOT / "README.md"
    readme.write_text(
        pypi_readme(readme.read_text(encoding="utf-8"), repo=repository_url(), ref=args.tag), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
