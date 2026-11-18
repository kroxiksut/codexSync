"""Carrying the project list from one machine to the next (CS-333).

What Codex shows in its sidebar -- the projects, their order, which are
pinned, and which chats an explicit binding puts under a project -- lives in
``.codex-global-state.json``. That file is semantic-owned and is never copied
whole: it also holds this machine's window geometry, its remote-control
installation id and per-host migration markers, which on another machine are
simply wrong. 0.1 copied it whole; 0.2 carries only the project part, and
merges it instead of overwriting.

Each machine publishes **one file of its own** in the coordination folder of
the synced workspace, exactly like a handoff record: the project entries as
Codex wrote them, the order, the pins and the bindings, translated to the
legacy project ids every machine shares. Only that machine writes it, it is
one self-verifying JSON document replaced in a single step, and a copy that
claims another machine or fails its digest is reported and never believed.

The receiving machine merges a peer's publication into its own state:

- A project is matched by id, then by root (through ``[[path_mappings]]``).
  A match keeps this machine's entry untouched; a project matched by neither
  is added as the peer's own entry -- nothing is invented, the fields are the
  ones Codex wrote on the other machine. Two local candidates for one root is
  ``AMBIGUOUS`` and the project is left alone.
- A merge never removes a project because a peer lacks it. A project only
  this machine has stays, pinned and ordered as it was. A project removed in
  Codex is told apart by this machine's own last list (D-039): removed here,
  it is not taken back from the others while the person decides (``[sync]
  removed_projects = "ask"``); once removed on purpose, every other machine
  removes it too, once.
- For projects both machines know, the peer's pins, order and chat bindings
  win -- but only from a publication this machine has not taken yet. Which one
  it took last is recorded by *content id* in this machine's own file, so an
  unchanged peer is not re-applied over a local change made since.

A project whose folder does not exist here is still added, with its chats
(D-034): a project nobody works on here is the usual case, not a fault, so it
carries a code and no warning. Same-system roots arrive as the rules make them
(``FOLDER_MISSING_HERE``). A root written for another operating system that no
rule or answer places arrives *as it is* (``NO_PLACE_HERE``): Codex on Linux
shows such a project and opens its chats (observed 2026-10-10), and nothing
pretends a folder exists. Once the person says where it is here
(`path_places`, D-033) the next merge moves the root there (``PLACED``); told
not to carry it, it is ``SKIPPED``. Two projects of one machine whose
different roots map onto one folder here are ``ROOTS_COLLAPSE`` and neither is
touched: merging them would move the chats of one into the other.

An answer given after a peer's list was taken still takes effect: every peer
list, taken or not, is read for projects that can now be added (``ADD``) or
that were carried with a root naming nothing here and now have a place
(``PLACED``) -- the order, pins and other bindings still come only from a list
not taken yet.

Projects are also rows in ``state_*.sqlite``, which codexSync never writes
(``project_registry.PROVEN_PROJECT_REGISTRY``). On the machine this was built
against every thread's ``project_id`` there is NULL and the table holds the
same names three and four times over from repeated migrations, while the
sidebar follows the JSON exactly -- so the JSON is what is carried.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid
from typing import Any, Callable, Iterable

from .guardian_schema import (
    ELECTRON_V2_SCHEMA,
    LEGACY_V1_SCHEMA,
    binding_project_id,
    build_binding_value,
    detect_state_schema,
    project_root_paths,
)
from .path_mapping import PathMappingRule, path_flavor
from .sidebar_sections import forget_project, merge_sections, published_sections
from .version import PRODUCER_VERSION
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

PROJECTS_FORMAT = "codexsync-projects-v1"
PROJECT_MERGE_PLAN_VERSION = 1

_PROJECTS_KEY = "local-projects"
_ORDER_KEY = "project-order"
_PINNED_KEY = "pinned-project-ids"
_BINDINGS_KEY = "thread-project-assignments"
_PROJECTLESS_KEY = "projectless-thread-ids"
_APP_SERVER_KEY = "app-server-project-id-by-legacy-project-id-by-host"

#: The project's folder does not exist on this machine. Added anyway.
FOLDER_MISSING_HERE = "FOLDER_MISSING_HERE"
#: Same id on both machines, but the roots differ. This machine's root is kept.
ROOT_DIFFERS = "ROOT_DIFFERS"
#: The peer's root could not be mapped unambiguously to this machine.
ROOT_MAPPING_AMBIGUOUS = "ROOT_MAPPING_AMBIGUOUS"
#: Several projects here claim the peer project's root.
SEVERAL_LOCAL_CANDIDATES = "SEVERAL_LOCAL_CANDIDATES"
#: The peer's entry has no root a project can be matched or added by.
NO_ROOT = "NO_ROOT"
#: The root is another operating system's path and nothing says where it is
#: here; the project is carried with that root as it is (D-034).
NO_PLACE_HERE = "NO_PLACE_HERE"
#: Two projects of one machine, with different roots, map onto one folder here.
ROOTS_COLLAPSE = "ROOTS_COLLAPSE"
#: This machine was told not to carry the project (`path_places`).
SKIPPED_HERE = "SKIPPED_HERE"
#: The project was carried with a root naming nothing here; it now follows a place.
ROOT_PLACED = "ROOT_PLACED"
#: Removed in Codex here; the person has not said yet whether on purpose (D-039).
REMOVED_HERE_WAITING = "REMOVED_HERE_WAITING"
#: Removed in Codex here on purpose; the other machines remove it too.
REMOVED_HERE = "REMOVED_HERE"
#: Removed on purpose on another machine; removed here too.
REMOVED_ELSEWHERE = "REMOVED_ELSEWHERE"
#: Another machine removed it, and more than one project here could be it.
REMOVAL_AMBIGUOUS = "REMOVAL_AMBIGUOUS"

#: `[sync] removed_projects` (D-039): what a project removed in Codex on this
#: machine means. ``ask`` holds it back until the person says; ``remove`` takes
#: it off the other machines too; ``bring_back`` puts it back here from them.
REMOVED_PROJECTS_POLICIES: tuple[str, ...] = ("ask", "remove", "bring_back")


class ProjectSyncError(ValueError):
    """A publication that cannot be believed, or a state that cannot be read."""


class ProjectSyncUnsupported(ProjectSyncError):
    """The global state is in a shape whose project list is not carried."""


class MergeKind(str, Enum):
    #: Both machines have it; this machine's entry is kept as it is.
    MATCHED = "MATCHED"
    #: Only the peer has it; its entry is added here.
    ADD = "ADD"
    #: Cannot be told apart safely; left alone.
    AMBIGUOUS = "AMBIGUOUS"
    #: Removed on purpose on another machine; removed here (D-039).
    REMOVE = "REMOVE"
    #: Only the peer has it, but it was removed here or elsewhere: not added.
    HELD = "HELD"
    #: This machine was told not to carry it.
    SKIPPED = "SKIPPED"
    #: Already here with the peer's root, which names nothing here; its root
    #: is rewritten to the place it now has.
    PLACED = "PLACED"


@dataclass(frozen=True, slots=True)
class Removal:
    """A project removed in Codex on the machine that publishes it (D-039).

    ``confirmed`` is the person's (or the policy's) word that it was on
    purpose; only then do the other machines remove it too. Until then the
    machine that lost it only stops taking it back.
    """

    removal_id: str
    project_id: str
    name: str
    #: Its roots as that machine had them.
    roots: tuple[str, ...]
    removed_at_utc: str
    confirmed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "removal_id": self.removal_id, "project_id": self.project_id, "name": self.name,
            "roots": list(self.roots), "removed_at_utc": self.removed_at_utc, "confirmed": self.confirmed,
        }

    @staticmethod
    def from_json(raw: Any) -> "Removal":
        if not isinstance(raw, dict) or not isinstance(raw.get("removal_id"), str) or not raw["removal_id"]:
            raise ProjectSyncError("a removal lacks its id")
        if not isinstance(raw.get("project_id"), str) or not raw["project_id"]:
            raise ProjectSyncError("a removal lacks its project id")
        return Removal(
            raw["removal_id"], raw["project_id"], str(raw.get("name") or ""),
            tuple(str(item) for item in raw.get("roots") or ()),
            str(raw.get("removed_at_utc") or ""), raw.get("confirmed") is True,
        )


def new_removal(
    machine: str, project_id: str, entry: dict[str, Any], *, confirmed: bool, now: str, basis: str,
) -> Removal:
    """``basis`` is the publication the removal was found against: the id stays
    the same between a preview and its apply, and differs for a later removal."""
    name = entry.get("name") if isinstance(entry.get("name"), str) else ""
    removal_id = _digest(["removal", machine, project_id, basis])[:24]
    return Removal(removal_id, project_id, name, project_root_paths(ELECTRON_V2_SCHEMA, entry), now, confirmed)


@dataclass(frozen=True, slots=True)
class Publication:
    """One machine's project list as it published it."""

    machine: str
    schema_id: str
    #: Legacy project id -> the entry exactly as Codex wrote it there.
    projects: dict[str, dict[str, Any]]
    order: tuple[str, ...]
    pinned: tuple[str, ...]
    #: Thread id -> legacy project id.
    bindings: dict[str, str]
    #: Other machine -> content id of its publication this machine has taken.
    accepted: dict[str, str] = field(default_factory=dict)
    #: Threads Codex shows there as "no project" (`projectless-thread-ids`),
    #: whatever folder they are in. Not part of the content id: a chat taken
    #: out of a project there must not make that machine's order and pins win
    #: here again.
    projectless: tuple[str, ...] = ()
    #: The sidebar sections of its one account (`sidebar_sections`, CS-408).
    sections: dict[str, Any] | None = None
    published_at_utc: str = ""
    producer_version: str = PRODUCER_VERSION
    #: Projects removed in Codex here (D-039). Outside the content id, like
    #: ``projectless``: a removal is read from every list, taken or not.
    removed: tuple[Removal, ...] = ()
    #: Removal ids of other machines this machine has already acted on, so a
    #: project created here again later is not removed a second time.
    followed: tuple[str, ...] = ()
    #: Chat items of other machines' sections whose chat was not here yet, so
    #: a later merge places each once it arrives (CS-528). Outside the content id.
    section_waiting: tuple[str, ...] = ()

    @property
    def waiting(self) -> tuple[Removal, ...]:
        """Removals here the person has not answered yet."""
        return tuple(item for item in self.removed if not item.confirmed)

    @property
    def content(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "projects": {key: self.projects[key] for key in sorted(self.projects)},
            "order": list(self.order),
            "pinned": list(self.pinned),
            "bindings": dict(sorted(self.bindings.items())),
            # Only when there are any, so a list without sections keeps its id.
            **({"sections": self.sections} if self.sections else {}),
        }

    @property
    def publication_id(self) -> str:
        """Content id: equal for equal project lists, whoever wrote them when.

        A project counts by what the merge reads of it (id, name, roots):
        Codex bumps `updatedAt` on its own, and hashing it made a peer's order
        and pins win again on every such touch (CS-445).
        """
        content = self.content
        content["projects"] = {
            key: _identity_fields(entry) for key, entry in content["projects"].items()
        }
        return _digest(content)

    def to_json(self) -> dict[str, Any]:
        body = {
            "format": PROJECTS_FORMAT,
            "machine": self.machine,
            **self.content,
            "projectless": list(self.projectless),
            "accepted": dict(sorted(self.accepted.items())),
            "published_at_utc": self.published_at_utc,
            "producer_version": self.producer_version,
            # Only when there are any, so an older reader sees the same file.
            **({"removed": [item.to_json() for item in self.removed]} if self.removed else {}),
            **({"followed": sorted(self.followed)} if self.followed else {}),
            **({"section_waiting": sorted(self.section_waiting)} if self.section_waiting else {}),
        }
        return {**body, "entry_digest": _digest(body)}


