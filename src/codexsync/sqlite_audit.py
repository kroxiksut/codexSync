"""Strictly read-only, schema-level audit of Codex SQLite assets.

Read-only here has to mean read-only about the *files*, not merely about the
rows, and SQLite makes that harder than it looks. Opening a WAL database
creates ``-wal`` and ``-shm`` beside it if they are absent, in C, below every
Python-level guard this project has -- measured: a plain ``mode=ro`` open of a
cleanly closed database created both files inside the state directory. That is
the one thing `doctor` and every scan must never do, and it is invisible to
`tests/test_guardian_state_isolation.py` because no Python call is involved.

So the connection is chosen by what is already on disk (`_read_only_connect`):

* no ``-wal`` -- nothing is pending, so the main file holds the whole truth and
  ``immutable=1`` reads it without any WAL machinery and creates nothing. The
  absence is re-checked afterwards, because Codex could have started meanwhile,
  and a ``-wal`` that appeared makes the reading indeterminate rather than
  merely old.
* a ``-wal`` with a ``-shm`` beside it -- the normal running case. Both files
  exist already, so opening creates nothing; the ``-shm`` mtime moves, which is
  what any reader including Codex does to it, and its bytes do not change.
* a ``-wal`` with no ``-shm`` -- opening would create one. Refused as
  ``INDETERMINATE``: a crashed writer's database is exactly where guessing is
  least affordable, and no reading is worth a write into `.codex`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
import sqlite3
from typing import Callable, TypeVar
from urllib.parse import quote


class SQLiteRole(str, Enum):
    THREAD_CATALOG = "THREAD_CATALOG"
    LOCAL_APP_DATA = "LOCAL_APP_DATA"
    DERIVED_SUMMARIES = "DERIVED_SUMMARIES"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SQLiteAsset:
    relative_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class SQLiteSet:
    database: SQLiteAsset
    sidecars: tuple[SQLiteAsset, ...]


@dataclass(frozen=True, slots=True)
class SQLiteAuditReport:
    asset_set: SQLiteSet
    role: SQLiteRole
    status: str
    schema_digest: str | None
    user_version: int | None
    application_id: int | None
    journal_mode: str | None
    table_count: int
    index_count: int
    trigger_count: int
    quick_check_errors: int | None = None
    foreign_key_errors: int | None = None
    codes: tuple[str, ...] = ()


class PlacementStatus(str, Enum):
    #: No thread catalogue database exists, so nothing constrains a placement.
    ABSENT = "ABSENT"
    #: The catalogue was read and its rows are below.
    AVAILABLE = "AVAILABLE"
    #: A catalogue exists but could not be read — busy, locked, or a schema
    #: without the columns this needs. Not the same as ABSENT, and callers must
    #: not treat it as one: it means the runtime may well be the authority here
    #: and we simply cannot see what it says.
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class ThreadPlacements:
    """Where the runtime records each thread's rollout file, and nothing else.

    Only three columns are read — the thread id, its rollout path and its
    archived flag — because they are the placement. `title`, `preview` and
    `first_user_message` sit in the same table and are never touched.

    A path is kept only when it resolves inside the state root, as a relative
    POSIX path directly comparable to a transfer plan's destination. A path
    pointing outside is recorded as unknown rather than normalised into
    something that looks local.
    """
    status: PlacementStatus
    by_session: dict[str, str | None]
    archived: frozenset[str] = frozenset()
    codes: tuple[str, ...] = ()

    def placement_of(self, session_id: str) -> str | None:
        return self.by_session.get(session_id)

    def knows(self, session_id: str) -> bool:
        return session_id in self.by_session


def read_thread_placements(
    state_root: Path, *, timeout_seconds: float = 2.0
) -> ThreadPlacements:
    """Rollout path per thread, from the thread catalogue, strictly read-only.

    This exists so a write into the Codex state directory can be refused when
    the runtime would not look at it. On an observed machine every session on
    disk has a catalogue row carrying a rollout path, which means the file
    system is not the whole truth about where a session lives: putting a branch
    somewhere the catalogue does not name makes it invisible, with no error.
    """
    root = state_root.resolve()
    discovered = discover_sqlite_sets(root)
    unopenable = [
        item for item in discovered
        if _would_create_a_sidecar(root / Path(item.database.relative_path))
    ]
    if unopenable:
        # A database we declined to open is not a database that is not there.
        # `ABSENT` constrains nothing and would let a write proceed unchecked;
        # the honest answer is that the catalogue could not be consulted.
        return ThreadPlacements(
            PlacementStatus.INDETERMINATE, {}, codes=(WAL_WITHOUT_SHARED_INDEX,)
        )
    shapes = [(item, _looks_like_thread_catalogue(root, item, timeout_seconds)) for item in discovered]
    if any(shape is None for _, shape in shapes):
        # A database whose tables could not be listed may be the catalogue.
        # Reading that as "no catalogue" would be `ABSENT`, which constrains
        # nothing; what is actually known is that the runtime's answer could
        # not be seen.
        return ThreadPlacements(
            PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",)
        )
    catalogues = [item for item, shape in shapes if shape]
    if not catalogues:
        return ThreadPlacements(PlacementStatus.ABSENT, {})

    by_session: dict[str, str | None] = {}
    archived: set[str] = set()
    codes: list[str] = []
    for item in catalogues:
        database = root / Path(item.database.relative_path)
        immutable = not _has_pending_frames(database)
        try:
            connection = _read_only_connect(database, timeout_seconds)
        except sqlite3.Error:
            return ThreadPlacements(
                PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",)
            )
        if connection is None:
            # Opening would create a shared-index file inside the state root.
            return ThreadPlacements(
                PlacementStatus.INDETERMINATE, {}, codes=(WAL_WITHOUT_SHARED_INDEX,)
            )
        try:
            for thread_id, rollout, is_archived in connection.execute(
                "SELECT id, rollout_path, archived FROM threads"
            ):
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                by_session[thread_id] = _relative_rollout(rollout, root)
                if is_archived:
                    archived.add(thread_id)
        except sqlite3.Error:
            return ThreadPlacements(
                PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNREADABLE",)
            )
        finally:
            connection.close()
        if _wal_appeared(database, immutable):
            # A writer started mid-read, so this picture was never a whole state.
            return ThreadPlacements(
                PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_CHANGED",)
            )
    if any(value is None for value in by_session.values()):
        codes.append("ROLLOUT_PATH_OUTSIDE_STATE_ROOT")
    return ThreadPlacements(
        PlacementStatus.AVAILABLE, by_session, frozenset(archived), tuple(dict.fromkeys(codes))
    )


@dataclass(frozen=True, slots=True)
class BackfillReading:
    """Whether Codex considers its thread catalogue built from the chat files.

    ``database`` is the catalogue's path relative to the state root and
    ``backfill`` the ``backfill_state.status`` value as Codex wrote it
    (``pending``, ``running``, ``complete``), ``None`` when the row is absent.
    Read with the same rules as the placements, so it creates nothing.
    """
    status: PlacementStatus
    database: str | None = None
    backfill: str | None = None
    codes: tuple[str, ...] = ()
    #: The rest of the row, ``None`` where the column is absent or NULL: how far
    #: a rebuild got, when it last finished and when the row last changed. A
    #: repair compares all four before it writes (D-032).
    last_watermark: str | None = None
    last_success_at: int | None = None
    updated_at: int | None = None

    @property
    def row(self) -> tuple[str | None, str | None, int | None, int | None]:
        return (self.backfill, self.last_watermark, self.last_success_at, self.updated_at)


class _Refused(Exception):
    """A catalogue that was read but lacks what a query needs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_T = TypeVar("_T")


