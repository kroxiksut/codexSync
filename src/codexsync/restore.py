"""Restore of a committed backup snapshot into local or cloud state.

Restore is a mutation: it runs the same envelope as sync (operation lock,
mutation journal, backup-first, final process check before every replace).
Legacy snapshots without a codexsync-backup-v1 manifest are accepted only
with an explicit id plus --allow-legacy-snapshot and can never carry
semantic-owned state.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import uuid
import zipfile

from .backup import BackupManager
from .config import load_config
from .exceptions import ConfigError
from .filters import PathFilter
from .models import AppConfig, CopyAction, SyncPlan
from .mutation_journal import JournalState, JournalStore
from .operation_lock import OperationLock
from .runtime import (
    _hash_file,
    _is_included_root,
    _is_semantic_owned,
    _make_safety_gate,
    _plan_hash,
    _require_mutation_compatible_config,
    _require_within,
    initialize_runtime_paths,
)
from .safety_gate import OperationKind
from .state_locator import detect_local_state_dir
from .sync_engine import SyncEngine

LOG = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RestoreResult:
    snapshot_name: str
    target: str
    restored_files: int

def restore_from_backup(
    config_path: Path,
    snapshot_name: str | None,
    target: str,
    dry_run: bool,
    manual_terminate_confirmation_override: bool | None = None,
    allow_legacy_snapshot: bool = False,
) -> RestoreResult:
    cfg = load_config(config_path)
    safety_gate = _make_safety_gate(cfg)
    _require_mutation_compatible_config(cfg)
    safety_gate.require(OperationKind.RESTORE)
    initialize_runtime_paths(cfg)

    target_root = _resolve_restore_target(cfg, target)
    snapshot = _resolve_snapshot_dir(
        cfg.paths.backup_dir,
        snapshot_name,
        allow_legacy_snapshot=allow_legacy_snapshot,
    )
    _verify_backup_snapshot(snapshot, allow_legacy_snapshot=allow_legacy_snapshot)
    plan, extracted_snapshot_dir = _build_restore_plan_from_snapshot(
        snapshot=snapshot,
        target_root=target_root,
        include_roots=cfg.targets.include_roots,
        exclude_globs=cfg.filters.exclude_globs,
        temp_root=cfg.paths.temp_dir,
    )

    mgr = BackupManager(
        backup_root=cfg.paths.backup_dir,
        machine_id=cfg.identity.machine_id or platform.node(),
        retention_days=cfg.backup.retention_days,
        max_backups=cfg.backup.max_backups,
        compression=cfg.backup.compression,
    )
    recovery_required = False
    try:
        if dry_run:
            SyncEngine(mgr, cfg.paths.temp_dir, True, True).execute(plan, dry_run=True)
        else:
            with OperationLock(
                cfg.paths.temp_dir,
                state_root=target_root,
                machine_id=cfg.identity.machine_id or platform.node(),
                family="restore",
            ):
                journals = JournalStore(cfg.paths.temp_dir)
                journal = journals.begin(
                    "restore",
                    _plan_hash(plan),
                    plan.action_count,
                    backup_snapshot=mgr.snapshot_name,
                )
                current = [journal]

                def after_backup() -> None:
                    current[0] = journals.transition(current[0], JournalState.BACKED_UP)

                def pre_commit() -> None:
                    safety_gate.require(OperationKind.RESTORE, final=True)
                    current[0] = journals.transition(current[0], JournalState.COMMITTING)

                engine = SyncEngine(
                    backup_manager=mgr,
                    temp_dir=cfg.paths.temp_dir,
                    backup_before_overwrite=True,
                    fail_on_unknown=True,
                    pre_commit_check=pre_commit,
                    before_replace_check=lambda: safety_gate.require(OperationKind.RESTORE, final=True),
                    after_backup=after_backup,
                )
                try:
                    engine.execute(plan, dry_run=False)
                    current[0] = journals.transition(current[0], JournalState.COMMITTED)
                    mgr.prune()
                except Exception:
                    recovery_required = current[0].state is JournalState.COMMITTING
                    failure = JournalState.RECOVERY_REQUIRED if recovery_required else JournalState.FAILED
                    try:
                        current[0] = journals.transition(current[0], failure)
                    except Exception:
                        LOG.exception("Could not persist terminal restore journal state")
                    raise
    finally:
        if extracted_snapshot_dir is not None and not recovery_required:
            shutil.rmtree(extracted_snapshot_dir, ignore_errors=True)

    return RestoreResult(
        snapshot_name=snapshot.name,
        target=target,
        restored_files=plan.action_count,
    )

def _resolve_restore_target(cfg: AppConfig, target: str) -> Path:
    if target == "local":
        return detect_local_state_dir(cfg.paths.local_state_dir)
    if target == "cloud":
        return cfg.paths.cloud_root_dir
    raise ConfigError(f"Unsupported restore target: {target}")

def _resolve_snapshot_dir(
    backup_root: Path,
    snapshot_name: str | None,
    *,
    allow_legacy_snapshot: bool = False,
) -> Path:
    if snapshot_name:
        snapshot = (backup_root / snapshot_name).resolve()
        if snapshot.exists() and _is_supported_snapshot(snapshot):
            if not _backup_manifest_path(snapshot).is_file() and not allow_legacy_snapshot:
                raise ConfigError("Legacy backup requires --allow-legacy-snapshot and an explicit id")
            return snapshot
        if snapshot.suffix.lower() != ".zip":
            zip_candidate = (backup_root / f"{snapshot_name}.zip").resolve()
            if zip_candidate.exists() and _is_supported_snapshot(zip_candidate):
                if not _backup_manifest_path(zip_candidate).is_file() and not allow_legacy_snapshot:
                    raise ConfigError("Legacy backup requires --allow-legacy-snapshot and an explicit id")
                return zip_candidate
        raise ConfigError(f"Backup snapshot not found: {snapshot_name}")

    snapshots = [
        p for p in backup_root.iterdir()
        if _is_supported_snapshot(p) and _backup_manifest_path(p).is_file()
    ] if backup_root.exists() else []
    if not snapshots:
        raise ConfigError(f"No backup snapshots found in: {backup_root}")
    snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return snapshots[0]

def _is_supported_snapshot(path: Path) -> bool:
    return path.is_dir() or (path.is_file() and path.suffix.lower() == ".zip")

def _backup_manifest_path(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.name + ".manifest.json")

def _verify_backup_snapshot(snapshot: Path, *, allow_legacy_snapshot: bool) -> None:
    manifest_path = _backup_manifest_path(snapshot)
    if not manifest_path.is_file():
        if allow_legacy_snapshot:
            return
        raise ConfigError("Backup snapshot has no committed codexSync manifest")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("Backup snapshot manifest is invalid") from exc
    if not isinstance(raw, dict) or raw.get("format") != "codexsync-backup-v1" or raw.get("committed") is not True:
        raise ConfigError("Backup snapshot manifest is unsupported or uncommitted")
    if raw.get("snapshot") != snapshot.name or not isinstance(raw.get("entries"), list):
        raise ConfigError("Backup snapshot manifest does not match the selected snapshot")
    entries: dict[str, tuple[str, int]] = {}
    for item in raw["entries"]:
        if not isinstance(item, dict):
            raise ConfigError("Backup snapshot manifest entry is invalid")
        rel = item.get("relative_path")
        sha = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(rel, str)
            or rel in entries
            or not isinstance(sha, str)
            or len(sha) != 64
            or not isinstance(size, int)
            or size < 0
        ):
            raise ConfigError("Backup snapshot manifest entry is invalid")
        entries[rel] = (sha, size)
    actual = _snapshot_hash_inventory(snapshot)
    if actual != entries:
        raise ConfigError("Backup snapshot contents do not match its committed manifest")

def _snapshot_hash_inventory(snapshot: Path) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    if snapshot.is_dir():
        for source in snapshot.rglob("*"):
            if source.is_symlink():
                raise ConfigError("Backup snapshot contains a symlink")
            if source.is_file():
                rel = source.relative_to(snapshot).as_posix()
                result[rel] = (_hash_file(source), source.stat().st_size)
        return result
    with zipfile.ZipFile(snapshot, "r") as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            rel = _safe_zip_relative(entry)
            if rel in result:
                raise ConfigError("Backup zip contains duplicate entries")
            digest = hashlib.sha256()
            with archive.open(entry, "r") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[rel] = (digest.hexdigest(), entry.file_size)
    return result

def _build_restore_plan_from_snapshot(
    snapshot: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
    temp_root: Path,
) -> tuple[SyncPlan, Path | None]:
    if snapshot.is_dir():
        return _build_restore_plan(snapshot, target_root, include_roots, exclude_globs), None
    if snapshot.is_file() and snapshot.suffix.lower() == ".zip":
        return _build_restore_plan_from_zip_snapshot(
            snapshot_zip=snapshot,
            target_root=target_root,
            include_roots=include_roots,
            exclude_globs=exclude_globs,
            temp_root=temp_root,
        )
    raise ConfigError(f"Unsupported backup snapshot format: {snapshot}")

def _build_restore_plan_from_zip_snapshot(
    snapshot_zip: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
    temp_root: Path,
) -> tuple[SyncPlan, Path]:
    path_filter = PathFilter(exclude_globs)
    allowed_roots = [root.strip("/\\") for root in include_roots if root.strip("/\\")]
    staging_dir = temp_root / f".codexsync-restore-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    actions: list[CopyAction] = []
    seen_entries: set[str] = set()
    with zipfile.ZipFile(snapshot_zip, mode="r") as zf:
        for entry in zf.infolist():
            if entry.is_dir():
                continue
            rel = _safe_zip_relative(entry)
            if rel in seen_entries:
                raise ConfigError(f"Backup zip contains a duplicate entry: {rel}")
            seen_entries.add(rel)
            if _is_semantic_owned(rel):
                raise ConfigError("Legacy restore contains semantic-owned state and requires the semantic restore pipeline")
            if not _is_included_root(rel, allowed_roots):
                continue
            if path_filter.is_excluded(rel):
                continue
            staged = staging_dir / f"{uuid.uuid4().hex}.bin"
            with zf.open(entry, "r") as src, staged.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            dst_path = target_root / Path(rel.replace("/", os.sep))
            _require_within(dst_path, target_root, "restore target")
            actions.append(CopyAction(src=staged, dst=dst_path, relative_path=rel))
    return SyncPlan(to_local=actions, to_cloud=[]), staging_dir

def _build_restore_plan(
    snapshot_dir: Path,
    target_root: Path,
    include_roots: list[str],
    exclude_globs: list[str],
) -> SyncPlan:
    path_filter = PathFilter(exclude_globs)
    allowed_roots = [root.strip("/\\") for root in include_roots if root.strip("/\\")]
    actions: list[CopyAction] = []

    snapshot_root = snapshot_dir.resolve()
    for source in snapshot_dir.rglob("*"):
        if source.is_symlink():
            raise ConfigError("Legacy snapshot contains a symlink and cannot be restored safely")
        if not source.is_file():
            continue
        _require_within(source, snapshot_root, "snapshot source")
        rel = source.relative_to(snapshot_dir).as_posix()
        if _is_semantic_owned(rel):
            raise ConfigError("Legacy restore contains semantic-owned state and requires the semantic restore pipeline")
        if not _is_included_root(rel, allowed_roots):
            continue
        if path_filter.is_excluded(rel):
            continue
        dst = target_root / Path(rel.replace("/", os.sep))
        _require_within(dst, target_root, "restore target")
        actions.append(CopyAction(src=source, dst=dst, relative_path=rel))

    return SyncPlan(to_local=actions, to_cloud=[])

def _safe_zip_relative(entry: zipfile.ZipInfo) -> str:
    raw = entry.filename.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith(("/", "//"))
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ConfigError("Backup zip contains an unsafe path")
    unix_mode = (entry.external_attr >> 16) & 0xFFFF
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise ConfigError("Backup zip contains a symlink entry")
    return path.as_posix()
