"""Streaming, read-only catalog of active and archived Codex sessions."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import BinaryIO, Callable, Iterator

from .cancellation import CancellationToken
from .fs_links import is_link
from .fs_replace import write_json_atomically
from .jsonl_codec import (
    JSONL_READ_ERRORS,
    JsonlCodec,
    READABLE_CODECS,
    codec_of,
    is_branch_file,
    logical_name,
    logical_relative_path,
    open_jsonl,
    unsupported_container,
)
from .progress import ProgressCallback, report
from .shared_read import open_shared, windows_change_time
from .work_scheduler import WorkScheduler


DEFAULT_MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024

#: Compressed files unpacked at once. Unpacking the mirror's xz containers is
#: most of a mirror scan (19 s of 28 s on the machine this was measured on) and
#: releases the GIL, so threads run it in parallel: 30 s -> 14 s with four.
#: A plain file is the opposite -- parsing its JSON holds the GIL, and four
#: threads made a 9 s scan of `.codex` take 10-13 s -- so plain files stay on
#: the calling thread.
SCAN_WORKERS = max(1, min(4, os.cpu_count() or 1))

# Starting child interpreters costs more than threads on a small catalogue.
# Above this physical input size, however, parsing every JSON record is the
# dominant work and holds the GIL.  A bounded process pool then keeps the exact
# full-byte scan while allowing independent branches to use separate cores.
PROCESS_SCAN_MIN_BYTES = 128 * 1024 * 1024
_PARSE_CACHE_ABI = 1
_PARSE_CACHE_LIMIT = 16_384
_PARSE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_PARSE_FIELDS = (
    "session_id", "sha256", "byte_count", "line_count", "cwd", "timestamp", "parent_id",
    "record_format", "last_record_at", "page", "max_ordinal", "first_record_format",
)


def _cache_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _cache_context() -> list:
    return [_PARSE_CACHE_ABI, list(sys.version_info[:3]), [codec.value for codec in READABLE_CODECS]]


class SessionParseCache:
    """Local and disposable. Reading a cache never writes it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self.dirty = False
        try:
            with path.open("rb") as handle:
                body = handle.read(_PARSE_CACHE_MAX_BYTES + 1)
            if len(body) > _PARSE_CACHE_MAX_BYTES:
                return
            saved = json.loads(body)
            data = saved["data"]
            if data["context"] == _cache_context() and _cache_digest(data) == saved["sha256"]:
                self.entries = data["entries"] if isinstance(data["entries"], dict) else {}
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            pass

    def remember(self, key: str, item: dict) -> None:
        if self.entries.get(key) != item:
            self.entries.pop(key, None)
            self.entries[key] = item
            self.dirty = True

    def publish(self) -> None:
        """Publish only after the operation using these reads has succeeded."""
        if not self.dirty:
            return
        try:
            recent = dict(list(self.entries.items())[-_PARSE_CACHE_LIMIT:])
            data = {"context": _cache_context(), "entries": recent}
            write_json_atomically(self.path, {"data": data, "sha256": _cache_digest(data)})
        except (OSError, ValueError, RecursionError):
            return
        self.dirty = False


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
    #: The completed scan's file identity and line limit. A descriptor made
    #: outside the scanner cannot substitute for reading a branch comparison.
    scan_signature: tuple[int, int, str | None] | None = field(default=None, compare=False, repr=False)
    scan_change_stamp: int | None = field(default=None, compare=False, repr=False)
    scan_line_limit: int | None = field(default=None, compare=False, repr=False)

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
    parse_cache: SessionParseCache | None = None,
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
        parse_cache=parse_cache,
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
    parse_cache: SessionParseCache | None = None,
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
    cache_keys = [_cache_digest(str(path)) for path, *_rest in jobs] if parse_cache is not None else []
    cache_inputs = [parse_cache.entries.get(key) for key in cache_keys] if parse_cache is not None else []
    cache_updates: list[dict | None] = [None] * total if parse_cache is not None else []

    def read(index: int) -> SessionDescriptor:
        if cancellation is not None:
            cancellation.checkpoint()
        path, root, state, machine = jobs[index]
        if parse_cache is not None:
            item, entry = _scan_cached_jsonl(
                path, root, state, max_line_bytes, volatile, machine, cache_inputs[index],
            )
            if entry is not None:
                cache_updates[index] = entry
            return item
        return _scan_jsonl(path, root, state, max_line_bytes, volatile, machine)

    def store(index: int, item: SessionDescriptor) -> None:
        nonlocal done
        results[index] = item
        done += 1
        report(progress, phase, done, total)

    process_scan = SCAN_WORKERS > 1 and _physical_bytes(jobs) >= PROCESS_SCAN_MIN_BYTES
    if process_scan:
        process_jobs = [
            (path, root, state, max_line_bytes, volatile, machine)
            for path, root, state, machine in jobs
        ]

        def process_result(index: int, _job, item) -> None:
            if parse_cache is None:
                store(index, item)
            else:
                descriptor, entry = item
                cache_updates[index] = entry
                store(index, descriptor)

        worker = _scan_process_job
        if parse_cache is not None:
            worker = _scan_cached_process_job
            process_jobs = [(*job, entry) for job, entry in zip(process_jobs, cache_inputs)]

        if scheduler is not None:
            scheduler.map_process(
                worker, process_jobs, on_result=process_result,
                cancellation=cancellation,
            )
        else:
            with WorkScheduler(
                cpu_workers=SCAN_WORKERS, io_workers=SCAN_WORKERS,
                large_file_limit=SCAN_WORKERS,
            ) as owned:
                owned.map_process(
                    worker, process_jobs, on_result=process_result,
                    cancellation=cancellation,
                )
    else:
        _read_catalog_jobs(
            jobs, store, read, scheduler=scheduler,
            cancellation=cancellation,
        )

    catalogs: list[SessionCatalog] = []
    if cancellation is not None:
        cancellation.checkpoint()
    if parse_cache is not None:
        for key, entry in zip(cache_keys, cache_updates):
            if entry is not None:
                parse_cache.remember(key, entry)
    offset = 0
    for size in sizes:
        descriptors = [item for item in results[offset:offset + size] if item is not None]
        catalogs.append(_catalog_from_descriptors(descriptors, volatile))
        offset += size
    return catalogs


