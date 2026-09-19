"""Streaming, read-only catalog of active and archived Codex sessions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Iterator

from .jsonl_codec import JSONL_READ_ERRORS, is_branch_file, logical_name, open_jsonl
from .progress import ProgressCallback, report


DEFAULT_MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024


class SessionState(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    session_id: str | None
    state: SessionState
    relative_path: str
    sha256: str
    byte_count: int
    line_count: int
    cwd: str | None = field(repr=False, default=None)
    timestamp: str | None = None
    parent_id: str | None = None
    source_machine: str | None = None
    codes: tuple[str, ...] = ()
    mtime_ns: int | None = None
    file_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionBranch:
    session_id: str
    descriptors: tuple[SessionDescriptor, ...]


@dataclass(slots=True)
class SessionCatalog:
    descriptors: list[SessionDescriptor]
    branches: dict[str, SessionBranch]
    codes: tuple[str, ...] = ()
    volatile: bool = False

    @property
    def valid(self) -> list[SessionDescriptor]:
        return [item for item in self.descriptors if item.state not in {SessionState.INVALID, SessionState.AMBIGUOUS}]


def scan_sessions(
    state_root: Path,
    *,
    max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
    volatile: bool = False,
    source_machine: str | None = None,
    progress: ProgressCallback | None = None,
    phase: str = "sessions",
) -> SessionCatalog:
    root = state_root.resolve()
    descriptors: list[SessionDescriptor] = []
    # Both trees are walked first. Listing them is cheap and hashing them is
    # not, so this is what makes the count a total rather than a guess.
    found: list[tuple[Path, SessionState]] = []
    for directory_name, state in (("sessions", SessionState.ACTIVE), ("archived_sessions", SessionState.ARCHIVED)):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        found.extend((path, state) for path in _walk_jsonl(directory, root))

    total = len(found)
    report(progress, phase, 0, total)
    for done, (path, state) in enumerate(found, start=1):
        descriptors.append(
            _scan_jsonl(path, root, state, max_line_bytes, volatile, source_machine)
        )
        report(progress, phase, done, total)

    by_id: dict[str, list[SessionDescriptor]] = {}
    for item in descriptors:
        if item.session_id:
            by_id.setdefault(item.session_id, []).append(item)
    branches = {
        session_id: SessionBranch(session_id, tuple(items))
        for session_id, items in by_id.items()
        if len(items) > 1
    }
    if branches:
        descriptors = [
            _with_code(item, SessionState.AMBIGUOUS, "DUPLICATE_SESSION_ID")
            if item.session_id in branches else item
            for item in descriptors
        ]

    graph_codes = _parent_graph_codes(descriptors)
    return SessionCatalog(descriptors, branches, tuple(sorted(graph_codes)), volatile)


def _walk_jsonl(directory: Path, state_root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(directory, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for name in dirs:
            child = current_path / name
            if child.is_symlink() or _is_reparse(child):
                continue
            _require_inside(child, state_root)
            safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in files:
            # A branch is recognised by its logical name, not by its container:
            # the cloud mirror may store the same history compressed.
            if not is_branch_file(name):
                continue
            path = current_path / name
            if path.is_symlink() or _is_reparse(path):
                continue
            _require_inside(path, state_root)
            yield path


def _scan_jsonl(
    path: Path,
    root: Path,
    lifecycle: SessionState,
    max_line_bytes: int,
    volatile: bool,
    source_machine: str | None,
) -> SessionDescriptor:
    before = path.stat()
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    session_id = cwd = timestamp = parent_id = None
    codes: list[str] = []
    complete_tail = True
    try:
        # Every metric below is taken from the decompressed stream, so a branch
        # kept in a compressed container is the same branch as the plain one.
        with open_jsonl(path) as handle:
            while True:
                line = handle.readline(max_line_bytes + 1)
                if not line:
                    break
                byte_count += len(line)
                digest.update(line)
                line_count += 1
                if len(line) > max_line_bytes:
                    codes.append("LINE_TOO_LARGE")
                    break
                complete_tail = line.endswith(b"\n")
                raw_line = line[:-1] if complete_tail else line
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
                if b"\0" in raw_line:
                    codes.append("NUL_BYTE")
                    continue
                if line_count == 1 and raw_line.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
                    codes.append("UNSUPPORTED_BOM")
                if not raw_line.strip():
                    codes.append("EMPTY_RECORD")
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    codes.append("INVALID_RECORD")
                    continue
                if not isinstance(record, dict):
                    codes.append("RECORD_NOT_OBJECT")
                    continue
                if line_count == 1:
                    if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
                        codes.append("MISSING_INITIAL_SESSION_META")
                        continue
                    payload = record["payload"]
                    if isinstance(payload.get("id"), str) and payload["id"]:
                        session_id = payload["id"]
                    else:
                        codes.append("MISSING_SESSION_ID")
                    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
                    timestamp = payload.get("timestamp") if isinstance(payload.get("timestamp"), str) else None
                    parent_id = payload.get("parent_thread_id") if isinstance(payload.get("parent_thread_id"), str) else None
                elif record.get("type") == "session_meta":
                    # A second `session_meta` is how the runtime records that
                    # the session was resumed, not damage: on the machine this
                    # was measured against, 54 of 252 files carry one (up to 298
                    # in a single file, 267 MiB of 864 MiB in total) and in none
                    # of them does the id, cwd, timestamp, source, originator or
                    # parent differ from the first. Only the identity has to
                    # agree; the later record also carries fields the first one
                    # predates, such as `memory_mode`.
                    repeated = record.get("payload")
                    repeated_id = repeated.get("id") if isinstance(repeated, dict) else None
                    if session_id is not None and repeated_id == session_id:
                        codes.append("RESUMED_SESSION")
                    else:
                        # Two identities in one file: which history this is
                        # cannot be decided, so it is not decided.
                        codes.append("CONFLICTING_SESSION_META")
    except JSONL_READ_ERRORS:
        # Includes a truncated or corrupt container, which is what a mirror
        # being written by a cloud client looks like mid-copy.
        codes.append("READ_ERROR")
    after = path.stat()
    if _signature(before) != _signature(after):
        codes.append("READ_CHANGED")
    if not complete_tail:
        codes.append("INCOMPLETE_TAIL" if volatile else "INVALID_TAIL")
    hint = Path(logical_name(path.name)).stem
    if session_id and session_id not in hint:
        codes.append("FILENAME_ID_MISMATCH")
    invalid_codes = {
        "LINE_TOO_LARGE", "NUL_BYTE", "UNSUPPORTED_BOM", "INVALID_RECORD", "RECORD_NOT_OBJECT",
        "MISSING_INITIAL_SESSION_META", "MISSING_SESSION_ID", "CONFLICTING_SESSION_META", "READ_ERROR",
        "READ_CHANGED", "INVALID_TAIL",
    }
    state = SessionState.INVALID if invalid_codes.intersection(codes) else lifecycle
    return SessionDescriptor(
        session_id=session_id,
        state=state,
        relative_path=path.relative_to(root).as_posix(),
        sha256=digest.hexdigest(),
        byte_count=byte_count,
        line_count=line_count,
        cwd=cwd,
        timestamp=timestamp,
        parent_id=parent_id,
        source_machine=source_machine,
        codes=tuple(dict.fromkeys(codes)),
        mtime_ns=after.st_mtime_ns,
        file_id=_file_id(after),
    )


def _parent_graph_codes(descriptors: list[SessionDescriptor]) -> set[str]:
    codes: set[str] = set()
    parents = {item.session_id: item.parent_id for item in descriptors if item.session_id}
    for session_id, parent_id in parents.items():
        if parent_id is None:
            continue
        if parent_id == session_id:
            codes.add("SELF_PARENT")
        elif parent_id not in parents:
            codes.add("MISSING_PARENT")
    for start in parents:
        seen: set[str] = set()
        current: str | None = start
        while current is not None and current in parents:
            if current in seen:
                codes.add("PARENT_CYCLE")
                break
            seen.add(current)
            current = parents[current]
    return codes


def _with_code(item: SessionDescriptor, state: SessionState, code: str) -> SessionDescriptor:
    return SessionDescriptor(
        item.session_id, state, item.relative_path, item.sha256, item.byte_count, item.line_count,
        item.cwd, item.timestamp, item.parent_id, item.source_machine, item.codes + (code,), item.mtime_ns, item.file_id,
    )


def _signature(value: os.stat_result) -> tuple[int, int, str | None]:
    return value.st_size, value.st_mtime_ns, _file_id(value)


def _file_id(value: os.stat_result) -> str | None:
    inode = getattr(value, "st_ino", 0)
    return f"{getattr(value, 'st_dev', 0):x}:{inode:x}" if inode else None


def _require_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise OSError("Session path escapes state root") from exc


def _is_reparse(path: Path) -> bool:
    try:
        return bool(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True
