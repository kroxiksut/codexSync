from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import shutil
from pathlib import Path

from .guardian_manifest import load_guardian_manifest, verify_guardian_snapshot
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_QUARANTINE_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GUARDIAN_STAGING_DIR_NAME,
    GuardianSnapshot,
)
from .guardian_pointer import resolve_or_restore_latest_good


def prune_snapshots(
    root_dir: Path,
    machine_id: str,
    *,
    retention_days: int = 30,
    max_snapshots: int = 100,
    now: datetime | None = None,
) -> list[str]:
    """Prune only verified snapshots, never latest-good or its predecessor."""
    if retention_days < 0 or max_snapshots < 0:
        raise ValueError("Guardian retention values must be >= 0")
    root = root_dir.resolve()
    snapshots_root = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine_id
    if not snapshots_root.is_dir():
        return []
    entries: list[tuple[datetime, GuardianSnapshot]] = []
    for directory in snapshots_root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not (directory / GUARDIAN_COMMITTED_NAME).is_file():
            continue
        try:
            manifest = load_guardian_manifest(directory / "manifest.json")
            snapshot = GuardianSnapshot(root, machine_id, manifest.snapshot_id, manifest.generation)
            if snapshot.directory != directory:
                continue
            verify_guardian_snapshot(snapshot)
            created = datetime.fromisoformat(manifest.created_at_utc[:-1] + "+00:00")
        except Exception:
            continue
        entries.append((created, snapshot))
    entries.sort(key=lambda item: item[1].generation, reverse=True)
    latest = resolve_or_restore_latest_good(root, machine_id)
    protected = {latest.snapshot_id} if latest else set()
    if latest:
        older = [item[1] for item in entries if item[1].generation < latest.generation]
        if older:
            protected.add(older[0].snapshot_id)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    removed: list[str] = []
    for index, (created, snapshot) in enumerate(entries):
        over_count = max_snapshots > 0 and index >= max_snapshots
        expired = retention_days > 0 and created < cutoff
        if snapshot.snapshot_id in protected or not (over_count or expired):
            continue
        try:
            snapshot.directory.resolve().relative_to(snapshots_root.resolve())
            if snapshot.directory.is_symlink():
                continue
            shutil.rmtree(snapshot.directory)
            removed.append(snapshot.snapshot_id)
        except OSError:
            continue
    return removed


def prune_quarantine(root_dir: Path, machine_id: str, *, retention_days: int = 30, now: datetime | None = None) -> list[str]:
    if retention_days < 0:
        raise ValueError("Guardian quarantine retention must be >= 0")
    if retention_days == 0:
        return []
    root = root_dir.resolve()
    base = root / GUARDIAN_QUARANTINE_DIR_NAME / machine_id
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    return _prune_event_directories(base, root, cutoff, "event_id")


def prune_staging(root_dir: Path, machine_id: str, *, retention_hours: int = 24, now: datetime | None = None) -> list[str]:
    if retention_hours < 0:
        raise ValueError("Guardian staging retention must be >= 0")
    if retention_hours == 0:
        return []
    root = root_dir.resolve()
    base = root / GUARDIAN_STAGING_DIR_NAME / machine_id
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    removed: list[str] = []
    if not base.is_dir():
        return removed
    for directory in base.iterdir():
        # Only codexSync-owned UUID or quarantine-UUID temporary names are eligible.
        name = directory.name.removeprefix("quarantine-")
        if directory.is_symlink() or not directory.is_dir() or len(name) != 36:
            continue
        try:
            created = datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
            directory.resolve().relative_to(base.resolve())
            if created < cutoff:
                shutil.rmtree(directory)
                removed.append(directory.name)
        except OSError:
            continue
    return removed


def _prune_event_directories(base: Path, root: Path, cutoff: datetime, identifier: str) -> list[str]:
    removed: list[str] = []
    if not base.is_dir():
        return removed
    for directory in base.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            raw = json.loads((directory / GUARDIAN_MANIFEST_NAME).read_text(encoding="utf-8"))
            event_id = raw.get(identifier)
            created = datetime.fromisoformat(raw["created_at_utc"][:-1] + "+00:00")
            if not isinstance(event_id, str) or event_id != directory.name:
                continue
            directory.resolve().relative_to(root)
            if created < cutoff:
                shutil.rmtree(directory)
                removed.append(event_id)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
            continue
    return removed
