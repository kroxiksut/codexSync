"""Read-only inventory of the Guardian store for one machine.

The Guardian writer (``guardian_store``) and its helpers are allowed to repair
what they find: ``resolve_or_restore_latest_good`` rewrites a damaged pointer,
and retention prunes. A screen that merely *shows* the store must do neither.
If opening a window could move ``latest-good``, the pointer would change
because someone looked at it, and a pointer rebuilt from a scan the user never
asked for is exactly the kind of silent state change this project exists to
prevent. So this module reuses the same verification primitives but never the
functions that write, never takes the writer lock (that creates a file), and
never creates the Guardian root.

What it reports is deliberately split into ``committed`` (the COMMITTED marker
exists and agrees with the manifest) and ``verified`` (the payload matches the
manifest's size and SHA-256). They fail independently: a crash between
publishing a snapshot directory and writing its marker leaves a verified but
uncommitted snapshot, and damage to the payload leaves a committed but
unverified one. Only a snapshot that is both can be ``latest-good``.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path

from .config import load_config, require_guardian_identity
from .exceptions import GuardianIntegrityError
from .guardian_manifest import load_guardian_manifest, verify_guardian_snapshot
from .guardian_models import (
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_QUARANTINE_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GuardianManifest,
    GuardianSnapshot,
)
from .guardian_pointer import _pointer_path, _read_pointer, _snapshot_from_pointer
from .guardian_store import _verify_committed_marker


#: Verification hashes each listed payload, so the listing is bounded. The
#: writer's own default retention (``guardian.max_snapshots``) is 100; the cap
#: only matters for a store whose retention was disabled.
MAX_LISTED_SNAPSHOTS = 500
MAX_LISTED_QUARANTINE = 500


@dataclass(frozen=True, slots=True)
class GuardianSnapshotInfo:
    snapshot_id: str
    generation: int
    created_at_utc: str
    #: ``source_size`` from the manifest: the size of the captured state file.
    size: int
    project_count: int
    binding_count: int
    validation_status: str
    validation_codes: tuple[str, ...]
    schema_id: str | None
    #: The COMMITTED marker is present and names this manifest's id, generation and hash.
    committed: bool
    #: ``verify_guardian_snapshot`` passed: the payload matches the manifest.
    verified: bool
    latest_good: bool


@dataclass(frozen=True, slots=True)
class QuarantineInfo:
    event_id: str
    created_at_utc: str | None
    reason_codes: tuple[str, ...]
    #: Size of the rejected payload, ``None`` when it was not stable enough to be saved.
    size: int | None


@dataclass(frozen=True, slots=True)
class GuardianInventory:
    machine_id: str
    root_dir: Path
    #: The snapshot the pointer names, only when that snapshot verifies right
    #: now. A dangling or damaged pointer yields ``None`` plus a ``problems`` line.
    latest_good_id: str | None
    snapshots: tuple[GuardianSnapshotInfo, ...]
    quarantine: tuple[QuarantineInfo, ...]
    problems: tuple[str, ...]


def read_guardian_inventory(config_path: Path) -> GuardianInventory:
    """Describe this machine's Guardian snapshots, pointer and quarantine.

    Creates, rewrites and prunes nothing. A missing Guardian root is an empty
    inventory, not an error: Guardian simply has not run on this machine yet.
    """
    cfg = load_config(config_path)
    machine = require_guardian_identity(cfg)
    root = cfg.guardian.root_dir.resolve()
    if not root.is_dir():
        return GuardianInventory(machine, root, None, (), (), ())

    problems: list[str] = []
    latest_good_id = _read_latest_good(root, machine, problems)
    snapshots = _list_snapshots(root, machine, latest_good_id, problems)
    if latest_good_id is None and any(item.committed and item.verified for item in snapshots):
        problems.append(
            "No usable latest-good pointer although verified committed snapshots exist; "
            "Guardian rebuilds it on its next run"
        )
    quarantine = _list_quarantine(root, machine, problems)
    return GuardianInventory(machine, root, latest_good_id, snapshots, quarantine, tuple(problems))


def _read_latest_good(root: Path, machine: str, problems: list[str]) -> str | None:
    """Resolve the pointer the way ``resolve_or_restore_latest_good`` does, minus the rebuild."""
    path = _pointer_path(root, machine)
    if not path.exists():
        return None
    pointer = _read_pointer(path)
    if pointer is None:
        problems.append("latest-good pointer is unreadable or malformed")
        return None
    if pointer.machine_id != machine:
        problems.append(f"latest-good pointer claims machine {pointer.machine_id!r}, expected {machine!r}")
        return None
    if _snapshot_from_pointer(root, pointer) is None:
        problems.append(
            f"latest-good pointer names snapshot {pointer.snapshot_id} (generation {pointer.generation}), "
            "which is missing, uncommitted or fails verification"
        )
        return None
    return pointer.snapshot_id


def _list_snapshots(
    root: Path,
    machine: str,
    latest_good_id: str | None,
    problems: list[str],
) -> tuple[GuardianSnapshotInfo, ...]:
    snapshots_root = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine
    if not snapshots_root.is_dir():
        return ()
    # Pass 1 reads only manifests, which are small, so the cap can be applied
    # by generation before anything is hashed in pass 2.
    loaded: list[tuple[GuardianManifest, Path]] = []
    for directory in snapshots_root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            manifest = load_guardian_manifest(directory / GUARDIAN_MANIFEST_NAME)
        except GuardianIntegrityError:
            problems.append(f"snapshot directory {directory.name} has no readable manifest")
            continue
        if manifest.snapshot_id != directory.name or manifest.machine_id != machine:
            problems.append(f"snapshot directory {directory.name} holds a manifest for another snapshot or machine")
            continue
        loaded.append((manifest, directory))

    counts = Counter(manifest.generation for manifest, _ in loaded)
    for generation in sorted((g for g, n in counts.items() if n > 1), reverse=True):
        ids = sorted(manifest.snapshot_id for manifest, _ in loaded if manifest.generation == generation)
        problems.append(f"generation {generation} is used by {len(ids)} snapshots: {', '.join(ids)}")

    loaded.sort(key=lambda item: (item[0].generation, item[0].snapshot_id), reverse=True)
    if len(loaded) > MAX_LISTED_SNAPSHOTS:
        problems.append(
            f"listing truncated to the newest {MAX_LISTED_SNAPSHOTS} of {len(loaded)} snapshots"
        )
        loaded = loaded[:MAX_LISTED_SNAPSHOTS]

    result: list[GuardianSnapshotInfo] = []
    for manifest, _directory in loaded:
        snapshot = GuardianSnapshot(root, machine, manifest.snapshot_id, manifest.generation)
        result.append(
            GuardianSnapshotInfo(
                snapshot_id=manifest.snapshot_id,
                generation=manifest.generation,
                created_at_utc=manifest.created_at_utc,
                size=manifest.source_size,
                project_count=manifest.project_count,
                binding_count=manifest.binding_count,
                validation_status=manifest.validation_status.value,
                validation_codes=manifest.validation_codes,
                schema_id=manifest.schema_id,
                committed=_is_committed(snapshot, manifest),
                verified=_is_verified(snapshot),
                latest_good=manifest.snapshot_id == latest_good_id,
            )
        )
    return tuple(result)


def _is_committed(snapshot: GuardianSnapshot, manifest: GuardianManifest) -> bool:
    if not snapshot.committed_path.is_file():
        return False
    try:
        _verify_committed_marker(snapshot, manifest)
    except GuardianIntegrityError:
        return False
    return True


def _is_verified(snapshot: GuardianSnapshot) -> bool:
    try:
        verify_guardian_snapshot(snapshot)
    except GuardianIntegrityError:
        return False
    return True


def _list_quarantine(root: Path, machine: str, problems: list[str]) -> tuple[QuarantineInfo, ...]:
    """List quarantine events from their manifests; the saved payload is never opened."""
    base = root / GUARDIAN_QUARANTINE_DIR_NAME / machine
    if not base.is_dir():
        return ()
    events: list[QuarantineInfo] = []
    for directory in base.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            raw = json.loads((directory / GUARDIAN_MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            problems.append(f"quarantine event {directory.name} has no readable manifest")
            events.append(QuarantineInfo(directory.name, None, (), None))
            continue
        if raw.get("event_id") != directory.name:
            problems.append(f"quarantine event {directory.name} has a manifest for another event")
        created = raw.get("created_at_utc")
        codes = raw.get("reason_codes")
        size = raw.get("source_size")
        events.append(
            QuarantineInfo(
                event_id=directory.name,
                created_at_utc=created if isinstance(created, str) else None,
                reason_codes=tuple(code for code in codes if isinstance(code, str)) if isinstance(codes, list) else (),
                size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            )
        )
    # Event ids begin with their UTC timestamp, so the id is a usable tiebreak
    # and a fallback order for an event whose manifest could not be read.
    events.sort(key=lambda event: (event.created_at_utc or "", event.event_id), reverse=True)
    if len(events) > MAX_LISTED_QUARANTINE:
        problems.append(
            f"quarantine listing truncated to the newest {MAX_LISTED_QUARANTINE} of {len(events)} events"
        )
        events = events[:MAX_LISTED_QUARANTINE]
    return tuple(events)
