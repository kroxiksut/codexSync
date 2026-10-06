"""Ways out of an interrupted mutation.

``JournalStore.begin`` refuses to start a mutation while an earlier one is
still non-terminal, which is what protects a half-applied state from being
mutated further.  Without a way to close that journal the protection becomes a
dead end: every later ``sync``/``restore``/``repair-projects apply`` fails and
the only remedy is deleting the journal by hand.  This module provides the two
supported exits.

Both rely on two ordering guarantees. ``SyncEngine.execute`` (and
``app.commit_global_state``) moves the journal to ``COMMITTING`` before the
first destination is replaced, so a journal still ``PREPARED`` or
``BACKED_UP`` proves nothing was. And the complete backup set is written and
``BackupManager.finalize`` stamps its ``codexsync-backup-v1`` manifest *before*
that first replace, so a snapshot without a manifest proves the same.

A snapshot the journal names but that is *gone* proves nothing (CS-294): the
backup directory may be shared with another machine whose retention removed
it, so a rollback refuses instead of reporting that nothing was overwritten.

Rollback is planned and proven before the journal is closed (CS-292), and it
follows the journal's family: files of a sync or restore go back through the
restore envelope, each into the side its manifest entry records (CS-293); the
global state goes back through ``app.commit_global_state``; a session transfer
is not rolled back at all (see ``_rollback_refusal``).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
from pathlib import Path
import platform
import shutil
from typing import Mapping
import zipfile

from .config import load_config
from .exceptions import ConfigError, FailSafeError
from .guardian_models import ValidationStatus
from .guardian_schema import validate_global_state_references
from .models import AppConfig, SyncPlan
from .mutation_journal import (
    BEFORE_COMMIT,
    TERMINAL,
    JournalState,
    JournalStore,
    MutationJournal,
    owned_by,
)
from .operation_lock import OperationLock
from .restore import (
    _backup_manifest_path,
    _build_restore_plan_from_snapshot,
    _is_supported_snapshot,
    _verify_backup_snapshot,
    execute_restore_plan,
    read_backup_manifest_sides,
)
from .runtime import _make_safety_gate, _require_mutation_compatible_config
from .safety_gate import OperationKind
from .state_locator import locate_local_state_dir

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
    #: when none is recorded. Absent is harmless only while the journal never
    #: reached ``COMMITTING``; after that rollback refuses (CS-294).
    backup_snapshot_present: bool | None
    # Descriptive only, from builds that record them (`MutationJournal`).
    counts: Mapping[str, int] | None = None
    origin: str | None = None
    finished_at_utc: str | None = None
    failure: str | None = None
    #: Why ``recover rollback`` would refuse this journal; ``None`` when it
    #: would not (or when the journal cannot be read at all).
    rollback_refusal: str | None = None
    #: The machine the journal records; ``None`` for journals of older builds.
    machine_id: str | None = None
    #: Whether this machine ran it (``mutation_journal.owned_by``).
    own: bool = False
    #: Whether the next mutation here closes it by itself: this machine's, and
    #: stopped before the commit phase, so nothing was replaced. Anything else
    #: still open waits for ``recover resume`` or ``recover rollback``.
    closes_itself: bool = False


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
    machine = cfg.identity.machine_id or platform.node()
    result = [
        _describe_journal(store, path, cfg.paths.backup_dir, machine)
        for path in store.root.glob("*.json")
        if path.is_file()
    ]
    # Two stable sorts: newest first, then non-terminal before terminal.
    result.sort(key=lambda item: (item.created_at_utc or "", item.operation_id), reverse=True)
    result.sort(key=lambda item: item.terminal)
    return result


def list_history(
    config_path: Path, *, family: str | None = None, limit: int | None = None
) -> list[JournalInfo]:
    """Past runs, newest first, optionally of one family.

    The same journals `list_journals` reads -- a history needs no store of its
    own -- but ordered by time alone: here an unfinished run is one more row,
    not the thing to put first. A dry run writes no journal and so is never in
    it. Like the recovery listing it creates, repairs and closes nothing.
    """
    items = [
        item for item in list_journals(config_path)
        if family is None or item.family == family
    ]
    items.sort(key=lambda item: (item.created_at_utc or "", item.operation_id), reverse=True)
    return items if limit is None else items[:max(limit, 0)]


def _describe_journal(store: JournalStore, path: Path, backup_root: Path, machine: str) -> JournalInfo:
    operation_id = path.stem
    try:
        journal: MutationJournal | None = store.load(operation_id)
    except FailSafeError:
        journal = None
    if journal is not None and journal.operation_id == operation_id:
        terminal = journal.state in TERMINAL
        refusal = None if terminal else _rollback_refusal(journal, backup_root)
        own = owned_by(journal, machine)
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
            can_rollback=not terminal and refusal is None,
            backup_snapshot_present=_snapshot_presence(backup_root, journal.backup_snapshot),
            counts=journal.counts,
            origin=journal.origin,
            finished_at_utc=journal.finished_at_utc,
            failure=journal.failure,
            rollback_refusal=refusal,
            machine_id=journal.machine_id,
            own=own,
            closes_itself=own and journal.state in BEFORE_COMMIT,
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
    target: str | None = None,
    dry_run: bool,
) -> RecoveryOutcome:
    """Put back what the interrupted operation replaced, then close its journal.

    Every file goes back to the side its backup manifest entry records
    (CS-293), because one sync backs up both sides into one snapshot under
    their relative paths. ``target`` is optional: given, it must name the one
    side the snapshot holds -- a rollback never writes a file into a root it
    was not taken from. It is required only for a snapshot written before
    sides were recorded, and only where a single side is provable (a
    ``restore``); an older ``sync`` snapshot is refused rather than guessed.

    Everything that can refuse -- the snapshot's integrity, its sides, whether
    this family can be rolled back at all, the candidate global state's
    validity -- is checked before the journal is closed (CS-292). The journal
    is then closed and the restore run while holding the state root's lock,
    so no other mutation can slip in between.
    """
    if target not in (None, "local", "cloud"):
        raise ConfigError(f"Unsupported rollback target: {target}")
    cfg, journal, gate = _load_recoverable(config_path, operation_id)
    if journal.backup_snapshot is None:
        raise FailSafeError(
            f"Operation {journal.operation_id} has no recorded backup snapshot, so a rollback target "
            "cannot be proven. Inspect the backup directory and use `restore --from <snapshot>` "
            "explicitly, or `recover resume` to retry the command."
        )
    gate.require(OperationKind.RECOVER_ROLLBACK)
    store = JournalStore(cfg.paths.temp_dir)

    if journal.state in _BEFORE_COMMIT:
        # COMMITTING is recorded before the first replace, so a journal that
        # never reached it proves every destination is untouched.
        return _no_op_rollback(
            store, journal, dry_run,
            "The operation never entered its commit phase, so no destination was replaced.",
        )
    refusal = _rollback_refusal(journal, cfg.paths.backup_dir)
    if refusal is not None:
        raise FailSafeError(f"Operation {journal.operation_id} cannot be rolled back: {refusal}")
    snapshot = _locate_snapshot(cfg.paths.backup_dir, journal.backup_snapshot)
    if snapshot is None:  # _rollback_refusal proved it is there; re-checked, never assumed
        raise FailSafeError(f"Backup snapshot {journal.backup_snapshot} is not in the backup directory")
    if not _backup_manifest_path(snapshot).is_file():
        # finalize() runs before the first replace, so an unstamped snapshot
        # means no destination was replaced.
        return _no_op_rollback(
            store, journal, dry_run,
            "The backup snapshot carries no committed manifest, so no destination was replaced.",
        )

    _verify_backup_snapshot(snapshot, allow_legacy_snapshot=False)
    local_root = locate_local_state_dir(cfg)
    if journal.family in _GLOBAL_STATE_FAMILIES:
        rollback: _Rollback = _plan_global_state_rollback(snapshot, local_root, target)
    else:
        rollback = _plan_file_rollback(cfg, journal, snapshot, local_root, target)
    try:
        if dry_run:
            return RecoveryOutcome(
                journal.operation_id, journal.family, journal.state.value,
                RecoveryAction.WOULD_RECOVER, snapshot.name, 0,
                f"Dry-run: snapshot {snapshot.name} verifies; {rollback.describe()} would be restored.",
            )
        machine = cfg.identity.machine_id or platform.node()
        with OperationLock(
            cfg.paths.temp_dir, state_root=local_root, machine_id=machine,
            family="recover", reentrant=True,
        ):
            # Re-read under the lock: another `recover` may have closed it.
            if store.load(journal.operation_id) != journal:
                raise FailSafeError(f"Operation {journal.operation_id} changed while the rollback was prepared")
            rollback.recheck()
            _close(store, journal)
            restored = rollback.execute(cfg, gate)
    finally:
        rollback.discard()
    return RecoveryOutcome(
        journal.operation_id, journal.family, journal.state.value,
        RecoveryAction.ROLLED_BACK, snapshot.name, restored,
        f"Restored {restored} file(s) from {snapshot.name}: {rollback.describe()}.",
    )


#: Journal states that prove the commit phase was never entered.
_BEFORE_COMMIT = BEFORE_COMMIT
#: Families whose only write is `.codex-global-state.json`, through
#: `app.commit_global_state`; they are rolled back through it too.
_GLOBAL_STATE_FAMILIES = frozenset({"repair", "chats", "guardian-restore", "project-move", "project-sync"})
#: Families whose files are rolled back through the restore envelope.
_FILE_FAMILIES = frozenset({"sync", "restore"})
_GLOBAL_STATE_FILE = ".codex-global-state.json"


def _rollback_refusal(journal: MutationJournal, backup_root: Path) -> str | None:
    """Why rollback would refuse this non-terminal journal, cheaply; ``None`` if it would not.

    Shared by the listing (``can_rollback``) and ``rollback_operation`` so a
    screen never offers an exit that then refuses. Reads the small manifest
    JSON at most, never the payload.
    """
    if journal.backup_snapshot is None:
        return "no backup snapshot is recorded"
    if journal.state in _BEFORE_COMMIT:
        return None
    if journal.family == "sessions":
        # A transfer only ever fast-forwards a branch, and a branch it
        # displaced by a resolution is kept in a conflict bundle, so re-running
        # the transfer after `recover resume` loses nothing. Writing old
        # session bytes back would bypass the catalogue and prefix checks that
        # decide every session write, so it is refused rather than done blind.
        return (
            "a session transfer is not rolled back; use `recover resume`, then `sessions scan` "
            "and `sessions apply` re-plan from what is on disk"
        )
    if journal.family not in _GLOBAL_STATE_FAMILIES | _FILE_FAMILIES:
        return f"rollback of a {journal.family!r} operation is not supported; use `recover resume`"
    snapshot = _locate_snapshot(backup_root, journal.backup_snapshot)
    if snapshot is None:
        return (
            f"its backup snapshot {journal.backup_snapshot} is not in the backup directory. The commit "
            "phase was entered, so files may have been replaced; the snapshot was removed (retention, "
            "or another machine sharing the folder) or was never written because nothing existed to "
            "back up. What was replaced cannot be proven, so nothing is restored: use `recover "
            "resume` and check the result"
        )
    if not _backup_manifest_path(snapshot).is_file():
        return None
    try:
        sides = read_backup_manifest_sides(snapshot)
    except ConfigError as exc:
        return f"its backup snapshot cannot be read: {exc}"
    if journal.family in _GLOBAL_STATE_FAMILIES:
        if set(sides) != {_GLOBAL_STATE_FILE} or sides[_GLOBAL_STATE_FILE] not in (None, "local"):
            return "its backup snapshot holds something other than the global state it replaced"
        return None
    if journal.family == "sync" and any(side is None for side in sides.values()):
        return (
            "its backup snapshot does not record which side each file came from (written before "
            "codexSync recorded it), and a sync backs up both sides; restore it by hand with "
            "`restore --from <snapshot> --target <side>` after checking its files"
        )
    return None


class _Rollback:
    """A rollback proven possible and not yet run."""

    def describe(self) -> str:
        raise NotImplementedError

    def recheck(self) -> None:
        """Re-prove, under the lock, what planning read from the live state."""

    def execute(self, cfg: AppConfig, gate) -> int:
        raise NotImplementedError

    def discard(self) -> None:
        """Drop whatever planning staged."""


@dataclass
class _FileRollback(_Rollback):
    plan: SyncPlan
    staging_dir: Path | None

    def describe(self) -> str:
        parts = []
        if self.plan.to_local:
            parts.append(f"{len(self.plan.to_local)} file(s) to local")
        if self.plan.to_cloud:
            parts.append(f"{len(self.plan.to_cloud)} file(s) to cloud")
        return ", ".join(parts) or "no file"

    def execute(self, cfg: AppConfig, gate) -> int:
        staging, self.staging_dir = self.staging_dir, None
        execute_restore_plan(cfg, gate, self.plan, staging, dry_run=False)
        return self.plan.action_count

    def discard(self) -> None:
        if self.staging_dir is not None:
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            self.staging_dir = None


def _plan_file_rollback(
    cfg: AppConfig,
    journal: MutationJournal,
    snapshot: Path,
    local_root: Path,
    target: str | None,
) -> _FileRollback:
    sides = read_backup_manifest_sides(snapshot)
    recorded = {side for side in sides.values() if side is not None}
    if any(side is None for side in sides.values()):
        # Only a `restore` gets here (see `_rollback_refusal`): its snapshot
        # holds the one side it wrote, and the caller names which.
        if target is None:
            raise ConfigError(
                f"Snapshot {snapshot.name} does not record which side its files came from; "
                "name it with --target (the side the interrupted restore wrote into)."
            )
        if recorded - {target}:
            raise ConfigError(f"Snapshot {snapshot.name} mixes recorded and unrecorded sides")
    elif target is not None and recorded - {target}:
        raise ConfigError(
            f"Snapshot {snapshot.name} holds files of the {' and '.join(sorted(recorded))} side(s); "
            f"--target {target} would put some of them into the wrong root. Omit --target: each file "
            "goes back to the side it was backed up from."
        )
    roots = {"local": local_root, "cloud": cfg.paths.cloud_root_dir}

    def destination(rel: str) -> tuple[str, Path]:
        side = sides.get(rel) or target
        if side not in roots:
            raise ConfigError(f"Snapshot entry has no restorable side: {rel}")
        return side, roots[side]

    plan, staging = _build_restore_plan_from_snapshot(
        snapshot=snapshot,
        destination=destination,
        include_roots=cfg.targets.include_roots,
        exclude_globs=cfg.filters.exclude_globs,
        temp_root=cfg.paths.temp_dir,
    )
    return _FileRollback(plan, staging)


@dataclass
class _GlobalStateRollback(_Rollback):
    source: Path
    live: bytes
    candidate: bytes

    def describe(self) -> str:
        return f"{_GLOBAL_STATE_FILE} to local"

    def recheck(self) -> None:
        try:
            current = self.source.read_bytes()
        except OSError as exc:
            raise FailSafeError(f"{_GLOBAL_STATE_FILE} cannot be read: {exc}") from exc
        if current != self.live:
            raise FailSafeError(f"{_GLOBAL_STATE_FILE} changed while the rollback was prepared; run it again")

    def execute(self, cfg: AppConfig, gate) -> int:
        # `app` imports this module; the envelope is reached lazily.
        from .app import commit_global_state

        commit_global_state(
            cfg, gate, OperationKind.RECOVER_ROLLBACK,
            family="restore",
            plan_id=hashlib.sha256(self.candidate).hexdigest(),
            action_count=1,
            state_root=self.source.parent,
            source=self.source,
            original=self.live,
            candidate=self.candidate,
        )
        return 1


def _plan_global_state_rollback(snapshot: Path, local_root: Path, target: str | None) -> _GlobalStateRollback:
    """Prove the snapshot's global state can go back through `commit_global_state`.

    A restore would refuse the file as semantic-owned, and rightly: the global
    state is written only through that envelope, which re-validates the result.
    So the candidate is checked here with the same Guardian validation and the
    same schema rule `guardian restore` applies, before anything is closed.
    """
    if target not in (None, "local"):
        raise ConfigError(f"The global state lives on the local side; --target {target} does not apply")
    candidate = _read_snapshot_member(snapshot, _GLOBAL_STATE_FILE)
    source = local_root / _GLOBAL_STATE_FILE
    try:
        live = source.read_bytes()
    except FileNotFoundError as exc:
        raise FailSafeError(
            f"{source} is missing; a rollback backs up the live file first and cannot run without it"
        ) from exc
    except OSError as exc:
        raise FailSafeError(f"{source} cannot be read: {exc}") from exc
    accepted = {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}
    report = validate_global_state_references(candidate)
    if report.status not in accepted:
        raise FailSafeError(
            f"The global state in snapshot {snapshot.name} fails validation ({report.status.value}); not restored"
        )
    live_report = validate_global_state_references(live)
    if live_report.schema_id is not None and live_report.schema_id != report.schema_id:
        raise FailSafeError(
            f"The live global state is schema {live_report.schema_id!r} and the snapshot's is "
            f"{report.schema_id!r}; Codex changed its format since, so the old state is not written back"
        )
    return _GlobalStateRollback(source, live, candidate)


def _read_snapshot_member(snapshot: Path, relative: str) -> bytes:
    try:
        if snapshot.is_dir():
            return (snapshot / relative).read_bytes()
        with zipfile.ZipFile(snapshot, "r") as archive:
            return archive.read(relative)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise FailSafeError(f"Snapshot {snapshot.name} does not hold a readable {relative}") from exc


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
