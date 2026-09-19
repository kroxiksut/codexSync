from __future__ import annotations

import hashlib
from pathlib import Path

from .models import CopyAction, DeleteAction, FileMeta, SnapshotFingerprint, SyncManifest, SyncPlan

#: Directions a plan may be built for (`D-012`).
DIRECTIONS = ("bidirectional", "to_cloud", "to_local")


def build_sync_plan(
    local_index: dict[str, FileMeta],
    cloud_index: dict[str, FileMeta],
    local_root: Path,
    cloud_root: Path,
    previous_manifest: SyncManifest | None = None,
    compare_mode: str = "mtime",
    tolerance_seconds: int = 0,
    conflict_policy: str = "manual_abort",
    equal_mtime_action: str = "skip",
    direction: str = "bidirectional",
    delete_policy: str = "never",
) -> SyncPlan:
    """Build the copy plan, with conflict detection.

    ``direction`` narrows what may be written (`D-012`): the actions the
    skipped side would have received are recorded in ``plan.skipped`` rather
    than dropped, because the manifest has to know not to call those paths
    synchronised. ``delete_policy`` decides what a file present on one side
    only means (`D-013`): copy it back, or -- with proof from the previous
    manifest -- remove it here too.
    """
    tolerance_ns = tolerance_seconds * 1_000_000_000
    plan = SyncPlan()
    all_paths = sorted(set(local_index.keys()) | set(cloud_index.keys()))
    prev_files = previous_manifest.files if previous_manifest else {}

    for rel in all_paths:
        local_meta = local_index.get(rel)
        cloud_meta = cloud_index.get(rel)
        prev_entry = prev_files.get(rel)

        if local_meta and not cloud_meta:
            if _deletion_is_proven(local_meta, prev_entry, tolerance_ns, delete_policy, side="cloud"):
                plan.deletions.append(DeleteAction(local_meta.abs_path, rel, "local"))
            elif _deletion_is_disputed(prev_entry, delete_policy, side="cloud"):
                plan.conflicts.append(rel)
            else:
                plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
            continue

        if cloud_meta and not local_meta:
            if _deletion_is_proven(cloud_meta, prev_entry, tolerance_ns, delete_policy, side="local"):
                plan.deletions.append(DeleteAction(cloud_meta.abs_path, rel, "cloud"))
            elif _deletion_is_disputed(prev_entry, delete_policy, side="local"):
                plan.conflicts.append(rel)
            else:
                plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))
            continue

        if not local_meta or not cloud_meta:
            continue

        if _same_file(local_meta, cloud_meta, tolerance_ns, compare_mode):
            continue

        if prev_entry:
            local_changed = _has_side_changed(local_meta, prev_entry.local, tolerance_ns)
            cloud_changed = _has_side_changed(cloud_meta, prev_entry.cloud, tolerance_ns)

            if local_changed and cloud_changed:
                resolved = _resolve_conflict(
                    rel=rel,
                    local_meta=local_meta,
                    cloud_meta=cloud_meta,
                    local_root=local_root,
                    cloud_root=cloud_root,
                    plan=plan,
                    conflict_policy=conflict_policy,
                )
                if not resolved:
                    plan.conflicts.append(rel)
                continue

            if local_changed:
                plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
                continue

            if cloud_changed:
                plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))
                continue

        if abs(local_meta.mtime_ns - cloud_meta.mtime_ns) <= tolerance_ns:
            resolved = _resolve_equal_mtime(
                rel=rel,
                local_meta=local_meta,
                cloud_meta=cloud_meta,
                local_root=local_root,
                cloud_root=cloud_root,
                plan=plan,
                equal_mtime_action=equal_mtime_action,
            )
            if not resolved:
                plan.conflicts.append(rel)
            continue

        if local_meta.mtime_ns > cloud_meta.mtime_ns:
            plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
        else:
            plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))

    return _apply_direction(plan, direction)


