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
