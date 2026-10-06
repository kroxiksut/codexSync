"""Copies of the valuable part of the Codex state directory (CS-276, `D-017`).

Every other backup codexSync makes is a side effect of a write: the files a
sync or a restore is about to replace. This one is a copy for its own sake --
the sessions, the global state, the SQLite catalogues -- taken on a schedule
the user sets, into a folder the user chose, so a machine that loses `.codex`
entirely still has yesterday's working state.

Four rules carry it.

*Only while Codex is closed.* A copy of a SQLite file taken while its writer
runs, or of a session file between two appended lines, is a copy of nothing in
particular. The copy therefore goes through the safety gate like a mutation --
a continuously stopped window before the first file, a final check before the
archive is committed -- and with ``wait`` it *waits* for Codex to close
instead of refusing, which is what a task started at sign-in or on a timer
needs. Every file is also stat-checked before and after it is read: one that
moved while it was copied fails the whole copy rather than recording a torn
file.

*Never a secret.* `auth.json`, `cap_sid` and `.sandbox-secrets` hold tokens
(`AI_RULES` 1); they are refused by name at any depth, whatever the selection
says.

*Nothing is replaced until the new copy is proven.* The archive is written as
``<name>.zip.partial``, every entry is read back and hashed against the source,
and only then renamed to its final name. Old copies are pruned after that and
only by the exact name pattern this module writes, and only this machine's.

*Read-only towards `.codex`.* Nothing here opens a file in the state directory
for writing, creates one there, or runs SQLite against it: a database is
copied as bytes, together with its `-wal`/`-shm`, which with no writer running
is the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import stat
import time
from typing import Callable, Iterable
import zipfile

from .exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from .fs_links import is_link
from .native_programs import is_native_program
from .guardian_models import normalize_machine_id
from .models import AppConfig
from .operation_lock import OperationLock
from .progress import ProgressCallback, report
from .safety_gate import OperationKind, ProcessState, SafetyGate
from .state_locator import locate_local_state_dir
from .sync_candidates import SECRET_NAMES
from .version import PRODUCER_VERSION
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_MANIFEST",
    "StateBackupEntry",
    "StateBackupResult",
    "create_state_backup",
    "list_state_backups",
    "select_state_files",
]

FORMAT = "codexsync-state-backup-v1"
#: The manifest inside every archive. No name in `.codex` starts like this.
ARCHIVE_MANIFEST = ".codexsync-state-backup.json"
PARTIAL_SUFFIX = ".partial"
_NAME = re.compile(r"^codex-(?P<machine>.+)-(?P<stamp>\d{8}T\d{6}Z)\.zip$")

#: What is worth a copy, relative to the state directory. A directory means
#: everything under it; a name with `*` is matched against the top level.
#: The SQLite files are named by pattern because their numeric suffix is the
#: runtime's schema version (`state_5.sqlite`) and changes with it.
VALUABLE: tuple[str, ...] = (
    "sessions",
    "archived_sessions",
    ".codex-global-state.json",
    ".codex-global-state.json.bak",
    "session_index.jsonl",
    "config.toml",
    "AGENTS.md",
    "rules",
    "skills",
    "memories",
    "automations",
    "state_*.sqlite",
    "state_*.sqlite-wal",
    "state_*.sqlite-shm",
    "thread_history_*.sqlite",
    "thread_history_*.sqlite-wal",
    "thread_history_*.sqlite-shm",
    "memories_*.sqlite",
    "memories_*.sqlite-wal",
    "memories_*.sqlite-shm",
    "goals_*.sqlite",
    "goals_*.sqlite-wal",
    "goals_*.sqlite-shm",
)
#: Never copied, at any depth, whatever `VALUABLE` says.
_SKIPPED_SUFFIXES = (".tmp", ".temp", ".lock", ".codexsync.tmp")

#: How often a waiting copy asks whether Codex has closed.
DEFAULT_POLL_SECONDS = 30.0
#: How long a scheduled copy waits before giving up with "Codex is open". The
#: task's own time limit is set above this, so the program -- not the OS --
#: ends the wait and says why.
DEFAULT_MAX_WAIT_SECONDS = 23 * 3600
_CHUNK = 1024 * 1024
#: Entries larger than this are written with ZIP64 headers from the start.
_ZIP64_FROM = 1 << 30


@dataclass(frozen=True, slots=True)
class StateBackupEntry:
    """One committed copy in the backup folder."""

    name: str
    path: Path
    machine: str
    created_utc: str
    size: int
    #: Written by this machine (only these are pruned by this machine).
    own: bool


@dataclass(frozen=True, slots=True)
class StateBackupResult:
    path: Path
    files: int
    bytes: int
    #: How long the copy waited for Codex to close, in seconds.
    waited_seconds: float
    #: Copies of this machine removed after this one was committed.
    pruned: tuple[str, ...] = field(default_factory=tuple)


def _is_secret(relative: str) -> bool:
    return any(part in SECRET_NAMES for part in relative.split("/"))


def _is_skipped(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_SKIPPED_SUFFIXES) or name == ARCHIVE_MANIFEST


def select_state_files(state_dir: Path) -> list[tuple[str, Path]]:
    """Every file a copy takes, as ``(relative path, absolute path)``, sorted.

    Listing (`iterdir`/`os.walk` and `lstat`), plus the first bytes of each
    file to leave compiled programs out (D-021). Links -- symlinks and
    Windows junctions alike (``fs_links``) -- are not followed and not copied:
    a link inside `.codex` may point anywhere at all, and `skills` routinely
    holds junctions into folders outside it. A folder that cannot be listed
    fails the copy rather than leaving a silent hole in it.
    """
    chosen: dict[str, Path] = {}
    try:
        top = sorted(state_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise FailSafeError(f"Cannot list the Codex state directory {state_dir}: {exc}") from exc
    for entry in top:
        name = entry.name
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in VALUABLE):
            continue
        if _is_secret(name) or _is_skipped(name):
            continue
        info = _lstat(entry)
        if is_link(entry, info):
            continue
        if stat.S_ISREG(info.st_mode):
            if not is_native_program(entry):
                chosen[name] = entry
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        for folder, directories, files in os.walk(entry, followlinks=False, onerror=_unlistable):
            base = Path(folder)
            relative_base = base.relative_to(state_dir).as_posix()
            directories[:] = sorted(
                item for item in directories
                if item not in SECRET_NAMES and not is_link(base / item, _lstat(base / item))
            )
            for file_name in sorted(files):
                relative = f"{relative_base}/{file_name}"
                if _is_secret(relative) or _is_skipped(file_name):
                    continue
                path = base / file_name
                info = _lstat(path)
                if is_link(path, info) or not stat.S_ISREG(info.st_mode):
                    continue
                # A program is reinstalled, never restored from a copy (D-021).
                if is_native_program(path):
                    continue
                chosen[relative] = path
    return sorted(chosen.items())


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise FailSafeError(f"Cannot read {path} in the Codex state directory: {exc}") from exc


def _unlistable(exc: OSError) -> None:
    """``os.walk`` skips a folder it cannot list unless told otherwise."""
    raise FailSafeError(
        f"Cannot list {exc.filename} in the Codex state directory: {exc}"
    ) from exc


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _machine(cfg: AppConfig) -> str:
    return normalize_machine_id(cfg.identity.machine_id or platform.node()) or "unknown-machine"


def _require_root(cfg: AppConfig) -> Path:
    root = cfg.state_backup.root_dir
    if root is None:
        raise ConfigError(
            "state_backup.root_dir is not set: choose the folder for copies of the Codex state first"
        )
    return root


def _parse_name(name: str) -> tuple[str, datetime] | None:
    """The machine and time a copy's name records, or ``None`` if it is not one.

    A name that matches the pattern but holds no real time (a hand-renamed
    file, another tool's) is not a copy: it is neither listed nor pruned.
    """
    match = _NAME.match(name)
    if match is None:
        return None
    try:
        created = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return match.group("machine"), created


def list_state_backups(cfg: AppConfig) -> list[StateBackupEntry]:
    """Committed copies in the folder, newest first. Reads names and sizes only."""
    root = cfg.state_backup.root_dir
    if root is None or not root.is_dir():
        return []
    own = _machine(cfg)
    found: list[StateBackupEntry] = []
    for path in root.iterdir():
        parsed = _parse_name(path.name)
        if parsed is None:
            continue
        machine, created = parsed
        # One odd file must not hide the others: a folder a cloud client
        # syncs may lose a file between the listing and this read.
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        found.append(StateBackupEntry(
            name=path.name,
            path=path,
            machine=machine,
            created_utc=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            size=size,
            own=machine == own,
        ))
    found.sort(key=lambda entry: (entry.created_utc, entry.name), reverse=True)
    return found


def _wait_until_closed(
    gate: SafetyGate,
    *,
    wait: bool,
    poll_seconds: float,
    max_wait_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> float:
    """Return once the gate sees a continuously stopped Codex; the seconds waited.

    An undetermined process state is never waited out: it fails closed at once,
    exactly as a mutation would.
    """
    started = monotonic()
    announced = False
    while True:
        decision = gate.check(OperationKind.STATE_BACKUP)
        if decision.allowed:
            return monotonic() - started
        if decision.process_state is not ProcessState.RUNNING:
            raise FailSafeError(f"Cannot safely verify that Codex is closed; no copy was taken. {decision.reason}")
        if not wait:
            raise SafetyPreconditionError(
                f"Codex is running; a copy of its state is taken only while it is closed. {decision.reason}"
            )
        waited = monotonic() - started
        if waited >= max_wait_seconds:
            raise SafetyPreconditionError(
                f"Codex stayed open for {int(waited)} s; no copy was taken this time. {decision.reason}"
            )
        if not announced:
            LOG.info("Codex is open; the copy waits until it closes (%s)", decision.reason)
            announced = True
        sleep(poll_seconds)


def _copy_into(archive: zipfile.ZipFile, relative: str, source: Path) -> tuple[str, int]:
    """Stream one file into the archive; its sha256 and size as read.

    The file must look the same before and after: a writer that touched it in
    between means this copy is not a copy of any one moment.
    """
    before = source.stat()
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as handle, archive.open(
        relative, "w", force_zip64=before.st_size >= _ZIP64_FROM
    ) as target:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            target.write(chunk)
            size += len(chunk)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise FailSafeError(f"{relative} changed while it was being copied; no copy was kept")
    return digest.hexdigest(), size


def _verify(path: Path, entries: Iterable[dict]) -> None:
    with zipfile.ZipFile(path) as archive:
        for entry in entries:
            digest = hashlib.sha256()
            with archive.open(entry["path"]) as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise FailSafeError(f"The copy of {entry['path']} does not read back as written")


def _prune(root: Path, machine: str, keep: int, *, current: str) -> tuple[str, ...]:
    own = [
        path for path in root.iterdir()
        if (parsed := _parse_name(path.name)) is not None and parsed[0] == machine and path.is_file()
    ]
    own.sort(key=lambda path: path.name, reverse=True)
    removed: list[str] = []
    for stale in own[keep:]:
        if stale.name == current:  # pragma: no cover - the newest is never beyond keep >= 1
            continue
        stale.unlink()
        LOG.info("Removed an old copy of the Codex state: %s", stale.name)
        removed.append(stale.name)
    return tuple(removed)


def _sweep_partials(root: Path, machine: str) -> None:
    """Left-overs of a copy this machine was killed during. Held under the lock."""
    for path in root.iterdir():
        name = path.name
        if not name.endswith(PARTIAL_SUFFIX):
            continue
        match = _NAME.match(name[: -len(PARTIAL_SUFFIX)])
        if match is not None and match.group("machine") == machine and path.is_file():
            path.unlink()
            LOG.info("Removed an unfinished copy: %s", name)


def create_state_backup(
    cfg: AppConfig,
    *,
    gate: SafetyGate,
    wait: bool = False,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    progress: ProgressCallback | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> StateBackupResult:
    """Take one verified copy of the valuable part of `.codex`, then prune."""
    root = _require_root(cfg)
    # Proven clear of the folder actually read, not only of the configured one.
    state_dir = locate_local_state_dir(cfg)
    machine = _machine(cfg)
    waited = _wait_until_closed(
        gate, wait=wait, poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds,
        monotonic=monotonic, sleep=sleep,
    )
    root.mkdir(parents=True, exist_ok=True)
    with OperationLock(cfg.paths.temp_dir, state_root=state_dir, machine_id=machine, family="state-backup"):
        _sweep_partials(root, machine)
        files = select_state_files(state_dir)
        if not files:
            raise FailSafeError(f"Nothing to copy: {state_dir} holds none of the files a copy takes")
        created = now()
        name = f"codex-{machine}-{_stamp(created)}.zip"
        final = root / name
        if final.exists():
            raise FailSafeError(f"A copy named {name} already exists; nothing was replaced")
        partial = root / (name + PARTIAL_SUFFIX)
        entries: list[dict] = []
        total = 0
        LOG.info("Copying %d files of the Codex state into %s", len(files), final)
        try:
            with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for index, (relative, source) in enumerate(files):
                    report(progress, "state_backup", index, len(files))
                    try:
                        digest, size = _copy_into(archive, relative, source)
                    except FileNotFoundError:
                        # Gone between the listing and the read: with Codex
                        # closed that is a file something else removed. The
                        # copy describes what was there when it was read.
                        LOG.warning("Skipped %s: it disappeared before it was copied", relative)
                        continue
                    except PermissionError as exc:
                        raise FailSafeError(f"Cannot read {relative}: {exc}; no copy was kept") from exc
                    entries.append({"path": relative, "size": size, "sha256": digest})
                    total += size
                report(progress, "state_backup", len(files), len(files))
                manifest = {
                    "format": FORMAT,
                    "producer": PRODUCER_VERSION,
                    "machine": machine,
                    "created_at_utc": created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "files": len(entries),
                    "bytes": total,
                    "entries": entries,
                }
                archive.writestr(ARCHIVE_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            _verify(partial, entries)
            with partial.open("rb+") as handle:
                os.fsync(handle.fileno())
            # The last word belongs to the gate: a Codex that opened during the
            # copy may have written after a file was read.
            gate.require(OperationKind.STATE_BACKUP, final=True)
            replace_with_retry(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        LOG.info("Copy of the Codex state committed: %s (%d files, %d bytes)", final.name, len(entries), total)
        pruned = _prune(root, machine, int(cfg.state_backup.keep), current=name)
    return StateBackupResult(final, len(entries), total, waited, pruned)
