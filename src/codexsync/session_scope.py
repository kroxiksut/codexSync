"""The working set: which chats a transfer may bring into this `.codex`.

The scenario is one machine's worth of work. A laptop needs `project-chloya`
and nothing else; dragging 864 MiB of sessions across for two chats is the
thing this avoids. So a scope names projects and single chats, and everything
else is classified, mirrored and left alone.

Three rules make it safe rather than merely smaller.

*A project means all of its chats*, however they reach it -- bound, found by
path, or connected only by a `[[path_mappings]]` rule. Including the ones that
appeared on the other machine since last time: a working set is a standing
choice, not a snapshot of ids.

*A spawned sub-thread follows its parent.* Roughly half the session files on a
real machine are sub-threads, and a set that carried the parent without them
would bring back a conversation whose branches are missing.

*The cloud mirror is never scoped.* The scope narrows writes into `.codex`
only; the mirror keeps every session, so the backup is complete no matter what
the working set says. `semantic_transfer._apply_scope` is where that is
enforced -- this module only decides which sessions are named.

The set lives in `plans/`, beside the conflict resolutions, and not in
`config.toml`: it is working state that changes with what someone is doing this
week, not a setting that describes the machine.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .chat_directory import ChatDirectory, ChatEntry

__all__ = [
    "SCOPE_FORMAT",
    "SessionScope",
    "build_session_scope",
    "load_session_scope",
    "save_session_scope",
    "scope_path_for",
]

SCOPE_FORMAT = "codexsync-session-scope-v1"


@dataclass(frozen=True, slots=True)
class SessionScope:
    """A chosen working set, and what it resolves to right now."""

    #: Project ids named by the set. Their chats are included wholesale.
    projects: tuple[str, ...] = ()
    #: Session ids named one by one, on top of whatever the projects bring.
    chats: tuple[str, ...] = ()
    #: What the two above expand to: session hashes, sorted. This is what a
    #: plan carries, so a plan never holds a session id or a file name.
    session_hashes: tuple[str, ...] = ()
    #: How many chats the set covers, for the line a screen shows.
    chat_count: int = 0
    #: Their total size in bytes, where the catalogue knows it.
    total_bytes: int = 0
    #: Sessions in the set this machine's thread catalogue does not place.
    #: They are still mirrored to the cloud, but nothing can be written for
    #: them into `.codex`: making the runtime see a new session needs a
    #: catalogue row codexSync does not write. Empty when there is no
    #: catalogue to ask, because "unknown" is not "fine".
    not_in_catalog: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.projects and not self.chats


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def build_session_scope(
    directory: ChatDirectory,
    *,
    projects: tuple[str, ...] = (),
    chats: tuple[str, ...] = (),
    placements: "object | None" = None,
) -> SessionScope:
    """Expand chosen projects and chats into the sessions they cover.

    ``projects`` may be given by id or by name, because a person reads names
    and a plan stores ids; a name that matches no project simply contributes
    nothing, which the counter then shows.

    ``placements`` is this machine's thread catalogue, when it could be read.
    It is used for one thing: saying which chats of the set cannot be written
    into `.codex` here, so a screen can show that limit instead of letting a
    person find out by a chat never appearing.
    """
    wanted_projects = _resolve_projects(directory, projects)
    chosen: dict[str, ChatEntry] = {}

    for chat in directory.chats:
        if chat.project_id and chat.project_id in wanted_projects:
            chosen[chat.session_id] = chat
    for reference in chats:
        for chat in directory.find(reference):
            chosen[chat.session_id] = chat

    # Sub-threads follow their parent, however deep. A set that brought a
    # conversation without the threads it spawned would be a conversation with
    # holes in it.
    changed = True
    while changed:
        changed = False
        for chat in directory.chats:
            if chat.parent_id and chat.parent_id in chosen and chat.session_id not in chosen:
                chosen[chat.session_id] = chat
                changed = True

    return SessionScope(
        projects=tuple(sorted(wanted_projects)),
        chats=tuple(sorted(chats)),
        session_hashes=tuple(sorted(session_hash(session_id) for session_id in chosen)),
        chat_count=len(chosen),
        total_bytes=sum(chat.byte_count for chat in chosen.values()),
        not_in_catalog=_unplaceable(chosen, placements),
    )


def _unplaceable(chosen: dict[str, ChatEntry], placements: "object | None") -> tuple[str, ...]:
    """Chosen sessions the thread catalogue here does not place.

    Only when the catalogue was actually readable. An absent or indeterminate
    catalogue means nobody knows, and answering "none" would be a claim that
    every chat will arrive.
    """
    if placements is None or getattr(placements, "status", None) is None:
        return ()
    if getattr(placements.status, "name", "") != "AVAILABLE":
        return ()
    return tuple(sorted(
        session_id for session_id in chosen if not placements.knows(session_id)
    ))


def _resolve_projects(directory: ChatDirectory, projects: tuple[str, ...]) -> set[str]:
    wanted: set[str] = set()
    by_name = {
        (view.name or "").casefold(): project_id
        for project_id, view in directory.projects.items()
        if view.name
    }
    for reference in projects:
        value = reference.strip()
        if not value:
            continue
        if value in directory.projects:
            wanted.add(value)
            continue
        matched = by_name.get(value.casefold())
        if matched:
            wanted.add(matched)
    return wanted


def scope_path_for(plans_dir: Path, source_machine: str, target_machine: str) -> Path:
    """Where a pair of machines keeps its working set."""
    pair = f"{_file_safe(source_machine)}-{_file_safe(target_machine)}"
    return plans_dir / f"sessions-scope-{pair}.json"


def save_session_scope(scope: SessionScope, path: Path) -> Path:
    """Write the choice -- the names, not the expansion.

    Only projects and chats are stored. Session hashes are the result of
    reading the state at a moment in time, and a set that froze them would
    stop covering a chat started on the other machine tomorrow, which is
    exactly what a standing working set is for.
    """
    payload = {
        "format": SCOPE_FORMAT,
        "projects": list(scope.projects),
        "chats": list(scope.chats),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)
    return path


def load_session_scope(path: Path) -> SessionScope:
    """Read a stored working set. A missing file is an empty set, not an error."""
    if not path.is_file():
        return SessionScope()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("format") != SCOPE_FORMAT:
        raise ValueError(f"unsupported working-set format in {path}")
    return SessionScope(
        projects=tuple(str(item) for item in raw.get("projects", ())),
        chats=tuple(str(item) for item in raw.get("chats", ())),
    )


def _file_safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value.strip()) or "machine"
