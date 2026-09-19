"""The comment-preserving config editor and the GUI's save path.

Two properties are checked everywhere. Byte preservation: an edit changes the
bytes of one value and nothing else, because a user's hand-written comments
are the whole reason not to rewrite the file from a dictionary. And meaning:
`tomllib.loads(result)` equals the original document with exactly that change
applied, on both shipped templates and on a Russian-commented CRLF file.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tomllib
import unittest
import uuid

from codexsync.config import load_config
from codexsync.config_edit import (
    ConfigHistoryEntry,
    config_diff,
    config_history_dir,
    create_config,
    list_config_history,
    read_config_document,
    remove_key,
    render_toml_value,
    replace_array_of_tables,
    save_config_text,
    set_value,
    validate_config_text,
)
from codexsync.exceptions import ConfigError, ConflictError, FailSafeError


REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"
TEMPLATES = {
    "root": REPO_ROOT / "config.example.toml",
    "packaged": REPO_ROOT / "src" / "codexsync" / "config.example.toml",
}
NOW = datetime(2026, 9, 13, 10, 11, 12, 123456, tzinfo=timezone.utc)

RUSSIAN_CRLF = "\r\n".join([
    "# Конфигурация codexSync для ноутбука",
    "[identity]",
    'machine_id = "laptop" # имя машины, не менять',
    "",
    "[sync]",
    'mode = "cold"  # только холодная синхронизация',
    'compare = "mtime"',
    "",
    "[paths]",
    "# Папки задаются относительно этого файла",
    'workspace_root_dir = "workspace"',
    'local_state_dir = "codex-state" # папка .codex',
    'cloud_root_dir = "${workspace_root}/sync"',
    'backup_dir = "${workspace_root}/backups"',
    'temp_dir = "${workspace_root}/.tmp"',
    "",
    "[targets]",
    "include_roots = [",
    '  "sessions", # сессии',
    "  # навыки",
    '  "skills",',
    "]",
    "",
    "# Правила путей между машинами",
    "[[path_mappings]]",
    'rule_id = "дом"',
    'source_machine = "desktop"',
    'target_machine = "laptop"',
    'from = "D:/Проекты"',
    'to = "C:/Проекты"',
    "",
    "# Резервные копии",
    "[backup]",
    "retention_days = 30",
    "",
])


def _with(document: dict, dotted: str, key: str, value: object) -> dict:
    expected = copy.deepcopy(document)
    node = expected
    for part in dotted.split("."):
        node = node.setdefault(part, {})
    node[key] = value
    return expected


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    old, new = before.split("\n"), after.split("\n")
    assert len(old) == len(new), "line count changed"
    return [(a, b) for a, b in zip(old, new) if a != b]


class RenderTomlValueTests(unittest.TestCase):
    def test_values_round_trip_through_tomllib(self) -> None:
        values = [
            "plain", "", 'quote " and \\ backslash', "tab\tnew\nline\r\x00\x1f\x7f",
            "Документы/проект # не комментарий [нет]", True, False, 0, -7, 2**63 - 1,
            1.5, -0.25, 1e-7, 3.0, float("inf"), ["a", "b"], [], [1, 2], [[1], ["x"]],
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(tomllib.loads(f"v = {render_toml_value(value)}")["v"], value)

    def test_exact_renderings(self) -> None:
        self.assertEqual(render_toml_value(True), "true")
        self.assertEqual(render_toml_value(10), "10")
        self.assertEqual(render_toml_value(["a", "b"]), '["a", "b"]')
        self.assertEqual(render_toml_value([]), "[]")
        self.assertEqual(render_toml_value('C:\\x "y"'), '"C:\\\\x \\"y\\""')
        self.assertEqual(render_toml_value("Дом"), '"Дом"')
        self.assertEqual(render_toml_value("\x01"), '"\\u0001"')

    def test_unsupported_types_are_refused(self) -> None:
        for value in (None, {"a": 1}, ("a",), Path("x"), b"x", ["ok", None]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    render_toml_value(value)


class SetValueTests(unittest.TestCase):
    def test_replaces_only_the_value_keeping_indent_and_comment(self) -> None:
        text = '[sync]\n  compare = "mtime"   # how files are compared\nmode = "cold"\n'
        result = set_value(text, "sync", "compare", "mtime_hash_fallback")
        self.assertEqual(
            result,
            '[sync]\n  compare = "mtime_hash_fallback"   # how files are compared\nmode = "cold"\n',
        )

    def test_hash_and_bracket_inside_strings_are_not_structure(self) -> None:
        text = (
            "[a]\n"
            'x = "a # not a comment [not a header]" # real comment\n'
            "y = 'C:\\path # literal [x]'\n"
            "z = 1\n"
        )
        result = set_value(text, "a", "x", "new")
        self.assertEqual(_changed_lines(text, result), [
            ('x = "a # not a comment [not a header]" # real comment', 'x = "new" # real comment'),
        ])
        result = set_value(text, "a", "z", 2)
        self.assertEqual(_changed_lines(text, result), [("z = 1", "z = 2")])
        result = set_value(text, "a", "y", "D:\\")
        self.assertEqual(tomllib.loads(result)["a"]["y"], "D:\\")

    def test_header_with_spaces_and_comment_is_found(self) -> None:
        text = "[ sync ] # the sync section\ncompare = \"mtime\"\n\n[ process_detection . background_process_names ]\nlinux = []\n"
        result = set_value(text, "sync", "compare", "mtime_hash_fallback")
        self.assertEqual(_changed_lines(text, result), [
            ('compare = "mtime"', 'compare = "mtime_hash_fallback"'),
        ])
        result = set_value(text, "process_detection.background_process_names", "linux", ["x"])
        self.assertEqual(_changed_lines(text, result), [("linux = []", 'linux = ["x"]')])

    def test_multiline_array_becomes_one_line(self) -> None:
        text = '[targets]\ninclude_roots = [\n  "sessions", # comment\n  # another\n  "skills",\n]  # after\n\n[x]\ny = 1\n'
        result = set_value(text, "targets", "include_roots", ["a"])
        self.assertEqual(result, '[targets]\ninclude_roots = ["a"]  # after\n\n[x]\ny = 1\n')

    def test_multiline_string_value_is_refused(self) -> None:
        text = '[a]\nnote = """\n[sync]\ncompare = 1\n"""\nother = 1\n'
        with self.assertRaises(ValueError):
            set_value(text, "a", "note", "x")
        # The header-looking lines inside the string are not a section.
        result = set_value(text, "a", "other", 2)
        self.assertEqual(_changed_lines(text, result), [("other = 1", "other = 2")])
        result = set_value(text, "sync", "compare", "mtime")
        self.assertEqual(tomllib.loads(result)["sync"], {"compare": "mtime"})
        self.assertEqual(tomllib.loads(result)["a"]["note"], "[sync]\ncompare = 1\n")

    def test_closing_quotes_followed_by_extra_quotes(self) -> None:
        text = "[a]\nq = '''it''''\nlit = 'x'\nb = \"\"\"say \"\"hi\"\"\"\"\"\nc = 1\n"
        self.assertEqual(tomllib.loads(text)["a"]["q"], "it'")
        result = set_value(text, "a", "c", 5)
        self.assertEqual(_changed_lines(text, result), [("c = 1", "c = 5")])

    def test_missing_key_goes_after_the_last_key_of_its_section(self) -> None:
        text = "[a]\nx = 1\n\n# about b\n[b]\ny = 2\n"
        result = set_value(text, "a", "z", 3)
        self.assertEqual(result, "[a]\nx = 1\nz = 3\n\n# about b\n[b]\ny = 2\n")

    def test_missing_key_in_an_empty_section_follows_the_header(self) -> None:
        text = "[a] # empty\n\n[b]\ny = 2\n"
        self.assertEqual(set_value(text, "a", "z", "v"), '[a] # empty\nz = "v"\n\n[b]\ny = 2\n')

    def test_missing_section_is_appended(self) -> None:
        text = "[a]\nx = 1\n"
        self.assertEqual(set_value(text, "new.sub", "k", True), "[a]\nx = 1\n\n[new.sub]\nk = true\n")
        self.assertEqual(set_value("[a]\nx = 1", "b", "k", 1), "[a]\nx = 1\n\n[b]\nk = 1\n")
        self.assertEqual(set_value("", "b", "k", 1), "[b]\nk = 1\n")

    def test_file_without_trailing_newline(self) -> None:
        self.assertEqual(set_value("[a]\nx = 1", "a", "y", 2), "[a]\nx = 1\ny = 2\n")
        self.assertEqual(set_value("[a]\nx = 1", "a", "x", 2), "[a]\nx = 2")

    def test_crlf_is_preserved(self) -> None:
        text = "[a]\r\nx = 1\r\n\r\n[b]\r\ny = [\r\n  1,\r\n]\r\n"
        for result in (
            set_value(text, "a", "z", 3),
            set_value(text, "b", "y", [2]),
            set_value(text, "c", "k", "v"),
        ):
            self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertEqual(set_value(text, "a", "z", 3), "[a]\r\nx = 1\r\nz = 3\r\n\r\n[b]\r\ny = [\r\n  1,\r\n]\r\n")

    def test_a_key_toml_defines_elsewhere_is_refused(self) -> None:
        text = "[process_detection]\nbackground_process_names.windows = []\n"
        with self.assertRaises(ValueError):
            set_value(text, "process_detection.background_process_names", "linux", [])
        text = "[process_detection]\n\n[process_detection.background_process_names]\nlinux = []\n"
        with self.assertRaises(ValueError):
            set_value(text, "process_detection", "background_process_names", [])

    def test_invalid_input_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            set_value("[a\nx = 1\n", "a", "x", 2)

    def test_templates_round_trip_every_existing_key(self) -> None:
        replacements = {str: "changed \"value\" Ä", bool: None, int: 12345, float: 0.75, list: ["one", "two"]}
        for label, path in TEMPLATES.items():
            original = path.read_bytes().decode("utf-8")
            document = tomllib.loads(original)
            for section, table in _tables(document):
                for key, current in table.items():
                    if isinstance(current, dict) or (isinstance(current, list) and current and isinstance(current[0], dict)):
                        continue
                    value = (not current) if isinstance(current, bool) else replacements[type(current)]
                    with self.subTest(template=label, section=section, key=key):
                        result = set_value(original, section, key, value)
                        self.assertEqual(tomllib.loads(result), _with(document, section, key, value))
                        if not isinstance(current, list):
                            self.assertEqual(len(_changed_lines(original, result)), 1)

    def test_templates_accept_new_keys_and_sections(self) -> None:
        for label, path in TEMPLATES.items():
            original = path.read_bytes().decode("utf-8")
            document = tomllib.loads(original)
            for section in ("sync", "scheduler", "assumptions", "process_detection.background_process_names", "gui"):
                with self.subTest(template=label, section=section):
                    result = set_value(original, section, "added_key", "Проверка")
                    self.assertEqual(tomllib.loads(result), _with(document, section, "added_key", "Проверка"))
                    self.assertTrue(result.startswith(original[: original.index("[")]))
                    self.assertEqual(remove_key(result, section, "added_key").replace("\r\n", "\n"),
                                     original.replace("\r\n", "\n") if section != "gui" else
                                     original.replace("\r\n", "\n") + "\n[gui]\n")

    def test_russian_crlf_sample(self) -> None:
        document = tomllib.loads(RUSSIAN_CRLF)
        result = set_value(RUSSIAN_CRLF, "identity", "machine_id", "ноутбук-2")
        self.assertIn('machine_id = "ноутбук-2" # имя машины, не менять\r\n', result)
        self.assertEqual(tomllib.loads(result), _with(document, "identity", "machine_id", "ноутбук-2"))
        self.assertEqual(len(_changed_lines(RUSSIAN_CRLF, result)), 1)

        result = set_value(RUSSIAN_CRLF, "targets", "include_roots", ["сессии"])
        self.assertIn('include_roots = ["сессии"]\r\n\r\n# Правила путей между машинами\r\n', result)
        self.assertEqual(tomllib.loads(result), _with(document, "targets", "include_roots", ["сессии"]))

        result = set_value(RUSSIAN_CRLF, "paths", "backup_dir", "резерв")
        self.assertEqual(tomllib.loads(result), _with(document, "paths", "backup_dir", "резерв"))
        self.assertNotIn("\n", result.replace("\r\n", ""))


def _tables(document: dict, prefix: str = ""):
    for key, value in document.items():
        if isinstance(value, dict):
            name = f"{prefix}{key}"
            yield name, value
            yield from _tables(value, f"{name}.")


class RemoveKeyTests(unittest.TestCase):
    def test_removes_the_line_only(self) -> None:
        text = "[a]\n# keep me\nx = 1 # gone\ny = 2\n"
        self.assertEqual(remove_key(text, "a", "x"), "[a]\n# keep me\ny = 2\n")

    def test_removes_a_multiline_array(self) -> None:
        text = '[t]\nroots = [\n  "a", # c\n  "b",\n]\nkeep = 1\n'
        self.assertEqual(remove_key(text, "t", "roots"), "[t]\nkeep = 1\n")

    def test_absent_key_is_a_noop(self) -> None:
        text = "[a]\nx = 1\n"
        self.assertIs(remove_key(text, "a", "missing"), text)
        self.assertIs(remove_key(text, "nope", "x"), text)

    def test_key_defined_another_way_is_refused(self) -> None:
        text = "[a]\nb.c = 1\n"
        with self.assertRaises(ValueError):
            remove_key(text, "a.b", "c")

    def test_templates_round_trip(self) -> None:
        for label, path in TEMPLATES.items():
            original = path.read_bytes().decode("utf-8")
            document = tomllib.loads(original)
            for section, key in (("scheduler", "jitter_seconds"), ("filters", "exclude_globs"), ("identity", "machine_id")):
                with self.subTest(template=label, key=key):
                    result = remove_key(original, section, key)
                    expected = copy.deepcopy(document)
                    del expected[section][key]
                    self.assertEqual(tomllib.loads(result), expected)


class ReplaceArrayOfTablesTests(unittest.TestCase):
    ENTRY_A = {"rule_id": "a", "source_machine": "m1", "target_machine": "m2", "from": "D:/x", "to": "C:/x"}
    ENTRY_B = {"rule_id": "b", "source_machine": "m1", "target_machine": "m2", "from": "D:/y", "to": "C:/y", "case_sensitive": False}

    def _check(self, original: str, entries: list[dict]) -> str:
        result = replace_array_of_tables(original, "path_mappings", entries)
        expected = tomllib.loads(original)
        if entries:
            expected["path_mappings"] = entries
        else:
            expected.pop("path_mappings", None)
        self.assertEqual(tomllib.loads(result), expected)
        return result

    def test_no_existing_block_appends_at_the_end(self) -> None:
        text = "[a]\nx = 1\n"
        result = self._check(text, [self.ENTRY_A])
        self.assertEqual(result, text + "\n[[path_mappings]]\n" + "\n".join(
            f"{k} = {render_toml_value(v)}" for k, v in self.ENTRY_A.items()) + "\n")

    def test_no_block_and_no_entries_is_unchanged(self) -> None:
        text = "[a]\nx = 1\n"
        self.assertEqual(replace_array_of_tables(text, "path_mappings", []), text)

    def test_one_block_is_replaced_in_place(self) -> None:
        text = (
            "[a]\nx = 1\n\n"
            "# Mappings between machines\n"
            '[[path_mappings]]\nrule_id = "old"\n# inside\nfrom = "p"\n\n'
            "# About backup\n[backup]\nretention_days = 3\n"
        )
        result = self._check(text, [self.ENTRY_A, self.ENTRY_B])
        self.assertTrue(result.startswith("[a]\nx = 1\n\n# Mappings between machines\n[[path_mappings]]\nrule_id = \"a\"\n"))
        self.assertIn('case_sensitive = false\n\n# About backup\n[backup]\nretention_days = 3\n', result)
        self.assertIn('to = "C:/x"\n\n[[path_mappings]]\nrule_id = "b"\n', result)
        self.assertNotIn("# inside", result)

    def test_two_adjacent_blocks_are_replaced_by_one(self) -> None:
        text = (
            "[a]\nx = 1\n\n"
            '[[path_mappings]]\nrule_id = "one"\n\n'
            '[[path_mappings]]\nrule_id = "two"\n\n'
            "[backup]\nretention_days = 3\n"
        )
        result = self._check(text, [self.ENTRY_A])
        self.assertEqual(result, "[a]\nx = 1\n\n" + "\n".join(
            ["[[path_mappings]]"] + [f"{k} = {render_toml_value(v)}" for k, v in self.ENTRY_A.items()]
        ) + "\n\n[backup]\nretention_days = 3\n")

    def test_two_separated_blocks_write_at_the_first(self) -> None:
        text = (
            '[[path_mappings]]\nrule_id = "one"\n\n'
            "[a]\nx = 1\n\n"
            '[[path_mappings]]\nrule_id = "two"\n\n'
            "[backup]\nretention_days = 3\n"
        )
        result = self._check(text, [self.ENTRY_B])
        self.assertTrue(result.startswith('[[path_mappings]]\nrule_id = "b"\n'))
        self.assertTrue(result.endswith("\n[a]\nx = 1\n\n\n[backup]\nretention_days = 3\n"))

    def test_removing_all_blocks(self) -> None:
        text = (
            "[a]\nx = 1\n\n"
            '[[path_mappings]]\nrule_id = "one"\n\n'
            '[[path_mappings]]\nrule_id = "two"\n\n'
            "[backup]\nretention_days = 3\n"
        )
        self.assertEqual(self._check(text, []), "[a]\nx = 1\n\n[backup]\nretention_days = 3\n")
        at_end = "[a]\nx = 1\n\n" + '[[path_mappings]]\nrule_id = "one"\n'
        self.assertEqual(self._check(at_end, []), "[a]\nx = 1\n")

    def test_sub_tables_of_an_element_go_with_it(self) -> None:
        text = '[[path_mappings]]\nrule_id = "one"\n[path_mappings.extra]\nk = 1\n\n[b]\ny = 1\n'
        self.assertEqual(self._check(text, []), "[b]\ny = 1\n")

    def test_template_add_then_remove_is_byte_identical(self) -> None:
        for label, path in TEMPLATES.items():
            original = path.read_bytes().decode("utf-8")
            with self.subTest(template=label):
                added = self._check(original, [self.ENTRY_A, self.ENTRY_B])
                self.assertTrue(added.startswith(original))
                self.assertEqual(self._check(added, []), original)

    def test_russian_crlf_sample(self) -> None:
        result = self._check(RUSSIAN_CRLF, [self.ENTRY_A, self.ENTRY_B])
        self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertIn("# Правила путей между машинами\r\n[[path_mappings]]\r\nrule_id = \"a\"\r\n", result)
        self.assertIn("case_sensitive = false\r\n\r\n# Резервные копии\r\n[backup]\r\n", result)
        removed = self._check(RUSSIAN_CRLF, [])
        self.assertIn("# Правила путей между машинами\r\n\r\n# Резервные копии\r\n", removed)
        self.assertEqual(len(removed.split("\r\n")), len(RUSSIAN_CRLF.split("\r\n")) - 6)

    def test_non_dict_entries_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            replace_array_of_tables("", "path_mappings", [["a"]])  # type: ignore[list-item]


def _valid_config(*, compare: str = "mtime", extra: str = "") -> str:
    return (
        "# Hand-written comment that must survive\n"
        "[identity]\n"
        'machine_id = "machine-a" # this machine\n'
        "\n"
        "[sync]\n"
        f'compare = "{compare}"\n'
        "\n"
        "[paths]\n"
        'workspace_root_dir = "workspace"\n'
        'local_state_dir = "codex-state"\n'
        'cloud_root_dir = "${workspace_root}/sync"\n'
        'backup_dir = "${workspace_root}/backups"\n'
        'temp_dir = "${workspace_root}/.tmp"\n'
        + extra
    )


class SaveConfigTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"config-edit-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.path = self.root / "config.toml"
        self.history = (self.root / "workspace" / "config-history").resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, text: str) -> bytes:
        data = text.encode("utf-8")
        self.path.write_bytes(data)
        return data

    def test_save_replaces_the_file_and_keeps_the_old_bytes(self) -> None:
        old = self._write(_valid_config().replace("\n", "\r\n"))
        document = read_config_document(self.path)
        edited = set_value(document.text, "sync", "compare", "mtime_hash_fallback")

        saved = save_config_text(self.path, edited, expected_sha256=document.sha256, now=NOW)

        self.assertEqual(self.path.read_bytes(), edited.encode("utf-8"))
        self.assertIn(b"# Hand-written comment that must survive\r\n", self.path.read_bytes())
        self.assertEqual(load_config(self.path).sync.compare, "mtime_hash_fallback")
        self.assertEqual(saved.history_entry, self.history / "config-20260913T101112123456Z.toml")
        self.assertEqual(saved.history_entry.read_bytes(), old)
        self.assertNotEqual(saved.sha256, document.sha256)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["config.toml", "workspace"])
        self.assertEqual(sorted(p.name for p in (self.root / "workspace").iterdir()), ["config-history"])

        history = list_config_history(load_config(self.path), self.path)
        self.assertEqual(history, [ConfigHistoryEntry(
            "config-20260913T101112123456Z.toml", saved.history_entry, len(old), NOW,
        )])

    def test_history_is_listed_newest_first(self) -> None:
        self._write(_valid_config())
        sha = read_config_document(self.path).sha256
        for number, compare in enumerate(("mtime_hash_fallback", "mtime", "mtime_hash_fallback")):
            sha = save_config_text(
                self.path, _valid_config(compare=compare), expected_sha256=sha,
                now=NOW.replace(second=number),
            ).sha256
        names = [entry.name for entry in list_config_history(load_config(self.path), self.path)]
        self.assertEqual(names, [
            "config-20260913T101102123456Z.toml",
            "config-20260913T101101123456Z.toml",
            "config-20260913T101100123456Z.toml",
        ])

    def test_stale_sha_is_refused_and_disk_is_untouched(self) -> None:
        self._write(_valid_config())
        document = read_config_document(self.path)
        elsewhere = self._write(_valid_config() + "# edited elsewhere\n")
        with self.assertRaisesRegex(ConflictError, "changed on disk since it was opened; reload it"):
            save_config_text(
                self.path, set_value(document.text, "sync", "compare", "mtime_hash_fallback"),
                expected_sha256=document.sha256, now=NOW,
            )
        self.assertEqual(self.path.read_bytes(), elsewhere)
        self.assertFalse((self.root / "workspace").exists())

    def test_existing_file_without_a_base_is_refused(self) -> None:
        old = self._write(_valid_config())
        with self.assertRaises(ConflictError):
            save_config_text(self.path, _valid_config(compare="mtime_hash_fallback"), expected_sha256=None)
        self.assertEqual(self.path.read_bytes(), old)

    def test_removed_file_with_a_base_is_refused(self) -> None:
        with self.assertRaises(ConflictError):
            save_config_text(self.path, _valid_config(), expected_sha256="0" * 64)
        self.assertFalse(self.path.exists())

    def test_invalid_config_is_refused_with_nothing_written(self) -> None:
        old = self._write(_valid_config())
        sha = read_config_document(self.path).sha256
        for bad in (
            _valid_config(compare="bogus"),
            _valid_config() + "[sync\n",
            _valid_config(extra="\n[backup]\nbackup_before_overwrite = false\n"),
            _valid_config(extra="\n[scheduler]\nmode = \"sync\"\n"),
        ):
            with self.subTest(bad=bad[-40:]):
                with self.assertRaises(ConfigError):
                    save_config_text(self.path, bad, expected_sha256=sha, now=NOW)
                self.assertEqual(self.path.read_bytes(), old)
                self.assertFalse((self.root / "workspace").exists())
        self.assertEqual([p.name for p in self.root.iterdir()], ["config.toml"])

    def test_old_file_that_does_not_validate_is_not_kept(self) -> None:
        self._write(_valid_config(compare="bogus"))
        sha = read_config_document(self.path).sha256
        saved = save_config_text(self.path, _valid_config(), expected_sha256=sha, now=NOW)
        self.assertIsNone(saved.history_entry)
        self.assertFalse(self.history.exists())
        self.assertEqual(load_config(self.path).sync.compare, "mtime")

    def test_history_entry_is_never_overwritten(self) -> None:
        self._write(_valid_config())
        first = save_config_text(
            self.path, _valid_config(compare="mtime_hash_fallback"),
            expected_sha256=read_config_document(self.path).sha256, now=NOW,
        )
        kept = first.history_entry.read_bytes()
        with self.assertRaises(FailSafeError):
            save_config_text(self.path, _valid_config(), expected_sha256=first.sha256, now=NOW)
        self.assertEqual(first.history_entry.read_bytes(), kept)
        self.assertEqual(load_config(self.path).sync.compare, "mtime_hash_fallback")

    def test_creating_without_a_base_writes_no_history(self) -> None:
        saved = save_config_text(self.path, _valid_config(), expected_sha256=None, now=NOW)
        self.assertIsNone(saved.history_entry)
        self.assertEqual(self.path.read_text(encoding="utf-8"), _valid_config())

    def test_byte_order_mark_is_read_and_not_written(self) -> None:
        self.path.write_bytes(b"\xef\xbb\xbf" + _valid_config().encode("utf-8"))
        document = read_config_document(self.path)
        self.assertFalse(document.text.startswith("\ufeff"))
        saved = save_config_text(self.path, document.text, expected_sha256=document.sha256, now=NOW)
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(saved.history_entry.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_config_inside_the_state_directory_is_refused(self) -> None:
        state = self.root / "codex-state"
        state.mkdir()
        inner = state / "config.toml"
        text = _valid_config().replace('"workspace"', '"../workspace"').replace('"codex-state"', '"."')
        with self.assertRaisesRegex(ConfigError, "outside paths.local_state_dir"):
            save_config_text(inner, text, expected_sha256=None)
        self.assertEqual(list(state.iterdir()), [])

    def test_read_config_document(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Config file not found"):
            read_config_document(self.path)
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaises(ConfigError):
            read_config_document(self.path)

    def test_validate_config_text_resolves_against_the_path(self) -> None:
        cfg = validate_config_text(_valid_config(), path=self.path)
        self.assertEqual(cfg.paths.workspace_root_dir, (self.root / "workspace").resolve())


class HistoryDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"config-history-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.path = self.root / "config.toml"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_workspace_root_or_config_directory(self) -> None:
        cfg = validate_config_text(_valid_config(), path=self.path)
        self.assertEqual(config_history_dir(cfg, self.path), (self.root / "workspace" / "config-history").resolve())
        cfg.paths.workspace_root_dir = None
        self.assertEqual(config_history_dir(cfg, self.path), (self.root / "config-history").resolve())

    def test_protected_directories_are_refused(self) -> None:
        for field_name in ("backup_dir", "temp_dir", "local_state_dir"):
            with self.subTest(field=field_name):
                cfg = validate_config_text(_valid_config(), path=self.path)
                setattr(cfg.paths, field_name, self.root / "workspace")
                with self.assertRaisesRegex(ConfigError, f"paths.{field_name}"):
                    config_history_dir(cfg, self.path)

    def test_listing_an_absent_history_creates_nothing(self) -> None:
        cfg = validate_config_text(_valid_config(), path=self.path)
        self.assertEqual(list_config_history(cfg, self.path), [])
        self.assertEqual(list(self.root.iterdir()), [])


class ConfigDiffTests(unittest.TestCase):
    def test_equal_is_empty(self) -> None:
        self.assertEqual(config_diff("a\n", "a\n", path_label="config.toml"), "")

    def test_unified_diff(self) -> None:
        old = _valid_config()
        new = set_value(old, "sync", "compare", "mtime_hash_fallback")
        diff = config_diff(old, new, path_label="config.toml")
        self.assertIn("--- config.toml", diff)
        self.assertIn("+++ config.toml", diff)
        self.assertIn('\n-compare = "mtime"\n', diff)
        self.assertIn('\n+compare = "mtime_hash_fallback"\n', diff)
        self.assertTrue(diff.endswith("\n"))

    def test_missing_final_newline_does_not_glue_lines(self) -> None:
        diff = config_diff("a = 1", "a = 2", path_label="c")
        self.assertIn("\n-a = 1\n+a = 2\n", diff)


class CreateConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"config-create-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.state = self.root / "codex-state"
        self.workspace = self.root / "workspace"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _create(self, path: Path, **overrides: object):
        arguments = {
            "machine_id": "laptop-1",
            "local_state_dir": self.state.as_posix(),
            "workspace_root_dir": self.workspace.as_posix(),
        }
        arguments.update(overrides)
        return create_config(path, **arguments)  # type: ignore[arg-type]

    def test_creates_a_valid_config_that_keeps_every_template_comment(self) -> None:
        path = self.root / "settings" / "config.toml"
        saved = self._create(path)

        self.assertIsNone(saved.history_entry)
        cfg = load_config(path)
        self.assertEqual(cfg.identity.machine_id, "laptop-1")
        self.assertEqual(cfg.paths.local_state_dir, self.state.resolve())
        self.assertEqual(cfg.paths.workspace_root_dir, self.workspace.resolve())
        self.assertEqual(cfg.paths.cloud_root_dir, (self.workspace / "sync").resolve())

        template = TEMPLATES["packaged"].read_bytes().decode("utf-8")
        written = path.read_bytes().decode("utf-8")
        comments = lambda text: [line.strip() for line in text.splitlines() if line.strip().startswith("#")]
        self.assertEqual(comments(written), comments(template))
        self.assertEqual(len(_changed_lines(template, written)), 3)

        # Only the config's own parent was created; nothing in .codex or the workspace.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["settings"])
        self.assertEqual([p.name for p in path.parent.iterdir()], ["config.toml"])
        self.assertFalse(self.state.exists())

    def test_cloud_root_is_set_when_given(self) -> None:
        path = self.root / "config.toml"
        cloud = self.root / "Облако" / "sync"
        self._create(path, cloud_root_dir=cloud.as_posix())
        self.assertEqual(load_config(path).paths.cloud_root_dir, cloud.resolve())

    def test_existing_state_directory_is_left_untouched(self) -> None:
        self.state.mkdir()
        (self.state / "marker").write_bytes(b"x")
        self._create(self.root / "config.toml")
        self.assertEqual([p.name for p in self.state.iterdir()], ["marker"])

    def test_existing_file_is_refused(self) -> None:
        path = self.root / "config.toml"
        path.write_bytes(b"keep")
        with self.assertRaisesRegex(ConfigError, "already exists"):
            self._create(path)
        self.assertEqual(path.read_bytes(), b"keep")

    def test_bad_machine_id_is_refused_and_nothing_is_created(self) -> None:
        path = self.root / "new" / "config.toml"
        for machine_id in ("", "   ", "..."):
            with self.subTest(machine_id=machine_id):
                with self.assertRaisesRegex(ConfigError, "machine_id"):
                    self._create(path, machine_id=machine_id)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_config_inside_the_state_directory_is_refused(self) -> None:
        path = self.state / "config.toml"
        with self.assertRaisesRegex(ConfigError, "outside paths.local_state_dir"):
            self._create(path)
        self.assertFalse(self.state.exists())

    def test_missing_grandparent_is_not_created(self) -> None:
        path = self.root / "a" / "b" / "config.toml"
        with self.assertRaises(ConfigError):
            self._create(path)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
