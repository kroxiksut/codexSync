"""Ask Codex to rebuild its thread catalogue from the chat files (D-024).

Codex lists a chat from the ``threads`` table of ``state_5.sqlite``, not from
the folder, and fills that table from the files only while its
``backfill_state`` row says the work is not ``complete``. A chat file written
into ``sessions/`` afterwards -- every chat another machine started -- is
therefore on disk and invisible: observed on a laptop with 197 of them.

codexSync does not write thread rows. A row has some forty columns whose
meaning only Codex knows, and one filled differently from Codex is a broken
chat. What it does instead is put one row back into the state Codex itself
creates it in, so that Codex walks the files on its next start and writes every
row by its own rules. The evidence is Codex's own code (desktop 26.930.3930.0):
its migration inserts ``(1, 'pending', NULL, NULL, now)``, startup runs the
backfill while the status is anything but ``complete``, and its own diagnostic
advises starting with no state database so that the backfill rebuilds it.

Three things keep the write narrow. It happens only when a chat file sits
where the catalogue does not name it -- a session id the catalogue lacks, or
one whose recorded file is gone because the chat moved into the archive. It
changes exactly one row of one table, inside one transaction that re-reads the
status first. And it is not repeated for the same files: if Codex was asked
once and still does not list them, that is reported instead of asking again on
every sync (``ALREADY_ASKED``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .exceptions import FailSafeError
from .fs_links import is_link as _is_link
from .sqlite_audit import BackfillReading, PlacementStatus, ThreadPlacements

#: The status Codex writes when its backfill has run, and the one it starts from.
BACKFILL_COMPLETE = "complete"
BACKFILL_PENDING = "pending"
#: Where the reset was read from; recorded in the plan so a later Codex that
#: changes the meaning is a different plan, not a silent reuse.
OBSERVED_ON = "codex-desktop-26.930.3930.0"
#: The folders Codex keeps chat files in, relative to the state root.
CHAT_FOLDERS = ("sessions", "archived_sessions")

_ROLLOUT_NAME = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_PLAN_VERSION = 1


class RefreshStatus(str, Enum):
    #: Every chat file is where the catalogue says; nothing to ask.
    NOT_NEEDED = "NOT_NEEDED"
    #: Chat files the catalogue does not name, and Codex considers it complete.
    NEEDED = "NEEDED"
    #: Codex has not finished (or not started) its own backfill; it will walk
    #: the files on its next start anyway.
    ALREADY_PENDING = "ALREADY_PENDING"
    #: Codex was asked for exactly these files before and still does not list
    #: them. Asking again on every sync would change nothing.
    ALREADY_ASKED = "ALREADY_ASKED"
    #: No catalogue yet: Codex builds one from the files when it first starts.
    NO_CATALOGUE = "NO_CATALOGUE"
    #: The catalogue could not be read; nothing is written on a guess.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CatalogueRefreshPlan:
    plan_id: str
    status: RefreshStatus
    #: The catalogue database, relative to the state root.
    database: str | None
    backfill: str | None
    #: Chat files the catalogue does not name, relative POSIX paths.
    unnamed: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        return self.status is RefreshStatus.NEEDED


def is_link(path: Path) -> bool:
    try:
        return _is_link(path, path.lstat())
    except OSError:
        # An entry that cannot even be stat'ed is not walked into.
        return True


def chat_file_id(name: str) -> str | None:
    """The thread id a Codex chat file's name carries, or ``None``."""
    match = _ROLLOUT_NAME.match(name)
    return match.group(1).lower() if match else None


def find_unnamed_chat_files(state_root: Path, placements: ThreadPlacements) -> tuple[str, ...]:
    """Chat files Codex's catalogue does not lead to, sorted.

    A file counts when its thread id is not in the catalogue, or when the
    catalogue names a file for that id that no longer exists (a chat moved into
    or out of the archive). A second copy of a thread whose catalogued file is
    still there does not count: that is Codex's own leftover, and the catalogue
    already leads to the chat.
    """
    root = state_root.resolve()
    found: list[str] = []
    for folder in CHAT_FOLDERS:
        base = root / folder
        if not base.is_dir() or is_link(base):
            continue
        for directory, subdirs, files in os.walk(base):
            current = Path(directory)
            subdirs[:] = [name for name in subdirs if not is_link(current / name)]
            for name in files:
                thread_id = chat_file_id(name)
                if thread_id is None or not name.endswith(".jsonl"):
                    continue
                path = current / name
                if is_link(path):
                    continue
                relative = path.relative_to(root).as_posix()
                if not placements.knows(thread_id):
                    found.append(relative)
                    continue
                named = placements.placement_of(thread_id)
                if named is None or named == relative:
                    continue
                if not (root / Path(named)).is_file():
                    found.append(relative)
    return tuple(sorted(found))


def unnamed_digest(unnamed: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(unnamed).encode("utf-8")).hexdigest()


def database_fingerprint(database: Path) -> str:
    """SHA-256 over the database and whichever of its sidecars exist."""
    digest = hashlib.sha256()
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        digest.update(suffix.encode("ascii") + b"\0")
        if path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"absent")
        digest.update(b"\0")
    return digest.hexdigest()