def _physical_bytes(jobs: list[tuple[Path, Path, SessionState, str | None]]) -> int:
    """Best-effort size used only to choose an execution strategy."""
    total = 0
    for path, *_rest in jobs:
        try:
            total += path.stat().st_size
        except OSError:
            # The real reader records a vanished/unreadable branch.  Selection
            # of a pool must not turn that evidence into a scan failure.
            pass
    return total


def _scan_process_job(
    job: tuple[Path, Path, SessionState, int, bool, str | None],
) -> SessionDescriptor:
    """Picklable process entry point for one independent branch."""
    path, root, state, max_line_bytes, volatile, machine = job
    return _scan_jsonl(path, root, state, max_line_bytes, volatile, machine)


def _scan_cached_process_job(job) -> tuple[SessionDescriptor, dict | None]:
    return _scan_cached_jsonl(*job)


class _PhysicalReader(io.RawIOBase):
    """Hash the bytes supplied to the decoder, including its read-ahead."""

    def __init__(self, raw: BinaryIO) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        count = self.raw.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.size += count
        return count

    def finish(self) -> list:
        # A decoder may stop at a valid member without reading until the
        # physical EOF. Include whatever is left in the fingerprint.
        while chunk := self.raw.read(1024 * 1024):
            self.digest.update(chunk)
            self.size += len(chunk)
        return [self.digest.hexdigest(), self.size]


def _scan_cached_jsonl(
    path: Path, root: Path, state: SessionState, max_line_bytes: int,
    volatile: bool, machine: str | None, entry: dict | None,
) -> tuple[SessionDescriptor, dict | None]:
    codec = codec_of(path)
    if codec is None:
        return _scan_jsonl(path, root, state, max_line_bytes, volatile, machine), None
    context = [codec.value, max_line_bytes]
    try:
        with open_shared(path) as raw:
            info = os.fstat(raw.fileno())
            signature = _signature(info)
            stamp = _handle_change_stamp(raw, info)
            if stamp is not None and _file_id(info) is not None:
                if isinstance(entry, dict) and entry.get("options") == context:
                    physical = _PhysicalReader(raw).finish()
                    if physical == entry.get("physical") and physical[1] == info.st_size:
                        cached = _cached_descriptor(
                            entry, path, root, state, machine, info, stamp, max_line_bytes,
                        )
                        if cached is not None and matches_session_scan(root, path, cached, require_change_stamp=True):
                            return cached, None
                    raw.seek(0)
                physical_reader = _PhysicalReader(raw)
                with io.BufferedReader(physical_reader) as reader:
                    item = _scan_jsonl(path, root, state, max_line_bytes, volatile, machine, raw=reader)
                if item.scan_signature is None:
                    return item, None
                physical = physical_reader.finish()
                if (
                    item.scan_signature == signature and item.scan_change_stamp == stamp
                    and physical[1] == info.st_size
                    and matches_session_scan(root, path, item, require_change_stamp=True)
                ):
                    return item, {
                        "options": context, "physical": physical,
                        "fields": {name: getattr(item, name) for name in _PARSE_FIELDS},
                        "codes": [code for code in item.codes if code != "FILENAME_ID_MISMATCH"],
                    }
                return _with_code(item, SessionState.INVALID, "READ_CHANGED"), None
    except OSError:
        # Let the standard scanner handle disappearing files, optional
        # identity support and shared-handle errors using its normal codes.
        pass
    return _scan_jsonl(path, root, state, max_line_bytes, volatile, machine), None


