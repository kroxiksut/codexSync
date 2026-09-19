"""Plan a restore of ``.codex-global-state.json`` from a Guardian snapshot.

Guardian keeps verified copies of the global state outside ``.codex``, but a
copy nobody can put back only proves what was lost. This module is the
*planning* half of putting one back: it finds the snapshot, proves it is the
same committed, verified, still-valid state Guardian recorded, and describes
what a restore would change. It writes nothing.

The write itself belongs to ``app.commit_global_state``, which owns the
operation lock, journal, verified backup, final process check and rollback for
every edit of that file. Keeping the two apart means a restore gets exactly the
same care as a repair or a chat move, and this module never imports ``app``
(``app`` imports it).

Three decisions carry the safety:

* Only the snapshot directory of *this* machine is searched. Another machine's
  global state names that machine's project roots; restoring it here would be a
  handoff, which is ``repair-projects``' job and not a restore.
* Verification reuses the checks the latest-good pointer and the inventory
  already apply, and the bytes returned are re-hashed against the manifest, so
  the payload handed to the writer is the one that was verified rather than a
  second read that merely followed a successful first one.
* The plan id covers the snapshot hash *and* the current state hash. A preview
  shown to the user stops matching the moment Codex writes the file, which is
  what makes ``--confirm-plan`` mean "this exact replacement" without a plan
  file left on disk.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .exceptions import ConfigError, ConflictError, FailSafeError, GuardianIntegrityError
from .guardian_manifest import load_guardian_manifest
from .guardian_models import (
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GuardianManifest,
    GuardianSnapshot,
    ValidationStatus,
    require_guardian_machine_id,
)
from .guardian_pointer import _verify_candidate
from .guardian_schema import validate_global_state_references
from .guardian_store import _verify_committed_marker


GUARDIAN_RESTORE_PLAN_VERSION = 1

#: No snapshot directory with that id exists for this machine.
SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
#: The directory exists but is not committed, its marker disagrees with the
#: manifest, or the payload does not match the manifest's size and SHA-256.
SNAPSHOT_UNVERIFIED = "SNAPSHOT_UNVERIFIED"
#: The payload is intact but no longer validates under today's schema rules.
SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
#: The live state already has exactly the snapshot's bytes.
NOTHING_TO_RESTORE = "NOTHING_TO_RESTORE"
#: The live state is recognised as a different schema than the snapshot. The
#: runtime that wrote today's file would be handed a shape it may no longer
#: read -- a desktop build upgrade is exactly when that happens.
SCHEMA_CHANGED = "SCHEMA_CHANGED"
#: The snapshot and the live state share not one project id, though both have
#: projects. Codex re-created its projects since the snapshot (observed on a
#: real machine: all 16 ids replaced by 18 new ones between two snapshots), so
#: putting the snapshot back would hand the runtime ids it no longer knows and
#: drop every project it knows now. Not a damaged state to repair: another one.
PROJECT_IDS_REPLACED = "PROJECT_IDS_REPLACED"

_RESTORABLE = frozenset({ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING})


@dataclass(frozen=True, slots=True)
class GuardianRestorePlan:
    version: int
    plan_id: str
    machine_id: str
    snapshot_id: str
    #: ``generation``, ``snapshot_created_at_utc`` and ``snapshot_sha256`` come
    #: from the manifest. They are ``0``/``""`` when the snapshot was not found
    #: or its manifest is unreadable; for an unverified snapshot they are what
    #: the manifest *claims*, which is why any code blocks the apply.
    generation: int
    snapshot_created_at_utc: str
    snapshot_sha256: str
    current_state_sha256: str | None
    schema_id: str | None
    projects_now: int | None
    bindings_now: int | None
    projects_in_snapshot: int
    bindings_in_snapshot: int
    identical: bool
    codes: tuple[str, ...]


def build_guardian_restore_plan(
    *,
    root_dir: Path,
    machine_id: str,
    snapshot_id: str,
    current_state: bytes | None,
) -> tuple[GuardianRestorePlan, bytes | None]:
    """Describe restoring ``snapshot_id`` over ``current_state``. Reads only.

    Returns the plan and, when the plan carries no blocking code other than
    ``NOTHING_TO_RESTORE``, the verified snapshot payload. The caller reads the
    current state itself (through the stable reader) and passes its bytes, so
    the hash in the plan is of exactly what the user was shown.

    Deliberately absent: rebuilding the latest-good pointer, taking the Guardian
    writer lock, or creating the Guardian root. Each writes a file, and looking
    at what a restore would do must not change the store it looks at.
    """
    machine = _require_machine(machine_id)
    _require_safe_snapshot_id(snapshot_id)
    current_sha256 = hashlib.sha256(current_state).hexdigest() if current_state is not None else None
    projects_now: int | None = None
    bindings_now: int | None = None
    schema_now: str | None = None
    if current_state is not None:
        # Counts only. A broken or unrecognised live state is precisely what a
        # restore exists to replace, so its status never becomes a code.
        now_report = validate_global_state_references(current_state)
        projects_now = now_report.project_count
        bindings_now = now_report.binding_count
        schema_now = now_report.schema_id

    root = root_dir.resolve()
    manifest, payload, code = _load_verified_snapshot(root, machine, snapshot_id)
    codes: list[str] = [] if code is None else [code]
    schema_id: str | None = None
    projects_in_snapshot = 0
    bindings_in_snapshot = 0
    if payload is not None:
        report = validate_global_state_references(payload)
        schema_id = report.schema_id
        projects_in_snapshot = report.project_count or 0
        bindings_in_snapshot = report.binding_count or 0
        if report.status not in _RESTORABLE:
            # Committed under the rules of the day it was taken; a restore must
            # meet today's, or the post-write validation in the commit envelope
            # would roll it back anyway — after a backup and a replace.
            codes.append(SNAPSHOT_INVALID)
            payload = None
        elif schema_now is not None and schema_id is not None and schema_now != schema_id:
            codes.append(SCHEMA_CHANGED)
        elif current_state is not None:
            ids_now = _project_ids(current_state)
            ids_then = _project_ids(payload)
            if ids_now and ids_then and not ids_now & ids_then:
                codes.append(PROJECT_IDS_REPLACED)

    identical = payload is not None and current_state is not None and payload == current_state
    if identical:
        codes.append(NOTHING_TO_RESTORE)

    snapshot_sha256 = manifest.sha256 if manifest is not None else ""
    plan = GuardianRestorePlan(
        version=GUARDIAN_RESTORE_PLAN_VERSION,
        plan_id=_plan_id(machine, snapshot_id, snapshot_sha256, current_sha256),
        machine_id=machine,
        snapshot_id=snapshot_id,
        generation=manifest.generation if manifest is not None else 0,
        snapshot_created_at_utc=manifest.created_at_utc if manifest is not None else "",
        snapshot_sha256=snapshot_sha256,
        current_state_sha256=current_sha256,
        schema_id=schema_id,
        projects_now=projects_now,
        bindings_now=bindings_now,
        projects_in_snapshot=projects_in_snapshot,
        bindings_in_snapshot=bindings_in_snapshot,
        identical=identical,
        codes=tuple(codes),
    )
    return plan, payload


def verify_restore_still_valid(
    plan: GuardianRestorePlan,
    *,
    root_dir: Path,
    current_state: bytes | None,
    confirm_plan: str,
) -> bytes:
    """Re-prove ``plan`` against the store and live state; return the bytes to commit.

    The confirmation is checked against the plan first, because a mistyped id
    is an argument error and says nothing about the state. The rebuild is then
    compared by id, which covers both the snapshot hash and the live state hash:
    if either moved since the preview, the user approved a replacement that is
    no longer the one on offer. Codes are read from the rebuild, not the
    preview, so a snapshot damaged in between is refused even though its
    manifest — and therefore the id — did not change.
    """
    if confirm_plan != plan.plan_id:
        raise ConfigError(
            f"--confirm-plan {confirm_plan!r} does not match the restore plan id {plan.plan_id}"
        )
    rebuilt, payload = build_guardian_restore_plan(
        root_dir=root_dir,
        machine_id=plan.machine_id,
        snapshot_id=plan.snapshot_id,
        current_state=current_state,
    )
    if rebuilt.plan_id != plan.plan_id:
        raise FailSafeError(
            "The state or the snapshot changed since the preview; build a new restore plan"
        )
    if rebuilt.codes:
        raise ConflictError(f"Guardian restore is blocked: {', '.join(rebuilt.codes)}")
    if payload is None:  # Unreachable while every refusal is a code; kept fail-closed.
        raise FailSafeError("Guardian restore has no verified payload to write")
    return payload


def _require_machine(machine_id: str) -> str:
    try:
        return require_guardian_machine_id(machine_id)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _require_safe_snapshot_id(snapshot_id: str) -> None:
    """Refuse an id that could name anything but one child of the machine directory.

    A snapshot id is generated as ``<timestamp>-<uuid>-<hash prefix>`` and never
    contains these. ``:`` is included because on Windows ``Path(root) / "C:x"``
    silently leaves ``root`` for a drive-relative path, and NUL because the
    operating system would truncate or reject the name rather than open it.
    """
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id.strip()
        or snapshot_id in {".", ".."}
        or any(token in snapshot_id for token in ("/", "\\", "..", ":", "\x00"))
    ):
        raise ConfigError(f"Guardian snapshot id is not a plain snapshot name: {snapshot_id!r}")


def _load_verified_snapshot(
    root: Path,
    machine: str,
    snapshot_id: str,
) -> tuple[GuardianManifest | None, bytes | None, str | None]:
    """Return ``(manifest, payload, code)``; payload is set only when fully verified."""
    machine_dir = root / GUARDIAN_SNAPSHOTS_DIR_NAME / machine
    directory = machine_dir / snapshot_id
    if not directory.is_dir() and not directory.is_symlink():
        return None, None, SNAPSHOT_NOT_FOUND
    if directory.is_symlink():
        # The inventory never lists a linked directory and the writer never
        # creates one; whatever it points at was not committed here.
        return None, None, SNAPSHOT_UNVERIFIED
    try:
        manifest = load_guardian_manifest(directory / GUARDIAN_MANIFEST_NAME)
    except (GuardianIntegrityError, ValueError):
        return None, None, SNAPSHOT_UNVERIFIED
    snapshot = GuardianSnapshot(root, machine, snapshot_id, manifest.generation)
    try:
        snapshot.directory.resolve().relative_to(machine_dir.resolve())
    except (OSError, ValueError):
        return manifest, None, SNAPSHOT_UNVERIFIED
    try:
        # The pointer's check (committed, payload matches manifest, marker
        # agrees) plus the writer's own marker check, which also requires the
        # commit timestamp: together that is what the inventory calls
        # committed *and* verified.
        verified = _verify_candidate(root, snapshot)
        _verify_committed_marker(snapshot, verified)
        payload = snapshot.payload_path.read_bytes()
    except (GuardianIntegrityError, ValueError, OSError):
        return manifest, None, SNAPSHOT_UNVERIFIED
    # These bytes are the ones returned for writing, and they are a separate
    # read from the one verified above; hash them rather than trust the order.
    if len(payload) != verified.source_size or hashlib.sha256(payload).hexdigest() != verified.sha256:
        return manifest, None, SNAPSHOT_UNVERIFIED
    return verified, payload, None


def _plan_id(machine: str, snapshot_id: str, snapshot_sha256: str, current_sha256: str | None) -> str:
    canonical = json.dumps(
        {
            "version": GUARDIAN_RESTORE_PLAN_VERSION,
            "machine_id": machine,
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot_sha256,
            "current_state_sha256": current_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _project_ids(payload: bytes) -> set[str]:
    """Project ids a state names, or nothing when they cannot be read.

    Only the ids are compared; names and roots never leave this function.
    """
    try:
        state = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return set()
    projects = state.get("local-projects") if isinstance(state, dict) else None
    return {key for key in projects if isinstance(key, str)} if isinstance(projects, dict) else set()
