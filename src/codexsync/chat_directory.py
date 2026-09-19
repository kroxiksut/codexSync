"""A read-only view of which chats exist and which project each one is in.

This is the picture a person needs before moving anything: the projects, the
chats under them, and the chats under none. It answers three questions the raw
state does not answer on its own.

**Which sessions are chats.** Roughly half of the session files on a real
machine are sub-threads an agent spawned, not conversations anybody started.
They are told apart structurally, by the ``parent_thread_id`` and
``thread_source`` the runtime itself writes — never by looking at what the
records say. A text heuristic would silently reclassify a chat the day someone
writes the wrong sentence in it.

**Which project a chat belongs to.** Measured on real state, both mechanisms
are live: a chat with a ``thread-project-assignments`` entry is bound to that
project, and a chat without one reaches a project only by its ``cwd`` falling
under the project's root. So the association is reported *with its reason*
(:class:`Association`), because the two behave differently the moment a project
moves: a bound chat follows the project, a derived one is left behind by the
old path. That distinction is the whole reason this module exists.

There is a third reason, and it is the one a person hits after moving between
machines. A chat written on the desktop records a ``D:`` path; on the laptop the
same project lives under ``C:``, so the chat derives to nothing and Codex shows
it under no project at all. Given ``[[path_mappings]]`` this module can still
say which project it *means* — reported as ``DERIVED_VIA_MAPPING`` and never
folded into a plain ``DERIVED``, because the runtime does not know those rules.
A chat in that state is invisible in Codex until it is given a real binding,
which makes this exactly the list of chats a handoff leaves to repair.

**What a chat is about.** The title is the first thing the person actually
typed — an ``event_msg`` record of type ``user_message``. Everything the
runtime injects into the conversation as if the user had said it (an
environment block, a plugin list, a desktop-context preamble) is not that, and
is skipped.

Nothing here writes, and nothing here decides: a caller that wants to change an
association builds a plan from this view and applies it through the ordinary
mutation envelope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path

from .guardian_schema import binding_project_id, detect_state_schema
from .path_mapping import PathMappingError, PathMappingRule, apply_path_mapping
from .progress import ProgressCallback, report
from .session_catalog import (
    DEFAULT_MAX_JSONL_LINE_BYTES,
    SessionCatalog,
    SessionDescriptor,
    SessionState,
    scan_sessions,
)


#: How many records to read looking for the opening line of a chat. A title is
#: a convenience, so it is bounded rather than allowed to stream a whole file.
TITLE_SCAN_RECORDS = 60
TITLE_MAX_CHARS = 120

#: ``thread_source`` values that mean a person opened this chat. Absent counts
#: as a chat too: the field is newer than the oldest sessions on disk.
_TOP_LEVEL_THREAD_SOURCES = frozenset({"user"})


class ChatKind(str, Enum):
    #: A conversation somebody started.
    TOP_LEVEL = "TOP_LEVEL"
    #: A thread an agent spawned from another thread.
    SUB_THREAD = "SUB_THREAD"


class Association(str, Enum):
    #: An explicit ``thread-project-assignments`` entry names the project.
    BOUND = "BOUND"
    #: No entry; the chat reaches the project only through its ``cwd``.
    DERIVED = "DERIVED"
    #: The chat's ``cwd`` names another machine's path, and only a configured
    #: mapping rule connects it to a project here. Codex does not read those
    #: rules, so the chat is invisible to it until it is bound.
    DERIVED_VIA_MAPPING = "DERIVED_VIA_MAPPING"
    #: Neither: the chat appears under no project.
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ProjectView:
    project_id: str
    name: str | None
    roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChatEntry:
    session_id: str
    relative_path: str
    state: SessionState
    kind: ChatKind
    association: Association
    project_id: str | None
    timestamp: str | None
    cwd: str | None
    record_count: int
    parent_id: str | None = None
    title: str | None = None
    codes: tuple[str, ...] = ()
    #: Size of the branch file on disk. Comes from the catalogue, which
    #: measured it while hashing, so nothing is stat'ed twice for it.
    byte_count: int = 0

    @property
    def short_id(self) -> str:
        """First segment of the id, which is what a person can actually read."""
        return self.session_id.split("-", 1)[0]


@dataclass(slots=True)
class ChatDirectory:
    projects: dict[str, ProjectView]
    chats: tuple[ChatEntry, ...]
    schema_id: str
    volatile: bool = False
    codes: tuple[str, ...] = ()

    def by_project(self, project_id: str | None) -> tuple[ChatEntry, ...]:
        return tuple(chat for chat in self.chats if chat.project_id == project_id)

    def top_level(self) -> tuple[ChatEntry, ...]:
        return tuple(chat for chat in self.chats if chat.kind is ChatKind.TOP_LEVEL)

    def children_of(self, session_id: str) -> tuple[ChatEntry, ...]:
        return tuple(chat for chat in self.chats if chat.parent_id == session_id)

    def find(self, reference: str) -> tuple[ChatEntry, ...]:
        """Chats a person's shorthand could mean: a full id or an id prefix."""
        reference = reference.strip().casefold()
        if not reference:
            return ()
        return tuple(
            chat for chat in self.chats if chat.session_id.casefold().startswith(reference)
        )

    def project_named(self, reference: str) -> tuple[ProjectView, ...]:
        """Projects a person's shorthand could mean: an id, a prefix, or a name."""
        reference = reference.strip().casefold()
        if not reference:
            return ()
        exact = tuple(
            project for project in self.projects.values()
            if (project.name or "").casefold() == reference
            or project.project_id.casefold() == reference
        )
        if exact:
            return exact
        return tuple(
            project for project in self.projects.values()
            if project.project_id.casefold().startswith(reference)
            or reference in (project.name or "").casefold()
        )


