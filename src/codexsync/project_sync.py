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
- Nothing is ever removed. A project only this machine has stays, pinned and
  ordered as it was.
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

from dataclasses import dataclass, field
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
from .sidebar_sections import merge_sections, published_sections
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
    #: This machine was told not to carry it.
    SKIPPED = "SKIPPED"
    #: Already here with the peer's root, which names nothing here; its root
    #: is rewritten to the place it now has.
    PLACED = "PLACED"


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
        """Content id: equal for equal project lists, whoever wrote them when."""
        return _digest(self.content)

    def to_json(self) -> dict[str, Any]:
        body = {
            "format": PROJECTS_FORMAT,
            "machine": self.machine,
            **self.content,
            "projectless": list(self.projectless),
            "accepted": dict(sorted(self.accepted.items())),
            "published_at_utc": self.published_at_utc,
            "producer_version": self.producer_version,
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

    @property
    def added(self) -> tuple[ProjectMergeItem, ...]:
        return tuple(item for item in self.items if item.kind is MergeKind.ADD)

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
            bool(self.added) or bool(self.placed) or self.pins_changed or self.order_changed
            or self.bindings_written > 0 or self.bound_by_folder > 0 or self.sections_changed > 0
        )

    @property
    def action_count(self) -> int:
        return (
            len(self.added) + len(self.placed) + int(self.pins_changed) + int(self.order_changed)
            + self.bindings_written + self.bound_by_folder + self.sections_changed
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """
    state = json.loads(state_bytes.decode("utf-8-sig"))
    schema_id = require_carried_schema(state)
    chats = list(chats)
    chat_ids = [thread_id for thread_id, _ in chats if thread_id] if chats else None
    sections_changed = 0
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
    # Every root another machine publishes, with the ids it has there: a root
    # carried as it is names the same folder as that machine's project.
    published: dict[str, set[str]] = {}
    for peer, _ in pending + earlier:
        for peer_id, entry in peer.projects.items():
            for root in project_root_paths(ELECTRON_V2_SCHEMA, entry):
                published.setdefault(root_key(root), set()).add(peer_id)
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
            if fresh or item.kind in {MergeKind.ADD, MergeKind.PLACED, MergeKind.AMBIGUOUS}:
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
            current = _legacy_binding(bindings.get(thread_id), reverse)
            if current == local_project:
                continue
            if not fresh and (peer_project not in settled_now or current is not None):
                continue
            bindings[thread_id] = build_binding_value(schema_id, local_project)
            bindings_written += 1

        sections_changed += merge_sections(state, peer.sections, projects=translate, fresh=fresh, chats=chat_ids)

    kept_apart = set(_projectless(state))
    for peer, _ in translations:
        kept_apart.update(peer.projectless)
    bound_by_folder = _bind_by_folder(
        chats, bindings, projects, schema_id, reverse, translations, frozenset(kept_apart), native,
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
    )
    return _with_plan_id(plan), state


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
        if _legacy_binding(bindings.get(thread_id), reverse) is not None:
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
    }
    return ProjectMergePlan(
        machine=plan.machine,
        items=plan.items,
        taken=plan.taken,
        pins_changed=plan.pins_changed,
        order_changed=plan.order_changed,
        bindings_written=plan.bindings_written,
        state_sha256=plan.state_sha256,
        plan_id=_digest(material)[:32],
        bound_by_folder=plan.bound_by_folder,
        sections_changed=plan.sections_changed,
    )


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