def _query_the_catalogue(
    state_root: Path, query: Callable[[sqlite3.Connection], _T], *, timeout_seconds: float,
) -> tuple[PlacementStatus, str | None, _T | None, tuple[str, ...]]:
    """Run ``query`` against the one thread catalogue, strictly read-only.

    Exactly one catalogue is required -- two would make "the" catalogue a
    guess -- and every way of not reading it is ``INDETERMINATE``, never
    ``ABSENT``. ``query`` raises `_Refused` for a shape it cannot use.
    Returns (status, catalogue path relative to the root, result, codes).
    """
    root = state_root.resolve()
    discovered = discover_sqlite_sets(root)
    if any(_would_create_a_sidecar(root / Path(item.database.relative_path)) for item in discovered):
        return PlacementStatus.INDETERMINATE, None, None, (WAL_WITHOUT_SHARED_INDEX,)
    shapes = [(item, _looks_like_thread_catalogue(root, item, timeout_seconds)) for item in discovered]
    if any(shape is None for _, shape in shapes):
        return PlacementStatus.INDETERMINATE, None, None, ("CATALOG_UNAVAILABLE",)
    catalogues = [item for item, shape in shapes if shape]
    if not catalogues:
        return PlacementStatus.ABSENT, None, None, ()
    if len(catalogues) > 1:
        return PlacementStatus.INDETERMINATE, None, None, ("SEVERAL_CATALOGUES",)
    relative = catalogues[0].database.relative_path
    database = root / Path(relative)
    immutable = not _has_pending_frames(database)
    try:
        connection = _read_only_connect(database, timeout_seconds)
    except sqlite3.Error:
        return PlacementStatus.INDETERMINATE, relative, None, ("CATALOG_UNAVAILABLE",)
    if connection is None:
        return PlacementStatus.INDETERMINATE, relative, None, (WAL_WITHOUT_SHARED_INDEX,)
    try:
        result = query(connection)
    except _Refused as refused:
        return PlacementStatus.INDETERMINATE, relative, None, (refused.code,)
    except sqlite3.Error:
        return PlacementStatus.INDETERMINATE, relative, None, ("CATALOG_UNREADABLE",)
    finally:
        connection.close()
    if _wal_appeared(database, immutable):
        return PlacementStatus.INDETERMINATE, relative, None, ("CATALOG_CHANGED",)
    return PlacementStatus.AVAILABLE, relative, result, ()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def read_backfill_state(state_root: Path, *, timeout_seconds: float = 2.0) -> BackfillReading:
    """The catalogue's own backfill status, strictly read-only."""

    optional = ("last_watermark", "last_success_at", "updated_at")

    def query(connection: sqlite3.Connection) -> tuple[object, ...] | None:
        columns = _columns(connection, "backfill_state")
        if not {"id", "status"}.issubset(columns):
            raise _Refused("NO_BACKFILL_STATE")
        selected = ", ".join(name if name in columns else "NULL" for name in ("status", *optional))
        return connection.execute(f"SELECT {selected} FROM backfill_state WHERE id = 1").fetchone()

    status, relative, row, codes = _query_the_catalogue(state_root, query, timeout_seconds=timeout_seconds)
    if row is None:
        return BackfillReading(status, relative, None, codes)
    value, watermark, success, updated = row
    return BackfillReading(
        status, relative, value if isinstance(value, str) else None, codes,
        last_watermark=watermark if isinstance(watermark, str) else None,
        last_success_at=success if isinstance(success, int) else None,
        updated_at=updated if isinstance(updated, int) else None,
    )


