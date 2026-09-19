"""The language catalogue, checked without Qt.

A translation fails quietly: a missing key is a label that reads ``nav.sync``,
a missing plural form is a count glued to the wrong word, and neither raises.
So these tests do the reading a user would otherwise do -- every key the window
names exists in English, every language has the keys English has, and every
plural entry has each form its language's rule can ask for.

Keys a screen computes (``transfer.action.<value>``) are enumerated from the
core's own enums, so a new action added to the core without a label fails here
instead of showing its internal name in the window.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import tempfile
import unittest

from codexsync.chat_directory import Association
from codexsync.chat_move import ChatMoveKind
from codexsync.gui import i18n
from codexsync.gui.controller import Failure
from codexsync.guardian_models import GuardianResultStatus
from codexsync.progress import PHASES
from codexsync.mutation_journal import JournalState
from codexsync.recovery import RecoveryAction
from codexsync.repair_plan import RepairActionKind
from codexsync.semantic_merge import BranchRelation
from codexsync.semantic_transfer import ResolutionChoice, TransferAction

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "codexsync"
GUI = PACKAGE / "gui"
PREFLIGHT = PACKAGE / "preflight.py"
#: Every module that draws text: the window, the shared widgets and each screen.
DRAWING_MODULES = sorted([GUI / "window.py", GUI / "widgets.py", *(GUI / "screens").glob("*.py")])
_KEY = re.compile(r"^[a-z_]+(\.[A-Za-z0-9_-]+)+$")


def _raw(language: str) -> dict:
    return json.loads((i18n.LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))


def _prefixes() -> tuple[str, ...]:
    return tuple(sorted({key.split(".", 1)[0] + "." for key in _raw("en")}))


def _literal_keys() -> set[str]:
    """String constants in drawing code that look like catalogue keys.

    Constants, not only direct arguments of `t()`/`p()`: a screen often picks a
    key first (`key = "sync.done.dry" if ... else "sync.done.apply"`) and looks
    it up afterwards, and a scan that saw only direct arguments would miss both.
    """
    prefixes = _prefixes()
    keys: set[str] = set()
    for path in DRAWING_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _KEY.match(node.value) and node.value.startswith(prefixes):
                    keys.add(node.value)
    return keys


def _module_constant(path: Path, name: str):
    """A literal module-level constant, read without importing (and so without Qt)."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def _module_codes(path: Path) -> set[str]:
    """Upper-case string constants whose value equals their name: a module's reason codes."""
    codes = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Constant) and node.value.value == name and name.isupper():
                codes.add(name)
    return codes


