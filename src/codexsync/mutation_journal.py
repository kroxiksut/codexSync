"""Durable, payload-free evidence for an interrupted mutation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
from uuid import uuid4

from .exceptions import FailSafeError


class JournalState(str, Enum):
    PREPARED = "PREPARED"
    BACKED_UP = "BACKED_UP"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


TERMINAL = frozenset({JournalState.COMMITTED, JournalState.FAILED})
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
    ) -> MutationJournal:
        pending = self.non_terminal()
        if pending:
            raise FailSafeError(
                f"Recovery is required for an earlier mutation operation ({pending[0].operation_id}). "
                f"Run `recover inspect {pending[0].operation_id}`, then `recover resume` or `recover rollback`."
            )
        journal = MutationJournal(
            str(uuid4()),
            family,
            JournalState.PREPARED,
            _now(),
            plan_hash,
            action_count,
            backup_snapshot,
        )
        self.write(journal)
        return journal

    def write(self, journal: MutationJournal) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{journal.operation_id}.json"
        temp = path.with_suffix(".tmp")
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({**asdict(journal), "state": journal.state.value}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def transition(self, journal: MutationJournal, state: JournalState) -> MutationJournal:
        if state not in _TRANSITIONS[journal.state]:
            raise FailSafeError(f"Invalid mutation journal transition: {journal.state.value} -> {state.value}")
        updated = MutationJournal(
            journal.operation_id,
            journal.family,
            state,
            journal.created_at_utc,
            journal.plan_hash,
            journal.action_count,
            journal.backup_snapshot,
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