@dataclass(frozen=True, slots=True)
class ThreadNames:
    """Each thread's shown name and its title, from the catalogue (D-025).

    ``name`` is what the chat list shows; ``title`` is Codex's first-message
    fallback. Only these two text columns are read, besides the id.
    """
    status: PlacementStatus
    database: str | None = None
    #: Thread id -> (name or ``None``, title).
    rows: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    codes: tuple[str, ...] = ()


def read_thread_names(state_root: Path, *, timeout_seconds: float = 2.0) -> ThreadNames:
    """Names and titles of every thread, strictly read-only.

    A catalogue without a ``name`` column is ``INDETERMINATE`` with
    ``NO_NAME_COLUMN``: an older Codex, whose names live somewhere else.
    """

    def query(connection: sqlite3.Connection) -> dict[str, tuple[str | None, str]]:
        if not {"name", "title"}.issubset(_columns(connection, "threads")):
            raise _Refused("NO_NAME_COLUMN")
        rows: dict[str, tuple[str | None, str]] = {}
        for thread_id, name, title in connection.execute("SELECT id, name, title FROM threads"):
            if isinstance(thread_id, str) and thread_id:
                # Kept exactly as stored ('' and NULL both occur): a write
                # requires the row to still hold this very value.
                rows[thread_id] = (
                    name if isinstance(name, str) else None,
                    title if isinstance(title, str) else "",
                )
        return rows

    status, relative, rows, codes = _query_the_catalogue(state_root, query, timeout_seconds=timeout_seconds)
    return ThreadNames(status, relative, rows or {}, codes)


