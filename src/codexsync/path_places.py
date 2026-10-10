"""Where another machine's project folders are on this one, remembered (D-033).

0.1 assumed every machine kept a project at the same path. Between Windows,
Linux and macOS that is never true, and a `[[path_mappings]]` rule is written
by hand, for one direction, in one machine's config. This module is the
memory a person builds by answering "where is this project here?" once.

Each machine writes one self-verifying file of its own beside the other
coordination boards (`peer_board`): the operating system it runs, every place
it was told (a peer machine's folder -> the folder here, for one project or
for a whole parent folder) and the peer projects it was told not to carry. A
reader turns every board into ordinary `PathMappingRule`s, so each consumer
of `[[path_mappings]]` -- projects, chats, the working set, repair, the
session transfer -- uses the memory without knowing it exists:

- what this machine was told: the peer's folder -> the folder here;
- what another machine was told about *this* one: read backwards, so one
  answer serves both directions;
- two machines told about the same folder of a third: joined through it, so a
  Linux and a Mac that each placed a Windows folder map onto each other.

Nothing here is chosen for the person. A suggestion (a folder of the same
name next to a project that is already placed) is offered, never recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
from typing import Any, Iterable

from .path_mapping import PathMappingError, PathMappingRule, path_flavor
from .peer_board import read_bodies, write_body
from .version import PRODUCER_VERSION

LOG = logging.getLogger(__name__)

PLACES_FORMAT = "codexsync-path-places-v1"

#: Operating systems a board may name; they decide how a path compares.
SYSTEMS = ("windows", "linux", "macos")
#: A place for one project, and the folder above it learned at the same time.
SCOPE_PROJECT = "project"
SCOPE_FOLDER = "folder"


def this_system() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def case_sensitive_on(system: str | None) -> bool | None:
    """Whether paths on that system differ by case; ``None`` when unknown.

    Windows and macOS (APFS and HFS+ by default) ignore case while macOS writes
    POSIX paths, so the decision follows the machine, not the path's look.
    """
    if system == "linux":
        return True
    if system in {"windows", "macos"}:
        return False
    return None


def path_key(value: str, system: str | None = None) -> str:
    """One spelling of a path for comparing; the folder itself, never a prefix."""
    flavor = path_flavor(value)
    if flavor in {"windows", "unc"}:
        return str(PureWindowsPath(value.replace("/", "\\"))).rstrip("\\").casefold()
    text = str(PurePosixPath(value)).rstrip("/") or "/"
    return text.casefold() if case_sensitive_on(system) is False else text


@dataclass(frozen=True, slots=True)
class Place:
    """One answer: the peer's folder ``there`` is ``here`` on this machine."""

    peer: str
    there: str
    here: str
    scope: str = SCOPE_PROJECT
    #: The peer project the answer was given for; empty for a parent folder.
    project_id: str = ""
    name: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "peer": self.peer, "there": self.there, "here": self.here, "scope": self.scope,
            "project_id": self.project_id, "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class Skip:
    """A peer project this machine was told not to carry."""

    project_id: str
    name: str = ""
    peer: str = ""

    def to_json(self) -> dict[str, str]:
        return {"project_id": self.project_id, "name": self.name, "peer": self.peer}


@dataclass(frozen=True, slots=True)
class MachinePlaces:
    machine: str
    system: str | None
    places: tuple[Place, ...] = ()
    skipped: tuple[Skip, ...] = ()

    def body(self) -> dict[str, Any]:
        return {
            "format": PLACES_FORMAT,
            "machine": self.machine,
            "system": self.system,
            "places": [item.to_json() for item in self.places],
            "skipped": [item.to_json() for item in self.skipped],
            "producer_version": PRODUCER_VERSION,
        }


