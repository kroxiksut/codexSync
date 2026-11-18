from __future__ import annotations

from collections.abc import Sequence
import hashlib
from pathlib import Path

from .models import (
    CopyAction,
    DeleteAction,
    DestinationPrecondition,
    FileMeta,
    SnapshotFingerprint,
    SyncManifest,
    SyncPlan,
)
from .work_scheduler import WorkScheduler
from .cancellation import CancellationToken

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
    include_roots: Sequence[str] = (),
    *,
    scheduler: WorkScheduler | None = None,
    cancellation: CancellationToken | None = None,
) -> SyncPlan:
    """Build the copy plan, with conflict detection.

    ``direction`` narrows what may be written (`D-012`): the actions the
    skipped side would have received are recorded in ``plan.skipped`` rather
    than dropped, because the manifest has to know not to call those paths
    synchronised. ``delete_policy`` decides what a file present on one side
    only means (`D-013`): copy it back, or -- with proof from the previous
    manifest -- remove it here too.

    Two situations are conflicts whatever the policies say (CS-324): paths
    that differ only in letter case, which a case-insensitive volume stores as
    one file, and a deletion out of an include root the other side holds
    nothing of at all -- that is a missing or re-synchronising folder, not a
    person deleting every file in it.
    """
    tolerance_ns = tolerance_seconds * 1_000_000_000
    plan = SyncPlan()
    all_paths = sorted(set(local_index.keys()) | set(cloud_index.keys()))
    prev_files = previous_manifest.files if previous_manifest else {}
    case_collisions = _case_collisions(all_paths)
    plan.conflicts.extend(sorted(case_collisions))
    # Only the ambiguous content reads fan out. Decisions and action ordering
    # stay on the caller, before any journal or mutation stage is created.
    same_content: dict[str, bool] = {}
    if scheduler is not None and compare_mode == "mtime_hash_fallback":
        ambiguous = [
            rel for rel in all_paths
            if rel not in case_collisions and rel in local_index and rel in cloud_index
            and local_index[rel].size == cloud_index[rel].size
            and abs(local_index[rel].mtime_ns - cloud_index[rel].mtime_ns) <= tolerance_ns
        ]

        def compare(rel: str) -> tuple[str, bool]:
            return rel, _same_content(local_index[rel].abs_path, cloud_index[rel].abs_path)

        same_content = dict(scheduler.map_read(compare, ambiguous, large_file=lambda _rel: True, cancellation=cancellation))

    for rel in all_paths:
        if cancellation is not None:
            cancellation.checkpoint()
        if rel in case_collisions:
            continue
        local_meta = local_index.get(rel)
        cloud_meta = cloud_index.get(rel)
        prev_entry = prev_files.get(rel)

        if local_meta and not cloud_meta:
            if _deletion_is_proven(local_meta, prev_entry, tolerance_ns, delete_policy, side="cloud"):
                plan.deletions.append(_delete_action(local_meta, rel, "local"))
            elif _deletion_is_disputed(prev_entry, delete_policy, side="cloud"):
                plan.conflicts.append(rel)
            else:
                plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
            continue

        if cloud_meta and not local_meta:
            if _deletion_is_proven(cloud_meta, prev_entry, tolerance_ns, delete_policy, side="local"):
                plan.deletions.append(_delete_action(cloud_meta, rel, "cloud"))
            elif _deletion_is_disputed(prev_entry, delete_policy, side="local"):
                plan.conflicts.append(rel)
            else:
                plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))
            continue

        if not local_meta or not cloud_meta:
            continue

        if (same_content[rel] if rel in same_content
                else _same_file(local_meta, cloud_meta, tolerance_ns, compare_mode)):
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
                plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
                continue

            if cloud_changed:
                plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))
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
            plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
        else:
            plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))

    _hold_deletions_from_empty_roots(plan, local_index, cloud_index, include_roots)
    return _apply_direction(plan, direction)


