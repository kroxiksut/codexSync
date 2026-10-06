"""Handing work from one machine to the next (CS-328, `D-018`).

The cold-sync protocol in `AI_RULES.md` is: close Codex on the machine you
worked on, let the cloud deliver, sync on the next machine, only then start
Codex there. This module is the record that makes the middle two steps
checkable instead of hoped for.

Each machine keeps **one file of its own** in ``[handoff] root_dir``, which
sits in the folder the cloud client syncs. Only that machine writes it, so two
machines never race for one file and a cloud client never has to invent a
"conflicted copy". A file says:

- whether the machine is ``working`` (Codex was seen running there) or has
  ``handed_off`` (a full sync finished after Codex closed);
- the id of its last handoff, and a fingerprint of the cloud copy as that
  handoff left it -- one size and SHA-256 per file, keyed by the SHA-256 of
  the path, so no path or session id is written out;
- which handoff of every other machine it has already taken.

"Have I taken A's work?" is a question about an *id*, never about a clock:
two machines' clocks disagree, and `AI_RULES` §6 forbids reconciling them.
Times are recorded for people to read and decide nothing. A handoff of
another machine counts as delivered here once every file its fingerprint
names is present in the cloud copy with that size and hash -- which is how
the cloud is waited for without ever asking the cloud client anything
(`AI_RULES` §1).

The file is one self-verifying JSON document replaced in a single step, like
a `semantic_store` entry: a copy the cloud delivered half-written, or one
that landed under another machine's name, is reported and never believed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid
from typing import Any, Callable, Iterable

from .filters import PathFilter
from .guardian_models import normalize_machine_id
from .models import AppConfig
from .runtime import SECRET_NAMES
from .scanner import scan_tree
from .version import PRODUCER_VERSION
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

HANDOFF_FORMAT = "codexsync-handoff-v1"

#: Codex was seen running on this machine and it has not handed off since.
STATE_WORKING = "working"
#: The last full sync after Codex closed finished; the cloud copy holds it.
STATE_HANDED_OFF = "handed_off"
STATES = (STATE_WORKING, STATE_HANDED_OFF)

#: Session trees are semantic-owned and mirrored by `sessions apply`, not by
#: plain `sync`, so they are fingerprinted whatever `include_roots` says.
_SEMANTIC_ROOTS = ("sessions", "archived_sessions")
#: A payload being staged beside its destination is not part of any handoff.
_STAGING_GLOBS = ["**/*.codexsync.tmp"]


class HandoffError(ValueError):
    """A handoff file that cannot be believed."""


@dataclass(frozen=True, slots=True)
class FileMark:
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    machine: str
    state: str
    #: When ``state`` was entered, as this machine's clock said. For people.
    state_since_utc: str
    #: The id of the last completed handoff; empty before the first one.
    handoff_id: str = ""
    #: Counts this machine's handoffs, for people to read. Decides nothing.
    generation: int = 0
    handed_off_at_utc: str = ""
    #: Other machine -> id of its handoff this machine has taken.
    accepted: dict[str, str] = field(default_factory=dict)
    #: SHA-256 of a cloud-relative path -> what that file held at the handoff.
    files: dict[str, FileMark] = field(default_factory=dict)
    producer_version: str = PRODUCER_VERSION

    def to_json(self) -> dict[str, Any]:
        body = {
            "format": HANDOFF_FORMAT,
            "machine": self.machine,
            "state": self.state,
            "state_since_utc": self.state_since_utc,
            "handoff_id": self.handoff_id,
            "generation": self.generation,
            "handed_off_at_utc": self.handed_off_at_utc,
            "accepted": dict(sorted(self.accepted.items())),
            "files": {
                key: {"size": mark.size, "sha256": mark.sha256}
                for key, mark in sorted(self.files.items())
            },
            "producer_version": self.producer_version,
        }
        return {**body, "entry_digest": _digest(body)}


@dataclass(frozen=True, slots=True)
class Board:
    """Every machine's handoff file as this machine can read it now."""

    records: dict[str, HandoffRecord]
    #: File name -> why it was not believed.
    unreadable: dict[str, str] = field(default_factory=dict)

    def own(self, machine: str) -> HandoffRecord | None:
        return self.records.get(machine)

    def others(self, machine: str) -> list[HandoffRecord]:
        return [record for name, record in sorted(self.records.items()) if name != machine]

    def pending(self, machine: str) -> list[HandoffRecord]:
        """Other machines' handoffs this machine has not taken yet."""
        own = self.records.get(machine)
        accepted = own.accepted if own is not None else {}
        return [
            record for record in self.others(machine)
            if record.handoff_id and accepted.get(record.machine) != record.handoff_id
        ]

    def working_elsewhere(self, machine: str) -> list[HandoffRecord]:
        """Other machines that were working and have not handed off since."""
        return [record for record in self.others(machine) if record.state == STATE_WORKING]


