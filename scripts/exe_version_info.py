"""The Windows version resource of both exes, built from package metadata (CS-273).

Without one, the *Details* tab of an exe's properties is empty: no version, no
product name, no description, no copyright, and a downloaded exe cannot say
what it is without being run. Both spec files call `build_version_info`, so
the release workflow adds nothing, and every value comes from somewhere that
already exists:

- the version from the installed `codexsync` metadata, which `pyproject.toml`
  writes -- the same source `version.py` reads, so the resource and
  `codexsync --version` cannot disagree. A version is never written here;
- the copyright holder from the metadata's author, the licence from its
  `License-Expression` (`license` in `pyproject.toml`), the link from its
  `Homepage` URL. A version resource has no licence field, and `Comments` is
  not shown by Explorer, so the licence rides in the copyright line, which
  the *Details* tab does show;
- each description from the window's language files, one string table per
  language the window speaks, so adding a language to the window is adding it
  here too. So is the trademark notice (`LegalTrademarks`, which Explorer
  shows as *Trademarks*): the name says Codex, and the file has to say that
  Codex is OpenAI's and this project is not.

A string table per language rather than one English table: Windows reads the
table matching the user's language when there is one, so a Russian or Chinese
Explorer describes the file in its own language, and the Translation list is
the honest answer to "which languages is this in". English stays first, which
is the table a reader without a match falls back to.

`CompanyName` (*Company* in Explorer) is the author as well: the owner chose
his own name over an invented company (2026-10-09), and it comes from the same
metadata as the copyright, so changing `authors` changes both.

The fixed part carries `VS_FF_PRERELEASE` for an alpha, beta, release
candidate or development version, so a tool that reads the flags is told
what the version string already says.

Nothing here imports PyInstaller until `build_version_info` is called, so the
suite can check every field on a machine that has no PyInstaller at all.
"""
from __future__ import annotations

import datetime as _dt
from email.message import Message
from importlib.metadata import metadata as _package_metadata
import json
from pathlib import Path
import re
from typing import Any, Mapping

DISTRIBUTION = "codexsync"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "src" / "codexsync" / "gui" / "locale"

#: The year the project began; the copyright runs from here to the build year.
FIRST_YEAR = 2026

#: What each build is called and which language-file key describes it.
KINDS: dict[str, dict[str, str]] = {
    "window": {
        "product": "CodexSync",
        "file": "codexsync-gui.exe",
        "internal": "codexsync-gui",
        "description_key": "exe.description.window",
    },
    "console": {
        "product": "codexsync",
        "file": "codexsync.exe",
        "internal": "codexsync",
        "description_key": "exe.description.console",
    },
}

#: The language-file key of the trademark notice both builds carry.
TRADEMARKS_KEY = "exe.trademarks"

#: Window language -> Windows language id. English first: it is the fallback.
LANGUAGE_IDS: dict[str, int] = {"en": 0x0409, "ru": 0x0419, "zh": 0x0804}

#: Every table is UTF-16, the only code page that holds all three languages.
UNICODE_CODEPAGE = 1200

#: `VS_FF_PRERELEASE` in the fixed part of the resource.
PRERELEASE_FLAG = 0x2


def numeric_version(text: str) -> tuple[int, int, int, int]:
    """`0.2.0` -> `(0, 2, 0, 0)`; anything after the release numbers is dropped.

    The fixed part of the resource holds four 16-bit numbers and nothing else,
    so `0.2.0.dev3` and `0.0.0+unknown` keep their leading numbers; the full
    string still goes into the text fields.
    """
    match = re.match(r"\d+(?:\.\d+)*", text.strip())
    if match is None:
        raise ValueError(f"Version {text!r} does not start with a number")
    parts = [int(part) for part in match.group(0).split(".")][:4]
    if any(part > 0xFFFF for part in parts):
        raise ValueError(f"Version {text!r} has a part larger than a version resource holds")
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def is_prerelease(text: str) -> bool:
    """`0.2.0a1`, `0.2.0rc1`, `0.2.0.dev3` -> True; `0.2.0`, `0.2.0.post1` -> False.

    Reads the normalised form metadata carries (PEP 440); a local label after
    `+` says where a build came from, not how finished it is.
    """
    public = text.strip().split("+", 1)[0].lower()
    match = re.match(r"\d+(?:\.\d+)*", public)
    rest = public[match.end():] if match else public
    return re.search(r"(?:a|b|rc|dev)\d*", rest.replace("post", "")) is not None