@dataclass(frozen=True, slots=True)
class PlacesBoard:
    boards: dict[str, MachinePlaces]
    unreadable: dict[str, str] = field(default_factory=dict)

    def own(self, machine: str) -> MachinePlaces:
        return self.boards.get(machine) or MachinePlaces(machine, this_system())

    def system_of(self, machine: str) -> str | None:
        board = self.boards.get(machine)
        return board.system if board is not None else None

    def skipped_here(self, machine: str) -> frozenset[str]:
        return frozenset(item.project_id for item in self.own(machine).skipped)


def read_places(root: Path) -> PlacesBoard:
    read = read_bodies(root, PLACES_FORMAT)
    unreadable = dict(read.unreadable)
    boards: dict[str, MachinePlaces] = {}
    for machine, body in read.bodies.items():
        try:
            boards[machine] = _parse(machine, body)
        except (TypeError, ValueError) as exc:
            unreadable[f"{machine}.json"] = str(exc)
    return PlacesBoard(boards, unreadable)


def _parse(machine: str, body: dict[str, Any]) -> MachinePlaces:
    system = body.get("system")
    if system not in (*SYSTEMS, None):
        raise ValueError(f"unknown system {system!r}")
    places: list[Place] = []
    for raw in body.get("places") or []:
        if not isinstance(raw, dict):
            raise ValueError("a place is not an object")
        values = {key: raw.get(key, "") for key in ("peer", "there", "here", "scope", "project_id", "name")}
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("a place holds a value that is not text")
        if not values["peer"] or not values["there"] or not values["here"]:
            raise ValueError("a place lacks its machine or one of its folders")
        if values["scope"] not in {SCOPE_PROJECT, SCOPE_FOLDER}:
            raise ValueError(f"unknown scope {values['scope']!r}")
        places.append(Place(**values))
    skipped: list[Skip] = []
    for raw in body.get("skipped") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("project_id"), str) or not raw["project_id"]:
            raise ValueError("a skipped project lacks its id")
        skipped.append(Skip(raw["project_id"], str(raw.get("name") or ""), str(raw.get("peer") or "")))
    return MachinePlaces(machine, system, tuple(places), tuple(skipped))


def write_places(root: Path, places: MachinePlaces) -> Path:
    return write_body(root, places.machine, places.body())


# --- recording an answer ------------------------------------------------------


def learn_place(
    current: MachinePlaces,
    *,
    peer: str,
    there: str,
    here: str,
    project_id: str = "",
    name: str = "",
    whole_folder: bool = True,
) -> MachinePlaces:
    """``current`` with one more answer, replacing any earlier one for the same folder.

    With ``whole_folder`` the folder above is learned too when both ends end in
    the same name -- `D:\\Projects\\atlas` placed at `/opt/atlas` also says
    `D:\\Projects` is `/opt` -- so the next project from that folder needs no
    question. A narrower answer still wins later, being the longer prefix.
    """
    _require_absolute(there)
    _require_absolute(here)
    learned = [Place(peer, there, here, SCOPE_PROJECT, project_id, name)]
    if whole_folder:
        parent = _parent_pair(there, here)
        if parent is not None:
            learned.append(Place(peer, parent[0], parent[1], SCOPE_FOLDER))
    keys = {(item.peer, path_key(item.there)) for item in learned}
    kept = [item for item in current.places if (item.peer, path_key(item.there)) not in keys]
    skipped = tuple(item for item in current.skipped if item.project_id != project_id or not project_id)
    return MachinePlaces(current.machine, current.system, tuple(kept + learned), skipped)


def learn_skip(current: MachinePlaces, *, project_id: str, name: str = "", peer: str = "") -> MachinePlaces:
    kept = tuple(item for item in current.skipped if item.project_id != project_id)
    return MachinePlaces(current.machine, current.system, current.places, kept + (Skip(project_id, name, peer),))


