"""Edit `config.toml` text in place, and save it the way a GUI must.

A user's config carries comments written by hand -- one real file is
commented in Russian -- and a settings window that rewrote the file from a
parsed dictionary would throw all of them away on the first save. The project
takes no runtime dependencies, so there is no round-tripping TOML library to
lean on either. What is here instead is a deliberately small, line-based
editor for the subset `config.toml` uses: find a statement, replace the bytes
of its value, leave every other byte where it was.

Being small is what makes it trustworthy, and one rule makes that safe: every
edit is checked against `tomllib` before it is returned. The original text is
parsed, the intended change is applied to that dictionary, and the edited text
must parse to exactly the result. An edit the scanner got wrong -- a key that
TOML also defines some other way, a header this editor does not understand --
raises `ValueError` rather than producing a file that means something else.

The second half is the save path: validate with the same loader the CLI uses,
refuse to overwrite a file that changed since it was opened, keep a verified
copy of what is being replaced, then write through a temporary file and
`os.replace`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any

from .config import _paths_overlap, decode_config_bytes, parse_config_text
from .exceptions import ConfigError, ConflictError, FailSafeError
from .guardian_models import require_guardian_machine_id
from .models import AppConfig
from .runtime import _require_mutation_compatible_config

TEMPLATE_PATH = Path(__file__).with_name("config.example.toml")
CONFIG_HISTORY_DIR_NAME = "config-history"

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_BARE_KEY_FULL = re.compile(r"[A-Za-z0-9_-]+\Z")
_BARE_VALUE = re.compile(r"[^\s,\]\}#]+")
_LOCAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
# TOML lets a space stand in for the `T` of a datetime: `1979-05-27 07:32:00`.
_SPACE_BEFORE_TIME = re.compile(r" \d{2}:\d{2}")
_HISTORY_NAME = re.compile(r"config-(\d{8}T\d{12})Z\.toml\Z")
_TRIVIA_KINDS = ("blank", "comment")
_BODY_KINDS = ("blank", "comment", "key")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

_STRING_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


# --------------------------------------------------------------------------
# Rendering values
# --------------------------------------------------------------------------


def render_toml_value(value: Any) -> str:
    """Render one value as TOML: str, bool, int, float, or a list of those.

    A list is always rendered on one line. Anything else -- a dict, a path, a
    tuple -- is a `TypeError`: the caller decides what the file should say,
    and a silent `str()` would decide it for them.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError(f"integer out of TOML's 64-bit range: {value}")
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _render_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(render_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot render {type(value).__name__} as a TOML value")


def _render_string(value: str) -> str:
    rendered: list[str] = ['"']
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("string contains a lone surrogate, which UTF-8 cannot encode")
        if char in _STRING_ESCAPES:
            rendered.append(_STRING_ESCAPES[char])
        elif code < 0x20 or code == 0x7F:
            rendered.append(f"\\u{code:04X}")
        else:
            # Non-ASCII stays as typed: the file is UTF-8 and a user reading
            # `"Документы"` should not find `"Д..."` there instead.
            rendered.append(char)
    rendered.append('"')
    return "".join(rendered)


def _render_key(part: str) -> str:
    return part if _BARE_KEY_FULL.match(part) else _render_string(part)


def _render_name(parts: tuple[str, ...]) -> str:
    return ".".join(_render_key(part) for part in parts)


# --------------------------------------------------------------------------
# Scanning statements
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Statement:
    """One logical line of TOML, by absolute offsets into the text.

    `end` is just past the line break that closes the statement (or the end
    of the text). A key whose value is a multi-line array spans several
    physical lines, and `value_start`/`value_end` cover exactly the value.
    """

    kind: str  # "blank" | "comment" | "table" | "array_table" | "key"
    start: int
    end: int
    name: tuple[str, ...] = ()
    value_start: int = -1
    value_end: int = -1
    multiline_string: bool = False


class _Scanner:
    """Split valid TOML into statements without interpreting values.

    It is only ever run on text `tomllib` has already accepted, so it can
    assume structure is sound; what it must get right is where things end --
    a `#` or `[` inside a string, a comment inside a multi-line array, a
    closing `\"\"\"` followed by extra quotes.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.size = len(text)

    def statements(self) -> list[_Statement]:
        text = self.text
        result: list[_Statement] = []
        pos = 0
        while pos < self.size:
            cursor = self._skip_ws(pos)
            if cursor >= self.size or text[cursor] in "\r\n#":
                end = self._line_end(cursor)
                kind = "comment" if cursor < self.size and text[cursor] == "#" else "blank"
                result.append(_Statement(kind, pos, end))
            elif text[cursor] == "[":
                array = text.startswith("[[", cursor)
                name_start = cursor + (2 if array else 1)
                name_end = self._scan_key(name_start)
                name = _parse_key_text(text[name_start:name_end])
                cursor = self._skip_ws(name_end)
                closer = "]]" if array else "]"
                if not text.startswith(closer, cursor):
                    raise ValueError(f"unrecognised table header at offset {pos}")
                end = self._finish_line(cursor + len(closer))
                result.append(_Statement("array_table" if array else "table", pos, end, name))
            else:
                key_end = self._scan_key(cursor)
                name = _parse_key_text(text[cursor:key_end])
                cursor = self._skip_ws(key_end)
                if cursor >= self.size or text[cursor] != "=":
                    raise ValueError(f"expected '=' at offset {cursor}")
                value_start = self._skip_ws(cursor + 1)
                value_end, multiline = self._scan_value(value_start)
                end = self._finish_line(value_end)
                result.append(
                    _Statement("key", pos, end, name, value_start, value_end, multiline)
                )
            pos = end
        return result

    def _skip_ws(self, pos: int) -> int:
        while pos < self.size and self.text[pos] in " \t":
            pos += 1
        return pos

    def _skip_trivia(self, pos: int) -> int:
        while pos < self.size:
            char = self.text[pos]
            if char in " \t\r\n":
                pos += 1
            elif char == "#":
                pos = self._line_end(pos)
            else:
                break
        return pos

    def _line_end(self, pos: int) -> int:
        index = self.text.find("\n", pos)
        return self.size if index < 0 else index + 1

    def _finish_line(self, pos: int) -> int:
        pos = self._skip_ws(pos)
        if pos >= self.size:
            return self.size
        if self.text[pos] == "#" or self.text[pos] == "\n" or self.text.startswith("\r\n", pos):
            return self._line_end(pos)
        raise ValueError(f"unexpected content after a statement at offset {pos}")

    def _scan_key(self, pos: int) -> int:
        """Return the end of a (possibly dotted, possibly quoted) key."""
        text = self.text
        while True:
            pos = self._skip_ws(pos)
            if pos < self.size and text[pos] == '"':
                pos = self._scan_basic_string(pos)
            elif pos < self.size and text[pos] == "'":
                pos = self._scan_literal_string(pos)
            else:
                match = _BARE_KEY.match(text, pos)
                if match is None:
                    raise ValueError(f"expected a key at offset {pos}")
                pos = match.end()
            after = self._skip_ws(pos)
            if after < self.size and text[after] == ".":
                pos = after + 1
                continue
            return pos

    def _scan_basic_string(self, pos: int) -> int:
        cursor = pos + 1
        while cursor < self.size:
            char = self.text[cursor]
            if char == "\\":
                cursor += 2
            elif char == '"':
                return cursor + 1
            elif char == "\n":
                break
            else:
                cursor += 1
        raise ValueError(f"unterminated string at offset {pos}")

    def _scan_literal_string(self, pos: int) -> int:
        close = self.text.find("'", pos + 1)
        newline = self.text.find("\n", pos + 1)
        if close < 0 or (0 <= newline < close):
            raise ValueError(f"unterminated literal string at offset {pos}")
        return close + 1

    def _scan_multiline_string(self, pos: int, quote: str) -> int:
        cursor = pos + 3
        while cursor < self.size:
            if quote == '"' and self.text[cursor] == "\\":
                cursor += 2
                continue
            if self.text.startswith(quote * 3, cursor):
                end = cursor + 3
                # Up to two quotes directly before the delimiter are content.
                extra = 0
                while end < self.size and self.text[end] == quote and extra < 2:
                    end += 1
                    extra += 1
                return end
            cursor += 1
        raise ValueError(f"unterminated multi-line string at offset {pos}")

    def _scan_value(self, pos: int) -> tuple[int, bool]:
        """Return (end offset, whether a multi-line string occurs inside)."""
        text = self.text
        if pos >= self.size:
            raise ValueError("missing value at end of text")
        if text.startswith('"""', pos):
            return self._scan_multiline_string(pos, '"'), True
        if text.startswith("'''", pos):
            return self._scan_multiline_string(pos, "'"), True
        char = text[pos]
        if char == '"':
            return self._scan_basic_string(pos), False
        if char == "'":
            return self._scan_literal_string(pos), False
        if char == "[":
            return self._scan_array(pos)
        if char == "{":
            return self._scan_inline_table(pos)
        match = _BARE_VALUE.match(text, pos)
        if match is None:
            raise ValueError(f"expected a value at offset {pos}")
        end = match.end()
        if _LOCAL_DATE.match(match.group()) and _SPACE_BEFORE_TIME.match(text, end):
            time_match = _BARE_VALUE.match(text, end + 1)
            assert time_match is not None
            end = time_match.end()
        return end, False

    def _scan_array(self, pos: int) -> tuple[int, bool]:
        multiline = False
        cursor = pos + 1
        while True:
            cursor = self._skip_trivia(cursor)
            if cursor >= self.size:
                raise ValueError(f"unterminated array at offset {pos}")
            if self.text[cursor] == "]":
                return cursor + 1, multiline
            end, inner = self._scan_value(cursor)
            multiline = multiline or inner
            cursor = self._skip_trivia(end)
            if cursor < self.size and self.text[cursor] == ",":
                cursor += 1
                continue
            if cursor < self.size and self.text[cursor] == "]":
                return cursor + 1, multiline
            raise ValueError(f"malformed array at offset {pos}")

    def _scan_inline_table(self, pos: int) -> tuple[int, bool]:
        multiline = False
        cursor = self._skip_trivia(pos + 1)
        if cursor < self.size and self.text[cursor] == "}":
            return cursor + 1, multiline
        while True:
            cursor = self._skip_ws(self._scan_key(self._skip_trivia(cursor)))
            if cursor >= self.size or self.text[cursor] != "=":
                raise ValueError(f"malformed inline table at offset {pos}")
            end, inner = self._scan_value(self._skip_ws(cursor + 1))
            multiline = multiline or inner
            cursor = self._skip_trivia(end)
            if cursor < self.size and self.text[cursor] == ",":
                cursor += 1
                continue
            if cursor < self.size and self.text[cursor] == "}":
                return cursor + 1, multiline
            raise ValueError(f"malformed inline table at offset {pos}")


def _parse_key_text(key_text: str) -> tuple[str, ...]:
    """Decode a raw key (`sync`, ` sync `, `"a b".c`) into its parts.

    `tomllib` does the decoding, so quoting and escapes mean exactly what they
    mean to the loader; `[ sync ]` and `[sync]` compare equal.
    """
    node: Any = tomllib.loads(f"{key_text} = 0")
    parts: list[str] = []
    while isinstance(node, dict):
        ((key, node),) = node.items()
        parts.append(key)
    return tuple(parts)


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------


def set_value(text: str, section: str, key: str, value: Any) -> str:
    """Set `[section] key = value`, changing only the bytes of that value.

    `section` may be dotted (`process_detection.background_process_names`).
    An existing value keeps its key, indentation and trailing `# comment`. An
    existing multi-line array is replaced by a single-line one, so comments
    *inside* that array are lost -- the one place this editor drops text. A
    value that is (or contains) a multi-line string is refused with
    `ValueError` rather than guessed at.

    A missing key is inserted after the last key line of its section, so a
    comment or blank line that introduces the next section stays with it. A
    missing section is appended at the end. New lines use the newline style
    the text already uses.
    """
    document = _load(text)
    parts = _split_name(section)
    _require_key(key)
    rendered = render_toml_value(value)
    statements = _Scanner(text).statements()
    newline = _newline(text)

    bounds = _table_bounds(statements, parts)
    if bounds is None:
        result = _append_lines(
            text, newline, [f"[{_render_name(parts)}]", f"{_render_key(key)} = {rendered}"]
        )
    else:
        header, stop = bounds
        index = _find_key(statements, header + 1, stop, key)
        if index is not None:
            statement = statements[index]
            if statement.multiline_string:
                raise ValueError(
                    f"{section}.{key} holds a multi-line string; edit it by hand"
                )
            result = text[: statement.value_start] + rendered + text[statement.value_end :]
        else:
            keys = [i for i in range(header + 1, stop) if statements[i].kind == "key"]
            anchor = statements[keys[-1]] if keys else statements[header]
            indent = _indentation(text, anchor) if keys else ""
            result = _insert_line(
                text, anchor.end, newline, f"{indent}{_render_key(key)} = {rendered}"
            )

    expected = copy.deepcopy(document)
    _table_at(expected, parts, create=True)[key] = copy.deepcopy(value)
    _verify(result, expected, f"setting {section}.{key}")
    return result


def remove_key(text: str, section: str, key: str) -> str:
    """Remove `key` (its whole line, or its whole multi-line array) from `[section]`.

    A key that is absent is a no-op. A key that TOML defines some other way
    this editor cannot see (a dotted key in a parent table, for instance)
    is a `ValueError`, never a silent no-op that leaves it in force.
    """
    document = _load(text)
    parts = _split_name(section)
    _require_key(key)
    statements = _Scanner(text).statements()
    expected = copy.deepcopy(document)
    table = _table_at(expected, parts, create=False)

    bounds = _table_bounds(statements, parts)
    index = None if bounds is None else _find_key(statements, bounds[0] + 1, bounds[1], key)
    if index is None:
        if isinstance(table, dict) and key in table:
            raise ValueError(f"{section}.{key} is defined in a form this editor cannot remove")
        return text

    statement = statements[index]
    result = text[: statement.start] + text[statement.end :]
    assert isinstance(table, dict)
    del table[key]
    _verify(result, expected, f"removing {section}.{key}")
    return result


def replace_array_of_tables(text: str, name: str, entries: list[dict[str, Any]]) -> str:
    """Replace every `[[name]]` block with `entries`, in the dicts' key order.

    The new blocks go where the first old block was, or at the end when there
    was none. A block runs from its header through its last key line (and any
    `[name.sub]` headers that belong to it); comments and blank lines after
    that stay, because they usually introduce whatever comes next. Comments
    between the keys of a removed block are removed with it.
    """
    document = _load(text)
    parts = _split_name(name)
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise TypeError("entries must be a list of dicts")
    rendered_blocks: list[list[str]] = []
    for entry in entries:
        lines = [f"[[{_render_name(parts)}]]"]
        for entry_key, entry_value in entry.items():
            _require_key(entry_key)
            lines.append(f"{_render_key(entry_key)} = {render_toml_value(entry_value)}")
        rendered_blocks.append(lines)

    statements = _Scanner(text).statements()
    newline = _newline(text)
    spans = _array_table_spans(statements, parts, keep_trailing_blank_lines=bool(entries))
    block_text = (newline * 2).join(newline.join(lines) for lines in rendered_blocks)
    if block_text:
        block_text += newline

    if spans:
        pieces: list[str] = []
        cursor = 0
        for number, (start, end) in enumerate(spans):
            pieces.append(text[cursor:start])
            if number == 0:
                pieces.append(block_text)
            cursor = end
        pieces.append(text[cursor:])
        result = "".join(pieces)
    elif rendered_blocks:
        result = _append_lines(text, newline, [block_text[: -len(newline)]])
    else:
        result = text

    expected = copy.deepcopy(document)
    parent = _table_at(expected, parts[:-1], create=bool(entries))
    if entries:
        assert isinstance(parent, dict)
        parent[parts[-1]] = copy.deepcopy(entries)
    elif isinstance(parent, dict):
        parent.pop(parts[-1], None)
        _prune_implicit_parents(expected, parts, result)
    _verify(result, expected, f"replacing [[{name}]]")
    return result


def _array_table_spans(
    statements: list[_Statement], parts: tuple[str, ...], *, keep_trailing_blank_lines: bool
) -> list[tuple[int, int]]:
    """Offsets of the text to remove, one span per run of adjacent blocks."""
    starts = [
        i for i, statement in enumerate(statements)
        if statement.kind == "array_table" and statement.name == parts
    ]
    ranges: list[list[int]] = []
    for position, first in enumerate(starts):
        after = first + 1
        while after < len(statements):
            statement = statements[after]
            if statement.kind in _BODY_KINDS:
                after += 1
            elif len(statement.name) > len(parts) and statement.name[: len(parts)] == parts:
                after += 1  # `[name.sub]` belongs to the element above it
            else:
                break
        last = after
        while last > first + 1 and statements[last - 1].kind in _TRIVIA_KINDS:
            last -= 1
        run_start = ranges[-1][0] if ranges and ranges[-1][1] == first else first
        next_is_removed = position + 1 < len(starts) and starts[position + 1] == after
        is_final = position + 1 == len(starts)
        separated_above = run_start == 0 or statements[run_start - 1].kind == "blank"
        if next_is_removed or (is_final and not keep_trailing_blank_lines and separated_above):
            # Blank lines that only separated this block from the next removed
            # one go with it; so do the ones below the last block when nothing
            # is written back and a blank line above already separates the rest.
            while last < after and statements[last].kind == "blank":
                last += 1
        if ranges and ranges[-1][1] == first:
            ranges[-1][1] = last
        else:
            ranges.append([first, last])
    if ranges and not keep_trailing_blank_lines and ranges[-1][1] == len(statements):
        # Nothing follows and nothing is written back: the blank line that
        # separated the blocks from the text above would be left dangling.
        floor = ranges[-2][1] if len(ranges) > 1 else 0
        while ranges[-1][0] > floor and statements[ranges[-1][0] - 1].kind == "blank":
            ranges[-1][0] -= 1
    return [(statements[lo].start, statements[hi - 1].end) for lo, hi in ranges]


def _prune_implicit_parents(expected: dict[str, Any], parts: tuple[str, ...], result: str) -> None:
    """Drop a parent table that only existed because `[[a.b]]` created it."""
    actual = tomllib.loads(result)
    for depth in range(len(parts) - 1, 0, -1):
        prefix = parts[:depth]
        if _table_at(expected, prefix, create=False) == {} and _table_at(actual, prefix, create=False) is None:
            holder = _table_at(expected, prefix[:-1], create=False)
            assert isinstance(holder, dict)
            del holder[prefix[-1]]


def _load(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"text is not valid TOML, refusing to edit it: {exc}") from exc


def _verify(result: str, expected: dict[str, Any], what: str) -> None:
    try:
        actual = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{what} would produce invalid TOML: {exc}") from exc
    if not _same(actual, expected):
        raise ValueError(f"{what} would change more than that value; the text was left alone")


def _same(first: Any, second: Any) -> bool:
    """Deep equality that also tells `1` from `1.0` and `True` from `1`."""
    if isinstance(first, dict) or isinstance(second, dict):
        return (
            isinstance(first, dict) and isinstance(second, dict)
            and first.keys() == second.keys()
            and all(_same(first[key], second[key]) for key in first)
        )
    if isinstance(first, list) or isinstance(second, list):
        return (
            isinstance(first, list) and isinstance(second, list)
            and len(first) == len(second)
            and all(_same(a, b) for a, b in zip(first, second))
        )
    if type(first) is not type(second):
        return False
    if isinstance(first, float) and math.isnan(first):
        return math.isnan(second)
    return first == second


def _split_name(name: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in name.split("."))
    if not parts or any(not part for part in parts):
        raise ValueError(f"invalid table name: {name!r}")
    return parts


def _require_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError(f"invalid key: {key!r}")


def _table_bounds(statements: list[_Statement], parts: tuple[str, ...]) -> tuple[int, int] | None:
    for index, statement in enumerate(statements):
        if statement.kind == "table" and statement.name == parts:
            stop = index + 1
            while stop < len(statements) and statements[stop].kind in _BODY_KINDS:
                stop += 1
            return index, stop
    return None


def _find_key(statements: list[_Statement], start: int, stop: int, key: str) -> int | None:
    for index in range(start, stop):
        statement = statements[index]
        if statement.kind == "key" and statement.name == (key,):
            return index
    return None


def _table_at(document: dict[str, Any], parts: tuple[str, ...], *, create: bool) -> dict[str, Any] | None:
    node: Any = document
    for part in parts:
        if not isinstance(node, dict):
            raise ValueError(f"{'.'.join(parts)} is not a table")
        if part not in node:
            if not create:
                return None
            node[part] = {}
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"{'.'.join(parts)} is not a table")
    return node