def _author(meta: Message) -> str:
    author = (meta.get("Author") or "").strip()
    if author:
        return author
    # `authors = [{name, email}]` is written as `Author-email: Name <mail>`.
    named = (meta.get("Author-email") or "").split("<", 1)[0].strip().strip('"')
    if named:
        return named
    raise ValueError("Package metadata names no author to hold the copyright")


def _homepage(meta: Message) -> str:
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage" and url.strip():
            return url.strip()
    raise ValueError("Package metadata has no Homepage URL")


def _license(meta: Message) -> str:
    # Metadata 2.4 (PEP 639) writes `License-Expression`; older tools wrote `License`.
    for field in ("License-Expression", "License"):
        value = (meta.get(field) or "").strip()
        if value:
            return value
    raise ValueError("Package metadata names no licence")


def _descriptions(key: str, locale_dir: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for language in LANGUAGE_IDS:
        table = json.loads((locale_dir / f"{language}.json").read_text(encoding="utf-8"))
        value = table.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{language}.json has no {key!r}")
        found[language] = value
    return found


def version_fields(
    kind: str,
    *,
    meta: Mapping[str, Any] | Message | None = None,
    locale_dir: Path = LOCALE_DIR,
    year: int | None = None,
) -> dict[str, Any]:
    """Everything the resource says, as plain data: the part the suite checks."""
    if kind not in KINDS:
        raise ValueError(f"Unknown build kind {kind!r}; expected one of {sorted(KINDS)}")
    spec = KINDS[kind]
    meta = _package_metadata(DISTRIBUTION) if meta is None else meta
    version = str(meta["Version"])
    build_year = _dt.date.today().year if year is None else year
    years = str(FIRST_YEAR) if build_year <= FIRST_YEAR else f"{FIRST_YEAR}-{build_year}"
    author = _author(meta)
    copyright_line = f"© {years} {author} — {_license(meta)} — {_homepage(meta)}"
    descriptions = _descriptions(spec["description_key"], locale_dir)
    trademarks = _descriptions(TRADEMARKS_KEY, locale_dir)
    tables = {
        language: {
            "CompanyName": author,
            "FileDescription": descriptions[language],
            "FileVersion": version,
            "InternalName": spec["internal"],
            "LegalCopyright": copyright_line,
            "LegalTrademarks": trademarks[language],
            "OriginalFilename": spec["file"],
            "ProductName": spec["product"],
            "ProductVersion": version,
        }
        for language in LANGUAGE_IDS
    }
    return {
        "numeric": numeric_version(version),
        "flags": PRERELEASE_FLAG if is_prerelease(version) else 0,
        "version": version,
        "tables": tables,
        "translations": [(LANGUAGE_IDS[language], UNICODE_CODEPAGE) for language in LANGUAGE_IDS],
    }


def build_version_info(kind: str) -> Any:
    """The `VSVersionInfo` a spec file hands to `EXE(version=...)`."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    fields = version_fields(kind)
    translations: list[int] = []
    for language_id, codepage in fields["translations"]:
        translations += [language_id, codepage]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=fields["numeric"], prodvers=fields["numeric"], flags=fields["flags"]),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        f"{LANGUAGE_IDS[language]:04X}{UNICODE_CODEPAGE:04X}",
                        [StringStruct(name, value) for name, value in table.items()],
                    )
                    for language, table in fields["tables"].items()
                ]
            ),
            VarFileInfo([VarStruct("Translation", translations)]),
        ],
    )
