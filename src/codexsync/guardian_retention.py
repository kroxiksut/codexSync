from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import shutil
from pathlib import Path

from .guardian_accept import ACCEPTED_MARKERS
from .guardian_manifest import load_guardian_manifest, verify_guardian_snapshot
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_PAYLOAD_NAME,
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
    """Prune only verified snapshots, never latest-good or its predecessor.

    Also never a snapshot a person accepted over a suspicious shrink or a
    schema change, nor the baseline it overrode (``guardian_accept``). Both are rare, and the second
    is the only record of what the state looked like before the drop.
    """
    if retention_days < 0 or max_snapshots < 0:
        raise ValueError("Guardian retention values must be >= 0")
    root = root_dir.resolve()
    snapshots_root = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine_id
    if not snapshots_root.is_dir():
        return []
    entries: list[tuple[datetime, GuardianSnapshot]] = []
    decisions: set[str] = set()
    unread = False
    for directory in snapshots_root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not (directory / GUARDIAN_COMMITTED_NAME).is_file():
            continue
        try:
            manifest = load_guardian_manifest(directory / "manifest.json")
        except Exception:
            # Its accept marks are unknown, so a baseline it protects could be
            # pruned by mistake (D-014, CS-441): prune nothing this pass.
            unread = True
            continue
        # Marks come from the manifest alone: a payload that fails to verify
        # (or is locked right now) still names the baseline it overrode.
        if ACCEPTED_MARKERS.intersection(manifest.validation_codes):
            decisions.add(manifest.snapshot_id)
            if manifest.previous_good_snapshot_id is not None:
                decisions.add(manifest.previous_good_snapshot_id)
        try:
            snapshot = GuardianSnapshot(root, machine_id, manifest.snapshot_id, manifest.generation)
            if snapshot.directory != directory:
                continue
            verify_guardian_snapshot(snapshot)
            created = datetime.fromisoformat(manifest.created_at_utc[:-1] + "+00:00")
        except Exception:
            continue
        entries.append((created, snapshot))
    if unread:
        return []
    entries.sort(key=lambda item: item[1].generation, reverse=True)
    latest = resolve_or_restore_latest_good(root, machine_id)
    protected = ({latest.snapshot_id} if latest else set()) | decisions
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


#: ``build_snapshot_id``: ``<UTC timestamp>-<uuid>-<12 hex of the payload hash>``.
_SNAPSHOT_ID_RE = re.compile(
    r"\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{12}"
)
#: The ``COMMITTED`` marker's temporary name, current and before CS-310.
_MARKER_TEMP_RE = re.compile(re.escape(f".{GUARDIAN_COMMITTED_NAME}.") + r"(?:[0-9a-f]{32}\.)?tmp")


def prune_uncommitted_snapshots(
    root_dir: Path,
    machine_id: str,
    *,
    retention_hours: int = 24,
    now: datetime | None = None,
) -> list[str]:
    """Remove snapshot directories a crash left published but never committed.

    Such a directory is invisible to every reader -- they all require
    ``COMMITTED`` -- so it only takes space. It is removed only when it is
    provably the writer's own leftover: a snapshot-id name, no ``COMMITTED``
    entry of any kind, nothing inside but the payload, the manifest and the
    marker's temporary file, and nothing touched for ``retention_hours``.
    Anything else is left alone. Decided from names and times only, so a
    manifest that cannot be read right now does not keep it.
    """
    if retention_hours < 0:
        raise ValueError("Guardian staging retention must be >= 0")
    if retention_hours == 0:
        return []
    root = root_dir.resolve()
    base = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine_id
    if not base.is_dir():
        return []
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)).timestamp()
    removed: list[str] = []
    for directory in base.iterdir():
        try:
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not _SNAPSHOT_ID_RE.fullmatch(directory.name)
                or os.path.lexists(directory / GUARDIAN_COMMITTED_NAME)
                or directory.stat().st_mtime >= cutoff
            ):
                continue
            children = list(directory.iterdir())
            if not all(_is_uncommitted_leftover(child, cutoff) for child in children):
                continue
            directory.resolve().relative_to(base.resolve())
            shutil.rmtree(directory)
            removed.append(directory.name)
        except (OSError, ValueError):
            continue
    return removed


def _is_uncommitted_leftover(child: Path, cutoff: float) -> bool:
    if child.name not in {GUARDIAN_PAYLOAD_NAME, GUARDIAN_MANIFEST_NAME} and not _MARKER_TEMP_RE.fullmatch(child.name):
        return False
    if child.is_symlink() or not child.is_file():
        return False
    return child.stat().st_mtime < cutoff


def _prune_event_directories(base: Path, root: Path, cutoff: datetime, identifier: str) -> list[str]:
    removed: list[str] = []
    if not base.is_dir():
        return removed
    for directory in base.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            raw = json.loads((directory / GUARDIAN_MANIFEST_NAME).read_text(encoding="utf-8"))
            # A manifest of another shape is unreadable, not a crash (CS-478).
            if not isinstance(raw, dict) or not isinstance(raw.get("created_at_utc"), str):
                continue
            event_id = raw.get(identifier)
            created = datetime.fromisoformat(raw["created_at_utc"][:-1] + "+00:00")
            if not isinstance(event_id, str) or event_id != directory.name:
                continue
            directory.resolve().relative_to(root)
            if created < cutoff:
                shutil.rmtree(directory)
                removed.append(event_id)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return removed