def _newline(text: str) -> str:
    index = text.find("\n")
    return "\r\n" if index > 0 and text[index - 1] == "\r" else "\n"


def _indentation(text: str, statement: _Statement) -> str:
    cursor = statement.start
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    return text[statement.start : cursor]


def _insert_line(text: str, offset: int, newline: str, line: str) -> str:
    prefix = newline if offset == len(text) and text and not text.endswith("\n") else ""
    return text[:offset] + prefix + line + newline + text[offset:]


def _append_lines(text: str, newline: str, lines: list[str]) -> str:
    result = text
    if result and not result.endswith("\n"):
        result += newline
    if result.strip() and not _ends_with_blank_line(result):
        result += newline
    return result + newline.join(lines) + newline


def _ends_with_blank_line(text: str) -> bool:
    lines = text.split("\n")
    return len(lines) >= 2 and lines[-1] == "" and lines[-2].strip() == ""


# --------------------------------------------------------------------------
# Documents, validation, history and saving
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigDocument:
    """A config file as opened: its text and the hash of its exact bytes.

    `sha256` is what `save_config_text` compares against, so it covers the
    bytes on disk (a byte-order mark included), not the decoded text.
    """

    path: Path
    text: str
    sha256: str


@dataclass(frozen=True)
class SavedConfig:
    path: Path
    sha256: str
    #: The verified copy of the replaced file, or ``None`` when there was no
    #: file, or the old file did not itself validate (nothing proven to keep).
    history_entry: Path | None