@dataclass(frozen=True, slots=True)
class Delivery:
    """How much of one handoff's fingerprint the cloud copy here matches."""

    machine: str
    handoff_id: str
    total: int
    missing: int
    differing: int

    @property
    def delivered(self) -> bool:
        return self.missing == 0 and self.differing == 0

    @property
    def arrived(self) -> int:
        return self.total - self.missing - self.differing


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def machine_key(machine_id: str | None) -> str:
    key = normalize_machine_id(machine_id, allow_unknown=False)
    if key is None:
        raise HandoffError("a handoff needs a non-empty identity.machine_id")
    return key


def _digest(body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path_key(relative_path: str) -> str:
    return hashlib.sha256(relative_path.replace("\\", "/").encode("utf-8")).hexdigest()


def record_path(root: Path, machine: str) -> Path:
    return root / f"{machine}.json"


def _parse(raw: Any, *, expected_machine: str) -> HandoffRecord:
    if not isinstance(raw, dict):
        raise HandoffError("not a JSON object")
    if raw.get("format") != HANDOFF_FORMAT:
        raise HandoffError(f"unknown format {raw.get('format')!r}")
    declared = raw.get("entry_digest")
    body = {key: value for key, value in raw.items() if key != "entry_digest"}
    if declared != _digest(body):
        # Half-delivered by the cloud client, or edited by hand.
        raise HandoffError("digest does not match the contents")
    machine = body.get("machine")
    if machine != expected_machine:
        # A copy that landed under another machine's name is not adopted.
        raise HandoffError(f"file of {expected_machine!r} claims machine {machine!r}")
    state = body.get("state")
    if state not in STATES:
        raise HandoffError(f"unknown state {state!r}")
    try:
        accepted = {str(k): str(v) for k, v in dict(body.get("accepted") or {}).items()}
        files = {
            str(key): FileMark(int(value["size"]), str(value["sha256"]))
            for key, value in dict(body.get("files") or {}).items()
        }
        generation = body.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise HandoffError("generation must be a non-negative integer")
        return HandoffRecord(
            machine=str(machine),
            state=str(state),
            state_since_utc=str(body.get("state_since_utc", "")),
            handoff_id=str(body.get("handoff_id", "")),
            generation=generation,
            handed_off_at_utc=str(body.get("handed_off_at_utc", "")),
            accepted=accepted,
            files=files,
            producer_version=str(body.get("producer_version", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, HandoffError):
            raise
        raise HandoffError(f"malformed field: {exc}") from exc


def read_board(root: Path) -> Board:
    """Read every machine's file. Reads only; a bad file is reported, not fatal."""
    records: dict[str, HandoffRecord] = {}
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
            records[entry.stem] = _parse(raw, expected_machine=entry.stem)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, HandoffError) as exc:
            unreadable[entry.name] = str(exc)
    return Board(records, unreadable)


def write_record(root: Path, record: HandoffRecord) -> Path:
    """Replace this machine's file in one step, staged beside it."""
    root.mkdir(parents=True, exist_ok=True)
    target = record_path(root, record.machine)
    staging = root / f".{record.machine}.{uuid.uuid4().hex}.codexsync.tmp"
    payload = json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
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
                LOG.warning("could not remove handoff staging file %s", staging)
    return target


def mark_working(root: Path, machine: str, *, now: Callable[[], str] = now_utc) -> HandoffRecord:
    """Say that Codex is running here; the last handoff stays as it was."""
    board = read_board(root)
    own = board.own(machine) or HandoffRecord(machine, STATE_HANDED_OFF, "")
    if own.state == STATE_WORKING:
        return own
    record = HandoffRecord(
        machine, STATE_WORKING, now(), own.handoff_id, own.generation, own.handed_off_at_utc,
        dict(own.accepted), dict(own.files),
    )
    write_record(root, record)
    return record


def record_handoff(
    root: Path,
    machine: str,
    *,
    files: dict[str, FileMark],
    taken: Iterable[HandoffRecord],
    new_handoff: bool = True,
    now: Callable[[], str] = now_utc,
) -> HandoffRecord:
    """Write a finished full sync: what was taken and, if anything, what was handed off.

    ``new_handoff`` is whether this machine wrote to the cloud copy. Only then
    does it get a new id and fingerprint; otherwise the previous handoff is
    kept exactly, because a new id would make every other machine wait for
    the "delivery" of what it already holds.
    """
    board = read_board(root)
    own = board.own(machine)
    accepted = dict(own.accepted) if own is not None else {}
    for other in taken:
        if other.handoff_id:
            accepted[other.machine] = other.handoff_id
    moment = now()
    if new_handoff or own is None:
        handoff_id = uuid.uuid4().hex if new_handoff else ""
        generation = (own.generation if own is not None else 0) + (1 if new_handoff else 0)
        handed_off_at, kept_files = (moment, dict(files)) if new_handoff else ("", {})
    else:
        handoff_id, generation = own.handoff_id, own.generation
        handed_off_at, kept_files = own.handed_off_at_utc, dict(own.files)
    record = HandoffRecord(
        machine=machine,
        state=STATE_HANDED_OFF,
        state_since_utc=moment,
        handoff_id=handoff_id,
        generation=generation,
        handed_off_at_utc=handed_off_at,
        accepted=accepted,
        files=kept_files,
    )
    write_record(root, record)
    return record


class Fingerprinter:
    """Sizes and hashes of the cloud copy, re-hashing only what changed.

    A delivery wait asks every few seconds; a file whose size and mtime have
    not moved since the last look is not read again.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, int, str]] = {}

    def files(self, cfg: AppConfig) -> dict[str, FileMark]:
        cloud = cfg.paths.cloud_root_dir
        roots = list(dict.fromkeys([*cfg.targets.include_roots, *_SEMANTIC_ROOTS]))
        path_filter = PathFilter([*cfg.filters.exclude_globs, *_STAGING_GLOBS])
        index = scan_tree(cloud, roots, path_filter)
        marks: dict[str, FileMark] = {}
        for relative, meta in index.items():
            if any(part in SECRET_NAMES for part in relative.split("/")):
                continue
            cached = self._cache.get(relative)
            if cached is not None and cached[0] == meta.size and cached[1] == meta.mtime_ns:
                digest = cached[2]
            else:
                try:
                    digest = _sha256(meta.abs_path)
                except OSError:
                    # Being written by the cloud client right now: absent for
                    # this look, which is the conservative reading.
                    continue
                self._cache[relative] = (meta.size, meta.mtime_ns, digest)
            marks[path_key(relative)] = FileMark(meta.size, digest)
        return marks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def delivery(record: HandoffRecord, current: dict[str, FileMark]) -> Delivery:
    """Whether the cloud copy here holds everything ``record`` handed off.

    Extra files are fine -- they belong to someone else's later work; a file
    the handoff named that is missing or different means the cloud client
    has not finished bringing it.
    """
    missing = differing = 0
    for key, mark in record.files.items():
        seen = current.get(key)
        if seen is None:
            missing += 1
        elif seen != mark:
            differing += 1
    return Delivery(record.machine, record.handoff_id, len(record.files), missing, differing)


__all__ = [
    "Board",
    "Delivery",
    "FileMark",
    "Fingerprinter",
    "HANDOFF_FORMAT",
    "HandoffError",
    "HandoffRecord",
    "STATE_HANDED_OFF",
    "STATE_WORKING",
    "delivery",
    "machine_key",
    "mark_working",
    "now_utc",
    "path_key",
    "read_board",
    "record_handoff",
    "record_path",
    "write_record",
]
