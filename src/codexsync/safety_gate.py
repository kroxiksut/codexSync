"""Central, command-aware process safety policy.

The gate deliberately owns the decision about whether an operation may mutate
Codex state.  Callers must not turn an ``UNKNOWN`` process result into an
optimistic ``STOPPED`` result, and must not implement their own bypasses.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .exceptions import FailSafeError, SafetyPreconditionError


class OperationKind(str, Enum):
    GUARDIAN_WATCH = "guardian_watch"
    GUARDIAN_SNAPSHOT = "guardian_snapshot"
    DOCTOR = "doctor"
    PLAN = "plan"
    REPAIR_SCAN = "repair_scan"
    SYNC = "sync"
    RESTORE = "restore"
    REPAIR_APPLY = "repair_apply"
    CHAT_MOVE = "chat_move"
    SESSION_SCAN = "session_scan"
    SESSION_APPLY = "session_apply"
    RECOVER_RESUME = "recover_resume"
    RECOVER_ROLLBACK = "recover_rollback"
    GUARDIAN_RESTORE = "guardian_restore"
    PROJECT_MOVE = "project_move"


class ProcessState(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class OperationProfile:
    kind: OperationKind
    requires_stopped: bool
    mutation: bool
    side_effect_free: bool


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    operation: OperationKind
    process_state: ProcessState
    allowed: bool
    reason: str


OPERATION_PROFILES: dict[OperationKind, OperationProfile] = {
    OperationKind.GUARDIAN_WATCH: OperationProfile(OperationKind.GUARDIAN_WATCH, False, False, False),
    OperationKind.GUARDIAN_SNAPSHOT: OperationProfile(OperationKind.GUARDIAN_SNAPSHOT, False, False, False),
    OperationKind.DOCTOR: OperationProfile(OperationKind.DOCTOR, False, False, True),
    OperationKind.PLAN: OperationProfile(OperationKind.PLAN, False, False, True),
    OperationKind.REPAIR_SCAN: OperationProfile(OperationKind.REPAIR_SCAN, False, False, True),
    OperationKind.SYNC: OperationProfile(OperationKind.SYNC, True, True, False),
    OperationKind.RESTORE: OperationProfile(OperationKind.RESTORE, True, True, False),
    OperationKind.REPAIR_APPLY: OperationProfile(OperationKind.REPAIR_APPLY, True, True, False),
    OperationKind.CHAT_MOVE: OperationProfile(OperationKind.CHAT_MOVE, True, True, False),
    OperationKind.SESSION_SCAN: OperationProfile(OperationKind.SESSION_SCAN, False, False, True),
    OperationKind.SESSION_APPLY: OperationProfile(OperationKind.SESSION_APPLY, True, True, False),
    OperationKind.RECOVER_RESUME: OperationProfile(OperationKind.RECOVER_RESUME, True, True, False),
    OperationKind.RECOVER_ROLLBACK: OperationProfile(OperationKind.RECOVER_ROLLBACK, True, True, False),
    OperationKind.GUARDIAN_RESTORE: OperationProfile(OperationKind.GUARDIAN_RESTORE, True, True, False),
    OperationKind.PROJECT_MOVE: OperationProfile(OperationKind.PROJECT_MOVE, True, True, False),
}


class SafetyGate:
    """Evaluate process state and require a continuous stopped window.

    ``sample`` must return the current state or raise when enumeration is
    incomplete/unavailable.  A mutation always fails closed on ``UNKNOWN``.
    ``monotonic`` and ``sleep`` are injected to make the timing contract
    deterministic in tests.

    ``describe`` is optional and names what the last sample actually found. A
    reason that only says "Codex or known background process detected" cannot
    be argued with: it took a live machine to discover that the process being
    found was an always-running Windows service and not Codex at all (CS-264).
    Naming the process turns that from an investigation into a sentence on
    screen. It never influences the decision -- it only reports it.
    """

    def __init__(
        self,
        sample: Callable[[], ProcessState],
        *,
        describe: Callable[[], str] | None = None,
        stable_window_seconds: float = 2.0,
        sample_interval_seconds: float = 0.25,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._sample = sample
        self._describe = describe
        self._stable_window_seconds = max(0.0, stable_window_seconds)
        self._sample_interval_seconds = max(0.01, sample_interval_seconds)
        self._monotonic = monotonic
        self._sleep = sleep

    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        profile = OPERATION_PROFILES[operation]
        state, reason = self._sample_state()
        if not profile.requires_stopped:
            return SafetyDecision(operation, state, True, reason)

        if state is not ProcessState.STOPPED:
            return SafetyDecision(operation, state, False, reason)

        if final:
            return SafetyDecision(operation, ProcessState.STOPPED, True, "final stopped process check passed")

        deadline = self._monotonic() + self._stable_window_seconds
        while self._monotonic() < deadline:
            self._sleep(min(self._sample_interval_seconds, max(0.0, deadline - self._monotonic())))
            state, reason = self._sample_state()
            if state is not ProcessState.STOPPED:
                return SafetyDecision(operation, state, False, reason)
        return SafetyDecision(operation, ProcessState.STOPPED, True, "continuous stopped process window passed")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        decision = self.check(operation, final=final)
        if decision.allowed:
            return decision
        if decision.process_state is ProcessState.RUNNING:
            raise SafetyPreconditionError(f"Codex is running; {operation.value} requires it to be stopped. {decision.reason}")
        raise FailSafeError(f"Cannot safely verify Codex process state for {operation.value}. {decision.reason}")

    def _sample_state(self) -> tuple[ProcessState, str]:
        try:
            state = self._sample()
        except Exception as exc:
            return ProcessState.UNKNOWN, f"process enumeration failed: {exc}"
        if state is ProcessState.STOPPED:
            return state, "no Codex or known background process detected"
        if state is ProcessState.RUNNING:
            return state, f"Codex or known background process detected{self._detail()}"
        return ProcessState.UNKNOWN, "process detector returned an unknown state"

    def _detail(self) -> str:
        """``: name (pid N), …`` for what was found, or nothing.

        A describer that fails must not turn a decided state into an error, so
        anything it raises is dropped: the decision was already made without it.
        """
        if self._describe is None:
            return ""
        try:
            detail = self._describe()
        except Exception:
            return ""
        return f": {detail}" if detail else ""
