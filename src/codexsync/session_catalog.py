"""Streaming, read-only catalog of active and archived Codex sessions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Iterator

from .fs_links import is_link
from .jsonl_codec import (
    JSONL_READ_ERRORS,
    JsonlCodec,
    codec_of,
    is_branch_file,
    logical_name,
    logical_relative_path,
    open_jsonl,
)
from .progress import ProgressCallback, report


DEFAULT_MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024

#: Compressed files unpacked at once. Unpacking the mirror's xz containers is
#: most of a mirror scan (19 s of 28 s on the machine this was measured on) and
#: releases the GIL, so threads run it in parallel: 30 s -> 14 s with four.
#: A plain file is the opposite -- parsing its JSON holds the GIL, and four
#: threads made a 9 s scan of `.codex` take 10-13 s -- so plain files stay on
#: the calling thread.
SCAN_WORKERS = max(1, min(4, os.cpu_count() or 1))


class SessionState(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"


#: Records without an `ordinal`: how every session was written until the
#: desktop build of September 2026.
RECORD_FORMAT_LEGACY = "legacy"
#: Every record numbered by `ordinal`. That build rewrote every existing session
#: file into it at once -- keeping each file's mtime -- which is the one
#: observed case of the runtime changing a history anywhere but at its end.
RECORD_FORMAT_ORDINAL = "ordinal"
#: Some records numbered and some not: an old history the new build appended to
#: without rewriting it.
RECORD_FORMAT_MIXED = "mixed"
#: Newer formats rank higher. Only a difference in rank means anything.
RECORD_FORMAT_RANK = {RECORD_FORMAT_LEGACY: 0, RECORD_FORMAT_MIXED: 1, RECORD_FORMAT_ORDINAL: 2}


#: Codes that make a branch `INVALID`: it cannot be compared safely, so it is
#: never a side of a fast-forward -- and never mistaken for a side that is
#: simply not there.
INVALID_CODES = frozenset({
    "LINE_TOO_LARGE", "NUL_BYTE", "UNSUPPORTED_BOM", "INVALID_RECORD", "RECORD_NOT_OBJECT",
    "MISSING_INITIAL_SESSION_META", "MISSING_SESSION_ID", "CONFLICTING_SESSION_META", "READ_ERROR",
    "READ_CHANGED", "INVALID_TAIL",
})
#: The code that makes a branch `AMBIGUOUS`: one session id in two files.
DUPLICATE_SESSION_ID = "DUPLICATE_SESSION_ID"
#: A file that continues a chat begun in another file (CS-356). Codex 0.160
#: writes a long chat's history in pages: the new file opens with the same
#: `session_meta` id plus `history_base` -- the thread and the record the page
#: carries on from -- and is named `rollout-<time>-<id>_<other id>.jsonl`. The
#: chat is the chain of files, so a page is a branch of its own and never a
#: duplicate of the file it continues.
HISTORY_PAGE = "HISTORY_PAGE"
#: A `history_base` that names this thread but not in the shape read above.
#: The file is then an ordinary branch, so a second file with its id stays a
#: duplicate rather than a page whose position is guessed.
UNREADABLE_HISTORY_BASE = "UNREADABLE_HISTORY_BASE"


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
    #: How the records are written: `RECORD_FORMAT_LEGACY`, `RECORD_FORMAT_ORDINAL`
    #: or `RECORD_FORMAT_MIXED`; ``None`` when no record could be read.
    record_format: str | None = None
    #: The latest `timestamp` any record carries, as written (ISO 8601 UTC).
    last_record_at: str | None = None
    #: For a page of a paginated chat (`HISTORY_PAGE`), where it carries on:
    #: ``page-<first ordinal>``. ``None`` for the file a chat begins in.
    page: str | None = None

    @property
    def branch_key(self) -> str | None:
        """What identifies this file's history: the session id, plus its page.

        Everything that pairs a branch with its copy on the other side -- the
        transfer plan, the semantic manifest, conflict ids -- uses this, so the
        first file of a chat keeps the identity it always had.
        """
        if self.session_id is None or self.page is None:
            return self.session_id
        return f"{self.session_id}#{self.page}"


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
    packed = {
        index for index, (path, _) in enumerate(found)
        if codec_of(path) not in (None, JsonlCodec.NONE)
    }
    if min(SCAN_WORKERS, len(packed)) <= 1:
        packed = set()
    # Each file is still read whole by one thread, exactly as before; only the
    # order they finish in varies. The result keeps the order they were found
    # in, so the catalogue never depends on timing, and progress is reported
    # from this thread, which is the caller's.
    results: list[SessionDescriptor | None] = [None] * total
    done = 0
    with ThreadPoolExecutor(
        max_workers=max(1, min(SCAN_WORKERS, len(packed))), thread_name_prefix="codexsync-scan"
    ) as pool:
        queued = {
            pool.submit(
                _scan_jsonl, found[index][0], root, found[index][1], max_line_bytes, volatile, source_machine
            ): index
            for index in sorted(packed)
        }
        # Plain files are read here while the pool unpacks the others.
        for index, (path, state) in enumerate(found):
            if index in packed:
                continue
            results[index] = _scan_jsonl(path, root, state, max_line_bytes, volatile, source_machine)
            done += 1
            report(progress, phase, done, total)
        for future in as_completed(queued):
            results[queued[future]] = future.result()
            done += 1
            report(progress, phase, done, total)
    descriptors.extend(item for item in results if item is not None)

    # A duplicate is two files for one history; a chat continued in a page is
    # one history per file, keyed apart by `branch_key`.
    by_key: dict[str, list[SessionDescriptor]] = {}
    for item in descriptors:
        if item.branch_key:
            by_key.setdefault(item.branch_key, []).append(item)
    branches = {
        key: SessionBranch(items[0].session_id or key, tuple(items))
        for key, items in by_key.items()
        if len(items) > 1
    }
    if branches:
        descriptors = [
            _with_code(item, SessionState.AMBIGUOUS, DUPLICATE_SESSION_ID)
            if item.branch_key in branches else item
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
    try:
        before = path.stat()
    except OSError:
        # Listed a moment ago and gone now, or not readable at all. Either way
        # this one branch cannot be described, which is not a reason to stop
        # describing the others: it is recorded as unreadable, and a branch that
        # cannot be read is never treated as absent.
        return SessionDescriptor(
            session_id=None, state=SessionState.INVALID,
            relative_path=path.relative_to(root).as_posix(),
            sha256=hashlib.sha256().hexdigest(), byte_count=0, line_count=0,
            source_machine=source_machine, codes=("READ_ERROR",),
        )
    digest = hashlib.sha256()
    byte_count = 0
    line_count = 0
    session_id = cwd = timestamp = parent_id = page = None
    codes: list[str] = []
    complete_tail = True
    numbered = unnumbered = 0
    last_record_at: str | None = None
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
                ordinal = record.get("ordinal")
                if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                    numbered += 1
                else:
                    unnumbered += 1
                stamp = record.get("timestamp")
                if isinstance(stamp, str) and (last_record_at is None or stamp > last_record_at):
                    last_record_at = stamp
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
                    page, page_code = _history_page(payload, session_id)
                    if page_code:
                        codes.append(page_code)
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
    try:
        after = path.stat()
    except OSError:
        # Removed or replaced while it was being read.
        after = None
    if after is None or _signature(before) != _signature(after):
        codes.append("READ_CHANGED")
        after = after or before
    if not complete_tail:
        codes.append("INCOMPLETE_TAIL" if volatile else "INVALID_TAIL")
    hint = Path(logical_name(path.name)).stem
    if session_id and session_id not in hint:
        codes.append("FILENAME_ID_MISMATCH")
    state = SessionState.INVALID if INVALID_CODES.intersection(codes) else lifecycle
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
        record_format=_record_format(numbered, unnumbered),
        last_record_at=last_record_at,
        page=page,
    )


def _history_page(payload: dict, session_id: str | None) -> tuple[str | None, str | None]:
    """Where a page of a paginated chat carries on, and the code saying so.

    Observed on Codex 0.160: ``history_mode = "paginated"`` on every new file,
    and on a page ``history_base = {thread_id, end_ordinal_exclusive,
    end_byte_offset}`` -- the thread is this file's own id, and the page holds
    the records from that ordinal on. A base naming another thread is not a
    page of this chat (the file has an id of its own then), and a base in any
    other shape is reported rather than read.
    """
    base = payload.get("history_base")
    if base is None or session_id is None:
        return None, None
    if not isinstance(base, dict) or base.get("thread_id") != session_id:
        return None, (UNREADABLE_HISTORY_BASE if isinstance(base, dict) and base.get("thread_id") is None else None)
    start = base.get("end_ordinal_exclusive")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        return None, UNREADABLE_HISTORY_BASE
    return f"page-{start}", HISTORY_PAGE


def one_per_chat(descriptors: list[SessionDescriptor]) -> list[SessionDescriptor]:
    """One file per chat, in the order given: the file it begins in, else its first page.

    For whatever asks about chats rather than files -- the chat list, project
    bindings, `doctor`. A chat continued in pages is one chat, and each of its
    files carries the same `session_meta`.
    """
    chosen: dict[str, SessionDescriptor] = {}
    for item in descriptors:
        if not item.session_id:
            continue
        held = chosen.get(item.session_id)
        if held is None or _page_order(item) < _page_order(held):
            chosen[item.session_id] = item
    keep = {id(item) for item in chosen.values()}
    return [item for item in descriptors if not item.session_id or id(item) in keep]


def latest_page(descriptors: list[SessionDescriptor]) -> dict[str, SessionDescriptor]:
    """Per chat, the file its history currently ends in: the furthest page."""
    latest: dict[str, SessionDescriptor] = {}
    for item in descriptors:
        if not item.session_id:
            continue
        held = latest.get(item.session_id)
        if held is None or _page_order(item) > _page_order(held):
            latest[item.session_id] = item
    return latest


def _page_order(item: SessionDescriptor) -> int:
    return -1 if item.page is None else int(item.page.removeprefix("page-"))


def peek_record_formats(state_root: Path, *, max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES) -> dict[str, str]:
    """The record format of each branch under ``state_root``, by its logical path.

    Reads the first record of each file only, so a compressed mirror costs a
    few kilobytes per branch instead of all of it. That cannot see a history
    that became numbered half-way (`RECORD_FORMAT_MIXED`); it answers the
    question a diagnostic asks, which is whether a rewrite reached one copy of
    a branch and not the other. Keyed by logical path so a plain branch and its
    compressed mirror copy are the same key.
    """
    root = state_root.resolve()
    formats: dict[str, str] = {}
    for directory_name in ("sessions", "archived_sessions"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in _walk_jsonl(directory, root):
            try:
                with open_jsonl(path) as handle:
                    first = handle.readline(max_line_bytes + 1)
                record = json.loads(first.decode("utf-8")) if first.strip() else None
            except (*JSONL_READ_ERRORS, UnicodeError, ValueError):
                record = None
            if not isinstance(record, dict):
                key = "unreadable"
            else:
                ordinal = record.get("ordinal")
                key = (
                    RECORD_FORMAT_ORDINAL
                    if isinstance(ordinal, int) and not isinstance(ordinal, bool)
                    else RECORD_FORMAT_LEGACY
                )
            formats[logical_relative_path(path.relative_to(root).as_posix())] = key
    return formats


def _record_format(numbered: int, unnumbered: int) -> str | None:
    if numbered and unnumbered:
        return RECORD_FORMAT_MIXED
    if numbered:
        return RECORD_FORMAT_ORDINAL
    if unnumbered:
        return RECORD_FORMAT_LEGACY
    return None


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
    return replace(item, state=state, codes=item.codes + (code,))


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
    """Whether an entry is a link the scan must not follow or read through.

    Only a reparse point that names another location counts (``fs_links``):
    a cloud placeholder is an ordinary session file, and skipping it made a
    checkout inside Yandex.Disk or OneDrive lose sessions with no error.
    """
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return True
    return is_link(path, info)


def scan_both_sides(
    local_root: Path,
    remote_root: Path,
    *,
    local_machine: str | None,
    remote_machine: str | None,
    max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
    volatile: bool = False,
    progress: ProgressCallback | None = None,
) -> tuple[SessionCatalog, SessionCatalog]:
    """`.codex` and the cloud mirror, read at the same time. Reads only.

    One after the other they took 9 s + 30 s on the machine this was measured
    on, and a full sync reads both twice (the plan, then its rebuild before the
    write). The two hardly compete: `.codex` is plain JSONL parsed on the
    calling thread, the mirror is mostly xz unpacking, which releases the GIL.
    Each catalogue is exactly what `scan_sessions` returns for its side; the
    progress is one phase, ``sessions``, over both.
    """
    lock = threading.Lock()
    seen: dict[str, tuple[int, int]] = {}

    def side(name: str) -> ProgressCallback | None:
        if progress is None:
            return None

        def callback(_phase: str, done: int, total: int) -> None:
            # Under the lock, so the two sides never report a smaller "done"
            # after a larger one.
            with lock:
                seen[name] = (done, total)
                report(
                    progress, "sessions",
                    sum(item[0] for item in seen.values()), sum(item[1] for item in seen.values()),
                )
        return callback

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="codexsync-scan-mirror") as pool:
        remote = pool.submit(
            scan_sessions, remote_root, max_line_bytes=max_line_bytes, volatile=volatile,
            source_machine=remote_machine, progress=side("remote"),
        )
        local = scan_sessions(
            local_root, max_line_bytes=max_line_bytes, volatile=volatile,
            source_machine=local_machine, progress=side("local"),
        )
        return local, remote.result()
