"""Every string the window shows, looked up by key in a per-language file.

No widget holds a sentence. A language is one JSON file under ``locale/``, so
adding a language is adding a file -- plus, if its grammar counts differently,
one entry in ``PLURAL_RULES`` -- and never touching a screen.

Three rules keep a half-finished translation from becoming a broken window.

**English is the floor.** A key the chosen language lacks is read from
``en.json``, so a new screen can ship before its translation does and shows
English rather than a blank label. A key missing from English too comes back
as the key itself: visible, and caught by the test that checks every key the
window names, but never a crash in front of the user.

**Plurals are the language's, not English's.** Russian needs three forms where
English needs two and Chinese needs one, so a count is never glued to a noun in
code. A plural entry is an object of CLDR categories (``one``, ``few``,
``many``, ``other``) and the rule for the language picks one.

**Nothing here imports Qt.** The boundary test forbids it outside
``window.py``, and it is what lets the catalogue be checked on a machine that
never installed the extra.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Iterable, Mapping

LOCALE_DIR = Path(__file__).resolve().parent / "locale"

#: The language every other one falls back to, and the one that must be complete.
FALLBACK = "en"

#: CLDR cardinal plural rules for integers, by language. A language with no
#: entry uses ``other`` for every count, which is correct for Chinese and
#: Japanese and merely unidiomatic anywhere else -- never wrong in a way that
#: hides a number.
PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "en": lambda n: "one" if n == 1 else "other",
    "ru": lambda n: (
        "one" if n % 10 == 1 and n % 100 != 11
        else "few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14
        else "many"
    ),
    "zh": lambda n: "other",
}


def available_languages(locale_dir: Path = LOCALE_DIR) -> tuple[str, ...]:
    """Language codes that have a file, fallback first."""
    codes = sorted(path.stem for path in locale_dir.glob("*.json"))
    return tuple(sorted(codes, key=lambda code: code != FALLBACK))


def pick_language(preferred: Iterable[str], locale_dir: Path = LOCALE_DIR) -> str:
    """The first preferred language there is a file for, else the fallback.

    Accepts system-style tags (``ru-RU``, ``ru_RU``, ``zh-Hans-CN``) and matches
    on the primary subtag, because a catalogue is per language, not per region.
    """
    available = set(available_languages(locale_dir))
    for tag in preferred:
        code = str(tag).replace("_", "-").split("-")[0].lower()
        if code in available:
            return code
    return FALLBACK


def _read(language: str, locale_dir: Path) -> dict[str, object]:
    path = locale_dir / f"{language}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must hold a JSON object of keys")
    return data


@dataclass(frozen=True, slots=True)
class Catalog:
    """One language's strings, with English underneath."""

    language: str
    strings: Mapping[str, object]
    fallback: Mapping[str, object]

    @property
    def native_name(self) -> str:
        """How the language names itself, for the language picker."""
        return self.text("language.native_name")

    def text(self, key: str, /, **params: object) -> str:
        value = self._lookup(key)
        if not isinstance(value, str):
            return key
        return value.format(**params) if params else value

    def plural(self, key: str, count: int, /, **params: object) -> str:
        """The form of ``key`` this language uses for ``count``.

        ``count`` is always passed to the template, so a form can place the
        number wherever the grammar wants it.
        """
        forms = self._lookup(key)
        if not isinstance(forms, Mapping):
            return key
        rule = PLURAL_RULES.get(self.language, lambda n: "other")
        form = forms.get(rule(count))
        if not isinstance(form, str):
            form = forms.get("other")
        if not isinstance(form, str):
            return key
        return form.format(count=count, **params)

    def has(self, key: str) -> bool:
        return key in self.strings or key in self.fallback

    def _lookup(self, key: str) -> object:
        # A form object the language has is used whole; mixing its categories
        # with English ones would pick an English form for a Russian count.
        if key in self.strings:
            return self.strings[key]
        return self.fallback.get(key)


def load(language: str, locale_dir: Path = LOCALE_DIR) -> Catalog:
    fallback = _read(FALLBACK, locale_dir)
    strings = fallback if language == FALLBACK else _read(language, locale_dir)
    return Catalog(
        language=language if strings or language == FALLBACK else FALLBACK,
        strings=strings or fallback,
        fallback=fallback,
    )


__all__ = [
    "FALLBACK",
    "LOCALE_DIR",
    "PLURAL_RULES",
    "Catalog",
    "available_languages",
    "load",
    "pick_language",
]
