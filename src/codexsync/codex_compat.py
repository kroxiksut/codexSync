"""Whether this Codex keeps its state in formats codexSync knows (CS-394). Reads only.

Each part of `.codex` codexSync writes has its own refusal when its format is
unknown: a chat file in a container it cannot open blocks that chat, a chat
list (SQLite) in an unknown shape blocks every write into it and every chat
written into `.codex`, an unknown rebuild status is never repaired, and
projects in an unknown shape are not carried. Those refusals are scattered
over a plan's codes; this module collects them into one answer for `doctor`:
compatible, or which writes are off and why. Version numbers decide nothing --
Codex changes formats inside a version line -- so the version is reported,
and the formats are what is judged.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .codex_health import BACKFILL_PENDING, BACKFILL_RUNNING
from .guardian_restore import carries_project_state
from .guardian_schema import validate_global_state_references
from .jsonl_codec import unsupported_container
from .session_catalog import UNSUPPORTED_CONTAINER, SessionCatalog
from .sqlite_audit import CATALOG_SHAPE_UNKNOWN, PlacementStatus, read_backfill_state, read_thread_placements
from .thread_catalogue import BACKFILL_COMPLETE

__all__ = [
    "CHATS",
    "CHAT_LIST",
    "GLOBAL_STATE",
    "REBUILD_STATUS",
    "CompatPart",
    "CompatReport",
    "check_codex_compat",
    "read_codex_version",
]

#: Chat files (`sessions/`, `archived_sessions/`).
CHATS = "chats"
#: Codex's chat list, the `threads` table of `state_<n>.sqlite`.
CHAT_LIST = "chat_list"
#: The chat-list rebuild status (`backfill_state`).
REBUILD_STATUS = "rebuild_status"
#: Projects in `.codex-global-state.json`.
GLOBAL_STATE = "global_state"

#: Codes saying the chat list is in a shape this version does not read; any
#: other reason it could not be read (busy, a log without its index) is about
#: this moment, not about the format.
_SHAPE_CODES = frozenset({CATALOG_SHAPE_UNKNOWN, "SEVERAL_CATALOGUES"})
_KNOWN_REBUILD = frozenset({BACKFILL_COMPLETE, BACKFILL_RUNNING, BACKFILL_PENDING})
#: Written by Codex's core, which the app and the console share; only this one
#: key is read.
_VERSION_FILE = "models_cache.json"
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")


@dataclass(frozen=True, slots=True)
class CompatPart:
    """One part of `.codex` in a format this version does not know."""

    part: str
    code: str
    count: int = 0
    #: For chat files: the containers found (``("zst",)``).
    containers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompatReport:
    codex_version: str | None
    unknown: tuple[CompatPart, ...]
    #: Parts that could not be read just now, for a reason that is not their format.
    unchecked: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        return not self.unknown


def read_codex_version(state_root: Path) -> str | None:
    """The Codex version that last refreshed `models_cache.json`, or ``None``."""
    try:
        data = json.loads((state_root / _VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    value = data.get("client_version") if isinstance(data, dict) else None
    return value if isinstance(value, str) and _VERSION.match(value) else None


def check_codex_compat(
    state_root: Path, catalog: SessionCatalog | None, *, max_state_bytes: int,
) -> CompatReport:
    """Which parts of ``state_root`` are in a format this version does not know.

    ``catalog`` is a scan of ``state_root`` the caller already made; without it
    the chat files are reported as not checked.
    """
    unknown: list[CompatPart] = []
    unchecked: list[str] = []

    if catalog is None:
        unchecked.append(CHATS)
    else:
        containers = sorted({
            container
            for item in catalog.descriptors
            if UNSUPPORTED_CONTAINER in item.codes
            and (container := unsupported_container(item.relative_path.rsplit("/", 1)[-1]))
        })
        count = sum(1 for item in catalog.descriptors if UNSUPPORTED_CONTAINER in item.codes)
        if count:
            unknown.append(CompatPart(CHATS, UNSUPPORTED_CONTAINER, count, tuple(containers)))

    placements = read_thread_placements(state_root)
    if placements.status is PlacementStatus.INDETERMINATE:
        shape = _SHAPE_CODES.intersection(placements.codes)
        if shape:
            unknown.append(CompatPart(CHAT_LIST, sorted(shape)[0]))
        else:
            unchecked.append(CHAT_LIST)

    backfill = read_backfill_state(state_root)
    if (
        backfill.status is PlacementStatus.AVAILABLE
        and backfill.backfill is not None
        and backfill.backfill not in _KNOWN_REBUILD
    ):
        unknown.append(CompatPart(REBUILD_STATUS, "CATALOGUE_STATUS_UNKNOWN"))

    source = state_root / ".codex-global-state.json"
    try:
        if source.is_file() and source.stat().st_size <= max_state_bytes:
            payload = source.read_bytes()
            if validate_global_state_references(payload).schema_id is None and carries_project_state(payload):
                unknown.append(CompatPart(GLOBAL_STATE, "UNKNOWN_SCHEMA"))
        elif source.is_file():
            unchecked.append(GLOBAL_STATE)
    except OSError:
        unchecked.append(GLOBAL_STATE)

    return CompatReport(read_codex_version(state_root), tuple(unknown), tuple(unchecked))