@dataclass(frozen=True, slots=True)
class ThreadArchiveRows:
    """Each thread's rollout path and archive state, as stored (D-035).

    Only ``rollout_path``, ``archived`` and ``archived_at`` are read, kept
    exactly as stored: a write requires the row to still hold these values.
    """
    status: PlacementStatus
    database: str | None = None
    #: Thread id -> (rollout path as stored, archived flag, archived_at).
    rows: dict[str, tuple[str, int, int | None]] = field(default_factory=dict)
    codes: tuple[str, ...] = ()


def read_thread_archive_rows(state_root: Path, *, timeout_seconds: float = 2.0) -> ThreadArchiveRows:
    """Rollout path and archive state of every thread, strictly read-only.

    A catalogue without these columns is ``INDETERMINATE`` with
    ``NO_ARCHIVE_COLUMNS``: a Codex whose archive is kept some other way.
    """

    def query(connection: sqlite3.Connection) -> dict[str, tuple[str, int, int | None]]:
        if not {"rollout_path", "archived", "archived_at"}.issubset(_columns(connection, "threads")):
            raise _Refused("NO_ARCHIVE_COLUMNS")
        rows: dict[str, tuple[str, int, int | None]] = {}
        for thread_id, path, archived, archived_at in connection.execute(
            "SELECT id, rollout_path, archived, archived_at FROM threads"
        ):
            if isinstance(thread_id, str) and thread_id and isinstance(path, str) and isinstance(archived, int):
                rows[thread_id] = (path, archived, archived_at if isinstance(archived_at, int) else None)
        return rows

    status, relative, rows, codes = _query_the_catalogue(state_root, query, timeout_seconds=timeout_seconds)
    return ThreadArchiveRows(status, relative, rows or {}, codes)


@dataclass(frozen=True, slots=True)
class ThreadActivity:
    """When each thread last changed and in which folder (D-026).

    Only ``cwd`` and ``updated_at`` are read: enough to say which project saw
    work in chats, and when, without a word of what was said.
    """
    status: PlacementStatus
    #: (working folder, unix seconds) per thread.
    rows: tuple[tuple[str, int], ...] = ()
    codes: tuple[str, ...] = ()


def read_thread_activity(state_root: Path, *, timeout_seconds: float = 2.0) -> ThreadActivity:
    def query(connection: sqlite3.Connection) -> tuple[tuple[str, int], ...]:
        if not {"cwd", "updated_at"}.issubset(_columns(connection, "threads")):
            raise _Refused("NO_ACTIVITY_COLUMNS")
        return tuple(
            (cwd, int(updated))
            for cwd, updated in connection.execute("SELECT cwd, updated_at FROM threads")
            if isinstance(cwd, str) and cwd and isinstance(updated, int)
        )

    status, _, rows, codes = _query_the_catalogue(state_root, query, timeout_seconds=timeout_seconds)
    return ThreadActivity(status, rows or (), codes)


#: Reason a database could not be opened without writing beside it.
WAL_WITHOUT_SHARED_INDEX = "WAL_WITHOUT_SHARED_INDEX"


def _sidecars(database: Path) -> tuple[Path, Path]:
    return (
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    )


