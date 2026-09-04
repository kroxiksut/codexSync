"""Strictly read-only, schema-level audit of Codex SQLite assets."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import sqlite3


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
    catalogues = [
        item for item in discover_sqlite_sets(root)
        if _looks_like_thread_catalogue(root, item, timeout_seconds)
    ]
    if not catalogues:
        return ThreadPlacements(PlacementStatus.ABSENT, {})

    by_session: dict[str, str | None] = {}
    archived: set[str] = set()
    codes: list[str] = []
    for item in catalogues:
        database = root / Path(item.database.relative_path)
        try:
            connection = sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True, timeout=timeout_seconds
            )
        except sqlite3.Error:
            return ThreadPlacements(
                PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",)
            )
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
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
    if any(value is None for value in by_session.values()):
        codes.append("ROLLOUT_PATH_OUTSIDE_STATE_ROOT")
    return ThreadPlacements(
        PlacementStatus.AVAILABLE, by_session, frozenset(archived), tuple(dict.fromkeys(codes))
    )


def _looks_like_thread_catalogue(root: Path, item: SQLiteSet, timeout_seconds: float) -> bool:
    """Whether this database has the exact table and columns to read."""
    database = root / Path(item.database.relative_path)
    try:
        connection = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True, timeout=timeout_seconds
        )
    except sqlite3.Error:
        return False
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {str(row[1]) for row in connection.execute('PRAGMA table_info("threads")')}
    except sqlite3.Error:
        return False
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
    uri = f"file:{database.as_posix()}?mode=ro"
    codes: list[str] = []
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
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