def _cached_descriptor(
    entry: dict, path: Path, root: Path, state: SessionState, machine: str | None,
    info: os.stat_result, stamp: int, max_line_bytes: int,
) -> SessionDescriptor | None:
    fields, codes = entry.get("fields"), entry.get("codes")
    if not isinstance(fields, dict) or fields.keys() != set(_PARSE_FIELDS):
        return None
    if not isinstance(codes, list) or any(not isinstance(code, str) or code in INVALID_CODES for code in codes):
        return None
    if any(not isinstance(fields[name], str) or not fields[name] for name in ("session_id", "sha256", "first_record_format")):
        return None
    if any(type(fields[name]) is not int or fields[name] <= 0 for name in ("line_count", "byte_count")):
        return None
    if any(fields[name] is not None and not isinstance(fields[name], str) for name in (
        "cwd", "timestamp", "parent_id", "record_format", "last_record_at", "page",
    )):
        return None
    if fields["max_ordinal"] is not None and type(fields["max_ordinal"]) is not int:
        return None
    if fields["page"] is not None and re.fullmatch(r"page-[0-9]+", fields["page"]) is None:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", fields["sha256"]) is None:
        return None
    if fields["session_id"] not in Path(logical_name(path.name)).stem:
        codes = [*codes, "FILENAME_ID_MISMATCH"]
    return SessionDescriptor(
        state=state, relative_path=path.relative_to(root).as_posix(), source_machine=machine,
        codes=tuple(codes), mtime_ns=info.st_mtime_ns, file_id=_file_id(info),
        scan_signature=_signature(info), scan_change_stamp=stamp, scan_line_limit=max_line_bytes,
        **fields,
    )


def _read_catalog_jobs(
    jobs: list[tuple[Path, Path, SessionState, str | None]],
    store: Callable[[int, SessionDescriptor], None],
    read: Callable[[int], SessionDescriptor],
    *,
    scheduler: WorkScheduler | None,
    cancellation: CancellationToken | None,
) -> None:
    """Existing low-overhead path for catalogues too small for processes."""
    total = len(jobs)
    packed = [index for index, (path, *_rest) in enumerate(jobs)
              if codec_of(path) not in (None, JsonlCodec.NONE)]
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
    *,
    raw: BinaryIO | None = None,
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
    before_change = _change_stamp(path, before)
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
        with (open_jsonl(path) if raw is None else open_jsonl(path, raw=raw)) as handle:
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
    after_change = _change_stamp(path, after) if after is not None else None
    if (
        after is None or _signature(before) != _signature(after)
        or before_change is not None and after_change is not None and before_change != after_change
    ):
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
        scan_signature=_signature(after) if state is lifecycle and complete_tail else None,
        scan_change_stamp=after_change if before_change == after_change else None,
        scan_line_limit=max_line_bytes,
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


def _change_stamp(path: Path, info: os.stat_result) -> int | None:
    if os.name != "nt":
        return getattr(info, "st_ctime_ns", None)
    # Python's st_ctime on Windows is creation time. Use the native
    # change time and require this handle to name the same file as stat.
    try:
        with open_shared(path) as handle:
            if _file_id(info) is not None and _signature(os.fstat(handle.fileno())) == _signature(info):
                return _handle_change_stamp(handle, info)
    except OSError:
        pass
    return None


def _handle_change_stamp(handle: BinaryIO, info: os.stat_result) -> int | None:
    if os.name == "nt":
        return windows_change_time(handle) or None
    return getattr(info, "st_ctime_ns", None)


def matches_session_scan(
    root: Path, path: Path, item: SessionDescriptor, *, require_change_stamp: bool = False,
) -> bool:
    """Check whether the path still names the file read by the full scan."""
    if item.scan_signature is None:
        return False
    try:
        _require_inside(path, root.resolve())
        info = path.stat(follow_symlinks=False)
        if is_link(path, info) or _signature(info) != item.scan_signature:
            return False
        current = _change_stamp(path, info) if item.scan_change_stamp is not None else None
        if current is None:
            return not require_change_stamp
        return current == item.scan_change_stamp
    except (OSError, RuntimeError):
        return False


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
    parse_cache: SessionParseCache | None = None,
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
        parse_cache=parse_cache,
    )
    return local, remote