def _settings_fields() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(section, key, kind, choices) for every field the settings screen shows."""
    tree = ast.parse((GUI / "screens" / "settings.py").read_text(encoding="utf-8"))
    fields = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Field":
            args = [ast.literal_eval(arg) for arg in node.args]
            choices = args[4] if len(args) > 4 else ()
            fields.append((args[0], args[1], args[2], tuple(choices)))
    return fields


class CatalogueCompletenessTests(unittest.TestCase):
    def test_english_is_available_and_listed_first(self) -> None:
        languages = i18n.available_languages()
        self.assertEqual(languages[0], i18n.FALLBACK)
        self.assertIn("ru", languages)

    def test_every_language_has_exactly_the_english_keys(self) -> None:
        english = set(_raw("en"))
        for language in i18n.available_languages():
            with self.subTest(language=language):
                keys = set(_raw(language))
                self.assertEqual(sorted(english - keys), [], "missing, would show English")
                self.assertEqual(sorted(keys - english), [], "unknown to English, never shown")

    def test_every_literal_key_the_window_names_exists(self) -> None:
        keys = _literal_keys()
        self.assertGreater(len(keys), 200, "the scan stopped seeing the screens")
        self.assertEqual(sorted(keys - set(_raw("en"))), [])

    def test_every_computed_key_exists(self) -> None:
        english = set(_raw("en"))
        pages = _module_constant(GUI / "window.py", "PAGES")
        computed = {f"nav.{page}" for page in pages}
        computed |= {f"page.{page}.subtitle" for page in pages}
        computed |= {f"failure.{failure.value}" for failure in Failure}
        computed |= {f"status.{status}" for status in ("PASS", "WARN", "FAIL")}
        computed |= {f"association.{item.value}" for item in Association}
        computed |= {f"chats.plan.kind.{item.value}" for item in ChatMoveKind}
        computed |= {f"transfer.action.{item.value}" for item in TransferAction}
        computed |= {f"transfer.relation.{item.value}" for item in BranchRelation}
        computed |= {f"sessions.choice.{item.value}" for item in ResolutionChoice}
        computed |= {f"sessions.category.{value}" for value in set(_module_constant(GUI / "screens" / "sessions.py", "CATEGORIES").values())}
        computed |= {f"repair.kind.{item.value}" for item in RepairActionKind}
        computed |= {f"guardian.result.{item.value}" for item in GuardianResultStatus}
        computed |= {f"journal.state.{item.value}" for item in JournalState}
        computed |= {f"recovery.action.{item.value}" for item in RecoveryAction}
        computed |= {f"backups.target.{target}" for target in ("local", "cloud")}
        computed |= {f"recovery.{action}.apply" for action in ("resume", "rollback")}
        computed |= {f"automation.working.{kind}" for kind in ("apply", "remove", "run")}
        computed |= {f"first_run.{part}.{key}" for part in ("field", "hint") for key in ("config", "machine", "codex", "workspace", "mirror")}
        computed |= {f"settings.mapping.{column}" for column in _module_constant(GUI / "screens" / "settings.py", "MAPPING_COLUMNS")}
        computed |= {f"projects.move.code.{code}" for code in _module_codes(PACKAGE / "project_move.py") if code != "STAGING_OCCUPIED"}
        computed |= {f"guardian.reason.{code}" for code in _module_codes(PACKAGE / "guardian_restore.py")}
        computed |= {f"sessions.index.{side}{suffix}" for side in ("local", "cloud") for suffix in ("", ".missing")}
        computed |= {f"progress.phase.{phase}" for phase in PHASES}
        tabs = ("general", "sync", "protection", "automation", "mappings", "service")
        computed |= {f"settings.tab.{tab}" for tab in tabs}
        computed |= {f"settings.tab.{tab}.caption" for tab in tabs}
        for section, key, kind, choices in _settings_fields():
            computed.add(f"settings.field.{section}.{key}")
            # Log levels and formats are the config's own words (DEBUG, json):
            # shown as written, so they need no label.
            if kind == "choice" and section != "logging":
                computed |= {f"settings.choice.{section}.{key}.{choice}" for choice in choices}
        self.assertEqual(sorted(computed - english), [])

    def test_every_check_the_core_reports_has_a_label(self) -> None:
        """A new diagnostic would otherwise appear under its internal name."""
        source = PREFLIGHT.read_text(encoding="utf-8")
        names = set(re.findall(r'PreflightCheckResult\(\s*"([a-z_]+)"', source))
        names |= set(re.findall(r'_check_path_available\(\s*"([a-z_]+)"', source))
        self.assertGreaterEqual(len(names), 10, "the scan stopped finding checks")
        english = _raw("en")
        self.assertEqual(sorted(f"check.{name}" for name in names if f"check.{name}" not in english), [])

    def test_a_plural_entry_has_every_form_its_rule_can_ask_for(self) -> None:
        for language in i18n.available_languages():
            rule = i18n.PLURAL_RULES.get(language, lambda n: "other")
            needed = {rule(n) for n in range(0, 1000)}
            for key, value in _raw(language).items():
                if isinstance(value, dict):
                    with self.subTest(language=language, key=key):
                        self.assertEqual(sorted(needed - set(value)), [])

    def test_a_key_is_plural_in_every_language_or_in_none(self) -> None:
        english = _raw("en")
        for language in i18n.available_languages():
            for key, value in _raw(language).items():
                with self.subTest(language=language, key=key):
                    self.assertEqual(isinstance(value, dict), isinstance(english[key], dict))

    def test_a_value_is_a_string_or_a_set_of_plural_forms(self) -> None:
        for language in i18n.available_languages():
            for key, value in _raw(language).items():
                with self.subTest(language=language, key=key):
                    if isinstance(value, dict):
                        self.assertTrue(all(isinstance(form, str) for form in value.values()))
                    else:
                        self.assertIsInstance(value, str)

    def test_translations_keep_the_placeholders_english_uses(self) -> None:
        """A dropped ``{path}`` silently loses the value; a new one raises."""
        english = _raw("en")

        def fields(value) -> set[str]:
            texts = value.values() if isinstance(value, dict) else [value]
            return {name for text in texts for name in re.findall(r"\{(\w+)\}", text)}

        for language in i18n.available_languages():
            for key, value in _raw(language).items():
                with self.subTest(language=language, key=key):
                    self.assertEqual(fields(value), fields(english[key]))

    def test_every_template_formats(self) -> None:
        """A stray brace in a translation is a crash the first time it is drawn."""
        for language in i18n.available_languages():
            catalog = i18n.load(language)
            for key, value in _raw(language).items():
                with self.subTest(language=language, key=key):
                    texts = value.values() if isinstance(value, dict) else [value]
                    for text in texts:
                        names = set(re.findall(r"\{(\w+)\}", text))
                        text.format(**{name: "x" for name in names})
                    if isinstance(value, dict):
                        catalog.plural(key, 3, **{name: "x" for name in names - {"count"}})


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.locale = Path(self._dir.name)
        (self.locale / "en.json").write_text(json.dumps({
            "hello": "Hello, {name}",
            "only.english": "English only",
            "files": {"one": "{count} file", "other": "{count} files"},
        }), encoding="utf-8")
        (self.locale / "ru.json").write_text(json.dumps({
            "hello": "Привет, {name}",
            "files": {"one": "{count} файл", "few": "{count} файла", "many": "{count} файлов"},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_a_key_is_read_in_the_chosen_language(self) -> None:
        self.assertEqual(i18n.load("ru", self.locale).text("hello", name="Мир"), "Привет, Мир")

    def test_a_missing_key_falls_back_to_english(self) -> None:
        self.assertEqual(i18n.load("ru", self.locale).text("only.english"), "English only")

    def test_a_key_missing_everywhere_comes_back_as_itself(self) -> None:
        self.assertEqual(i18n.load("ru", self.locale).text("nowhere"), "nowhere")
        self.assertEqual(i18n.load("ru", self.locale).plural("nowhere", 3), "nowhere")

    def test_an_unknown_language_is_english(self) -> None:
        catalog = i18n.load("xx", self.locale)
        self.assertEqual(catalog.language, "en")
        self.assertEqual(catalog.text("hello", name="you"), "Hello, you")

    def test_russian_plural_forms(self) -> None:
        catalog = i18n.load("ru", self.locale)
        expected = {
            0: "0 файлов", 1: "1 файл", 2: "2 файла", 4: "4 файла", 5: "5 файлов",
            11: "11 файлов", 12: "12 файлов", 14: "14 файлов", 21: "21 файл",
            22: "22 файла", 25: "25 файлов", 101: "101 файл", 111: "111 файлов",
        }
        for count, text in expected.items():
            with self.subTest(count=count):
                self.assertEqual(catalog.plural("files", count), text)

    def test_english_plural_forms(self) -> None:
        catalog = i18n.load("en", self.locale)
        self.assertEqual(catalog.plural("files", 1), "1 file")
        self.assertEqual(catalog.plural("files", 0), "0 files")
        self.assertEqual(catalog.plural("files", 2), "2 files")

    def test_a_language_without_a_rule_uses_other(self) -> None:
        (self.locale / "ja.json").write_text(json.dumps({
            "files": {"other": "{count} ファイル"},
        }), encoding="utf-8")
        self.assertEqual(i18n.load("ja", self.locale).plural("files", 1), "1 ファイル")

    def test_a_system_tag_picks_the_language_by_its_primary_subtag(self) -> None:
        self.assertEqual(i18n.pick_language(["ru-RU", "en-US"], self.locale), "ru")
        self.assertEqual(i18n.pick_language(["de_DE", "ru_RU"], self.locale), "ru")
        self.assertEqual(i18n.pick_language(["zh-Hans-CN"], self.locale), "en")
        self.assertEqual(i18n.pick_language([], self.locale), "en")


if __name__ == "__main__":
    unittest.main()