@dataclass(frozen=True, slots=True)
class Board:
    publications: dict[str, Publication]
    #: File name -> why it was not believed.
    unreadable: dict[str, str] = field(default_factory=dict)

    def own(self, machine: str) -> Publication | None:
        return self.publications.get(machine)

    def others(self, machine: str) -> list[Publication]:
        return [item for name, item in sorted(self.publications.items()) if name != machine]

    def pending(self, machine: str) -> list[Publication]:
        """Peer publications this machine has not taken in their current content."""
        own = self.publications.get(machine)
        accepted = own.accepted if own is not None else {}
        return [item for item in self.others(machine) if accepted.get(item.machine) != item.publication_id]


@dataclass(frozen=True, slots=True)
class ProjectMergeItem:
    peer_machine: str
    peer_project_id: str
    kind: MergeKind
    #: The project's id on this machine after the merge; ``None`` when left alone.
    local_project_id: str | None
    name: str
    #: The roots as they will read here.
    roots: tuple[str, ...]
    codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectMergePlan:
    machine: str
    items: tuple[ProjectMergeItem, ...]
    #: Peer machine -> publication id this plan takes.
    taken: dict[str, str]
    pins_changed: bool
    order_changed: bool
    bindings_written: int
    #: SHA-256 of the state bytes the plan was built against.
    state_sha256: str
    plan_id: str = ""
    #: Chats bound because only a mapped folder connects them to a project.
    bound_by_folder: int = 0
    #: Sidebar sections added or changed (CS-408).
    sections_changed: int = 0
    #: This machine's removals as they stand after this run (D-039).
    removals: tuple[Removal, ...] = ()
    #: Other machines' removal ids acted on here, including this run's.
    followed: tuple[str, ...] = ()
    #: Section chat items waiting for their chat to arrive here (CS-528).
    section_waiting: tuple[str, ...] = ()

    @property
    def added(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if item.kind is MergeKind.ADD)

    @property
    def removed(self) -> tuple[ProjectMergeItem, ...]:
        """Projects another machine removed on purpose, removed here by this plan."""
        return tuple(item for item in self.items if item.kind is MergeKind.REMOVE)

    @property
    def held(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if item.kind is MergeKind.HELD)

    @property
    def waiting(self) -> tuple[Removal, ...]:
        """Removals here waiting for the person's answer."""
        return tuple(item for item in self.removals if not item.confirmed)

    @property
    def ambiguous(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if item.kind is MergeKind.AMBIGUOUS)

    @property
    def missing_folders(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if FOLDER_MISSING_HERE in item.codes)

    @property
    def unplaced(self) -> tuple[ProjectMergeItem, ...]:
        """Added with the other machine's root as it is: nothing places it here."""
        return tuple(item for item in self.items if NO_PLACE_HERE in item.codes)

    @property
    def placed(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if item.kind is MergeKind.PLACED)

    @property
    def writes(self) -> bool:
        return (
            bool(self.added) or bool(self.placed) or bool(self.removed) or self.pins_changed
            or self.order_changed or self.bindings_written > 0 or self.bound_by_folder > 0
            or self.sections_changed > 0
        )

    @property
    def action_count(self) -> int:
        return (
            len(self.added) + len(self.placed) + len(self.removed) + int(self.pins_changed)
            + int(self.order_changed) + self.bindings_written + self.bound_by_folder + self.sections_changed
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity_fields(entry: Any) -> Any:
    """The part of a project record the merge consumes (CS-445)."""
    if not isinstance(entry, dict):
        return entry
    return {key: entry[key] for key in ("id", "name", "rootPaths") if key in entry}


def _digest(body: Any) -> str:
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- reading this machine's state ------------------------------------------------


def require_carried_schema(state: Any) -> str:
    """The schema of ``state`` if its project list can be carried, else raise."""
    if not isinstance(state, dict):
        raise ProjectSyncUnsupported("the global state is not a JSON object")
    schema_id = detect_state_schema(state)
    if schema_id == LEGACY_V1_SCHEMA and _holds_no_project_data(state):
        # Nothing in an empty list tells the two shapes apart -- the legacy
        # adapter claims it only because the Electron one has no root key to
        # see. Such a state commits to no shape, so the Electron shape every
        # observed machine writes is used; the result is validated after the
        # merge and an entry with `rootPaths` can only be read one way.
        return ELECTRON_V2_SCHEMA
    if schema_id != ELECTRON_V2_SCHEMA:
        # The legacy shape predates every machine this was observed on; carrying
        # it would be a guess about entries nobody has seen written.
        raise ProjectSyncUnsupported(
            f"the project list is carried only for the desktop schema, not {schema_id or 'an unknown one'}"
        )
    return schema_id


def _holds_no_project_data(state: dict[str, Any]) -> bool:
    bindings = state.get(_BINDINGS_KEY) or {}
    return (
        not state.get(_PROJECTS_KEY)
        and not state.get("project-id-migrations")
        and isinstance(bindings, dict)
        and all(value is None for value in bindings.values())
    )


def _app_server_to_legacy(state: dict[str, Any]) -> dict[str, str]:
    reverse: dict[str, str] = {}
    by_host = state.get(_APP_SERVER_KEY) or {}
    if isinstance(by_host, dict):
        for mapping in by_host.values():
            if isinstance(mapping, dict):
                for legacy_id, app_server_id in mapping.items():
                    if isinstance(legacy_id, str) and isinstance(app_server_id, str):
                        reverse.setdefault(app_server_id, legacy_id)
    return reverse


def _legacy_binding(value: Any, reverse: dict[str, str]) -> str | None:
    """The legacy project id a binding points at, whatever kind it is written as."""
    if not isinstance(value, dict):
        return None
    project_id = binding_project_id(ELECTRON_V2_SCHEMA, value)
    if project_id is None:
        return None
    kind = value.get("projectKind")
    if kind == "local":
        return project_id
    if kind == "app-server":
        # App-server ids are per host; only the legacy id means the same
        # project on another machine.
        return reverse.get(project_id)
    return None


def publication_from_state(
    state: dict[str, Any], machine: str, *, accepted: dict[str, str] | None = None,
    now: Callable[[], str] = now_utc,
    removed: Iterable[Removal] = (), followed: Iterable[str] = (), section_waiting: Iterable[str] = (),
) -> Publication:
    schema_id = require_carried_schema(state)
    projects = {
        str(key): dict(value)
        for key, value in dict(state.get(_PROJECTS_KEY) or {}).items()
        if isinstance(value, dict)
    }
    order = tuple(item for item in state.get(_ORDER_KEY) or () if isinstance(item, str) and item in projects)
    pinned = tuple(
        item for item in state.get(_PINNED_KEY) or () if isinstance(item, str) and item in projects
    )
    reverse = _app_server_to_legacy(state)
    bindings: dict[str, str] = {}
    for thread_id, value in dict(state.get(_BINDINGS_KEY) or {}).items():
        legacy = _legacy_binding(value, reverse)
        if isinstance(thread_id, str) and legacy in projects:
            bindings[thread_id] = legacy
    return Publication(
        machine=machine,
        schema_id=schema_id,
        projects=projects,
        order=order,
        pinned=pinned,
        bindings=bindings,
        accepted=dict(accepted or {}),
        projectless=_projectless(state),
        sections=published_sections(state),
        published_at_utc=now(),
        removed=tuple(sorted(removed, key=lambda item: item.removal_id)),
        followed=tuple(sorted(set(followed))),
        section_waiting=tuple(sorted(set(section_waiting))),
    )


def _projectless(state: dict[str, Any]) -> tuple[str, ...]:
    value = state.get(_PROJECTLESS_KEY)
    if not isinstance(value, list):
        return ()
    return tuple(sorted({item for item in value if isinstance(item, str) and item}))


# --- the board in the workspace --------------------------------------------------


def publication_path(root: Path, machine: str) -> Path:
    return root / f"{machine}.json"


def _parse(raw: Any, *, expected_machine: str) -> Publication:
    if not isinstance(raw, dict):
        raise ProjectSyncError("not a JSON object")
    if raw.get("format") != PROJECTS_FORMAT:
        raise ProjectSyncError(f"unknown format {raw.get('format')!r}")
    declared = raw.get("entry_digest")
    body = {key: value for key, value in raw.items() if key != "entry_digest"}
    if declared != _digest(body):
        # Half-delivered by the cloud client, or edited by hand.
        raise ProjectSyncError("digest does not match the contents")
    if body.get("machine") != expected_machine:
        raise ProjectSyncError(f"file of {expected_machine!r} claims machine {body.get('machine')!r}")
    try:
        projects = body["projects"]
        if not isinstance(projects, dict) or not all(
            isinstance(key, str) and isinstance(value, dict) for key, value in projects.items()
        ):
            raise ProjectSyncError("projects must be an object of objects")
        order = tuple(str(item) for item in body["order"])
        pinned = tuple(str(item) for item in body["pinned"])
        bindings = {str(key): str(value) for key, value in dict(body["bindings"]).items()}
        accepted = {str(key): str(value) for key, value in dict(body.get("accepted") or {}).items()}
        # Absent before CS-406.
        projectless = tuple(sorted({str(item) for item in body.get("projectless") or ()}))
        # Absent before CS-408.
        sections = body.get("sections") if isinstance(body.get("sections"), dict) else None
        # Absent before D-039.
        removed = tuple(Removal.from_json(item) for item in body.get("removed") or ())
        followed = tuple(sorted({str(item) for item in body.get("followed") or ()}))
        section_waiting = tuple(sorted({str(item) for item in body.get("section_waiting") or ()}))
        return Publication(
            machine=expected_machine,
            schema_id=str(body["schema_id"]),
            projects={key: dict(value) for key, value in projects.items()},
            order=order,
            pinned=pinned,
            bindings=bindings,
            accepted=accepted,
            projectless=projectless,
            sections=sections,
            published_at_utc=str(body.get("published_at_utc", "")),
            producer_version=str(body.get("producer_version", "")),
            removed=removed,
            followed=followed,
            section_waiting=section_waiting,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ProjectSyncError):
            raise
        raise ProjectSyncError(f"malformed field: {exc}") from exc


def read_board(root: Path) -> Board:
    """Every machine's publication. Reads only; a bad file is reported, not fatal."""
    publications: dict[str, Publication] = {}
    unreadable: dict[str, str] = {}
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return Board({})
    except OSError as exc:
        return Board({}, {str(root): str(exc)})
    for entry in entries:
        if entry.suffix != ".json" or entry.name.startswith(".") or not entry.is_file():
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
            publications[entry.stem] = _parse(raw, expected_machine=entry.stem)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProjectSyncError) as exc:
            unreadable[entry.name] = str(exc)
    return Board(publications, unreadable)