def build_refresh_plan(
    state_root: Path,
    placements: ThreadPlacements,
    backfill: BackfillReading,
    *,
    asked_before: str | None = None,
) -> CatalogueRefreshPlan:
    """What a refresh would do now. Reads only.

    ``asked_before`` is the digest of the files a previous refresh on this
    machine was for (``unnamed_digest``), or ``None``.
    """
    root = state_root.resolve()
    if placements.status is PlacementStatus.ABSENT or backfill.status is PlacementStatus.ABSENT:
        status, unnamed, codes = RefreshStatus.NO_CATALOGUE, (), ()
    elif placements.status is not PlacementStatus.AVAILABLE or backfill.status is not PlacementStatus.AVAILABLE:
        status, unnamed = RefreshStatus.UNAVAILABLE, ()
        codes = tuple(dict.fromkeys((*placements.codes, *backfill.codes)))
    else:
        unnamed, codes = find_unnamed_chat_files(root, placements), ()
        if not unnamed:
            status = RefreshStatus.NOT_NEEDED
        elif backfill.backfill != BACKFILL_COMPLETE:
            status = RefreshStatus.ALREADY_PENDING
        elif asked_before is not None and asked_before == unnamed_digest(unnamed):
            status = RefreshStatus.ALREADY_ASKED
        else:
            status = RefreshStatus.NEEDED
    fingerprint = None
    if status is RefreshStatus.NEEDED and backfill.database is not None:
        fingerprint = database_fingerprint(root / Path(backfill.database))
    material = {
        "version": _PLAN_VERSION,
        "observed_on": OBSERVED_ON,
        "status": status.value,
        "database": backfill.database,
        "backfill": backfill.backfill,
        "unnamed": list(unnamed),
        "fingerprint": fingerprint,
    }
    plan_id = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CatalogueRefreshPlan(plan_id, status, backfill.database, backfill.backfill, unnamed, codes)


def reset_backfill(database: Path, *, now: int) -> None:
    """Put ``backfill_state`` back to the row Codex's migration creates.

    One transaction: the status is re-read under a write lock and must still be
    ``complete``, exactly one row changes, and anything else rolls back and
    raises. SQLite's own atomicity is the guarantee -- there is no moment at
    which the database holds half of this.
    """
    connection = sqlite3.connect(str(database), timeout=5.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute("SELECT status FROM backfill_state WHERE id = 1").fetchone()
            if row is None or row[0] != BACKFILL_COMPLETE:
                raise FailSafeError(
                    "Codex's backfill status changed after it was read; nothing was written. Run the sync again."
                )
            changed = connection.execute(
                "UPDATE backfill_state SET status = ?, last_watermark = NULL, last_success_at = NULL, "
                "updated_at = ? WHERE id = 1 AND status = ?",
                (BACKFILL_PENDING, int(now), BACKFILL_COMPLETE),
            ).rowcount
            if changed != 1:
                raise FailSafeError("Codex's backfill status could not be reset; nothing was written")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


BackfillRow = tuple[str | None, str | None, int | None, int | None]


def settle_backfill(database: Path, *, expected: BackfillRow, target: BackfillRow) -> None:
    """Put a rebuild Codex cannot finish back to ``complete`` (D-032).

    ``expected`` is the whole row as the plan read it -- status, watermark,
    last success and last change -- and it must still be exactly that under the
    write lock: a Codex that moved the row since (it took the rebuild up, or
    finished it) is never overwritten. ``target`` must say ``complete``. One
    transaction, one row, like `reset_backfill`.
    """
    if target[0] != BACKFILL_COMPLETE:
        raise FailSafeError("A repair only ever puts Codex's chat-list rebuild back to complete")
    connection = sqlite3.connect(str(database), timeout=5.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state WHERE id = 1"
            ).fetchone()
            if row is None or tuple(row) != tuple(expected):
                raise FailSafeError(
                    "Codex's chat-list rebuild changed after it was checked; nothing was written. Check again."
                )
            changed = connection.execute(
                "UPDATE backfill_state SET status = ?, last_watermark = ?, last_success_at = ?, updated_at = ? "
                "WHERE id = 1",
                tuple(target),
            ).rowcount
            if changed != 1:
                raise FailSafeError("Codex's chat-list rebuild could not be settled; nothing was written")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()


def read_backfill_row(database: Path) -> BackfillRow | None:
    """The whole row as stored now; same conditions as `read_backfill_status`."""
    connection = sqlite3.connect(str(database), timeout=5.0)
    try:
        row = connection.execute(
            "SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    return tuple(row) if row is not None else None  # type: ignore[return-value]


def read_snapshot_backfill_row(database: Path) -> BackfillRow | None:
    """The row a backup copy of the catalogue holds, opened immutable.

    The copy lives in codexSync's own backup folder and is never written; a
    copy that cannot be read is no evidence, so ``None``.
    """
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT status, last_watermark, last_success_at, updated_at FROM backfill_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return tuple(row) if row is not None else None  # type: ignore[return-value]


def read_backfill_status(database: Path) -> str | None:
    """The status as stored now, read through an ordinary connection.

    Used only after a write, with Codex closed and the database just opened
    read-write by `reset_backfill`, so a plain read creates nothing new.
    """
    connection = sqlite3.connect(str(database), timeout=5.0)
    try:
        row = connection.execute("SELECT status FROM backfill_state WHERE id = 1").fetchone()
    finally:
        connection.close()
    return row[0] if row is not None else None
