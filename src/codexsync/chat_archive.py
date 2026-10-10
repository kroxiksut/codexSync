"""Codex's catalogue follows a chat moved into or out of the archive (D-035).

Archiving a chat in Codex moves its file from ``sessions/<y>/<m>/<d>/`` to
``archived_sessions/`` and rewrites its catalogue row. A sync carries that move
to the other machine as a file move (D-023), but the row there still names the
old path with the old flag: Codex lists the chat where it was and finds no
file (this is how the 2026-10-09 incident began, D-032).

This module finds exactly that state and nothing wider: a row whose file is
gone from the path it names, while a file of the very same name -- carrying
the thread id -- sits in the *other* state folder and is named by no other
row. The row is then pointed at that file with the archive state its folder
means.

The write is the three columns Codex's own thread upsert changes when a chat
is archived or brought back (``rollout_path``, ``archived``, ``archived_at``,
read from `codex.exe` 26.1002; Codex has no narrower statement for it), each
guarded by the values the plan saw, in one transaction. ``archived_at`` is the
file's modification time in seconds, which is what the rows Codex writes hold.
Observed on the Linux laptop on 2026-10-10: a chat archived this way showed in
Codex's archive and opened; brought back with Codex's own button, Codex moved
the file to the same path this module would have chosen.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Callable

from .exceptions import FailSafeError
from .peer_board import digest as _digest
from .sqlite_audit import PlacementStatus, ThreadArchiveRows

_PLAN_VERSION = 1
ACTIVE_DIR = "sessions"
ARCHIVE_DIR = "archived_sessions"
_EXTENDED_UNC = "\\\\?\\UNC\\"
_EXTENDED = "\\\\?\\"


@dataclass(frozen=True, slots=True)
class ArchiveChange:
    thread_id: str
    #: The row as it is now, which the write requires.
    path_before: str
    archived_before: int
    #: The row as it is written.
    path_after: str
    archived_after: int
    archived_at_after: int | None

    @property
    def archives(self) -> bool:
        return bool(self.archived_after)


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    plan_id: str
    database: str | None
    changes: tuple[ArchiveChange, ...] = ()
    #: Rows whose file is gone and could not be followed unambiguously.
    unresolved: int = 0
    codes: tuple[str, ...] = ()

    @property
    def writes(self) -> bool:
        return bool(self.changes)

    @property
    def archived(self) -> int:
        return sum(1 for item in self.changes if item.archives)

    @property
    def restored(self) -> int:
        return sum(1 for item in self.changes if not item.archives)


def _plain(value: str) -> str:
    if value.startswith(_EXTENDED_UNC):
        return "\\\\" + value[len(_EXTENDED_UNC):]
    if value.startswith(_EXTENDED):
        return value[len(_EXTENDED):]
    return value


def _relative(stored: str, root: Path) -> str | None:
    try:
        return Path(_plain(stored)).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _stored_like(stored: str, relative_before: str, relative_after: str) -> str | None:
    """``relative_after`` written in the same form as ``stored`` names ``relative_before``.

    The same prefix (an extended-length one included) and the same separator,
    so the row keeps looking the way Codex wrote it.
    """
    separator = "\\" if "\\" in stored else "/"
    tail = relative_before.replace("/", separator)
    if not stored.endswith(tail):
        return None
    return stored[: len(stored) - len(tail)] + relative_after.replace("/", separator)


def _active_candidates(root: Path, name: str) -> list[str]:
    """Where an active chat of this file name sits: its dated folder, else anywhere under sessions/."""
    parts = name.split("-", 1)
    if len(parts) == 2 and len(parts[1]) >= 10:
        year, month, day = parts[1][:4], parts[1][5:7], parts[1][8:10]
        dated = root / ACTIVE_DIR / year / month / day / name
        if dated.is_file():
            return [dated.relative_to(root).as_posix()]
    base = root / ACTIVE_DIR
    if not base.is_dir():
        return []
    return sorted(path.relative_to(root).as_posix() for path in base.rglob(name) if path.is_file())


def build_archive_plan(
    rows: ThreadArchiveRows,
    state_root: Path,
    *,
    fingerprint: Callable[[str], str] | None = None,
    mtime: Callable[[Path], float] = lambda path: path.stat().st_mtime,
) -> ArchivePlan:
    """Which rows to point at the file the archive move left them. Reads only."""
    if rows.status is not PlacementStatus.AVAILABLE:
        material = {"version": _PLAN_VERSION, "status": rows.status.value, "codes": list(rows.codes)}
        return ArchivePlan(_digest(material), rows.database, codes=rows.codes)
    root = state_root.resolve()
    named = {
        relative for relative in (_relative(path, root) for path, _, _ in rows.rows.values()) if relative
    }
    changes: list[ArchiveChange] = []
    unresolved = 0
    for thread_id, (stored, archived, _archived_at) in sorted(rows.rows.items()):
        relative = _relative(stored, root)
        if relative is None or (root / relative).exists():
            continue
        name = relative.rsplit("/", 1)[-1]
        if thread_id not in name:
            continue
        top = relative.split("/", 1)[0]
        if top == ACTIVE_DIR:
            found = [f"{ARCHIVE_DIR}/{name}"] if (root / ARCHIVE_DIR / name).is_file() else []
            archived_after = 1
        elif top == ARCHIVE_DIR:
            found = _active_candidates(root, name)
            archived_after = 0
        else:
            continue
        if not found:
            # Gone, not moved: nothing here to point the row at.
            continue
        if len(found) != 1 or found[0] in named:
            unresolved += 1
            continue
        path_after = _stored_like(stored, relative, found[0])
        if path_after is None:
            unresolved += 1
            continue
        archived_at = int(mtime(root / found[0])) if archived_after else None
        changes.append(ArchiveChange(thread_id, stored, archived, path_after, archived_after, archived_at))
    material = {
        "version": _PLAN_VERSION,
        "database": rows.database,
        "changes": [
            [item.thread_id, item.path_before, item.archived_before, item.path_after, item.archived_after,
             item.archived_at_after]
            for item in changes
        ],
        "unresolved": unresolved,
        "fingerprint": fingerprint(rows.database) if changes and fingerprint and rows.database else None,
    }
    return ArchivePlan(_digest(material), rows.database, tuple(changes), unresolved)


def apply_archive_changes(database: Path, changes: tuple[ArchiveChange, ...]) -> int:
    """Rewrite every row in one transaction, or none.

    Each row must still name the path and carry the flag the plan saw; one that
    moved rolls the whole transaction back.
    """
    connection = sqlite3.connect(str(database), timeout=5.0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for change in changes:
                changed = connection.execute(
                    "UPDATE threads SET rollout_path = ?, archived = ?, archived_at = ? "
                    "WHERE id = ? AND rollout_path = ? AND archived = ?",
                    (
                        change.path_after, change.archived_after, change.archived_at_after,
                        change.thread_id, change.path_before, change.archived_before,
                    ),
                ).rowcount
                if changed != 1:
                    raise FailSafeError(
                        "A chat's catalogue row changed after it was read; nothing was written. "
                        "Run the sync again."
                    )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.close()
    return len(changes)


def read_archive_back(database: Path, thread_ids: list[str]) -> dict[str, tuple[str, int, int | None] | None]:
    """The rows as stored now, read after the write with Codex closed."""
    connection = sqlite3.connect(str(database), timeout=5.0)
    try:
        result: dict[str, tuple[str, int, int | None] | None] = {}
        for thread_id in thread_ids:
            row = connection.execute(
                "SELECT rollout_path, archived, archived_at FROM threads WHERE id = ?", (thread_id,),
            ).fetchone()
            result[thread_id] = (row[0], row[1], row[2]) if row is not None else None
        return result
    finally:
        connection.close()