def write_publication(root: Path, publication: Publication) -> Path:
    """Replace this machine's file in one step, staged beside it."""
    root.mkdir(parents=True, exist_ok=True)
    target = publication_path(root, publication.machine)
    staging = root / f".{publication.machine}.{uuid.uuid4().hex}.codexsync.tmp"
    payload = json.dumps(publication.to_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with staging.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(staging, target)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                LOG.warning("could not remove project publication staging file %s", staging)
    return target


# --- the merge -------------------------------------------------------------------


class RootMappingAmbiguous(Exception):
    """Raised by a root mapper that found more than one answer."""


class RootNotPlaced(Exception):
    """Raised by a root mapper for another system's path no rule or answer places here."""


def root_key(path: str) -> str:
    """A root as compared: separators unified, case folded on Windows paths."""
    value = path.strip()
    if value.startswith("\\\\?\\"):
        value = value[4:]
    windows = len(value) >= 2 and value[1] == ":" or "\\" in value
    if windows:
        value = value.replace("/", "\\").rstrip("\\").casefold()
        return value + "\\" if len(value) == 2 else value
    return value.rstrip("/") or "/"


def build_project_merge(
    state_bytes: bytes,
    peers: Iterable[Publication],
    *,
    machine: str,
    map_root: Callable[[str, str], str],
    folder_exists: Callable[[str], bool],
    taken_before: Iterable[Publication] = (),
    skipped: frozenset[str] = frozenset(),
    chats: Iterable[tuple[str, str | None]] = (),
    native: Callable[[str], bool] = lambda root: True,
    own: Publication | None = None,
    removed_projects: str = "bring_back",
    now: Callable[[], str] = now_utc,
) -> tuple[ProjectMergePlan, dict[str, Any]]:
    """The merge of ``peers`` into this machine's state, and the state it produces.

    ``map_root(peer_machine, root)`` returns the root as it reads here (the same
    value when no mapping applies), or raises ``RootMappingAmbiguous`` or
    ``RootNotPlaced``. ``taken_before`` are peer lists already taken: only a
    project they hold that can now be added or placed is acted on, with its
    own chats' bindings -- never their order or pins. ``skipped`` are peer
    project ids this machine was told not to carry. ``chats`` are this
    machine's chats as ``(thread id, cwd)``: one with no binding whose folder
    reaches a project here only through a mapping is bound to it
    (`_bind_by_folder`), unless Codex shows it as "no project" here or on a
    peer (CS-406). ``native(root)`` says whether a root is written the way
    this machine's system writes paths; Codex is not relied on to place a chat
    under one that is not. Reads only; the caller writes the returned state
    through the global-state envelope.

    ``own`` is this machine's last publication: a project it listed that the
    state no longer holds was removed in Codex here, and ``removed_projects``
    says what that means (D-039, `_removals`). A removal another machine
    confirmed is applied here once.
    """
    state = json.loads(state_bytes.decode("utf-8-sig"))
    schema_id = require_carried_schema(state)
    chats = list(chats)
    chat_ids = [thread_id for thread_id, _ in chats if thread_id] if chats else None
    sections_changed = 0
    section_waiting: set[str] = set()
    projects: dict[str, Any] = state.setdefault(_PROJECTS_KEY, {})
    original_order = [item for item in state.get(_ORDER_KEY) or [] if isinstance(item, str)]
    original_pinned = [item for item in state.get(_PINNED_KEY) or [] if isinstance(item, str)]
    order = list(original_order)
    pinned = list(original_pinned)
    bindings: dict[str, Any] = state.setdefault(_BINDINGS_KEY, {})
    reverse = _app_server_to_legacy(state)

    items: list[ProjectMergeItem] = []
    taken: dict[str, str] = {}
    bindings_written = 0
    pending = [(peer, True) for peer in sorted(peers, key=lambda item: item.machine)]
    pending_machines = {peer.machine for peer, _ in pending}
    earlier = [
        (peer, False) for peer in sorted(taken_before, key=lambda item: item.machine)
        if peer.machine not in pending_machines and peer.machine != machine
    ]
    translations: list[tuple[Publication, dict[str, str]]] = []
    # Chats a person took out of every project here (CS-444).
    apart_here = frozenset(_projectless(state))
    # Every root another machine publishes, with the ids it has there: a root
    # carried as it is names the same folder as that machine's project.
    published: dict[str, set[str]] = {}
    for peer, _ in pending + earlier:
        for peer_id, entry in peer.projects.items():
            for root in project_root_paths(ELECTRON_V2_SCHEMA, entry):
                published.setdefault(root_key(root), set()).add(peer_id)
    removals, followed, in_effect = _removals(
        state, projects, bindings, reverse, schema_id, [peer for peer, _ in pending + earlier],
        machine=machine, own=own, policy=removed_projects, map_root=map_root, now=now, items=items,
    )
    for peer, fresh in pending + earlier:
        if peer.schema_id != ELECTRON_V2_SCHEMA:
            if fresh:
                LOG.warning("projects of %s are in schema %s and are not carried", peer.machine, peer.schema_id)
            continue
        if fresh:
            taken[peer.machine] = peer.publication_id
        collapsing = _collapsing_ids(peer, map_root)
        translate: dict[str, str] = {}
        settled_now: set[str] = set()
        for peer_id in sorted(peer.projects):
            entry = peer.projects[peer_id]
            item = _merge_one(
                peer, peer_id, entry, projects, schema_id,
                map_root=map_root, folder_exists=folder_exists,
                collapsing=collapsing, skipped=skipped, published=published,
            )
            if item.kind is MergeKind.ADD:
                code = _held_by(peer, peer_id, entry, in_effect, machine=machine, map_root=map_root)
                if code is not None:
                    item = ProjectMergeItem(peer.machine, peer_id, MergeKind.HELD, None, item.name, item.roots, (code,))
            if fresh or item.kind in {MergeKind.ADD, MergeKind.PLACED, MergeKind.AMBIGUOUS, MergeKind.HELD}:
                items.append(item)
            if item.local_project_id is None:
                continue
            translate[peer_id] = item.local_project_id
            if item.kind is MergeKind.ADD:
                added = dict(entry)
                added["rootPaths"] = list(item.roots)
                projects[item.local_project_id] = added
                settled_now.add(peer_id)
            elif item.kind is MergeKind.PLACED:
                placed = dict(projects[item.local_project_id])
                placed["rootPaths"] = list(item.roots)
                projects[item.local_project_id] = placed
                settled_now.add(peer_id)

        translations.append((peer, translate))
        if fresh:
            # Projects both machines know follow the peer's view; projects only
            # this machine has keep their place.
            known_here = set(translate.values())
            peer_order = _unique(translate[pid] for pid in peer.order if pid in translate)
            order = peer_order + [pid for pid in order if pid not in set(peer_order)]
            peer_pins = _unique(translate[pid] for pid in peer.pinned if pid in translate)
            pinned = peer_pins + [pid for pid in pinned if pid not in known_here and pid not in set(peer_pins)]
        else:
            # A list already taken only adds what is new here, at the end.
            order = order + [translate[pid] for pid in sorted(settled_now) if translate[pid] not in order]

        for thread_id, peer_project in sorted(peer.bindings.items()):
            local_project = translate.get(peer_project)
            if local_project is None:
                continue
            # A chat kept out of every project, here or there, is never bound
            # again by a peer's list (CS-444; CS-406 for the folder rule).
            if thread_id in apart_here or thread_id in peer.projectless:
                continue
            current = _legacy_binding(bindings.get(thread_id), reverse)
            if current == local_project:
                continue
            # A binding of a kind not carried (a remote project, CS-526) is
            # still a binding: only a fresh list of a project both know moves it.
            if not fresh and (peer_project not in settled_now or bindings.get(thread_id) is not None):
                continue
            bindings[thread_id] = build_binding_value(schema_id, local_project)
            bindings_written += 1

        sections_changed += merge_sections(
            state, peer.sections, projects=translate, fresh=fresh, chats=chat_ids,
            deferred=own.section_waiting if own is not None else (), waiting=section_waiting,
        )

    kept_apart = set(_projectless(state))
    for peer, _ in translations:
        kept_apart.update(peer.projectless)
    bound_by_folder = _bind_by_folder(
        chats, bindings, projects, schema_id, reverse, translations, frozenset(kept_apart), native,
    )

    # A removal here ends once its folder is a project here again, even one
    # this very merge added (made again elsewhere after the removal).
    present = [
        _root_keys(project_root_paths(schema_id, entry)) for entry in projects.values() if isinstance(entry, dict)
    ]
    removals = tuple(
        item for item in removals
        if item.project_id not in projects and not any(_root_keys(item.roots) & keys for keys in present)
    )
    order = [pid for pid in order if pid in projects]
    pinned = [pid for pid in pinned if pid in projects]
    # A state that never held a project has no order key at all; one that
    # holds projects without it is in no shape any adapter accepts.
    order_changed = order != original_order or (_ORDER_KEY not in state and bool(projects))
    pins_changed = pinned != original_pinned
    if order_changed:
        state[_ORDER_KEY] = order
    if pins_changed:
        state[_PINNED_KEY] = pinned
    plan = ProjectMergePlan(
        machine=machine,
        items=tuple(items),
        taken=taken,
        pins_changed=pins_changed,
        order_changed=order_changed,
        bindings_written=bindings_written,
        state_sha256=hashlib.sha256(state_bytes).hexdigest(),
        bound_by_folder=bound_by_folder,
        sections_changed=sections_changed,
        removals=removals,
        followed=followed,
        section_waiting=tuple(sorted(section_waiting)),
    )
    return _with_plan_id(plan), state


def _root_keys(roots: Iterable[str]) -> set[str]:
    return {root_key(root) for root in roots}


def _keys_here(roots: Iterable[str], machine: str | None, map_root: Callable[[str, str], str]) -> set[str]:
    """Another machine's roots as they read here; ``machine=None`` for this one's own."""
    keys: set[str] = set()
    for root in roots:
        if machine is None:
            keys.add(root_key(root))
            continue
        try:
            keys.add(root_key(map_root(machine, root)))
        except (RootMappingAmbiguous, RootNotPlaced):
            # Carried as it is (D-034), so compared as it is.
            keys.add(root_key(root))
    return keys


def _removals(
    state: dict[str, Any],
    projects: dict[str, Any],
    bindings: dict[str, Any],
    reverse: dict[str, str],
    schema_id: str,
    peers: list[Publication],
    *,
    machine: str,
    own: Publication | None,
    policy: str,
    map_root: Callable[[str, str], str],
    now: Callable[[], str],
    items: list[ProjectMergeItem],
) -> tuple[tuple[Removal, ...], tuple[str, ...], list[tuple[str | None, Removal]]]:
    """Projects removed in Codex, here or on purpose elsewhere (D-039).

    First the other machines' confirmed removals not acted on yet: the project
    they name (by id, else the one project here under the same folder) is
    removed from ``state`` with its bindings, pins, order and sidebar entries,
    and the removal is recorded as followed -- once, so a project created
    here again later stays. Then this machine's own: a project the last
    publication listed that is neither here nor under its folder here, and
    that no removal elsewhere explains, was removed in Codex here. ``ask``
    records it waiting, ``remove`` confirmed, ``bring_back`` not at all (the
    merge then takes it back from the other machines). A removal whose project
    is here again is dropped.

    Returns this machine's removals, the followed ids, and every removal in
    effect as ``(owner or None for this machine, removal)`` for `_held_by`.
    """
    followed = set(own.followed) if own is not None else set()
    live = {
        pid: _root_keys(project_root_paths(schema_id, entry))
        for pid, entry in projects.items() if isinstance(entry, dict)
    }
    elsewhere = sorted(
        ((peer.machine, removal) for peer in peers if peer.machine != machine
         for removal in peer.removed if removal.confirmed),
        key=lambda pair: (pair[0], pair[1].removal_id),
    )
    for owner, removal in elsewhere:
        if removal.removal_id in followed:
            continue
        wanted = _keys_here(removal.roots, owner, map_root)
        if removal.project_id in live:
            candidates = [removal.project_id]
        else:
            candidates = sorted(pid for pid, keys in live.items() if keys & wanted)
        if len(candidates) > 1:
            # Not followed: asked again once only one project is left that could be it.
            items.append(ProjectMergeItem(owner, removal.project_id, MergeKind.AMBIGUOUS, None, removal.name,
                                          removal.roots, (REMOVAL_AMBIGUOUS,)))
            continue
        followed.add(removal.removal_id)
        if not candidates:
            continue
        local = candidates[0]
        roots_here = project_root_paths(schema_id, projects[local])
        _forget_project(state, projects, bindings, reverse, local)
        live.pop(local, None)
        items.append(ProjectMergeItem(owner, removal.project_id, MergeKind.REMOVE, local, removal.name,
                                      roots_here, (REMOVED_ELSEWHERE,)))

    def explained_elsewhere(project_id: str, keys: set[str]) -> bool:
        return any(
            removal.project_id == project_id or keys & _keys_here(removal.roots, owner, map_root)
            for owner, removal in elsewhere
        )

    def here_again(project_id: str, keys: set[str]) -> bool:
        return project_id in live or any(keys & other for other in live.values())

    mine: dict[str, Removal] = {}
    if policy != "bring_back":
        for removal in own.removed if own is not None else ():
            if here_again(removal.project_id, _root_keys(removal.roots)):
                continue
            if policy == "remove" and not removal.confirmed:
                removal = Removal(removal.removal_id, removal.project_id, removal.name, removal.roots,
                                  removal.removed_at_utc, True)
            mine[removal.project_id] = removal
        stamp = now()
        for project_id, entry in sorted((own.projects if own is not None else {}).items()):
            keys = _root_keys(project_root_paths(ELECTRON_V2_SCHEMA, entry))
            if project_id in mine or here_again(project_id, keys) or explained_elsewhere(project_id, keys):
                continue
            mine[project_id] = new_removal(
                machine, project_id, entry, confirmed=policy == "remove", now=stamp,
                basis=own.publication_id if own is not None else "",
            )
    in_effect: list[tuple[str | None, Removal]] = [(None, removal) for removal in mine.values()]
    in_effect += list(elsewhere)
    return (
        tuple(sorted(mine.values(), key=lambda item: item.removal_id)),
        tuple(sorted(followed)),
        in_effect,
    )


def _forget_project(
    state: dict[str, Any], projects: dict[str, Any], bindings: dict[str, Any], reverse: dict[str, str],
    project_id: str,
) -> None:
    """Take one project out of the state as Codex leaves it after a removal.

    Its entry, the chat bindings pointing at it (those chats then sit where
    their folder puts them), and its sidebar entries; pins and order are
    filtered at the end of the merge. The app-server id map and Codex's own
    SQLite rows are left alone: Codex was seen keeping a removed project out
    of its list with both still there (Linux, 2026-10-26).
    """
    projects.pop(project_id, None)
    for thread_id, value in list(bindings.items()):
        if _legacy_binding(value, reverse) == project_id:
            del bindings[thread_id]
    forget_project(state, project_id)


def _held_by(
    peer: Publication,
    peer_id: str,
    entry: dict[str, Any],
    in_effect: list[tuple[str | None, Removal]],
    *,
    machine: str,
    map_root: Callable[[str, str], str],
) -> str | None:
    """Why a project only ``peer`` has is not added here, or ``None``.

    A peer that already acted on a removal and still has a project there made
    it again after the removal, so its list is believed.
    """
    keys: set[str] | None = None
    for owner, removal in in_effect:
        if owner == peer.machine or removal.removal_id in peer.followed:
            continue
        if peer_id != removal.project_id:
            if keys is None:
                keys = _keys_here(project_root_paths(ELECTRON_V2_SCHEMA, entry), peer.machine, map_root)
            if not keys & _keys_here(removal.roots, owner, map_root):
                continue
        if owner is not None:
            return REMOVED_ELSEWHERE
        return REMOVED_HERE if removal.confirmed else REMOVED_HERE_WAITING
    return None


def _bind_by_folder(
    chats: Iterable[tuple[str, str | None]],
    bindings: dict[str, Any],
    projects: dict[str, Any],
    schema_id: str,
    reverse: dict[str, str],
    translations: list[tuple[Publication, dict[str, str]]],
    kept_apart: frozenset[str] = frozenset(),
    native: Callable[[str], bool] = lambda root: True,
) -> int:
    """Bind the chats that reach a project only by a folder another machine names.

    Codex puts most chats under a project by their folder alone: on the
    machine this was built against 93 chats had no binding at all. A chat
    written on another machine keeps that machine's folder, which falls under
    no project here, so it lands under none (observed on Linux 2026-10-09:
    projects arrived, their chats did not). Such a chat is placed the way
    Codex placed it *there* -- under the peer project whose root holds its
    folder -- and bound to that project's counterpart here (same id, or the one
    the merge matched it to). A project the merge left alone (two folders
    mapped onto one, skipped) passes on no chat. A chat with a
    binding, or whose folder already falls under a project here, is left
    alone; two answers are no answer.

    A chat in ``kept_apart`` is one a person took out of every project (Codex
    lists it in `projectless-thread-ids`) here or on a peer. Its folder may
    well be a project's folder -- that is exactly the case the list exists
    for -- so it stays without a project until someone binds it (CS-406).

    Only a project whose root is written this system's way counts as placing
    a chat by itself: one carried with another system's root (D-034) holds
    that machine's folder, and whether Codex matches a chat's folder against
    it is not something to rely on, so its chats are bound.
    """
    placing = {
        project_id: entry for project_id, entry in projects.items()
        if isinstance(entry, dict) and all(native(root) for root in project_root_paths(schema_id, entry))
    }
    written = 0
    for thread_id, cwd in sorted(set(chats), key=lambda item: item[0]):
        if not thread_id or not cwd or thread_id in kept_apart:
            continue
        if bindings.get(thread_id) is not None:
            # Any binding, of whatever kind, is Codex's own answer (CS-526).
            continue
        if _project_under(cwd, placing, schema_id, allow_tie=True) is not False:
            continue
        found: set[str] = set()
        for peer, translate in translations:
            there = _project_under(cwd, peer.projects, ELECTRON_V2_SCHEMA)
            if isinstance(there, str) and there in translate:
                found.add(translate[there])
        if len(found) == 1:
            bindings[thread_id] = build_binding_value(schema_id, found.pop())
            written += 1
    return written


def _project_under(path: str, projects: dict[str, Any], schema_id: str, *, allow_tie: bool = False):
    """The project whose root holds ``path`` (longest root wins), else ``False``.

    A tie between two roots is ``None`` -- not an answer, but with
    ``allow_tie`` still a sign that Codex places the chat by itself.
    """
    key = root_key(path)
    best: dict[str, int] = {}
    for project_id, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        for root in project_root_paths(schema_id, entry):
            prefix = root_key(root)
            separator = "\\" if "\\" in prefix else "/"
            if key == prefix or key.startswith(prefix.rstrip("\\/") + separator):
                best[project_id] = max(best.get(project_id, 0), len(prefix))
    if not best:
        return False
    longest = max(best.values())
    winners = [project_id for project_id, length in best.items() if length == longest]
    if len(winners) == 1:
        return winners[0]
    return True if allow_tie else None


def _mapped_roots(peer: Publication, entry: dict[str, Any], map_root: Callable[[str, str], str]):
    """The entry's roots, those roots as they read here, and what the mapper raised."""
    peer_roots = project_root_paths(ELECTRON_V2_SCHEMA, entry)
    try:
        return peer_roots, tuple(map_root(peer.machine, root) for root in peer_roots), None
    except (RootMappingAmbiguous, RootNotPlaced) as exc:
        return peer_roots, None, exc


def _collapsing_ids(peer: Publication, map_root: Callable[[str, str], str]) -> frozenset[str]:
    """Peer projects whose *different* roots map onto one folder here (CS-402).

    The same folder under two ids on one machine is still one folder; two
    folders that only the mapping makes one are two projects, and merging them
    would put the chats of both under one.
    """
    by_target: dict[str, dict[str, set[str]]] = {}
    for peer_id, entry in peer.projects.items():
        peer_roots, roots, _ = _mapped_roots(peer, entry, map_root)
        for raw, mapped in zip(peer_roots, roots or ()):
            by_target.setdefault(root_key(mapped), {}).setdefault(root_key(raw), set()).add(peer_id)
    collapsing: set[str] = set()
    for sources in by_target.values():
        if len(sources) > 1:
            for ids in sources.values():
                collapsing |= ids
    return frozenset(collapsing)


def _merge_one(
    peer: Publication,
    peer_id: str,
    entry: dict[str, Any],
    projects: dict[str, Any],
    schema_id: str,
    *,
    map_root: Callable[[str, str], str],
    folder_exists: Callable[[str], bool],
    collapsing: frozenset[str] = frozenset(),
    skipped: frozenset[str] = frozenset(),
    published: dict[str, set[str]] | None = None,
) -> ProjectMergeItem:
    name = entry.get("name") if isinstance(entry.get("name"), str) else ""
    peer_roots, roots, failure = _mapped_roots(peer, entry, map_root)

    if peer_id in projects and isinstance(projects[peer_id], dict):
        local_roots = project_root_paths(schema_id, projects[peer_id])
        local_keys = {root_key(root) for root in local_roots}
        if roots is not None and _follows_a_place(local_roots, peer_roots, roots, folder_exists):
            return ProjectMergeItem(peer.machine, peer_id, MergeKind.PLACED, peer_id, name, roots, (ROOT_PLACED,))
        same = local_keys == {root_key(root) for root in (roots if roots is not None else peer_roots)}
        codes = () if same else (ROOT_DIFFERS,)
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.MATCHED, peer_id, name, local_roots, codes)
    if peer_id in skipped:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.SKIPPED, None, name, peer_roots, (SKIPPED_HERE,))
    carried_as_is: tuple[str, ...] = ()
    if isinstance(failure, RootNotPlaced):
        # Carried with the other machine's root as it is (D-034).
        roots, failure, carried_as_is = peer_roots, None, (NO_PLACE_HERE,)
    if failure is not None:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, peer_roots,
                                (ROOT_MAPPING_AMBIGUOUS,))
    if not roots:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, (), (NO_ROOT,))
    if peer_id in collapsing:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, roots, (ROOTS_COLLAPSE,))

    wanted = {root_key(root) for root in roots}
    found = {
        local_id for local_id, local in projects.items()
        if isinstance(local, dict)
        and wanted & {root_key(root) for root in project_root_paths(schema_id, local)}
    }
    if carried_as_is:
        # Two machines of one system can hold one folder under two ids. When
        # another machine's project with this very root is already here --
        # placed, so under this machine's root -- it is that project, not a
        # second one with the other system's path (seen on Linux 2026-10-10:
        # seven duplicates).
        for key in wanted:
            found |= {
                other_id for other_id in (published or {}).get(key, ())
                if other_id != peer_id and isinstance(projects.get(other_id), dict)
            }
    candidates = sorted(found)
    if len(candidates) == 1:
        local_id = candidates[0]
        return ProjectMergeItem(
            peer.machine, peer_id, MergeKind.MATCHED, local_id, name,
            project_root_paths(schema_id, projects[local_id]),
        )
    if len(candidates) > 1:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, roots,
                                (SEVERAL_LOCAL_CANDIDATES,))
    if carried_as_is:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.ADD, peer_id, name, roots, carried_as_is)
    codes = () if all(folder_exists(root) for root in roots) else (FOLDER_MISSING_HERE,)
    return ProjectMergeItem(peer.machine, peer_id, MergeKind.ADD, peer_id, name, roots, codes)