@dataclass(frozen=True)
class ConfigHistoryEntry:
    name: str
    path: Path
    size: int
    #: Taken from the entry's name, not from file times, which a cloud client
    #: rewrites when it downloads the directory.
    created_utc: datetime


def read_config_document(path: Path) -> ConfigDocument:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {path}. {exc}") from exc
    return ConfigDocument(path, decode_config_bytes(data, source=str(path)), _sha256(data))


def validate_config_text(text: str, *, path: Path) -> AppConfig:
    """Validate unsaved text exactly as `load_config` would validate the saved file."""
    return parse_config_text(text, base_dir=path.parent.resolve(), source=str(path))


def config_diff(old: str, new: str, *, path_label: str) -> str:
    """Unified diff between two versions of a config; empty when they are equal."""
    if old == new:
        return ""
    lines = difflib.unified_diff(
        _split_keeping_newlines(old),
        _split_keeping_newlines(new),
        fromfile=f"{path_label} (on disk)",
        tofile=f"{path_label} (edited)",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _split_keeping_newlines(text: str) -> list[str]:
    # Not str.splitlines: that also breaks on U+2028 and form feeds, which
    # TOML allows inside comments and strings.
    return re.findall(r"[^\n]*\n|[^\n]+\Z", text)


def config_history_dir(cfg: AppConfig, config_path: Path) -> Path:
    """Where replaced versions of `config_path` are kept.

    Never inside the backup directory: backup pruning deletes every directory
    there by age, so a history kept there would disappear on a timer. Never
    inside the temp directory, whose orphans are cleaned up, and never inside
    the Codex state directory at all.
    """
    if cfg.paths.workspace_root_dir is not None:
        history = cfg.paths.workspace_root_dir / CONFIG_HISTORY_DIR_NAME
    else:
        history = config_path.parent.resolve() / CONFIG_HISTORY_DIR_NAME
    history = history.resolve()
    for field_name, protected in (
        ("paths.local_state_dir", cfg.paths.local_state_dir),
        ("paths.backup_dir", cfg.paths.backup_dir),
        ("paths.temp_dir", cfg.paths.temp_dir),
    ):
        if protected is not None and _paths_overlap(history, protected.resolve()):
            raise ConfigError(f"config history directory {history} must not overlap {field_name}")
    return history


def list_config_history(cfg: AppConfig, config_path: Path) -> list[ConfigHistoryEntry]:
    """Saved versions, newest first. Read-only: an absent directory is an empty list."""
    history = config_history_dir(cfg, config_path)
    if not history.is_dir():
        return []
    entries: list[ConfigHistoryEntry] = []
    for candidate in history.iterdir():
        match = _HISTORY_NAME.match(candidate.name)
        if match is None or not candidate.is_file():
            continue
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        entries.append(ConfigHistoryEntry(candidate.name, candidate, candidate.stat().st_size, created))
    entries.sort(key=lambda entry: (entry.created_utc, entry.name), reverse=True)
    return entries


def save_config_text(
    path: Path,
    new_text: str,
    *,
    expected_sha256: str | None,
    now: datetime | None = None,
) -> SavedConfig:
    """Replace `path` with `new_text`, but only over the version the editor opened.

    Order matters, and each step is there for a reason:

    1. `new_text` must pass the CLI's own loader, and must not be a config
       every mutation command refuses. Nothing is written otherwise.
    2. The file on disk must still be the one that was opened
       (`expected_sha256`); an edit made elsewhere is never overwritten. With
       no base (`None`) only creating a new file is allowed.
    3. The replaced version is copied into the history and read back first --
       if it validated. A file that did not validate is not worth pretending
       to have kept, and `history_entry` is ``None``.
    4. The new bytes go through a temporary file in the same directory and
       `os.replace`, then are read back and compared.
    """
    new_cfg = validate_config_text(new_text, path=path)
    _require_mutation_compatible_config(new_cfg)
    _require_outside_state_dir(new_cfg, path)
    history_dir = config_history_dir(new_cfg, path)
    try:
        new_bytes = new_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"Config text cannot be encoded as UTF-8: {exc}") from exc

    old_bytes = _read_if_present(path)
    _require_expected_base(path, old_bytes, expected_sha256)

    history_entry: Path | None = None
    if old_bytes is not None and _bytes_validate(old_bytes, path):
        history_entry = _write_history_entry(history_dir, old_bytes, now)

    # Writing the history took time; re-prove the base right before replacing.
    _require_expected_base(path, _read_if_present(path), expected_sha256)
    _write_atomically(path, new_bytes)

    written = _sha256(path.read_bytes())
    if written != _sha256(new_bytes):
        raise FailSafeError(f"{path} was replaced but reads back differently; check it before retrying")
    return SavedConfig(path, written, history_entry)


