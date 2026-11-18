"""One immutable SQLite observation for Doctor's dependent checks.

Source files are read as bytes only. SQLite sees one private materialized
in-memory database and transaction per discovered set, never a live path.
Repeated metadata AND byte comparisons reject a changing source. This is a
bounded observation interval, not an atomic snapshot across databases.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from types import MappingProxyType

from .preflight_observations import ObservationBoundary
from .shared_read import open_shared
from .sqlite_audit import (
    BackfillReading, CATALOG_SHAPE_UNKNOWN, PlacementStatus, PROJECTION_TABLE,
    SQLiteAsset, SQLiteAuditReport, SQLiteRole, SQLiteSet, ThreadPlacements,
    WAL_WITHOUT_SHARED_INDEX, _classify_role, _relative_rollout, _RUNTIME_STATE,
    discover_sqlite_sets,
)
from .sqlite_snapshot import MAX_SNAPSHOT_BYTES, SnapshotRefused, materialize_database
from .sqlite_read_lock import check_source_read_lock


@dataclass(frozen=True, slots=True)
class SQLiteObservation:
    boundary: ObservationBoundary
    reports: tuple[SQLiteAuditReport, ...]
    backfill: BackfillReading
    placements: ThreadPlacements
    cold: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Facts:
    asset_set: SQLiteSet
    report: SQLiteAuditReport
    shape: bool | None
    backfill: BackfillReading | None = None
    placements: ThreadPlacements | None = None
    history: tuple[PlacementStatus, frozenset[str], tuple[str, ...]] | None = None
    refusal: str | None = None
    unreadable: bool = False


def _read_bytes(path: Path, limit: int) -> bytes:
    with open_shared(path) as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise SnapshotRefused("SQLITE_SNAPSHOT_TOO_LARGE")
    return value


def _stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if path.is_symlink():
        raise SnapshotRefused("SQLITE_LINK_REFUSED")
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _capture(database: Path, limit: int) -> tuple[bytes, bytes]:
    check_source_read_lock(database)
    paths = (database, *(database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")))
    before = tuple(_stamp(path) for path in paths)
    if before[0] is None:
        raise SnapshotRefused("READ_CHANGED")
    if any(item is not None and item[2] > limit for item in before):
        raise SnapshotRefused("SQLITE_SNAPSHOT_TOO_LARGE")
    if before[1] is not None and before[1][2] and before[2] is None:
        raise SnapshotRefused(WAL_WITHOUT_SHARED_INDEX)
    if before[3] is not None and before[3][2]:
        raise SnapshotRefused("SQLITE_JOURNAL_PRESENT")
    payloads = tuple(_read_bytes(path, limit) if stamp is not None else None for path, stamp in zip(paths, before))
    # Include SHM, even though no part of it is trusted as SQLite data: it can
    # reveal a checkpoint/reset or a writer changing its generation mid-read.
    if tuple(_stamp(path) for path in paths) != before:
        raise SnapshotRefused("READ_CHANGED")
    for path, payload in zip(paths, payloads):
        if payload is not None and _read_bytes(path, limit) != payload:
            raise SnapshotRefused("READ_CHANGED")
    if tuple(_stamp(path) for path in paths) != before:
        raise SnapshotRefused("READ_CHANGED")
    check_source_read_lock(database)
    return payloads[0], payloads[1] or b""


def _unavailable(asset_set: SQLiteSet, code: str = "SQLITE_UNAVAILABLE", *, unreadable: bool = False) -> _Facts:
    report = SQLiteAuditReport(asset_set, SQLiteRole.UNKNOWN, "INDETERMINATE", None, None, None, None, 0, 0, 0, codes=(code,))
    return _Facts(asset_set, report, None, refusal=code, unreadable=unreadable)


def _materialize(root: Path, asset_set: SQLiteSet, *, cold: bool, limit: int) -> _Facts:
    database = root / asset_set.database.relative_path
    try:
        main, wal = _capture(database, limit)
        image = materialize_database(main, wal, max_bytes=limit)
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(image)
            connection.execute("BEGIN")
            facts = _query(connection, root, asset_set, cold=cold, has_wal=bool(wal))
        finally:
            connection.close()
        return facts
    except SnapshotRefused as exc:
        code = str(exc)
        return _unavailable(asset_set, code if code in ("READ_CHANGED", WAL_WITHOUT_SHARED_INDEX) else "SQLITE_UNAVAILABLE",
                            unreadable=code not in ("READ_CHANGED", WAL_WITHOUT_SHARED_INDEX, "SQLITE_SNAPSHOT_TOO_LARGE"))
    except sqlite3.Error:
        return _unavailable(asset_set, unreadable=True)
    except OSError:
        return _unavailable(asset_set)


def _query(connection: sqlite3.Connection, root: Path, asset_set: SQLiteSet, *, cold: bool, has_wal: bool) -> _Facts:
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    # Sort in Python: SQLite's ORDER BY may spill an oversized schema into a
    # temporary file, even when the main database lives entirely in memory.
    rows = sorted(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index','trigger')"
    ).fetchall(), key=lambda row: (row[0], row[1]))
    tables = [str(row[1]) for row in rows if row[0] == "table" and not str(row[1]).startswith("sqlite_")]
    columns = {}
    for table in tables:
        quoted = table.replace('"', '""')
        columns[table] = frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{quoted}")'))
    role = _classify_role(Path(asset_set.database.relative_path).name, columns)
    codes = ("UNKNOWN_SCHEMA",) if role is SQLiteRole.UNKNOWN else ()
    quick = foreign = None
    if cold:
        quick = sum(1 for row in connection.execute("PRAGMA quick_check") if row[0] != "ok")
        foreign = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
    report = SQLiteAuditReport(
        asset_set, role, "PASS" if not codes and quick in (None, 0) and foreign in (None, 0) else "INDETERMINATE",
        hashlib.sha256(repr(rows).encode("utf-8")).hexdigest(), user_version, application_id,
        "wal" if has_wal else "delete", len(tables), sum(row[0] == "index" for row in rows),
        sum(row[0] == "trigger" for row in rows), quick, foreign, codes,
    )
    shape = {"id", "rollout_path", "archived"}.issubset(columns.get("threads", ()))
    backfill = placements = None
    if shape:
        relative = asset_set.database.relative_path
        backfill_columns = columns.get("backfill_state", ())
        if not {"id", "status"}.issubset(backfill_columns):
            backfill = BackfillReading(PlacementStatus.INDETERMINATE, relative, codes=("NO_BACKFILL_STATE",))
        else:
            selected = ", ".join(name if name in backfill_columns else "NULL"
                                 for name in ("status", "last_watermark", "last_success_at", "updated_at"))
            row = connection.execute(f"SELECT {selected} FROM backfill_state WHERE id = 1").fetchone()
            if row is None:
                backfill = BackfillReading(PlacementStatus.AVAILABLE, relative)
            else:
                value, watermark, success, updated = row
                backfill = BackfillReading(
                    PlacementStatus.AVAILABLE, relative, value if isinstance(value, str) else None,
                    last_watermark=watermark if isinstance(watermark, str) else None,
                    last_success_at=success if isinstance(success, int) else None,
                    updated_at=updated if isinstance(updated, int) else None, raw=tuple(row),
                )
        by_session = {}
        archived = set()
        for thread_id, rollout, flag in connection.execute("SELECT id, rollout_path, archived FROM threads"):
            if isinstance(thread_id, str) and thread_id:
                by_session[thread_id] = _relative_rollout(rollout, root)
                if flag:
                    archived.add(thread_id)
        placement_codes = ("ROLLOUT_PATH_OUTSIDE_STATE_ROOT",) if None in by_session.values() else ()
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, MappingProxyType(by_session), frozenset(archived), placement_codes)
    history = None
    if "thread_id" in columns.get(PROJECTION_TABLE, ()):
        projected = frozenset(row[0] for row in connection.execute(f'SELECT thread_id FROM "{PROJECTION_TABLE}"')
                              if isinstance(row[0], str) and row[0])
        history = PlacementStatus.AVAILABLE, projected, ()
    return _Facts(asset_set, report, shape, backfill, placements, history)


def _empty_placements(status: PlacementStatus, codes: tuple[str, ...] = ()) -> ThreadPlacements:
    return ThreadPlacements(status, MappingProxyType({}), codes=codes)


def _catalogue_readings(facts: tuple[_Facts, ...]) -> tuple[BackfillReading, ThreadPlacements]:
    code = None
    if any(item.refusal == WAL_WITHOUT_SHARED_INDEX for item in facts):
        code = WAL_WITHOUT_SHARED_INDEX
    elif any(item.shape is None for item in facts):
        code = "CATALOG_CHANGED" if any(item.refusal == "READ_CHANGED" for item in facts) else "CATALOG_UNAVAILABLE"
    elif any(item.shape is False and _RUNTIME_STATE.match(Path(item.asset_set.database.relative_path).name) for item in facts):
        code = CATALOG_SHAPE_UNKNOWN
    catalogues = [item for item in facts if item.shape]
    if code is None and len(catalogues) > 1:
        code = "SEVERAL_CATALOGUES"
    if code is not None:
        return BackfillReading(PlacementStatus.INDETERMINATE, codes=(code,)), _empty_placements(PlacementStatus.INDETERMINATE, (code,))
    if not catalogues:
        return BackfillReading(PlacementStatus.ABSENT), _empty_placements(PlacementStatus.ABSENT)
    return catalogues[0].backfill, catalogues[0].placements


def observe_sqlite(state_root: Path, *, cold: bool = False, max_bytes: int = MAX_SNAPSHOT_BYTES) -> SQLiteObservation:
    """Capture once; a supplied observation must never be retried by a consumer.

    ``max_bytes`` bounds each source and the reconstructed image. No connection,
    file handle, lazy iterator or mutable mapping survives this call.
    """
    started = datetime.now(timezone.utc)
    root = state_root.resolve()
    reports = ()
    error = None
    try:
        if not hasattr(sqlite3.Connection, "deserialize"):
            raise OSError("SQLite deserialize is unavailable")
        discovered = discover_sqlite_sets(root)
        histories = sorted(path for path in root.glob("thread_history_*.sqlite") if path.is_file())
        facts = tuple(_materialize(root, item, cold=cold, limit=max_bytes) for item in discovered)
        backfill, placements = _catalogue_readings(facts)
        if placements.status is PlacementStatus.AVAILABLE:
            history_status, projected, history_codes = PlacementStatus.ABSENT, frozenset(), ()
            if len(histories) > 1:
                history_status, history_codes = PlacementStatus.INDETERMINATE, ("SEVERAL_HISTORY_STORES",)
            elif histories:
                path = histories[0]
                stat = path.stat()
                asset = SQLiteSet(SQLiteAsset(path.name, stat.st_size, stat.st_mtime_ns), ())
                history_facts = _materialize(root, asset, cold=False, limit=max_bytes)
                if history_facts.history is not None:
                    history_status, projected, history_codes = history_facts.history
                else:
                    history_status = PlacementStatus.INDETERMINATE
                    history_codes = (history_facts.refusal if history_facts.refusal == WAL_WITHOUT_SHARED_INDEX else
                                     "HISTORY_STORE_UNREADABLE" if history_facts.unreadable else
                                     "HISTORY_STORE_UNAVAILABLE" if history_facts.shape is None else "HISTORY_SHAPE_UNKNOWN",)
            placements = replace(placements, history=history_status, projected=projected, history_codes=history_codes)
        reports = tuple(item.report for item in facts)
        if discover_sqlite_sets(root) != discovered or sorted(path for path in root.glob("thread_history_*.sqlite") if path.is_file()) != histories:
            reports = tuple(replace(item, status="INDETERMINATE", codes=(*item.codes, "READ_CHANGED")) for item in reports)
            backfill = BackfillReading(PlacementStatus.INDETERMINATE, codes=("CATALOG_CHANGED",))
            placements = _empty_placements(PlacementStatus.INDETERMINATE, ("CATALOG_CHANGED",))
    except OSError as exc:
        error = str(exc)
        backfill = BackfillReading(PlacementStatus.INDETERMINATE, codes=("CATALOG_UNAVAILABLE",))
        placements = _empty_placements(PlacementStatus.INDETERMINATE, ("CATALOG_UNAVAILABLE",))
    return SQLiteObservation(ObservationBoundary(str(root), started, datetime.now(timezone.utc)), reports, backfill, placements, cold, error)