def _has_pending_frames(database: Path) -> bool:
    """Whether the write-ahead log holds anything the main file lacks.

    A ``-wal`` truncated to zero bytes is what a clean checkpoint leaves: no
    frames, so the main database is already the whole truth. That distinction
    is what lets a closed Codex be read without rebuilding the shared index --
    and rebuilding it is a write into `.codex`, measured on a real machine as
    the ``-shm`` mtime moving on every `doctor`.
    """
    wal, _ = _sidecars(database)
    try:
        return wal.stat().st_size > 0
    except OSError:
        return False


def _would_create_a_sidecar(database: Path) -> bool:
    """Whether opening this database would write a file beside it."""
    wal, shm = _sidecars(database)
    return wal.exists() and _has_pending_frames(database) and not shm.exists()


def _read_only_connect(database: Path, timeout_seconds: float) -> sqlite3.Connection | None:
    """Open for reading without creating a file next to the database.

    Returns ``None`` when that is impossible, which the caller reports rather
    than works around.
    """
    if _would_create_a_sidecar(database):
        return None
    # The path is percent-encoded: in a URI `?` and `#` start the query and
    # the fragment and `%` starts an escape, so a folder named with one of them
    # would otherwise open some other file, or none.
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro"
    if not _has_pending_frames(database):
        # No pending frames, so the main file is the whole database and the WAL
        # machinery -- the part that creates and rewrites files -- is not needed
        # to read it. This covers a `-wal` that exists but is empty, which is a
        # cleanly closed Codex: opening that one normally rebuilds the stale
        # shared index, and rebuilding it writes.
        uri += "&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    connection.execute("PRAGMA query_only=ON")
    connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
    return connection


def _wal_appeared(database: Path, opened_immutable: bool) -> bool:
    """Whether a writer started while an immutable reading was in progress.

    ``immutable=1`` also gives up locking, so a Codex that began writing during
    the read would leave us holding a picture that was never a whole state. The
    ``-wal`` appearing is that event, and the answer is to report it, not to
    keep the reading.
    """
    if not opened_immutable:
        return False
    return _has_pending_frames(database)


def _looks_like_thread_catalogue(root: Path, item: SQLiteSet, timeout_seconds: float) -> bool | None:
    """Whether this database has the exact table and columns to read.

    ``None`` when that could not be found out -- busy, locked, damaged, or
    impossible to open without writing beside it. That is not "no", and the
    caller must not treat it as one.
    """
    database = root / Path(item.database.relative_path)
    try:
        connection = _read_only_connect(database, timeout_seconds)
    except sqlite3.Error:
        return None
    if connection is None:
        return None
    try:
        columns = {str(row[1]) for row in connection.execute('PRAGMA table_info("threads")')}
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return {"id", "rollout_path", "archived"}.issubset(columns)


#: Windows extended-length path prefixes. The runtime stores many rollout
#: paths in that form, naming the very same file; leaving the prefix on makes
#: the path look like it is outside the state root and the placement unknown.
#: On one observed machine that mislabelled 39 of 250 threads as unplaceable.
_EXTENDED_UNC = "\\\\?\\UNC\\"
_EXTENDED = "\\\\?\\"


def _strip_extended_prefix(value: str) -> str:
    if value.startswith(_EXTENDED_UNC):
        return "\\\\" + value[len(_EXTENDED_UNC):]
    if value.startswith(_EXTENDED):
        return value[len(_EXTENDED):]
    return value


def _relative_rollout(value: object, root: Path) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return Path(_strip_extended_prefix(value)).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def discover_sqlite_sets(state_root: Path) -> list[SQLiteSet]:
    root = state_root.resolve()
    databases = sorted(root.glob("state_*.sqlite"))
    sqlite_dir = root / "sqlite"
    if sqlite_dir.is_dir():
        databases.extend(sorted(sqlite_dir.glob("*.db")))
    result: list[SQLiteSet] = []
    for database in databases:
        if database.is_symlink() or not database.is_file():
            continue
        _inside(database, root)
        sidecars: list[SQLiteAsset] = []
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = database.with_name(database.name + suffix)
            if candidate.is_file() and not candidate.is_symlink():
                sidecars.append(_asset(candidate, root))
        result.append(SQLiteSet(_asset(database, root), tuple(sidecars)))
    return result


