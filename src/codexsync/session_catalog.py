"""Streaming, read-only catalog of active and archived Codex sessions."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterator

from .cancellation import CancellationToken
from .fs_links import is_link
from .jsonl_codec import (
    JSONL_READ_ERRORS,
    JsonlCodec,
    codec_of,
    is_branch_file,
    logical_name,
    logical_relative_path,
    open_jsonl,
    unsupported_container,
)
from .progress import ProgressCallback, report
from .work_scheduler import WorkScheduler


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
#: What `peek_record_formats` reports for a branch in a container this version
#: cannot open (CS-394): its records are not read at all.
RECORD_FORMAT_UNSUPPORTED = "unsupported"
#: Newer formats rank higher. Only a difference in rank means anything.
RECORD_FORMAT_RANK = {RECORD_FORMAT_LEGACY: 0, RECORD_FORMAT_MIXED: 1, RECORD_FORMAT_ORDINAL: 2}


#: Codes that make a branch `INVALID`: it cannot be compared safely, so it is
#: never a side of a fast-forward -- and never mistaken for a side that is
#: simply not there.
INVALID_CODES = frozenset({
    "LINE_TOO_LARGE", "NUL_BYTE", "UNSUPPORTED_BOM", "INVALID_RECORD", "RECORD_NOT_OBJECT",
    "MISSING_INITIAL_SESSION_META", "MISSING_SESSION_ID", "CONFLICTING_SESSION_META", "READ_ERROR",
    "READ_CHANGED", "INVALID_TAIL", "UNSUPPORTED_CONTAINER",
})
#: A chat file in a container this version cannot open (`.jsonl.zst`, CS-394).
#: Its history cannot be compared, so the chat is blocked on both sides; its
#: id is taken from the file name (`ID_FROM_FILE_NAME`) so the block reaches
#: the mirror's copy of the same chat instead of letting it land beside it.
UNSUPPORTED_CONTAINER = "UNSUPPORTED_CONTAINER"
ID_FROM_FILE_NAME = "ID_FROM_FILE_NAME"
#: The thread id in a Codex chat file name: the first UUID after the time.
_NAME_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
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
    #: The highest record `ordinal` in this file; ``None`` when none is numbered.
    max_ordinal: int | None = None
    #: Diagnostic first-record reading, kept apart from whole-history format.
    #: It does not participate in a branch's identity or transfer decisions.
    first_record_format: str = field(default="unreadable", compare=False)

    @property
    def page_start(self) -> int:
        """The first ordinal this file carries: 0 for the file a chat begins in."""
        return int(self.page.removeprefix("page-")) if self.page else 0

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
    scheduler: WorkScheduler | None = None,
    on_tree_file: Callable[[Path], None] | None = None,
    on_tree_complete: Callable[[], None] | None = None,
) -> SessionCatalog:
    """Read one tree; an injected scheduler remains owned by its caller.

    Optional tree observers measure files before decoding. Completion marks
    that independent measurement usable even if decoding later fails.
    """
    return _scan_catalogs(
        [(state_root, source_machine)], max_line_bytes=max_line_bytes,
        volatile=volatile, progress=progress, phase=phase, scheduler=scheduler,
        on_tree_file=on_tree_file,
        on_tree_complete=on_tree_complete,
    )[0]


def _find_sessions(
    state_root: Path, on_tree_file: Callable[[Path], None] | None = None,
) -> tuple[Path, list[tuple[Path, SessionState]]]:
    root = state_root.resolve()
    # Both trees are walked first. Listing them is cheap and hashing them is
    # not, so this is what makes the count a total rather than a guess.
    found: list[tuple[Path, SessionState]] = []
    for directory_name, state in (("sessions", SessionState.ACTIVE), ("archived_sessions", SessionState.ARCHIVED)):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        # Startup's file count skips linked root folders; the scan's existing
        # branch selection remains independent of that measurement.
        observer = on_tree_file
        if observer is not None and (directory.is_symlink() or _is_reparse(directory)):
            observer = None
        # Keep the original two-argument seam when no observer is needed.
        # Besides avoiding needless callback plumbing, this preserves callers
        # that wrap/patch the walker to model a branch disappearing mid-scan.
        walked = (
            _walk_jsonl(directory, root)
            if observer is None
            else _walk_jsonl(directory, root, on_tree_file=observer)
        )
        found.extend((path, state) for path in walked)

    return root, found


def _scan_catalogs(
    sides: list[tuple[Path, str | None]], *, max_line_bytes: int, volatile: bool,
    progress: ProgressCallback | None, phase: str, scheduler: WorkScheduler | None,
    on_tree_file: Callable[[Path], None] | None = None,
    on_tree_complete: Callable[[], None] | None = None,
    cancellation: CancellationToken | None = None,
) -> list[SessionCatalog]:
    # Flatten both roots into one read batch: a scan never runs inside a
    # scheduler worker and therefore cannot multiply pools or deadlock them.
    jobs: list[tuple[Path, Path, SessionState, str | None]] = []
    sizes: list[int] = []
    for state_root, source_machine in sides:
        if cancellation is not None:
            cancellation.checkpoint()
        root, found = _find_sessions(state_root, on_tree_file)
        sizes.append(len(found))
        jobs.extend((path, root, state, source_machine) for path, state in found)
    if on_tree_complete is not None:
        on_tree_complete()
    total = len(jobs)
    report(progress, phase, 0, total)
    results: list[SessionDescriptor | None] = [None] * total
    done = 0

    def read(index: int) -> SessionDescriptor:
        if cancellation is not None:
            cancellation.checkpoint()
        path, root, state, machine = jobs[index]
        return _scan_jsonl(path, root, state, max_line_bytes, volatile, machine)

    def store(index: int, item: SessionDescriptor) -> None:
        nonlocal done
        results[index] = item
        done += 1
        report(progress, phase, done, total)

    packed = [index for index, (path, *_rest) in enumerate(jobs)
              if codec_of(path) not in (None, JsonlCodec.NONE)]
    # Preserve the explicit serial path used for comparison/diagnostics.
    if SCAN_WORKERS <= 1:
        packed = []
    packed_set = set(packed)

    def plain_steps() -> Iterator[Callable[[], None]]:
        for index in range(total):
            if index not in packed_set:
                yield lambda index=index: store(index, read(index))

    def read_packed(work: WorkScheduler) -> None:
        items = work.map_read(
            read, packed, large_file=lambda _index: True,
            caller_work=plain_steps() if len(packed) < total else None,
            cancellation=cancellation,
        )
        for index, item in zip(packed, items):
            store(index, item)

    if scheduler is not None:
        read_packed(scheduler)
    elif not packed:
        for step in plain_steps():
            step()
    else:
        with WorkScheduler(io_workers=SCAN_WORKERS, large_file_limit=SCAN_WORKERS) as owned:
            read_packed(owned)

    catalogs: list[SessionCatalog] = []
    if cancellation is not None:
        cancellation.checkpoint()
    offset = 0
    for size in sizes:
        descriptors = [item for item in results[offset:offset + size] if item is not None]
        catalogs.append(_catalog_from_descriptors(descriptors, volatile))
        offset += size
    return catalogs


def _catalog_from_descriptors(descriptors: list[SessionDescriptor], volatile: bool) -> SessionCatalog:
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


def _walk_jsonl(
    directory: Path, state_root: Path, *, on_tree_file: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
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
            if on_tree_file is not None:
                on_tree_file(current_path / name)
            # A branch is recognised by its logical name, not by its container:
            # the cloud mirror may store the same history compressed, and Codex
            # may store it in a container this version cannot open, which is
            # still that chat's history (CS-394).
            if not is_branch_file(name) and unsupported_container(name) is None:
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
    if unsupported_container(path.name) is not None:
        return _unsupported_container(path, root, lifecycle, source_machine)
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
    max_ordinal: int | None = None
    first_record_format = "unreadable"
    first_record: object = None
    first_record_readable = False
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
                if line_count == 1:
                    # The format check used to reopen this file and decode
                    # just this record. Capture that exact reading once.
                    try:
                        first_record = json.loads(line.decode("utf-8")) if line.strip() else None
                        first_record_readable = True
                    except (UnicodeError, ValueError):
                        pass
                    first_record_format = _first_record_format(first_record)
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
                    if line_count == 1:
                        if not first_record_readable:
                            codes.append("INVALID_RECORD")
                            continue
                        record = first_record
                    else:
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
                    if max_ordinal is None or ordinal > max_ordinal:
                        max_ordinal = ordinal
                else:
                    unnumbered += 1
                stamp = record.get("timestamp")
                if isinstance(stamp, str) and (last_record_at is None or _later(stamp, last_record_at)):
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
        max_ordinal=max_ordinal,
        first_record_format=first_record_format,
    )


def _moment(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _later(stamp: str, current: str) -> bool:
    """Whether ``stamp`` is a later moment than ``current`` (CS-466).

    Compared as moments: as text `...:01Z` outranks `...:01.900Z`. A stamp that
    is not a moment never displaces one that is; two that are not compare as text.
    """
    new, old = _moment(stamp), _moment(current)
    if new is not None and old is not None:
        return new > old
    if new is None and old is None:
        return stamp > current
    return old is None


def _unsupported_container(
    path: Path, root: Path, lifecycle: SessionState, source_machine: str | None
) -> SessionDescriptor:
    """A chat file this version cannot open, described without opening it as JSONL.

    The hash is of the stored bytes, so a change to the file still changes
    every plan that mentions it. The id comes from the file name: the file is
    then that chat's unreadable copy, which blocks the chat, rather than an
    unattributed file a copy might be planned next to.
    """
    relative = path.relative_to(root).as_posix()
    codes = [UNSUPPORTED_CONTAINER]
    digest = hashlib.sha256()
    byte_count = 0
    stat = None
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError:
        codes.append("READ_ERROR")
    match = _NAME_ID.search(Path(logical_name(path.name)).stem)
    if match:
        codes.append(ID_FROM_FILE_NAME)
    return SessionDescriptor(
        session_id=match.group(0).lower() if match else None,
        state=SessionState.INVALID,
        relative_path=relative,
        sha256=digest.hexdigest(),
        byte_count=byte_count,
        line_count=0,
        source_machine=source_machine,
        codes=tuple(codes),
        mtime_ns=stat.st_mtime_ns if stat is not None else None,
        file_id=_file_id(stat) if stat is not None else None,
        first_record_format=RECORD_FORMAT_UNSUPPORTED,
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
            if unsupported_container(path.name) is not None:
                formats[logical_relative_path(path.relative_to(root).as_posix())] = RECORD_FORMAT_UNSUPPORTED
                continue
            try:
                with open_jsonl(path) as handle:
                    first = handle.readline(max_line_bytes + 1)
                record = json.loads(first.decode("utf-8")) if first.strip() else None
            except (*JSONL_READ_ERRORS, UnicodeError, ValueError):
                record = None
            key = _first_record_format(record)
            formats[logical_relative_path(path.relative_to(root).as_posix())] = key
    return formats


def _first_record_format(record: object) -> str:
    if not isinstance(record, dict):
        return "unreadable"
    ordinal = record.get("ordinal")
    return (
        RECORD_FORMAT_ORDINAL
        if isinstance(ordinal, int) and not isinstance(ordinal, bool)
        else RECORD_FORMAT_LEGACY
    )


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
    scheduler: WorkScheduler | None = None,
    cancellation: CancellationToken | None = None,
) -> tuple[SessionCatalog, SessionCatalog]:
    """`.codex` and the cloud mirror, read at the same time. Reads only.

    One after the other they took 9 s + 30 s on the machine this was measured
    on, and a full sync reads both twice (the plan, then its rebuild before the
    write). The two hardly compete: `.codex` is plain JSONL parsed on the
    calling thread, the mirror is mostly xz unpacking, which releases the GIL.
    Each catalogue is exactly what `scan_sessions` returns for its side; the
    progress is one phase, ``sessions``, over both.
    """
    local, remote = _scan_catalogs(
        [(local_root, local_machine), (remote_root, remote_machine)],
        max_line_bytes=max_line_bytes, volatile=volatile, progress=progress,
        phase="sessions", scheduler=scheduler, cancellation=cancellation,
    )
    return local, remote