def _follows_a_place(
    local_roots: tuple[str, ...],
    peer_roots: tuple[str, ...],
    roots: tuple[str, ...],
    folder_exists: Callable[[str], bool],
) -> bool:
    """A project carried with the peer's own root, which names nothing here, now placed.

    Only that exact case: the root here is still the peer's, no folder answers
    to it, the mapping now gives another root, and that folder exists. A root
    a person or Codex chose here is never touched.
    """
    if not local_roots or {root_key(root) for root in local_roots} != {root_key(root) for root in peer_roots}:
        return False
    if any(folder_exists(root) for root in local_roots):
        return False
    if {root_key(root) for root in roots} == {root_key(root) for root in local_roots}:
        return False
    return bool(roots) and all(folder_exists(root) for root in roots)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _with_plan_id(plan: ProjectMergePlan) -> ProjectMergePlan:
    material = {
        "version": PROJECT_MERGE_PLAN_VERSION,
        "machine": plan.machine,
        "state_sha256": plan.state_sha256,
        "taken": dict(sorted(plan.taken.items())),
        "items": [
            [item.peer_machine, item.peer_project_id, item.kind.value, item.local_project_id,
             list(item.roots), list(item.codes)]
            for item in plan.items
        ],
        "pins_changed": plan.pins_changed,
        "order_changed": plan.order_changed,
        "bindings_written": plan.bindings_written,
        "bound_by_folder": plan.bound_by_folder,
        **({"sections_changed": plan.sections_changed} if plan.sections_changed else {}),
        # Only when there are any, so every earlier plan keeps its id (D-039).
        **({"removals": [[item.removal_id, item.confirmed] for item in plan.removals]} if plan.removals else {}),
        **({"followed": list(plan.followed)} if plan.followed else {}),
    }
    return dataclass_replace(plan, plan_id=_digest(material)[:32])