def create_config(
    path: Path,
    *,
    machine_id: str,
    local_state_dir: str,
    workspace_root_dir: str,
    cloud_root_dir: str | None = None,
) -> SavedConfig:
    """Write a new config from the packaged template, keeping all its comments.

    Only the config file's own parent directory may be created. Nothing is
    created inside `local_state_dir`: that is the Codex state directory, and a
    config placed inside it is refused outright.
    """
    if path.exists():
        raise ConfigError(f"Config file already exists: {path}")
    try:
        require_guardian_machine_id(machine_id)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    for field_name, value in (
        ("paths.local_state_dir", local_state_dir),
        ("paths.workspace_root_dir", workspace_root_dir),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{field_name} must not be empty")

    text = decode_config_bytes(TEMPLATE_PATH.read_bytes(), source=str(TEMPLATE_PATH))
    text = set_value(text, "identity", "machine_id", machine_id.strip())
    text = set_value(text, "paths", "local_state_dir", local_state_dir)
    text = set_value(text, "paths", "workspace_root_dir", workspace_root_dir)
    if cloud_root_dir is not None:
        text = set_value(text, "paths", "cloud_root_dir", cloud_root_dir)

    cfg = validate_config_text(text, path=path)
    _require_mutation_compatible_config(cfg)
    _require_outside_state_dir(cfg, path)
    try:
        path.parent.mkdir(exist_ok=True)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Cannot create {path.parent}: its parent directory does not exist"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot create {path.parent}. {exc}") from exc
    return save_config_text(path, text, expected_sha256=None)


