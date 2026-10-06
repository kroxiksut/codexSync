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

A project whose folder does not exist here is still added (as 0.1 did) and
carries ``FOLDER_MISSING_HERE``, so the person can create it or add a mapping.

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
from .version import PRODUCER_VERSION
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

PROJECTS_FORMAT = "codexsync-projects-v1"
PROJECT_MERGE_PLAN_VERSION = 1

_PROJECTS_KEY = "local-projects"
_ORDER_KEY = "project-order"
_PINNED_KEY = "pinned-project-ids"
_BINDINGS_KEY = "thread-project-assignments"
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
    def writes(self) -> bool:
        return bool(self.added) or self.pins_changed or self.order_changed or self.bindings_written > 0

    @property
    def action_count(self) -> int:
        return len(self.added) + int(self.pins_changed) + int(self.order_changed) + self.bindings_written


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
        published_at_utc=now(),
    )


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
        return Publication(
            machine=expected_machine,
            schema_id=str(body["schema_id"]),
            projects={key: dict(value) for key, value in projects.items()},
            order=order,
            pinned=pinned,
            bindings=bindings,
            accepted=accepted,
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
) -> tuple[ProjectMergePlan, dict[str, Any]]:
    """The merge of ``peers`` into this machine's state, and the state it produces.

    ``map_root(peer_machine, root)`` returns the root as it reads here (the same
    value when no mapping applies) or raises ``RootMappingAmbiguous``. Reads
    only; the caller writes the returned state through the global-state
    envelope.
    """
    state = json.loads(state_bytes.decode("utf-8-sig"))
    schema_id = require_carried_schema(state)
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
    for peer in sorted(peers, key=lambda item: item.machine):
        if peer.schema_id != ELECTRON_V2_SCHEMA:
            LOG.warning("projects of %s are in schema %s and are not carried", peer.machine, peer.schema_id)
            continue
        taken[peer.machine] = peer.publication_id
        translate: dict[str, str] = {}
        for peer_id in sorted(peer.projects):
            entry = peer.projects[peer_id]
            item = _merge_one(
                peer, peer_id, entry, projects, schema_id,
                map_root=map_root, folder_exists=folder_exists,
            )
            items.append(item)
            if item.local_project_id is None:
                continue
            translate[peer_id] = item.local_project_id
            if item.kind is MergeKind.ADD:
                added = dict(entry)
                added["rootPaths"] = list(item.roots)
                projects[item.local_project_id] = added

        # Projects both machines know follow the peer's view; projects only
        # this machine has keep their place.
        known_here = set(translate.values())
        peer_order = _unique(translate[pid] for pid in peer.order if pid in translate)
        order = peer_order + [pid for pid in order if pid not in set(peer_order)]
        peer_pins = _unique(translate[pid] for pid in peer.pinned if pid in translate)
        pinned = peer_pins + [pid for pid in pinned if pid not in known_here and pid not in set(peer_pins)]

        for thread_id, peer_project in sorted(peer.bindings.items()):
            local_project = translate.get(peer_project)
            if local_project is None:
                continue
            current = _legacy_binding(bindings.get(thread_id), reverse)
            if current == local_project:
                continue
            bindings[thread_id] = build_binding_value(schema_id, local_project)
            bindings_written += 1

    order = [pid for pid in order if pid in projects]
    pinned = [pid for pid in pinned if pid in projects]
    order_changed = order != original_order
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
    )
    return _with_plan_id(plan), state


def _merge_one(
    peer: Publication,
    peer_id: str,
    entry: dict[str, Any],
    projects: dict[str, Any],
    schema_id: str,
    *,
    map_root: Callable[[str, str], str],
    folder_exists: Callable[[str], bool],
) -> ProjectMergeItem:
    name = entry.get("name") if isinstance(entry.get("name"), str) else ""
    peer_roots = project_root_paths(ELECTRON_V2_SCHEMA, entry)
    try:
        roots = tuple(map_root(peer.machine, root) for root in peer_roots)
    except RootMappingAmbiguous:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, peer_roots,
                                (ROOT_MAPPING_AMBIGUOUS,))
    wanted = {root_key(root) for root in roots}

    if peer_id in projects and isinstance(projects[peer_id], dict):
        local_roots = project_root_paths(schema_id, projects[peer_id])
        codes = () if {root_key(root) for root in local_roots} == wanted else (ROOT_DIFFERS,)
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.MATCHED, peer_id, name, local_roots, codes)
    if not roots:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, (), (NO_ROOT,))

    candidates = sorted(
        local_id for local_id, local in projects.items()
        if isinstance(local, dict)
        and wanted & {root_key(root) for root in project_root_paths(schema_id, local)}
    )
    if len(candidates) == 1:
        local_id = candidates[0]
        return ProjectMergeItem(
            peer.machine, peer_id, MergeKind.MATCHED, local_id, name,
            project_root_paths(schema_id, projects[local_id]),
        )
    if len(candidates) > 1:
        return ProjectMergeItem(peer.machine, peer_id, MergeKind.AMBIGUOUS, None, name, roots,
                                (SEVERAL_LOCAL_CANDIDATES,))
    codes = () if all(folder_exists(root) for root in roots) else (FOLDER_MISSING_HERE,)
    return ProjectMergeItem(peer.machine, peer_id, MergeKind.ADD, peer_id, name, roots, codes)


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
    )


def serialise_state(state: dict[str, Any]) -> bytes:
    """The state as the other global-state writers serialise it."""
    return (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