def serialise_state(state: dict[str, Any]) -> bytes:
    """The state as the other global-state writers serialise it."""
    return (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def project_root_rules(
    state: dict[str, Any],
    publications: Iterable[Publication],
    machine: str,
    *,
    case_sensitive: Callable[[str], bool | None] = lambda _machine: None,
) -> list[PathMappingRule]:
    """Rules the project lists already prove: one project, its root there and here (D-033).

    A carried project keeps its id on every machine, and each machine
    publishes where it keeps it, so for every project both hold under one root
    each, the pair is a fact about where that folder went -- learned once,
    used by chats and the working set too, for any number of machines. A
    project whose roots agree adds nothing.
    """
    try:
        schema_id = require_carried_schema(state)
    except ProjectSyncUnsupported:
        return []
    local = state.get(_PROJECTS_KEY) or {}
    rules: list[PathMappingRule] = []
    for peer in publications:
        if peer.machine == machine:
            continue
        for project_id, entry in sorted(peer.projects.items()):
            mine = local.get(project_id)
            if not isinstance(mine, dict):
                continue
            there = project_root_paths(ELECTRON_V2_SCHEMA, entry)
            here = project_root_paths(schema_id, mine)
            if len(there) != 1 or len(here) != 1 or root_key(there[0]) == root_key(here[0]):
                continue
            if path_flavor(there[0]) is None or path_flavor(here[0]) is None:
                # A rule that cannot be parsed would fail every mapping, not just this one.
                continue
            digest = hashlib.sha256("\0".join((peer.machine, project_id, there[0], here[0])).encode("utf-8"))
            rules.append(PathMappingRule(
                rule_id=f"project-{digest.hexdigest()[:16]}",
                source_machine=peer.machine,
                target_machine=machine,
                source_prefix=there[0],
                target_prefix=here[0],
                case_sensitive=case_sensitive(peer.machine),
            ))
    return rules