def _require_outside_state_dir(cfg: AppConfig, path: Path) -> None:
    state_dir = cfg.paths.local_state_dir
    if state_dir is None:
        return
    target = path.resolve()
    root = state_dir.resolve()
    if target == root or root in target.parents:
        raise ConfigError(f"The config file must be outside paths.local_state_dir: {path}")


def _read_if_present(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {path}. {exc}") from exc


def _require_expected_base(path: Path, current: bytes | None, expected_sha256: str | None) -> None:
    if current is None:
        if expected_sha256 is not None:
            raise ConflictError(f"{path.name} was removed on disk since it was opened; reload it")
        return
    if expected_sha256 is None:
        raise ConflictError(
            f"{path.name} already exists; open it before saving so an edit made elsewhere is not overwritten"
        )
    if _sha256(current) != expected_sha256.lower():
        raise ConflictError(f"{path.name} changed on disk since it was opened; reload it")


def _bytes_validate(data: bytes, path: Path) -> bool:
    try:
        validate_config_text(decode_config_bytes(data, source=str(path)), path=path)
    except ConfigError:
        return False
    return True


def _write_history_entry(history_dir: Path, data: bytes, now: datetime | None) -> Path:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    history_dir.mkdir(parents=True, exist_ok=True)
    entry = history_dir / f"config-{stamp:%Y%m%dT%H%M%S%f}Z.toml"
    try:
        handle = open(entry, "xb")
    except FileExistsError as exc:
        raise FailSafeError(f"Config history entry already exists, refusing to overwrite it: {entry}") from exc
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(entry.read_bytes()) != _sha256(data):
            raise FailSafeError(f"Config history entry reads back differently: {entry}")
    except BaseException:
        try:
            entry.unlink()
        except OSError:
            pass
        raise
    return entry


def _write_atomically(path: Path, data: bytes) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