def _apply_direction(plan: SyncPlan, direction: str) -> SyncPlan:
    """Drop what this direction may not write, remembering what was dropped.

    A conflict stays a conflict: the direction decides what may be *written*,
    not what counts as agreement, so `conflict.policy` has already had its say
    by the time this runs.
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown sync direction: {direction}")
    if direction == "bidirectional":
        return plan
    skipped_side = "local" if direction == "to_cloud" else "cloud"
    dropped = plan.to_local if direction == "to_cloud" else plan.to_cloud
    plan.skipped.extend(action.relative_path for action in dropped)
    plan.skipped.extend(
        deletion.relative_path for deletion in plan.deletions if deletion.side == skipped_side
    )
    if direction == "to_cloud":
        plan.to_local = []
    else:
        plan.to_cloud = []
    plan.deletions = [item for item in plan.deletions if item.side != skipped_side]
    return plan


def _deletion_is_proven(
    surviving: FileMeta,
    previous: "object | None",
    tolerance_ns: int,
    delete_policy: str,
    *,
    side: str,
) -> bool:
    """Whether the missing side's absence is a deletion this run may follow.

    Three things must hold, and the first run after switching the setting on
    fails the first of them, which is why it deletes nothing (`D-013`):

    * the previous manifest recorded the file on the side it is missing from;
    * it also recorded it on the side that still has it;
    * the surviving side has not changed since that record -- an edit here
      while it was deleted there is a disagreement, not a deletion.
    """
    if delete_policy != "propagate":
        return False
    if previous is None:
        return False
    missing_before = getattr(previous, side, None)
    surviving_before = getattr(previous, "local" if side == "cloud" else "cloud", None)
    if missing_before is None or surviving_before is None:
        return False
    return not _has_side_changed(surviving, surviving_before, tolerance_ns)


def _deletion_is_disputed(previous: "object | None", delete_policy: str, *, side: str) -> bool:
    """A proven deletion whose surviving side moved on: the user decides."""
    if delete_policy != "propagate" or previous is None:
        return False
    missing_before = getattr(previous, side, None)
    surviving_before = getattr(previous, "local" if side == "cloud" else "cloud", None)
    return missing_before is not None and surviving_before is not None


def _resolve_conflict(
    rel: str,
    local_meta: FileMeta,
    cloud_meta: FileMeta,
    local_root: Path,
    cloud_root: Path,
    plan: SyncPlan,
    conflict_policy: str,
) -> bool:
    if conflict_policy == "prefer_cloud":
        plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))
        return True
    if conflict_policy == "prefer_local":
        plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
        return True
    if conflict_policy == "prefer_newer_mtime":
        if local_meta.mtime_ns > cloud_meta.mtime_ns:
            plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
        else:
            plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))
        return True
    return False


def _resolve_equal_mtime(
    rel: str,
    local_meta: FileMeta,
    cloud_meta: FileMeta,
    local_root: Path,
    cloud_root: Path,
    plan: SyncPlan,
    equal_mtime_action: str,
) -> bool:
    if equal_mtime_action == "skip":
        return True
    if equal_mtime_action == "prefer_local":
        plan.to_cloud.append(CopyAction(local_meta.abs_path, cloud_root / rel, rel))
        return True
    if equal_mtime_action == "prefer_cloud":
        plan.to_local.append(CopyAction(cloud_meta.abs_path, local_root / rel, rel))
        return True
    if equal_mtime_action == "manual_abort":
        return False
    return False


def _same_file(local_meta: FileMeta, cloud_meta: FileMeta, tolerance_ns: int, compare_mode: str) -> bool:
    if local_meta.size != cloud_meta.size:
        return False
    mtime_close = abs(local_meta.mtime_ns - cloud_meta.mtime_ns) <= tolerance_ns
    if not mtime_close:
        return False
    if compare_mode == "mtime_hash_fallback":
        return _same_content(local_meta.abs_path, cloud_meta.abs_path)
    return True


def _has_side_changed(meta: FileMeta, previous: SnapshotFingerprint | None, tolerance_ns: int) -> bool:
    if previous is None:
        return True
    if meta.size != previous.size:
        return True
    return abs(meta.mtime_ns - previous.mtime_ns) > tolerance_ns


def _same_content(left: Path, right: Path) -> bool:
    return _sha256(left) == _sha256(right)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
