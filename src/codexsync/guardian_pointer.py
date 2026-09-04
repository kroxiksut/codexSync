from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from .exceptions import GuardianIntegrityError
from .guardian_manifest import GUARDIAN_MANIFEST_VERSION, load_guardian_manifest, verify_guardian_snapshot
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_LATEST_GOOD_DIR_NAME,
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GuardianSnapshot,
    require_guardian_machine_id,
)


@dataclass(slots=True, frozen=True)
class LatestGoodPointer:
    manifest_version: int
    machine_id: str
    snapshot_id: str
    generation: int
    sha256: str
    updated_at_utc: str


def resolve_or_restore_latest_good(root_dir: Path, machine_id: str) -> GuardianSnapshot | None:
    """Resolve a verified pointer or safely rebuild it from committed snapshots."""
    root = root_dir.resolve()
    machine = require_guardian_machine_id(machine_id)
    pointer = _read_pointer(_pointer_path(root, machine))
    if pointer is not None:
        snapshot = _snapshot_from_pointer(root, pointer)
        if snapshot is not None:
            return snapshot
    recovered = _scan_latest_committed(root, machine)
    if recovered is not None:
        _write_pointer(root, recovered)
    return recovered


def publish_latest_good(root_dir: Path, candidate: GuardianSnapshot) -> GuardianSnapshot:
    """Advance latest-good only to a verified, strictly newer, distinct snapshot."""
    root = root_dir.resolve()
    verified = _verify_candidate(root, candidate)
    current = resolve_or_restore_latest_good(root, candidate.machine_id)
    if current is not None:
        current_manifest = _verify_candidate(root, current)
        if current_manifest.generation >= verified.generation or current_manifest.sha256 == verified.sha256:
            return current
    _write_pointer(root, candidate)
    return candidate


def _pointer_path(root: Path, machine_id: str) -> Path:
    return root / GUARDIAN_LATEST_GOOD_DIR_NAME / f"{machine_id}.json"


def _read_pointer(path: Path) -> LatestGoodPointer | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    required = {"manifest_version", "machine_id", "snapshot_id", "generation", "sha256", "updated_at_utc"}
    if not required.issubset(raw):
        return None
    try:
        pointer = LatestGoodPointer(
            manifest_version=raw["manifest_version"],
            machine_id=raw["machine_id"],
            snapshot_id=raw["snapshot_id"],
            generation=raw["generation"],
            sha256=raw["sha256"],
            updated_at_utc=raw["updated_at_utc"],
        )
        if (
            pointer.manifest_version != GUARDIAN_MANIFEST_VERSION
            or require_guardian_machine_id(pointer.machine_id) != pointer.machine_id
            or not isinstance(pointer.snapshot_id, str)
            or "/" in pointer.snapshot_id
            or "\\" in pointer.snapshot_id
            or not isinstance(pointer.generation, int)
            or pointer.generation < 1
            or not isinstance(pointer.sha256, str)
            or len(pointer.sha256) != 64
            or not isinstance(pointer.updated_at_utc, str)
        ):
            return None
    except (TypeError, ValueError):
        return None
    return pointer


def _snapshot_from_pointer(root: Path, pointer: LatestGoodPointer) -> GuardianSnapshot | None:
    snapshot = GuardianSnapshot(root, pointer.machine_id, pointer.snapshot_id, pointer.generation)
    expected_parent = (root / GUARDIAN_SNAPSHOTS_DIR_NAME / pointer.machine_id).resolve()
    try:
        snapshot.directory.resolve().relative_to(expected_parent)
    except ValueError:
        return None
    try:
        manifest = _verify_candidate(root, snapshot)
    except GuardianIntegrityError:
        return None
    return snapshot if manifest.sha256 == pointer.sha256 else None


def _scan_latest_committed(root: Path, machine_id: str) -> GuardianSnapshot | None:
    snapshots_root = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine_id
    if not snapshots_root.is_dir():
        return None
    candidates: list[tuple[int, GuardianSnapshot]] = []
    for directory in snapshots_root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not (directory / GUARDIAN_COMMITTED_NAME).is_file():
            continue
        try:
            manifest = load_guardian_manifest(directory / GUARDIAN_MANIFEST_NAME)
            snapshot = GuardianSnapshot(root, machine_id, manifest.snapshot_id, manifest.generation)
            if snapshot.directory != directory:
                continue
            _verify_candidate(root, snapshot)
        except GuardianIntegrityError:
            continue
        candidates.append((manifest.generation, snapshot))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _verify_candidate(root: Path, snapshot: GuardianSnapshot):
    if snapshot.root_dir.resolve() != root:
        raise GuardianIntegrityError("Guardian latest-good candidate has an unexpected root")
    if not snapshot.committed_path.is_file():
        raise GuardianIntegrityError("Guardian latest-good candidate is not committed")
    manifest = verify_guardian_snapshot(snapshot)
    try:
        marker = json.loads(snapshot.committed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuardianIntegrityError("Guardian latest-good candidate has an invalid COMMITTED marker") from exc
    if not isinstance(marker, dict) or (
        marker.get("snapshot_id") != manifest.snapshot_id
        or marker.get("generation") != manifest.generation
        or marker.get("sha256") != manifest.sha256
    ):
        raise GuardianIntegrityError("Guardian latest-good candidate marker does not match manifest")
    return manifest


def _write_pointer(root: Path, snapshot: GuardianSnapshot) -> None:
    manifest = _verify_candidate(root, snapshot)
    path = _pointer_path(root, snapshot.machine_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest_version": GUARDIAN_MANIFEST_VERSION,
        "machine_id": snapshot.machine_id,
        "snapshot_id": snapshot.snapshot_id,
        "generation": snapshot.generation,
        "sha256": manifest.sha256,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise GuardianIntegrityError("Cannot atomically update Guardian latest-good pointer") from exc