def build_chat_directory(
    state_root: Path,
    global_state: bytes,
    *,
    max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
    volatile: bool = False,
    catalog: SessionCatalog | None = None,
    with_titles: bool = True,
    rules: list[PathMappingRule] | None = None,
    source_machine: str | None = None,
    target_machine: str | None = None,
    progress: ProgressCallback | None = None,
) -> ChatDirectory:
    """Read the state and the sessions and say where every chat sits.

    ``rules`` with both machine names lets a chat recorded on another machine
    still name its project here; without them such a chat is simply reported as
    belonging to no project, which is what Codex itself shows.
    """
    state = _parse_state(global_state)
    schema_id = detect_state_schema(state)
    if schema_id is None:
        raise ValueError("UNKNOWN_SCHEMA")
    projects = _projects(state, schema_id)
    assignments = state.get("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}

    if catalog is None:
        catalog = scan_sessions(
            state_root, max_line_bytes=max_line_bytes, volatile=volatile, progress=progress,
        )
    codes: list[str] = []
    chats: list[ChatEntry] = []
    # The second pass: every descriptor's file is opened again for its meta
    # record and its title, which is why this phase is reported separately
    # from the hashing that produced the catalogue.
    total = len(catalog.descriptors)
    report(progress, "chats", 0, total)
    for done, descriptor in enumerate(catalog.descriptors, start=1):
        report(progress, "chats", done, total)
        if not descriptor.session_id:
            continue
        path = state_root / Path(*descriptor.relative_path.split("/"))
        meta = _read_meta(path, max_line_bytes) if path.is_file() else {}
        kind = _kind(descriptor, meta)
        association, project_id, entry_codes = _associate(
            descriptor, assignments, projects, schema_id,
            rules=rules or [], source_machine=source_machine, target_machine=target_machine,
        )
        title = None
        if with_titles and path.is_file():
            title = _read_title(path, max_line_bytes)
        chats.append(
            ChatEntry(
                descriptor.session_id, descriptor.relative_path, descriptor.state, kind,
                association, project_id, descriptor.timestamp, descriptor.cwd,
                descriptor.line_count, descriptor.parent_id, title, entry_codes,
                descriptor.byte_count,
            )
        )
        codes.extend(entry_codes)

    chats.sort(key=lambda chat: (chat.timestamp or "", chat.session_id))
    return ChatDirectory(
        projects, tuple(chats), schema_id,
        volatile or catalog.volatile, tuple(dict.fromkeys(codes)),
    )


def search_chats(
    directory: ChatDirectory,
    *,
    text: str | None = None,
    project: str | None = None,
    association: Association | None = None,
    since: str | None = None,
    until: str | None = None,
    include_sub_threads: bool = False,
    include_invalid: bool = False,
) -> tuple[ChatEntry, ...]:
    """Chats matching every filter given. Filters read, they never rank.

    ``text`` matches the title and the working directory, which is what a
    person remembers about a chat. It deliberately does not search whole
    conversations: that would mean streaming every record of every session on
    every query, and the answer would still be a list of chats to look at.
    """
    needle = text.casefold() if text else None
    wanted_projects: set[str | None] | None = None
    if project is not None:
        if project.casefold() in {"none", "-"}:
            wanted_projects = {None}
        else:
            matches = directory.project_named(project)
            wanted_projects = {item.project_id for item in matches}

    out: list[ChatEntry] = []
    for chat in directory.chats:
        if not include_sub_threads and chat.kind is not ChatKind.TOP_LEVEL:
            continue
        if not include_invalid and chat.state in {SessionState.INVALID, SessionState.AMBIGUOUS}:
            continue
        if wanted_projects is not None and chat.project_id not in wanted_projects:
            continue
        if association is not None and chat.association is not association:
            continue
        if since and (chat.timestamp or "") < since:
            continue
        if until and (chat.timestamp or "") > until:
            continue
        if needle:
            haystack = " ".join(part for part in (chat.title, chat.cwd) if part).casefold()
            if needle not in haystack:
                continue
        out.append(chat)
    return tuple(out)


def _projects(state: dict, schema_id: str) -> dict[str, ProjectView]:
    from .guardian_schema import project_root_paths

    entries = state.get("local-projects", {})
    out: dict[str, ProjectView] = {}
    if not isinstance(entries, dict):
        return out
    for project_id, entry in entries.items():
        if not isinstance(project_id, str) or not isinstance(entry, dict):
            continue
        name = entry.get("name")
        out[project_id] = ProjectView(
            project_id,
            name if isinstance(name, str) and name else None,
            project_root_paths(schema_id, entry),
        )
    return out


def _kind(descriptor: SessionDescriptor, meta: dict) -> ChatKind:
    """Chat or spawned sub-thread, decided by what the runtime recorded.

    Either signal alone is enough: a parent means something spawned it, and a
    ``thread_source`` the runtime set to anything but ``user`` says the same in
    its own words. An older session that carries neither is a chat, because
    that is what sessions were before sub-threads existed.
    """
    if descriptor.parent_id:
        return ChatKind.SUB_THREAD
    source = meta.get("thread_source")
    if isinstance(source, str) and source and source not in _TOP_LEVEL_THREAD_SOURCES:
        return ChatKind.SUB_THREAD
    return ChatKind.TOP_LEVEL


def _associate(
    descriptor: SessionDescriptor,
    assignments: dict,
    projects: dict[str, ProjectView],
    schema_id: str,
    *,
    rules: list[PathMappingRule],
    source_machine: str | None,
    target_machine: str | None,
) -> tuple[Association, str | None, tuple[str, ...]]:
    bound = binding_project_id(schema_id, assignments.get(descriptor.session_id))
    if bound is not None:
        if bound in projects:
            return Association.BOUND, bound, ()
        # The state points at a project it does not contain. Report it rather
        # than falling back to the derived answer, which would hide the break.
        return Association.BOUND, bound, ("BINDING_TO_MISSING_PROJECT",)

    derived, codes = _derive(descriptor.cwd, projects)
    if derived is not None:
        return Association.DERIVED, derived, codes

    mapped = _map_cwd(descriptor.cwd, rules, source_machine, target_machine)
    if mapped is not None:
        through_rule, rule_codes = _derive(mapped, projects)
        if through_rule is not None:
            return Association.DERIVED_VIA_MAPPING, through_rule, codes + rule_codes
        codes = codes + rule_codes
    return Association.NONE, None, codes


def _map_cwd(
    cwd: str | None,
    rules: list[PathMappingRule],
    source_machine: str | None,
    target_machine: str | None,
) -> str | None:
    if not cwd or not rules or not source_machine or not target_machine:
        return None
    try:
        return apply_path_mapping(
            cwd, source_machine=source_machine, target_machine=target_machine, rules=rules
        ).target_path
    except PathMappingError:
        return None


def _derive(
    cwd: str | None, projects: dict[str, ProjectView]
) -> tuple[str | None, tuple[str, ...]]:
    """Longest matching root wins; an exact tie is reported, never guessed."""
    if not cwd:
        return None, ()
    best: list[tuple[int, str]] = []
    for project in projects.values():
        for root in project.roots:
            if _under(cwd, root):
                best.append((len(root.rstrip("/\\")), project.project_id))
    if not best:
        return None, ()
    longest = max(length for length, _ in best)
    winners = {project_id for length, project_id in best if length == longest}
    if len(winners) > 1:
        return None, ("AMBIGUOUS_PROJECT_FOR_CWD",)
    return next(iter(winners)), ()


def _under(path: str, root: str) -> bool:
    if not root:
        return False
    lowered = path.casefold()
    prefix = root.rstrip("/\\").casefold()
    return lowered == prefix or lowered.startswith(prefix + "/") or lowered.startswith(prefix + "\\")


def _read_meta(path: Path, max_line_bytes: int) -> dict:
    try:
        with path.open("rb") as handle:
            line = handle.readline(max_line_bytes + 1)
        record = json.loads(line)
    except (OSError, ValueError):
        return {}
    payload = record.get("payload") if isinstance(record, dict) else None
    return payload if isinstance(payload, dict) else {}


def _read_title(path: Path, max_line_bytes: int) -> str | None:
    """The first thing the person typed, or nothing.

    ``event_msg``/``user_message`` is what the runtime records for a real turn,
    so it is preferred. The fallback exists for sessions written before that
    event and skips anything wrapped in a tag, which is how the runtime injects
    context in the user's voice.
    """
    fallback: str | None = None
    try:
        with path.open("rb") as handle:
            for _ in range(TITLE_SCAN_RECORDS):
                line = handle.readline(max_line_bytes + 1)
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    text = _clean(payload.get("message"))
                    if text:
                        return text
                if (
                    fallback is None
                    and record.get("type") == "response_item"
                    and payload.get("role") == "user"
                ):
                    text = _clean(_first_text(payload.get("content")))
                    if text and not text.startswith("<"):
                        fallback = text
    except OSError:
        return None
    return fallback


def _first_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return None


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if len(collapsed) <= TITLE_MAX_CHARS:
        return collapsed
    return collapsed[: TITLE_MAX_CHARS - 3].rstrip() + "..."


def _parse_state(payload: bytes) -> dict:
    try:
        state = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("UNKNOWN_SCHEMA") from exc
    if not isinstance(state, dict):
        raise ValueError("UNKNOWN_SCHEMA")
    return state
