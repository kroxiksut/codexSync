"""Ways out of an interrupted mutation.

``JournalStore.begin`` refuses to start a mutation while an earlier one is
still non-terminal, which is what protects a half-applied state from being
mutated further.  Without a way to close that journal the protection becomes a
dead end: every later ``sync``/``restore``/``repair-projects apply`` fails and
the only remedy is deleting the journal by hand.  This module provides the two
supported exits.

Both rely on one ordering guarantee from ``SyncEngine.execute``: the complete
backup set is written and ``BackupManager.finalize`` stamps its
``codexsync-backup-v1`` manifest *before* the first destination is replaced.
So a snapshot that carries a committed manifest proves the commit phase was
entered, and a snapshot without one proves it was not — which is why a missing
manifest means there is nothing to undo rather than an unverifiable backup.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import logging
from pathlib import Path

from .config import load_config
from .exceptions import FailSafeError
from .mutation_journal import TERMINAL, JournalState, JournalStore, MutationJournal
from .restore import (
    _backup_manifest_path,
    _is_supported_snapshot,
    _verify_backup_snapshot,
    restore_from_backup,
)
from .runtime import _make_safety_gate, _require_mutation_compatible_config
from .safety_gate import OperationKind

LOG = logging.getLogger(__name__)


class RecoveryAction(str, Enum):
    #: The journal was closed; the interrupted command can be run again.
    RETRY_ALLOWED = "RETRY_ALLOWED"
    #: No destination was replaced, so there is nothing to undo.
    NOTHING_TO_ROLL_BACK = "NOTHING_TO_ROLL_BACK"
    #: The operation's own backup snapshot was restored.
    ROLLED_BACK = "ROLLED_BACK"
    #: Dry run: nothing was changed.
    WOULD_RECOVER = "WOULD_RECOVER"


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    operation_id: str
    family: str
    state: str
    action: RecoveryAction
    snapshot: str | None
    restored_files: int
    detail: str


@dataclass(frozen=True, slots=True)
class JournalInfo:
    """One mutation journal as a recovery screen needs to show it."""

    operation_id: str
    family: str | None
    #: ``JournalState`` value; ``None`` when the journal cannot be read.
    state: str | None
    created_at_utc: str | None
    action_count: int | None
    backup_snapshot: str | None
    terminal: bool
    #: ``False`` means ``recover`` would refuse this journal while ``begin`` is
    #: still blocked by it. The descriptive fields then hold whatever could be
    #: salvaged from the file, as evidence only.
    readable: bool
    #: What ``recover`` allows for this journal, so a GUI offers only legal exits.
    can_resume: bool
    can_rollback: bool
    #: Whether the recorded snapshot exists in ``paths.backup_dir``; ``None``
    #: when none is recorded. Absent is not an error: rollback then closes the
    #: journal as ``NOTHING_TO_ROLL_BACK``.
    backup_snapshot_present: bool | None


def list_journals(config_path: Path) -> list[JournalInfo]:
    """List every mutation journal without creating, repairing or closing one.

    Non-terminal journals come first because they are the ones blocking every
    later mutation; within each group the newest is first. Unlike
    ``JournalStore.non_terminal``, which raises on the first unreadable file
    (correctly, for a gate), this lists a damaged journal as
    ``readable=False``: a recovery screen that fails to open on exactly the
    evidence it exists to show leaves the user nothing but deleting files by
    hand. An unreadable journal still blocks ``begin``, so it sorts with the
    non-terminal ones.
    """
    cfg = load_config(config_path)
    store = JournalStore(cfg.paths.temp_dir)
    if not store.root.is_dir():
        return []
    result = [
        _describe_journal(store, path, cfg.paths.backup_dir)
        for path in store.root.glob("*.json")
        if path.is_file()
    ]
    # Two stable sorts: newest first, then non-terminal before terminal.
    result.sort(key=lambda item: (item.created_at_utc or "", item.operation_id), reverse=True)
    result.sort(key=lambda item: item.terminal)
    return result


def _describe_journal(store: JournalStore, path: Path, backup_root: Path) -> JournalInfo:
    operation_id = path.stem
    try:
        journal: MutationJournal | None = store.load(operation_id)
    except FailSafeError:
        journal = None
    if journal is not None and journal.operation_id == operation_id:
        terminal = journal.state in TERMINAL
        return JournalInfo(
            operation_id=operation_id,
            family=journal.family,
            state=journal.state.value,
            created_at_utc=journal.created_at_utc,
            action_count=journal.action_count,
            backup_snapshot=journal.backup_snapshot,
            terminal=terminal,
            readable=True,
            can_resume=not terminal,
            can_rollback=not terminal and journal.backup_snapshot is not None,
            backup_snapshot_present=_snapshot_presence(backup_root, journal.backup_snapshot),
        )
    # Unreadable, or it claims another operation's identity, which
    # ``_load_recoverable`` refuses. Salvage correctly typed fields for display.
    raw = _salvage_journal_fields(path)
    family = raw.get("family")
    created = raw.get("created_at_utc")
    action_count = raw.get("action_count")
    snapshot = raw.get("backup_snapshot")
    snapshot = snapshot if isinstance(snapshot, str) and snapshot else None
    return JournalInfo(
        operation_id=operation_id,
        family=family if isinstance(family, str) else None,
        state=None,
        created_at_utc=created if isinstance(created, str) else None,
        action_count=action_count if isinstance(action_count, int) and not isinstance(action_count, bool) else None,
        backup_snapshot=snapshot,
        terminal=False,
        readable=False,
        can_resume=False,
        can_rollback=False,
        backup_snapshot_present=_snapshot_presence(backup_root, snapshot),
    )


def _salvage_journal_fields(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _snapshot_presence(backup_root: Path, snapshot_name: str | None) -> bool | None:
    if snapshot_name is None:
        return None
    # A name with a separator can only come from a damaged or edited journal and
    # never names a snapshot inside the backup directory, so it is not followed.
    if "/" in snapshot_name or "\\" in snapshot_name or snapshot_name in {".", ".."}:
        return False
    return _locate_snapshot(backup_root, snapshot_name) is not None


def resume_operation(config_path: Path, operation_id: str, *, dry_run: bool) -> RecoveryOutcome:
    """Close an interrupted journal so its command can be run again.

    This does not replay the interrupted plan: the journal is payload-free by
    design, so the plan no longer exists.  It does not need to.  Every
    destination is replaced with ``os.replace``, so after a crash each file is
    either fully old or fully new, never torn; re-running the original command
    re-plans against what is actually on disk and converges.  What ``resume``
    adds is the verification that re-running is safe, and the journal
    transition that unblocks it.
    """
    cfg, journal, gate = _load_recoverable(config_path, operation_id)
    gate.require(OperationKind.RECOVER_RESUME)

    snapshot = _locate_snapshot(cfg.paths.backup_dir, journal.backup_snapshot)
    if snapshot is not None and _backup_manifest_path(snapshot).is_file():
        # Keep the rollback option honest: if the snapshot no longer matches its
        # manifest, refuse rather than discard the only record of the old state.
        _verify_backup_snapshot(snapshot, allow_legacy_snapshot=False)

    detail = (
        f"Re-run the interrupted `{journal.family}` command; it re-plans from the current state. "
        f"The backup snapshot from the interrupted attempt is kept."
    )
    if dry_run:
        return RecoveryOutcome(
            journal.operation_id, journal.family, journal.state.value,
            RecoveryAction.WOULD_RECOVER, journal.backup_snapshot, 0,
            "Dry-run: the journal would be closed. " + detail,
        )

    _close(JournalStore(cfg.paths.temp_dir), journal)
    return RecoveryOutcome(
        journal.operation_id, journal.family, journal.state.value,
        RecoveryAction.RETRY_ALLOWED, journal.backup_snapshot, 0, detail,
    )


def rollback_operation(
    config_path: Path,
    operation_id: str,
    *,
    target: str,
    dry_run: bool,
) -> RecoveryOutcome:
    """Restore the snapshot the interrupted operation created, then close it.

    ``target`` is explicit on purpose. A single ``sync`` run can back up files
    from both the local and the cloud side, and the backup manifest records
    only relative paths — so the side cannot be recovered from the snapshot
    alone. Guessing it would be a write to the wrong root; the caller states it.
    """
    cfg, journal, gate = _load_recoverable(config_path, operation_id)
    if journal.backup_snapshot is None:
        raise FailSafeError(
            f"Operation {journal.operation_id} has no recorded backup snapshot, so a rollback target "
            "cannot be proven. Inspect the backup directory and use `restore --from <snapshot>` "
            "explicitly, or `recover resume` to retry the command."
        )
    gate.require(OperationKind.RECOVER_ROLLBACK)

    snapshot = _locate_snapshot(cfg.paths.backup_dir, journal.backup_snapshot)
    store = JournalStore(cfg.paths.temp_dir)

    if snapshot is None:
        return _no_op_rollback(
            store, journal, dry_run,
            "The operation created no backup snapshot, so it overwrote nothing.",
        )
    if not _backup_manifest_path(snapshot).is_file():
        # finalize() runs before the first replace, so an unstamped snapshot
        # means the commit phase was never entered.
        return _no_op_rollback(
            store, journal, dry_run,
            "The backup snapshot carries no committed manifest, so no destination was replaced.",
        )

    _verify_backup_snapshot(snapshot, allow_legacy_snapshot=False)
    if dry_run:
        return RecoveryOutcome(
            journal.operation_id, journal.family, journal.state.value,
            RecoveryAction.WOULD_RECOVER, snapshot.name, 0,
            f"Dry-run: snapshot {snapshot.name} verifies and would be restored to {target}.",
        )

    # The journal is closed only after the snapshot has been proven restorable,
    # so the block is never released on a rollback that cannot run. The restore
    # itself then opens and owns its own journal.
    _close(store, journal)
    result = restore_from_backup(
        config_path=config_path,
        snapshot_name=snapshot.name,
        target=target,
        dry_run=False,
    )
    return RecoveryOutcome(
        journal.operation_id, journal.family, journal.state.value,
        RecoveryAction.ROLLED_BACK, snapshot.name, result.restored_files,
        f"Restored {result.restored_files} file(s) from {snapshot.name} to {target}.",
    )


def _load_recoverable(config_path: Path, operation_id: str):
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    journal = JournalStore(cfg.paths.temp_dir).load(operation_id)
    if journal.operation_id != operation_id:
        raise FailSafeError("Mutation journal identity does not match the requested operation")
    if journal.state in TERMINAL:
        raise FailSafeError(
            f"Operation {operation_id} is already {journal.state.value}; nothing to recover."
        )
    return cfg, journal, _make_safety_gate(cfg)


def _locate_snapshot(backup_root: Path, snapshot_name: str | None) -> Path | None:
    if not snapshot_name:
        return None
    candidate = backup_root / snapshot_name
    if candidate.exists() and _is_supported_snapshot(candidate):
        return candidate
    return None


def _no_op_rollback(
    store: JournalStore,
    journal: MutationJournal,
    dry_run: bool,
    reason: str,
) -> RecoveryOutcome:
    if dry_run:
        return RecoveryOutcome(
            journal.operation_id, journal.family, journal.state.value,
            RecoveryAction.WOULD_RECOVER, journal.backup_snapshot, 0,
            f"Dry-run: {reason} The journal would be closed.",
        )
    _close(store, journal)
    return RecoveryOutcome(
        journal.operation_id, journal.family, journal.state.value,
        RecoveryAction.NOTHING_TO_ROLL_BACK, journal.backup_snapshot, 0, reason,
    )


def _close(store: JournalStore, journal: MutationJournal) -> None:
    """Drive the journal to FAILED along a legal transition path.

    ``COMMITTING`` has no direct edge to ``FAILED`` — a process killed mid
    commit must be acknowledged as ``RECOVERY_REQUIRED`` first, so the audit
    trail keeps saying that the commit phase was entered.
    """
    current = journal
    if current.state is JournalState.COMMITTING:
        current = store.transition(current, JournalState.RECOVERY_REQUIRED)
    store.transition(current, JournalState.FAILED)
    LOG.info("mutation journal %s closed as FAILED", journal.operation_id)
