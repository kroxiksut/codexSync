"""The versioned semantic manifest, and the bundles that keep divergent branches.

Two things live here and they are deliberately different in kind.

**The manifest is metadata.** Per session it records what a branch *was* at a
moment both machines were known to agree: its hash, its record count, its
active/archived state, its parent, and the base that agreement established. That
is what an ancestry decision needs — a later run can ask "did these two sides
ever share a history" without either side keeping a copy of it. The manifest
never stores a session's payload. Copying every reconciled session would
duplicate the whole session directory into a store the config explicitly allows
to live in the user's cloud folder, and nothing would ever read those bytes.

**A conflict bundle is the payload.** When a divergence is resolved, the branch
that loses is about to be overwritten and its only other copy would be a backup
that retention expires. That one really does need the raw bytes, so it keeps
them, and retention never touches this store.

Both are committed the same way and for the same reason: staging, hash
verification, then an atomic move. For a manifest entry that is a single
self-verifying file replaced in one step, rather than a payload plus a separate
``COMMITTED`` marker. In a folder a cloud client syncs on its own schedule, two
files can arrive in either order or half-written; one file whose digest covers
its own contents cannot be half-believed.

Entries from other machines are read, never rewritten, and are accepted only
when their digest verifies and their generation has not gone backwards for that
machine. A peer here is another machine of the same person, so the guard is
against a cloud client restoring an old copy, not against a hostile writer.

One field the task lists is deliberately absent: canonical record checkpoints.
Computing them means streaming every session — hundreds of megabytes — to write
digests that no code reads yet. When something needs to prove a prefix relation
from the manifest instead of from the files, that is the moment to add them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .exceptions import FailSafeError


SEMANTIC_MANIFEST_FORMAT = "codexsync-semantic-manifest-v1"
MANIFEST_DIR_NAME = "manifest"


@dataclass(frozen=True, slots=True)
class SemanticEntry:
    """What one machine last recorded about one session."""
    machine_id: str
    session_hash: str
    generation: int
    branch_id: str
    state: str
    sha256: str
    record_count: int
    byte_count: int
    parent_hash: str | None = None
    #: The history both sides were known to hold at once. Absent until a
    #: transfer actually reconciled this session.
    base_sha256: str | None = None
    base_record_count: int | None = None
    #: Highest generation seen from each other machine, so a restored older
    #: copy of a peer's entry can be recognised as going backwards.
    peers: dict[str, int] = field(default_factory=dict)
    recorded_at_utc: str = ""

    @property
    def has_base(self) -> bool:
        return bool(self.base_sha256)


def branch_id_for(session_hash: str, sha256: str) -> str:
    """Identity of one exact branch of one session."""
    return hashlib.sha256(f"{session_hash}\0{sha256}".encode("utf-8")).hexdigest()


def session_hash_for(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class SemanticStore:
    def __init__(self, root: Path, machine_id: str) -> None:
        if not machine_id or machine_id == "unknown-machine":
            raise ValueError("Semantic store requires a stable machine id")
        self.root = root.resolve()
        self.machine_id = machine_id

    # --- the manifest -----------------------------------------------------

    def record(
        self,
        session_id: str,
        *,
        state: str,
        sha256: str,
        record_count: int,
        byte_count: int,
        parent_id: str | None = None,
        agreed: bool = False,
    ) -> SemanticEntry:
        """Record what this machine now knows about one session.

        ``agreed`` marks the moment both sides held this exact history, which is
        the only thing that may become a base. Recording the same content twice
        is the same entry and does not advance the generation, so a repeated run
        neither grows the store nor rewrites a file the cloud has already synced.
        """
        session_hash = session_hash_for(session_id)
        previous = self.own_entry(session_hash)
        base_sha, base_records = (
            (sha256, record_count) if agreed
            else (
                (previous.base_sha256, previous.base_record_count) if previous
                else (None, None)
            )
        )
        candidate = SemanticEntry(
            self.machine_id, session_hash,
            previous.generation if previous else 1,
            branch_id_for(session_hash, sha256), state, sha256, record_count, byte_count,
            session_hash_for(parent_id) if parent_id else None,
            base_sha, base_records, self._peer_generations(session_hash), _now(),
        )
        if previous is not None and _same_content(previous, candidate):
            return previous
        entry = SemanticEntry(
            candidate.machine_id, candidate.session_hash,
            (previous.generation + 1) if previous else 1,
            candidate.branch_id, candidate.state, candidate.sha256,
            candidate.record_count, candidate.byte_count, candidate.parent_hash,
            candidate.base_sha256, candidate.base_record_count, candidate.peers,
            candidate.recorded_at_utc,
        )
        _write_json_atomic(self._entry_path(self.machine_id, session_hash), _serialise(entry))
        return entry

    def own_entry(self, session_hash: str) -> SemanticEntry | None:
        return self._read(self._entry_path(self.machine_id, session_hash))

    def entries(self, session_hash: str) -> list[SemanticEntry]:
        """Every machine's accepted entry for one session, this one included."""
        found: list[SemanticEntry] = []
        for path in self._entry_paths(session_hash):
            entry = self._read(path)
            if entry is not None and not self._regressed(entry):
                found.append(entry)
        return found

    def confirmed_bases(self) -> set[str]:
        """Sessions some machine recorded as shared, and still vouches for.

        A malformed, tampered or regressed entry is skipped rather than
        repaired: no base means ``MISSING_BASE``, which is where the ancestry
        code already refuses, so failing closed costs nothing but caution.
        """
        directory = self.root / MANIFEST_DIR_NAME
        if not directory.is_dir():
            return set()
        found: set[str] = set()
        for machine_directory in sorted(directory.iterdir()):
            if not machine_directory.is_dir():
                continue
            for path in sorted(machine_directory.glob("*.json")):
                entry = self._read(path)
                if entry is None or not entry.has_base or self._regressed(entry):
                    continue
                found.add(entry.session_hash)
        return found

    # --- reading and accepting --------------------------------------------

    def _entry_path(self, machine_id: str, session_hash: str) -> Path:
        return self.root / MANIFEST_DIR_NAME / machine_id / f"{session_hash}.json"

    def _entry_paths(self, session_hash: str) -> list[Path]:
        directory = self.root / MANIFEST_DIR_NAME
        if not directory.is_dir():
            return []
        return [
            machine / f"{session_hash}.json"
            for machine in sorted(directory.iterdir())
            if machine.is_dir() and (machine / f"{session_hash}.json").is_file()
        ]

    def _read(self, path: Path) -> SemanticEntry | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("format") != SEMANTIC_MANIFEST_FORMAT:
            return None
        declared = raw.get("entry_digest")
        body = {key: value for key, value in raw.items() if key != "entry_digest"}
        if not isinstance(declared, str) or declared != _digest(body):
            return None
        try:
            entry = SemanticEntry(
                str(raw["machine_id"]), str(raw["session_hash"]), int(raw["generation"]),
                str(raw["branch_id"]), str(raw["state"]), str(raw["sha256"]),
                int(raw["record_count"]), int(raw["byte_count"]),
                _optional_str(raw.get("parent_hash")),
                _optional_str(raw.get("base_sha256")),
                raw.get("base_record_count") if isinstance(raw.get("base_record_count"), int) else None,
                {str(k): int(v) for k, v in (raw.get("peers") or {}).items()},
                str(raw.get("recorded_at_utc", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        # The file's own name is part of its identity: an entry moved or copied
        # under another session or machine is not that session's entry.
        if entry.session_hash != path.stem or entry.machine_id != path.parent.name:
            return None
        if entry.branch_id != branch_id_for(entry.session_hash, entry.sha256):
            return None
        return entry

    def _regressed(self, entry: SemanticEntry) -> bool:
        if entry.machine_id == self.machine_id:
            return False
        own = self.own_entry(entry.session_hash)
        highest = own.peers.get(entry.machine_id) if own else None
        return highest is not None and entry.generation < highest

    def _peer_generations(self, session_hash: str) -> dict[str, int]:
        own = self.own_entry(session_hash)
        seen = dict(own.peers) if own else {}
        for path in self._entry_paths(session_hash):
            if path.parent.name == self.machine_id:
                continue
            peer = self._read(path)
            if peer is None:
                continue
            seen[peer.machine_id] = max(seen.get(peer.machine_id, 0), peer.generation)
        return seen

    # --- conflict bundles --------------------------------------------------

    def conflict_bundle(self, left: Path, right: Path, *, session_id: str, common_records: int) -> Path:
        left_hash, left_size, _ = _file_metrics(left)
        right_hash, right_size, _ = _file_metrics(right)
        conflict_id = hashlib.sha256(f"{session_id}\0{left_hash}\0{right_hash}".encode("utf-8")).hexdigest()
        destination = self.root / "conflicts" / conflict_id
        if destination.is_dir():
            return destination
        stage = self.root / ".staging" / "conflicts" / uuid4().hex
        _mkdir_private(stage)
        shutil.copyfile(left, stage / "left.jsonl")
        shutil.copyfile(right, stage / "right.jsonl")
        _chmod_file(stage / "left.jsonl")
        _chmod_file(stage / "right.jsonl")
        _write_json(stage / "manifest.json", {
            "format": "codexsync-conflict-v1", "conflict_id": conflict_id,
            "session_hash": session_hash_for(session_id),
            "left_sha256": left_hash, "left_size": left_size,
            "right_sha256": right_hash, "right_size": right_size,
            "common_records": common_records,
        })
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, destination)
        _write_json(destination / "COMMITTED", {"conflict_id": conflict_id})
        return destination


def _serialise(entry: SemanticEntry) -> dict:
    body = {
        "format": SEMANTIC_MANIFEST_FORMAT,
        "machine_id": entry.machine_id,
        "session_hash": entry.session_hash,
        "generation": entry.generation,
        "branch_id": entry.branch_id,
        "state": entry.state,
        "sha256": entry.sha256,
        "record_count": entry.record_count,
        "byte_count": entry.byte_count,
        "parent_hash": entry.parent_hash,
        "base_sha256": entry.base_sha256,
        "base_record_count": entry.base_record_count,
        "peers": dict(sorted(entry.peers.items())),
        "recorded_at_utc": entry.recorded_at_utc,
    }
    return {**body, "entry_digest": _digest(body)}


def _same_content(previous: SemanticEntry, candidate: SemanticEntry) -> bool:
    """Whether re-recording would say anything new. Time alone does not count."""
    return (
        previous.sha256 == candidate.sha256
        and previous.state == candidate.state
        and previous.record_count == candidate.record_count
        and previous.byte_count == candidate.byte_count
        and previous.parent_hash == candidate.parent_hash
        and previous.base_sha256 == candidate.base_sha256
        and previous.base_record_count == candidate.base_record_count
        and previous.peers == candidate.peers
    )


def _digest(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _file_metrics(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    size = lines = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
    return digest.hexdigest(), size, lines


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _chmod_file(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)


def _write_json(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _chmod_file(path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Stage, verify what landed, then replace in one step.

    The verification reads the staged bytes back rather than trusting the write,
    so a truncated or partially flushed entry never becomes the entry.
    """
    _ensure_private_dir(path.parent)
    stage = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_json(stage, payload)
        written = json.loads(stage.read_text(encoding="utf-8"))
        if written != payload:
            raise FailSafeError("Semantic manifest entry failed verification before commit")
        os.replace(stage, path)
    except Exception:
        stage.unlink(missing_ok=True)
        raise
    _chmod_file(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