def forget(current: MachinePlaces, *, there: str | None = None, project_id: str | None = None) -> MachinePlaces:
    """``current`` without the place for folder ``there`` or the skip of ``project_id``."""
    places = current.places
    skipped = current.skipped
    if there is not None:
        key = path_key(there)
        places = tuple(item for item in places if path_key(item.there) != key)
    if project_id is not None:
        skipped = tuple(item for item in skipped if item.project_id != project_id)
        places = tuple(item for item in places if item.project_id != project_id)
    return MachinePlaces(current.machine, current.system, places, skipped)


def _require_absolute(value: str) -> None:
    if path_flavor(value) is None:
        raise ValueError(f"not an absolute path: {value!r}")


def _split(value: str) -> tuple[str, str] | None:
    """(parent, last name) of an absolute path, or ``None`` at a root."""
    flavor = path_flavor(value)
    pure = PureWindowsPath(value) if flavor in {"windows", "unc"} else PurePosixPath(value)
    if pure.parent == pure or not pure.name:
        return None
    return str(pure.parent), pure.name


def _parent_pair(there: str, here: str) -> tuple[str, str] | None:
    left, right = _split(there), _split(here)
    if left is None or right is None or left[1].casefold() != right[1].casefold():
        return None
    if _split(left[0]) is None or _split(right[0]) is None:
        # Never learn a drive or `/` itself: "all of D:\\ is /" is a guess.
        return None
    return left[0], right[0]


# --- turning the memory into rules ---------------------------------------------


def learned_rules(board: PlacesBoard, machine: str) -> list[PathMappingRule]:
    """Every rule the memory gives *into* ``machine``, from any other machine."""
    rules: dict[tuple[str, str, str], PathMappingRule] = {}

    def add(source: str, source_prefix: str, target_prefix: str, origin: str) -> None:
        if source == machine or path_flavor(source_prefix) is None or path_flavor(target_prefix) is None:
            # A rule that cannot be parsed would fail every mapping, not just this one.
            return
        try:
            key = (source, path_key(source_prefix, board.system_of(source)), target_prefix)
        except PathMappingError:
            return
        if key in rules:
            return
        digest = hashlib.sha256("\0".join((origin, source, source_prefix, target_prefix)).encode("utf-8"))
        rules[key] = PathMappingRule(
            rule_id=f"learned-{digest.hexdigest()[:16]}",
            source_machine=source,
            target_machine=machine,
            source_prefix=source_prefix,
            target_prefix=target_prefix,
            case_sensitive=case_sensitive_on(board.system_of(source)),
        )

    own = board.boards.get(machine)
    for place in own.places if own is not None else ():
        add(place.peer, place.there, place.here, "told-here")
    for other, places in sorted(board.boards.items()):
        if other == machine:
            continue
        for place in places.places:
            if place.peer == machine:
                add(other, place.here, place.there, "told-there")
    # Joined through a third machine: both were told about one of its folders.
    for place in own.places if own is not None else ():
        for other, places in sorted(board.boards.items()):
            if other in {machine, place.peer}:
                continue
            for theirs in places.places:
                if theirs.peer == place.peer and _same_folder(theirs.there, place.there, board.system_of(place.peer)):
                    add(other, theirs.here, place.here, "joined")
    return sorted(rules.values(), key=lambda rule: rule.rule_id)


def _same_folder(left: str, right: str, system: str | None) -> bool:
    try:
        return path_key(left, system) == path_key(right, system)
    except PathMappingError:
        return False


def suggest_here(there: str, known_here: Iterable[str], *, exists=lambda path: Path(path).is_dir()) -> tuple[str, ...]:
    """Folders here named like ``there`` beside folders already placed. Offered, never recorded."""
    tail = _split(there)
    if tail is None:
        return ()
    found: dict[str, None] = {}
    for folder in known_here:
        parent = _split(folder)
        if parent is None:
            continue
        separator = "\\" if path_flavor(folder) in {"windows", "unc"} else "/"
        candidate = parent[0].rstrip("\\/") + separator + tail[1]
        if candidate not in found and exists(candidate):
            found[candidate] = None
    return tuple(found)
