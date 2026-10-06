"""Chat names between machines (D-025).

The name a chat shows in Codex's list lives only in ``threads.name`` of the
thread catalogue -- not in the chat file. A chat carried to another machine
therefore arrives nameless: Codex rebuilds its row from the file (D-024) and
shows the first message instead. Observed on the laptop on 2026-10-04 right
after D-024 made the chats appear at all.

Each machine publishes the names it holds in one self-verifying file of its
own beside the project lists, written only by that machine, exactly like those
(`project_sync`). The receiving machine sets a peer's name on a chat whose name
here is *unset* -- empty, or the first-message title Codex falls back to -- and
nothing else. A name set here is never replaced: renamed on both machines, the
local one stays and the difference is counted (``kept``). Two peers that name
one chat differently leave it alone (``ambiguous``).

The write is the statement Codex itself uses to rename a thread
(``UPDATE threads SET name = ? WHERE id = ?``, read from `codex.exe` 26.930),
guarded so it changes a row only while its name is still what the plan saw.
A chat Codex has no row for yet gets its name on the first sync after Codex
listed it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable

from .exceptions import FailSafeError
from .peer_board import digest as _digest, read_bodies, write_body
from .sqlite_audit import ThreadNames

NAMES_FORMAT = "codexsync-chat-names-v1"
_PLAN_VERSION = 1


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_unset(name: str | None, title: str) -> bool:
    """Whether a chat has no name of its own here: none, or Codex's fallback."""
    return not name or name == title


@dataclass(frozen=True, slots=True)
class NamesPublication:
    machine: str
    #: Thread id -> the name that machine shows.
    names: dict[str, str]
    published_at_utc: str = ""

    @property
    def content_id(self) -> str:
        return _digest(dict(sorted(self.names.items())))

    def to_json(self) -> dict[str, Any]:
        body = {
            "format": NAMES_FORMAT,
            "machine": self.machine,
            "names": dict(sorted(self.names.items())),
            "published_at_utc": self.published_at_utc,
        }
        return {**body, "entry_digest": _digest(body)}


@dataclass(frozen=True, slots=True)
class NamesBoard:
    publications: dict[str, NamesPublication]
    unreadable: dict[str, str] = field(default_factory=dict)

    def others(self, machine: str) -> list[NamesPublication]:
        return [item for name, item in sorted(self.publications.items()) if name != machine]


def publication_from_names(
    names: ThreadNames, machine: str, *, now: Callable[[], str] = now_utc,
) -> NamesPublication:
    """The names this machine shows: a set name, never the first-message fallback."""
    shown = {
        thread_id: name
        for thread_id, (name, title) in names.rows.items()
        if name is not None and not is_unset(name, title)
    }
    return NamesPublication(machine, shown, now())


def read_names_board(root: Path) -> NamesBoard:
    """Every machine's names. Reads only; a bad file is reported, not fatal."""
    read = read_bodies(root, NAMES_FORMAT)
    publications: dict[str, NamesPublication] = {}
    unreadable = dict(read.unreadable)
    for machine, body in read.bodies.items():
        names = body.get("names")
        if not isinstance(names, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and value for key, value in names.items()
        ):
            unreadable[f"{machine}.json"] = "names must map thread ids to non-empty strings"
            continue
        publications[machine] = NamesPublication(machine, dict(names), str(body.get("published_at_utc", "")))
    return NamesBoard(publications, unreadable)


def write_names_publication(root: Path, publication: NamesPublication) -> Path:
    """Replace this machine's file in one step."""
    body = {key: value for key, value in publication.to_json().items() if key != "entry_digest"}
    return write_body(root, publication.machine, body)


@dataclass(frozen=True, slots=True)
class NameChange:
    thread_id: str
    #: The name as it is here now (``None`` for none), which the write requires.
    current: str | None
    name: str
    peer: str


@dataclass(frozen=True, slots=True)
class NamePlan:
    plan_id: str
    database: str | None
    changes: tuple[NameChange, ...] = ()
    #: Chats with a name of their own here that a peer names differently.
    kept: int = 0
    #: Chats two peers name differently; left alone.
    ambiguous: int = 0
    #: Names for chats Codex has no row for here yet.
    waiting: int = 0
    codes: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        return bool(self.changes)


def build_name_plan(
    local: ThreadNames,
    peers: list[NamesPublication],
    *,
    fingerprint: Callable[[str], str] | None = None,
) -> NamePlan:
    """Which names to set here. Reads only.

    ``fingerprint`` hashes the catalogue (relative path in, digest out) so the
    plan id changes when the database does; it is consulted only when there is
    something to write.
    """
    from .sqlite_audit import PlacementStatus

    if local.status is not PlacementStatus.AVAILABLE:
        material = {"version": _PLAN_VERSION, "status": local.status.value, "codes": list(local.codes)}
        return NamePlan(_digest(material), local.database, codes=local.codes)
    offered: dict[str, dict[str, str]] = {}
    for peer in peers:
        for thread_id, name in peer.names.items():
            offered.setdefault(thread_id, {})[peer.machine] = name
    changes: list[NameChange] = []
    kept = ambiguous = waiting = 0
    for thread_id, by_peer in sorted(offered.items()):
        distinct = set(by_peer.values())
        row = local.rows.get(thread_id)
        if row is None:
            waiting += 1
            continue
        current, title = row
        if len(distinct) > 1:
            if is_unset(current, title):
                ambiguous += 1
            continue
        name = next(iter(distinct))
        if current == name:
            continue
        if not is_unset(current, title):
            kept += 1
            continue
        changes.append(NameChange(thread_id, current, name, sorted(by_peer)[0]))
    material = {
        "version": _PLAN_VERSION,
        "database": local.database,
        "changes": [[item.thread_id, item.current, item.name] for item in changes],
        "fingerprint": fingerprint(local.database) if changes and fingerprint and local.database else None,
    }
    return NamePlan(_digest(material), local.database, tuple(changes), kept, ambiguous, waiting)


def apply_names(database: Path, changes: tuple[NameChange, ...]) -> int:
    """Set every name in one transaction, or none.

    Each row must still carry the name the plan saw; one that moved rolls the
    whole transaction back.
    """
    connection = sqlite3.connect(str(database), timeout=5.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for change in changes:
                changed = connection.execute(
                    "UPDATE threads SET name = ? WHERE id = ? AND name IS ?",
                    (change.name, change.thread_id, change.current),
                ).rowcount
                if changed != 1:
                    raise FailSafeError(
                        "A chat's name changed after it was read; nothing was written. Run the sync again."
                    )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()
    return len(changes)


def read_names_back(database: Path, thread_ids: list[str]) -> dict[str, str | None]:
    """The names as stored now, read after the write with Codex closed."""
    connection = sqlite3.connect(str(database), timeout=5.0)
    try:
        result: dict[str, str | None] = {}
        for thread_id in thread_ids:
            row = connection.execute("SELECT name FROM threads WHERE id = ?", (thread_id,)).fetchone()
            result[thread_id] = row[0] if row is not None else None
        return result
    finally:
        connection.close()
