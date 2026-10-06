"""Durable, payload-free evidence for an interrupted mutation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
from typing import Mapping
from uuid import uuid4

from .exceptions import FailSafeError, RecoveryPendingError
from .guardian_models import normalize_machine_id
from .fs_replace import replace_with_retry


class JournalState(str, Enum):
    PREPARED = "PREPARED"
    BACKED_UP = "BACKED_UP"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


LOG = logging.getLogger(__name__)

TERMINAL = frozenset({JournalState.COMMITTED, JournalState.FAILED})
#: States that prove the commit phase was never entered: every writer moves the
#: journal to ``COMMITTING`` before its first replace, so nothing was replaced.
BEFORE_COMMIT = frozenset({JournalState.PREPARED, JournalState.BACKED_UP})
#: What ``failure`` says of a journal ``begin`` closed because its own run had
#: stopped before the commit phase (see ``JournalStore.close_abandoned``).
ABANDONED = "Abandoned"
_TRANSITIONS = {
    JournalState.PREPARED: {JournalState.BACKED_UP, JournalState.FAILED},
    JournalState.BACKED_UP: {JournalState.COMMITTING, JournalState.FAILED},
    JournalState.COMMITTING: {JournalState.COMMITTED, JournalState.RECOVERY_REQUIRED},
    JournalState.RECOVERY_REQUIRED: {JournalState.COMMITTING, JournalState.FAILED},
    JournalState.COMMITTED: set(),
    JournalState.FAILED: set(),
}


@dataclass(frozen=True, slots=True)
class MutationJournal:
    operation_id: str
    family: str
    state: JournalState
    created_at_utc: str
    plan_hash: str
    action_count: int
    #: Name of the backup snapshot this operation created, recorded before the
    #: first destination is replaced.  ``None`` means no snapshot is known:
    #: either the operation overwrote nothing, or it was journalled by a build
    #: that did not record the link.  Rollback refuses to guess in that case.
    backup_snapshot: str | None = None
    # What a history needs and a recovery does not. All optional, because a
    # journal written by an older build has none of them, and none of them is
    # ever consulted by a gate: they describe a run, they do not decide one.
    # Still payload-free -- numbers and fixed words, never a path or a message.
    #: How many actions of each kind the plan held (``to_cloud``, ``to_local``,
    #: ``deletions`` for a sync), when the caller knows.
    counts: Mapping[str, int] | None = None
    #: Who started the run: ``window``, ``cli`` or ``unattended``.
    origin: str | None = None
    #: When the journal reached ``COMMITTED`` or ``FAILED``.
    finished_at_utc: str | None = None
    #: The exception class that ended a failed run (``ConflictError`` ...),
    #: never its message, which may name files.
    failure: str | None = None
    #: The machine that ran the operation, normalised. Absent from journals of
    #: older builds; ``owned_by`` then falls back to the snapshot name, which
    #: carries the machine too. Decides only whether ``begin`` may close a
    #: journal on its own -- never whether a mutation may run.
    machine_id: str | None = None


def snapshot_belongs_to(name: str | None, machine_id: str | None) -> bool:
    """True when ``name`` is a backup snapshot name ``BackupManager`` writes for this machine."""
    if not name:
        return False
    safe_machine = normalize_machine_id(machine_id) or "unknown-machine"
    pattern = rf"{re.escape(safe_machine)}-\d{{8}}T\d{{6}}Z-[0-9a-f]{{12}}(\.zip)?"
    return re.fullmatch(pattern, name) is not None


def owned_by(journal: MutationJournal, machine_id: str | None) -> bool:
    """Whether this machine ran the operation the journal records.

    The journal folder lives in the shared workspace, so another machine's
    journals sit beside this one's. A recorded machine decides; a journal
    without one (older builds) belongs to the machine its snapshot is named
    after. Neither proves it -> not this machine's, which only means it is
    never closed without a person.
    """
    if journal.machine_id is not None:
        return journal.machine_id == normalize_machine_id(machine_id)
    return snapshot_belongs_to(journal.backup_snapshot, machine_id)


class JournalStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "journals"

    def begin(
        self,
        family: str,
        plan_hash: str,
        action_count: int,
        *,
        backup_snapshot: str | None = None,
        counts: Mapping[str, int] | None = None,
        origin: str | None = None,
        machine_id: str | None = None,
    ) -> MutationJournal:
        """Open a journal for a new operation.

        ``machine_id`` is this machine; every caller passes it while holding
        this machine's ``OperationLock``, and only then does ``begin`` close
        this machine's abandoned journals first (``close_abandoned``). Any
        journal still open after that blocks: one that entered the commit
        phase, another machine's, or one that cannot be read.
        """
        if machine_id is not None:
            self.close_abandoned(machine_id)
        pending = self.non_terminal()
        if pending:
            blocker = pending[0]
            owner = blocker.machine_id or "not recorded"
            raise RecoveryPendingError(
                f"Recovery is required for an earlier mutation operation ({blocker.operation_id}, "
                f"{blocker.family}, {blocker.state.value}, machine: {owner}). "
                f"Run `recover list` or `recover inspect {blocker.operation_id}`, "
                "then `recover resume` or `recover rollback`.",
                operation_id=blocker.operation_id,
                family=blocker.family,
                state=blocker.state.value,
                machine=blocker.machine_id,
            )
        journal = MutationJournal(
            str(uuid4()),
            family,
            JournalState.PREPARED,
            _now(),
            plan_hash,
            action_count,
            backup_snapshot,
            dict(counts) if counts is not None else None,
            origin,
            machine_id=normalize_machine_id(machine_id) if machine_id is not None else None,
        )
        self.write(journal)
        return journal

    def close_abandoned(self, machine_id: str) -> list[MutationJournal]:
        """Close this machine's journals whose run stopped before the commit phase.

        Such a journal proves nothing was replaced (``BEFORE_COMMIT``), yet it
        blocks every later mutation until a person runs ``recover``. The usual
        cause is not a crash but a journal write refused on the way out: the
        transition that would have closed it failed like the one before it.

        Safe only because of where it is called: the caller holds this
        machine's ``OperationLock``, so no run of this machine is in flight,
        and only journals ``owned_by`` this machine are touched -- another
        machine's may belong to a run that is still going. A journal that has
        entered ``COMMITTING`` is never closed here: what it replaced is a
        person's choice between ``recover resume`` and ``recover rollback``.
        An unreadable journal is left for ``non_terminal`` to refuse.
        """
        if not self.root.is_dir():
            return []
        closed: list[MutationJournal] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                journal = self.load(path.stem)
            except FailSafeError:
                continue
            if (
                journal.operation_id != path.stem
                or journal.state not in BEFORE_COMMIT
                or not owned_by(journal, machine_id)
            ):
                continue
            finished = replace(journal, state=JournalState.FAILED, finished_at_utc=_now(), failure=ABANDONED)
            self.write(finished)
            LOG.warning(
                "closed abandoned mutation journal %s (%s, %s): its run stopped before replacing anything",
                journal.operation_id, journal.family, journal.state.value,
            )
            closed.append(finished)
        return closed

    def write(self, journal: MutationJournal) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{journal.operation_id}.json"
        # A fresh name per write (CS-296). A fixed `<id>.tmp` opened with "x"
        # turned one crash between open and replace into a permanent block:
        # every later transition of that journal -- including the ones
        # `recover` makes to close it -- failed with FileExistsError, and the
        # orphan sweep runs only inside a sync, which that journal blocks.
        temp = self.root / f"{journal.operation_id}.{uuid4().hex}.tmp"
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump({**asdict(journal), "state": journal.state.value}, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def transition(
        self,
        journal: MutationJournal,
        state: JournalState,
        *,
        failure: BaseException | None = None,
    ) -> MutationJournal:
        if state not in _TRANSITIONS[journal.state]:
            raise FailSafeError(f"Invalid mutation journal transition: {journal.state.value} -> {state.value}")
        updated = replace(
            journal,
            state=state,
            finished_at_utc=_now() if state in TERMINAL else journal.finished_at_utc,
            failure=type(failure).__name__ if failure is not None else journal.failure,
        )
        self.write(updated)
        return updated

    def load(self, operation_id: str) -> MutationJournal:
        try:
            path = self.root / f"{operation_id}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            return MutationJournal(
                operation_id=str(raw["operation_id"]),
                family=str(raw["family"]),
                state=JournalState(raw["state"]),
                created_at_utc=str(raw["created_at_utc"]),
                plan_hash=str(raw["plan_hash"]),
                action_count=int(raw["action_count"]),
                backup_snapshot=_optional_str(raw.get("backup_snapshot")),
                counts=_lenient_counts(raw.get("counts")),
                origin=_lenient_str(raw.get("origin")),
                finished_at_utc=_lenient_str(raw.get("finished_at_utc")),
                failure=_lenient_str(raw.get("failure")),
                machine_id=_lenient_str(raw.get("machine_id")),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FailSafeError("Mutation journal is missing or invalid") from exc

    def non_terminal(self) -> list[MutationJournal]:
        if not self.root.is_dir():
            return []
        result: list[MutationJournal] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                journal = self.load(path.stem)
            except FailSafeError:
                raise FailSafeError("A mutation journal cannot be verified")
            if journal.state not in TERMINAL:
                result.append(journal)
        return result


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("backup_snapshot must be a non-empty string when present")
    return value


def _lenient_str(value: object) -> str | None:
    """A descriptive field: a malformed one is dropped, never a reason to block.

    The required fields stay strict because a gate reads them; these only
    feed a history, and a journal that became unreadable over a label would
    block every later mutation for nothing.
    """
    return value if isinstance(value, str) and value else None


def _lenient_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        str(key): item for key, item in value.items()
        if isinstance(item, int) and not isinstance(item, bool)
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