def _case_collisions(paths: Sequence[str]) -> set[str]:
    """Paths that another path in the set equals except for letter case.

    NTFS and APFS (by default) keep `Rules/a.md` and `rules/a.md` as one file,
    so copying both, or one over the other, writes into a file the plan
    believes is someone else's. Which spelling is right is the user's call.
    """
    groups: dict[str, list[str]] = {}
    for rel in paths:
        groups.setdefault(rel.casefold(), []).append(rel)
    return {rel for members in groups.values() if len(members) > 1 for rel in members}


def _hold_deletions_from_empty_roots(
    plan: SyncPlan,
    local_index: dict[str, FileMeta],
    cloud_index: dict[str, FileMeta],
    include_roots: Sequence[str],
) -> None:
    """Turn a deletion into a conflict when the side it came from holds nothing there.

    `propagate` reads "missing on one side" as "deleted there". When a whole
    include root is missing or empty on that side -- a cloud folder being
    re-downloaded, a disconnected drive, a root that was never created -- that
    reading would empty the same root on the other side. Nobody deletes a
    whole root by deleting its files one by one, so the user decides.
    """
    if not plan.deletions:
        return
    roots = sorted(
        {_normalise_root(root) for root in include_roots if _normalise_root(root)},
        key=len,
        reverse=True,
    )
    present = {
        "local": {_root_of(rel, roots) for rel in local_index},
        "cloud": {_root_of(rel, roots) for rel in cloud_index},
    }
    kept: list[DeleteAction] = []
    for deletion in plan.deletions:
        # The file is removed from `side` because it is missing on the other.
        absent_from = "cloud" if deletion.side == "local" else "local"
        if _root_of(deletion.relative_path, roots) in present[absent_from]:
            kept.append(deletion)
        else:
            plan.conflicts.append(deletion.relative_path)
    plan.deletions = kept


def _normalise_root(root: str) -> str:
    value = root.replace("\\", "/").strip().strip("/")
    while value.startswith("./"):
        value = value[2:]
    return "" if value == "." else value


def _root_of(rel: str, roots: Sequence[str]) -> str:
    """The include root ``rel`` belongs to; ``""`` (the whole side) without one."""
    for root in roots:
        if rel == root or rel.startswith(root + "/"):
            return root
    return ""


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
        plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))
        return True
    if conflict_policy == "prefer_local":
        plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
        return True
    if conflict_policy == "prefer_newer_mtime":
        if local_meta.mtime_ns > cloud_meta.mtime_ns:
            plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
        else:
            plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))
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
        plan.to_cloud.append(_copy_action(local_meta, cloud_root / rel, rel, cloud_meta))
        return True
    if equal_mtime_action == "prefer_cloud":
        plan.to_local.append(_copy_action(cloud_meta, local_root / rel, rel, local_meta))
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


def _precondition(meta: FileMeta | None) -> DestinationPrecondition | None:
    if meta is None:
        return DestinationPrecondition(exists=False)
    # Production indexes come from ``scan_tree`` and always carry filesystem
    # identity. Synthetic/legacy FileMeta values do not; binding an action to
    # their invented timestamp would turn compatibility callers into failures.
    if meta.device is None or meta.inode is None:
        return None
    return DestinationPrecondition(
        exists=True,
        mtime_ns=meta.mtime_ns,
        size=meta.size,
        device=meta.device,
        inode=meta.inode,
    )


def _copy_action(
    source: FileMeta,
    destination: Path,
    relative_path: str,
    destination_meta: FileMeta | None,
) -> CopyAction:
    return CopyAction(
        source.abs_path,
        destination,
        relative_path,
        destination_precondition=_precondition(destination_meta),
    )


def _delete_action(meta: FileMeta, relative_path: str, side: str) -> DeleteAction:
    return DeleteAction(
        meta.abs_path,
        relative_path,
        side,
        destination_precondition=_precondition(meta),
    )


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