def audit_sqlite(state_root: Path, *, cold: bool = False, timeout_seconds: float = 2.0) -> list[SQLiteAuditReport]:
    root = state_root.resolve()
    return [_audit_one(root, item, cold=cold, timeout_seconds=timeout_seconds) for item in discover_sqlite_sets(root)]


def _audit_one(root: Path, asset_set: SQLiteSet, *, cold: bool, timeout_seconds: float) -> SQLiteAuditReport:
    database = root / Path(asset_set.database.relative_path)
    before = _set_signature(root, asset_set)
    immutable = not _has_pending_frames(database)
    codes: list[str] = []
    try:
        connection = _read_only_connect(database, timeout_seconds)
        if connection is None:
            return SQLiteAuditReport(
                asset_set, SQLiteRole.UNKNOWN, "INDETERMINATE", None, None, None, None, 0, 0, 0,
                codes=(WAL_WITHOUT_SHARED_INDEX,),
            )
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            rows = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE type IN ('table','index','trigger') ORDER BY type,name"
            ).fetchall()
            schema_digest = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
            tables = [str(row[1]) for row in rows if row[0] == "table" and not str(row[1]).startswith("sqlite_")]
            columns: dict[str, frozenset[str]] = {}
            for table in tables:
                quoted = table.replace('"', '""')
                columns[table] = frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{quoted}")'))
            role = _classify_role(database.name, columns)
            if role is SQLiteRole.UNKNOWN:
                codes.append("UNKNOWN_SCHEMA")
            quick_errors = foreign_errors = None
            if cold:
                quick_errors = sum(1 for row in connection.execute("PRAGMA quick_check") if row[0] != "ok")
                foreign_errors = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
    except sqlite3.Error:
        return SQLiteAuditReport(asset_set, SQLiteRole.UNKNOWN, "INDETERMINATE", None, None, None, None, 0, 0, 0, codes=("SQLITE_UNAVAILABLE",))
    after_sets = discover_sqlite_sets(root)
    after_match = next((item for item in after_sets if item.database.relative_path == asset_set.database.relative_path), None)
    if after_match is None or _set_signature(root, after_match) != before:
        codes.append("READ_CHANGED")
    if _wal_appeared(database, immutable):
        codes.append("READ_CHANGED")
    status = "PASS" if not codes and (quick_errors in {None, 0}) and (foreign_errors in {None, 0}) else "INDETERMINATE"
    return SQLiteAuditReport(
        asset_set, role, status, schema_digest, user_version, application_id, journal_mode,
        len(tables), sum(1 for row in rows if row[0] == "index"), sum(1 for row in rows if row[0] == "trigger"),
        quick_errors, foreign_errors, tuple(codes),
    )


def _classify_role(filename: str, columns: dict[str, frozenset[str]]) -> SQLiteRole:
    column_sets = list(columns.values())
    if any({"id", "archived"}.issubset(items) or {"thread_id", "archived"}.issubset(items) for items in column_sets):
        return SQLiteRole.THREAD_CATALOG
    names = {name.lower() for name in columns}
    if "summary" in filename.lower() or any("summar" in name for name in names):
        return SQLiteRole.DERIVED_SUMMARIES
    if any(any(token in name for token in ("account", "automation", "credential")) for name in names):
        return SQLiteRole.LOCAL_APP_DATA
    return SQLiteRole.UNKNOWN


def _asset(path: Path, root: Path) -> SQLiteAsset:
    stat = path.stat()
    return SQLiteAsset(path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns)


def _set_signature(root: Path, asset_set: SQLiteSet) -> tuple[tuple[str, int, int], ...]:
    assets = (asset_set.database, *asset_set.sidecars)
    return tuple((item.relative_path, item.size, item.mtime_ns) for item in assets)


def _inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise OSError("SQLite asset escapes state root") from exc
