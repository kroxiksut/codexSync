"""Materialized, immutable sources shared by read-only preflight checks.

This is a vertical slice, not an atomic snapshot of a running Codex. Each
source names its own capture interval. SQLite and Guardian still have
independent readings; checks must remain sequential until those sources have
a similar boundary.
No connection, lazy reader, mutable config or filesystem handle escapes here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from .codex_health import ChatFiles
from .config import decode_config_bytes, read_config_bytes, require_config_outside_codex
from .jsonl_codec import logical_relative_path
from .safety_gate import SafetyDecision
from .session_catalog import (
    DEFAULT_MAX_JSONL_LINE_BYTES, SessionBranch, SessionDescriptor, SessionState,
    peek_record_formats, scan_sessions,
)
from .session_index import SESSION_INDEX_FILE, IndexParseResult, parse_session_index
from .thread_catalogue import chat_file_id


@dataclass(frozen=True, slots=True)
class ObservationBoundary:
    source: str
    started_at_utc: datetime
    completed_at_utc: datetime


class GlobalStateStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    TOO_BIG = "TOO_BIG"
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True, slots=True)
class GlobalStateObservation:
    boundary: ObservationBoundary
    status: GlobalStateStatus
    max_state_bytes: int
    payload: bytes | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    boundary: ObservationBoundary
    # Keep the actual operation's decision, including its continuous stopped
    # window. Startup consumes its state only; it does not re-authorize writes.
    decision: SafetyDecision


@dataclass(frozen=True, slots=True)
class ConfigObservation:
    boundary: ObservationBoundary
    payload: bytes | None
    text: str | None
    source_sha256: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenSessionCatalog:
    descriptors: tuple[SessionDescriptor, ...]
    branches: Mapping[str, SessionBranch]
    codes: tuple[str, ...]
    volatile: bool

    @property
    def valid(self) -> tuple[SessionDescriptor, ...]:
        return tuple(item for item in self.descriptors if item.state not in {SessionState.INVALID, SessionState.AMBIGUOUS})


@dataclass(frozen=True, slots=True)
class SessionObservation:
    boundary: ObservationBoundary
    catalog: FrozenSessionCatalog | None
    formats: Mapping[str, str]
    files: ChatFiles
    error: str | None = None
    #: Enumeration and decoding fail independently. A completed tree still
    #: supplies startup's counts when a later history/catalog read fails.
    files_error: str | None = None


@dataclass(frozen=True, slots=True)
class FormatObservation:
    boundary: ObservationBoundary
    formats: Mapping[str, str]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IndexObservation:
    boundary: ObservationBoundary
    result: IndexParseResult | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PreflightObservations:
    process: ProcessObservation
    global_state: GlobalStateObservation | None
    config: ConfigObservation | None = None
    sessions: SessionObservation | None = None
    cloud_formats: FormatObservation | None = None
    session_index: IndexObservation | None = None
    sqlite: object | None = None


def observe_config_source(source: Path) -> ConfigObservation:
    """The loader and compatibility inspection consume these same source bytes."""
    started = datetime.now(timezone.utc)
    payload = text = digest = error = None
    try:
        require_config_outside_codex(source)
        payload = read_config_bytes(source)
        digest = hashlib.sha256(payload).hexdigest()
        text = decode_config_bytes(payload, source=str(source))
    except Exception as exc:
        error = str(exc)
    return ConfigObservation(
        ObservationBoundary(str(source), started, datetime.now(timezone.utc)), payload, text, digest, error,
    )


def observe_sessions(source: Path, max_line_bytes: int) -> SessionObservation:
    """Catalog, first-record formats and rebuild size from one tree walk.

    Rebuild size is stored file size, including rollout names in containers a
    decoder does not recognise. Catalog byte_count instead counts the decoded
    history. These are deliberately separate measurements.
    """
    started = datetime.now(timezone.utc)
    count = size = 0
    tree_complete = False

    def complete() -> None:
        nonlocal tree_complete
        tree_complete = True

    def measure(path: Path) -> None:
        nonlocal count, size
        if chat_file_id(path.name) is None:
            return
        try:
            size += path.stat().st_size
        except OSError:
            return
        count += 1

    catalog = None
    formats: dict[str, str] = {}
    error = None
    try:
        scanned = scan_sessions(
            source, volatile=True, max_line_bytes=max_line_bytes,
            on_tree_file=measure, on_tree_complete=complete,
        )
        catalog = FrozenSessionCatalog(
            tuple(scanned.descriptors), MappingProxyType(dict(scanned.branches)), scanned.codes, scanned.volatile,
        )
        formats = {
            logical_relative_path(item.relative_path): item.first_record_format for item in scanned.descriptors
        }
    except Exception as exc:
        error = str(exc)
    return SessionObservation(
        ObservationBoundary(str(source), started, datetime.now(timezone.utc)), catalog,
        MappingProxyType(formats), ChatFiles(count, size), error, error if not tree_complete else None,
    )


def observe_formats(source: Path | None, max_line_bytes: int) -> FormatObservation:
    started = datetime.now(timezone.utc)
    formats: dict[str, str] = {}
    error = None
    try:
        if source is not None and source.is_dir():
            formats = peek_record_formats(source, max_line_bytes=max_line_bytes)
    except Exception as exc:
        error = str(exc)
    return FormatObservation(
        ObservationBoundary(str(source), started, datetime.now(timezone.utc)), MappingProxyType(formats), error,
    )


def observe_session_index(source: Path) -> IndexObservation:
    started = datetime.now(timezone.utc)
    result = None
    error = None
    try:
        parsed = parse_session_index(source)
        result = replace(
            parsed, reduced=MappingProxyType(dict(parsed.reduced)),
            reduced_by_updated_at=MappingProxyType(dict(parsed.reduced_by_updated_at)),
        )
    except OSError as exc:
        error = str(exc)
    return IndexObservation(ObservationBoundary(str(source), started, datetime.now(timezone.utc)), result, error)


def observe_global_state(source: Path, max_state_bytes: int) -> GlobalStateObservation:
    """Capture once, including absence/refusal: consumers must never retry it."""
    started = datetime.now(timezone.utc)
    status = GlobalStateStatus.MISSING
    payload = None
    error = None
    try:
        if source.is_file():
            if source.stat().st_size > max_state_bytes:
                status = GlobalStateStatus.TOO_BIG
            else:
                payload = source.read_bytes()
                if len(payload) > max_state_bytes:
                    status = GlobalStateStatus.TOO_BIG
                    payload = None
                else:
                    status = GlobalStateStatus.AVAILABLE
    except OSError as exc:
        status = GlobalStateStatus.UNREADABLE
        error = str(exc)
    boundary = ObservationBoundary(str(source), started, datetime.now(timezone.utc))
    return GlobalStateObservation(boundary, status, max_state_bytes, payload, error)


def collect_preflight_observations(
    local_dir: Path | None, max_state_bytes: int, *, process_check: Callable[[], SafetyDecision],
    config: ConfigObservation | None = None, cloud_dir: Path | None = None,
    max_line_bytes: int = DEFAULT_MAX_JSONL_LINE_BYTES,
) -> PreflightObservations:
    """One coordinator captures sources before their consumers can be scheduled.

    The injected check owns safety policy and any sampling window it needs.
    This provider performs no fixes, bootstrap, directory creation or writes.
    """
    started = datetime.now(timezone.utc)
    decision = process_check()
    process = ProcessObservation(
        ObservationBoundary("process safety gate", started, datetime.now(timezone.utc)), decision,
    )
    state = (
        observe_global_state(local_dir / ".codex-global-state.json", max_state_bytes)
        if local_dir is not None else None
    )
    sessions = observe_sessions(local_dir, max_line_bytes) if local_dir is not None else None
    cloud_formats = observe_formats(cloud_dir, max_line_bytes) if local_dir is not None else None
    index = observe_session_index(local_dir / SESSION_INDEX_FILE) if local_dir is not None else None
    from .sqlite_observation import observe_sqlite
    sqlite = observe_sqlite(local_dir, cold=False) if local_dir is not None else None
    return PreflightObservations(process, state, config, sessions, cloud_formats, index, sqlite)
