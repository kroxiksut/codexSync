"""Shared runtime foundation for the orchestration layer.

Everything here is used by more than one command family (sync, restore,
preflight): runtime path bootstrap, the process-safety gate factory, file
index construction and the plan hash.  This module must not import app,
restore or preflight — the dependency edge runs one way only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import logging
from pathlib import Path
import sys
import time

from .exceptions import ConfigError
from .filters import PathFilter
from .manifest import save_manifest
from .models import AppConfig, FileMeta, SyncManifest, SyncPlan
from .process_detector import CodexProcessDetector, ProcessInfo
from .safety_gate import ProcessState, SafetyGate
from .scanner import scan_tree

LOG = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ProcessSnapshot:
    main_processes: list[ProcessInfo]
    subprocesses: list[ProcessInfo]
    sandbox_detected: bool
    background_processes: list[ProcessInfo] = field(default_factory=list)

def initialize_runtime_paths(cfg: AppConfig) -> None:
    """
    Prepares runtime directories/files for first run.
    Creates only codexSync-owned infrastructure and never creates local Codex state dir.
    """
    _ensure_dir(cfg.paths.cloud_root_dir, "paths.cloud_root_dir")
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    _bootstrap_cloud_targets(cfg)

    if cfg.logging.file:
        _ensure_dir(cfg.logging.file.parent, "logging.file parent")

    if cfg.state.manifest_file:
        _ensure_dir(cfg.state.manifest_file.parent, "state.manifest_file parent")
        if not cfg.state.manifest_file.exists():
            empty_manifest = SyncManifest(data_version=cfg.state.data_version, files={})
            save_manifest(empty_manifest, cfg.state.manifest_file)

def _ensure_dir(path: Path, field_name: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot create directory for {field_name}: {path}. {exc}") from exc
    if not path.is_dir():
        raise ConfigError(f"Path for {field_name} is not a directory: {path}")

def _bootstrap_cloud_targets(cfg: AppConfig) -> None:
    root = cfg.paths.cloud_root_dir.resolve()
    for rel in cfg.targets.include_roots:
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ConfigError(f"targets.include_roots points outside cloud root: {rel}") from exc

        # Heuristic: entries with suffix are treated as files, others as directories.
        if Path(rel).suffix:
            _ensure_dir(candidate.parent, f"targets.include_roots parent for {rel}")
        else:
            _ensure_dir(candidate, f"targets.include_roots dir {rel}")

def _make_safety_gate(cfg: AppConfig) -> SafetyGate:
    detector = CodexProcessDetector(cfg.process_detection.process_names)

    def sample() -> ProcessState:
        capability = detector.capability()
        if not capability.supported:
            return ProcessState.UNKNOWN
        snapshot = collect_process_snapshot(cfg, detector=detector)
        if snapshot.main_processes or snapshot.sandbox_detected or snapshot.background_processes:
            return ProcessState.RUNNING
        return ProcessState.STOPPED

    return SafetyGate(
        sample,
        stable_window_seconds=2.0,
        sample_interval_seconds=0.25,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )

def _require_mutation_compatible_config(cfg: AppConfig) -> None:
    if cfg.process_detection.allow_terminate_if_running:
        raise ConfigError(
            "process_detection.allow_terminate_if_running=true is no longer supported for mutation commands; "
            "close Codex manually before retrying"
        )
    if not cfg.backup.backup_before_overwrite:
        raise ConfigError("backup.backup_before_overwrite=false is not supported for mutation commands")
    if cfg.sync.session_mode == "last_date_only":
        raise ConfigError("sync.session_mode=last_date_only is incompatible with branch-preserving semantic mode")

def _current_os_background_processes(cfg: AppConfig) -> list[str]:
    os_key = _current_os_key()
    configured = cfg.process_detection.background_process_names.get(os_key, [])
    return [name.strip() for name in configured if name.strip()]

def _current_os_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"

def collect_codex_processes(cfg: AppConfig) -> list[ProcessInfo]:
    snapshot = collect_process_snapshot(cfg)
    return snapshot.subprocesses

def collect_process_snapshot(cfg: AppConfig, detector: CodexProcessDetector | None = None) -> ProcessSnapshot:
    detector = detector or CodexProcessDetector(cfg.process_detection.process_names)
    main, subprocesses = detector.get_subprocess_tree(cfg.process_detection.process_names)
    markers = _current_os_background_processes(cfg)
    find_processes = getattr(detector, "find_processes", None)
    background_processes = find_processes(markers) if markers and callable(find_processes) else []
    sandbox_detected = any(
        detector.has_marker(proc, marker)
        for marker in markers
        for proc in subprocesses
    )
    return ProcessSnapshot(
        main_processes=main,
        subprocesses=subprocesses,
        sandbox_detected=sandbox_detected,
        background_processes=background_processes,
    )

def _build_indexes(cfg: AppConfig, local_dir: Path, cloud_dir: Path) -> tuple[dict[str, FileMeta], dict[str, FileMeta]]:
    path_filter = PathFilter(cfg.filters.exclude_globs)
    local_idx = scan_tree(local_dir, cfg.targets.include_roots, path_filter)
    cloud_idx = scan_tree(cloud_dir, cfg.targets.include_roots, path_filter)
    local_idx = {rel: meta for rel, meta in local_idx.items() if not _is_semantic_owned(rel)}
    cloud_idx = {rel: meta for rel, meta in cloud_idx.items() if not _is_semantic_owned(rel)}
    return _apply_session_mode(local_idx, cloud_idx, cfg.sync.session_mode)

def _is_semantic_owned(relative_path: str) -> bool:
    rel = relative_path.replace("\\", "/").strip("/")
    name = rel.rsplit("/", 1)[-1]
    return (
        rel == ".codex-global-state.json"
        or rel == "session_index.jsonl"
        or rel.startswith("sessions/")
        or rel.startswith("archived_sessions/")
        or rel.startswith("sqlite/")
        or (name.startswith("state_") and any(name.endswith(suffix) for suffix in (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")))
    )

def _apply_session_mode(
    local_idx: dict[str, FileMeta],
    cloud_idx: dict[str, FileMeta],
    session_mode: str | None,
) -> tuple[dict[str, FileMeta], dict[str, FileMeta]]:
    mode = (session_mode or "all").strip().lower()
    if mode != "last_date_only":
        return local_idx, cloud_idx

    latest_key = _latest_sessions_date_key(local_idx, cloud_idx)
    if latest_key is None:
        LOG.warning(
            "sync.session_mode=last_date_only is set, but no date-based sessions folders were detected. "
            "Proceeding with all sessions files."
        )
        return local_idx, cloud_idx

    LOG.info("sync.session_mode=last_date_only: only sessions for %s will be included", latest_key)
    return (
        _filter_sessions_by_date_key(local_idx, latest_key),
        _filter_sessions_by_date_key(cloud_idx, latest_key),
    )

def _filter_sessions_by_date_key(index: dict[str, FileMeta], date_key: str) -> dict[str, FileMeta]:
    filtered: dict[str, FileMeta] = {}
    for rel, meta in index.items():
        session_date = _extract_session_date_key(rel)
        if session_date is None:
            if not rel.startswith("sessions/"):
                filtered[rel] = meta
            continue
        if session_date == date_key:
            filtered[rel] = meta
    return filtered

def _latest_sessions_date_key(local_idx: dict[str, FileMeta], cloud_idx: dict[str, FileMeta]) -> str | None:
    all_dates: set[str] = set()
    for rel in set(local_idx.keys()) | set(cloud_idx.keys()):
        date_key = _extract_session_date_key(rel)
        if date_key:
            all_dates.add(date_key)
    if not all_dates:
        return None
    return sorted(all_dates)[-1]

def _extract_session_date_key(rel: str) -> str | None:
    normalized = rel.strip("/\\")
    if not normalized.startswith("sessions/"):
        return None

    parts = normalized.split("/")
    if len(parts) >= 2 and _is_iso_date(parts[1]):
        return parts[1]
    if len(parts) >= 4 and _is_ymd_triplet(parts[1], parts[2], parts[3]):
        return f"{parts[1]}-{parts[2]}-{parts[3]}"
    return None

def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True

def _is_ymd_triplet(year: str, month: str, day: str) -> bool:
    if len(year) != 4 or len(month) != 2 or len(day) != 2:
        return False
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return False
    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return False
    return True

def _plan_hash(plan: SyncPlan) -> str:
    """Freeze action direction and source bytes without persisting local paths."""
    digest = hashlib.sha256()
    for direction, actions in (("to_local", plan.to_local), ("to_cloud", plan.to_cloud)):
        for action in sorted(actions, key=lambda item: item.relative_path):
            digest.update(direction.encode("ascii"))
            digest.update(b"\0")
            digest.update(action.relative_path.encode("utf-8"))
            digest.update(b"\0")
            with action.src.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    for conflict in sorted(plan.conflicts):
        digest.update(b"conflict\0")
        digest.update(conflict.encode("utf-8"))
    return digest.hexdigest()

def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"{label} escapes its configured root") from exc

def _is_included_root(relative_path: str, include_roots: list[str]) -> bool:
    rel = relative_path.strip("/\\")
    for root in include_roots:
        if rel == root or rel.startswith(f"{root}/"):
            return True
    return False

def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
