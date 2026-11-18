"""Cooperative stop requests for one operation, shared by every caller and worker."""
from enum import Enum
from threading import RLock

from .exceptions import FailSafeError


class OperationCancelled(FailSafeError):
    code = "OPERATION_CANCELLED"


class OperationRolledBack(FailSafeError):
    code = "OPERATION_ROLLED_BACK"


class RollbackRequested(Exception):
    """Internal control transfer after an atomic operation finished."""


class CancellationState(str, Enum):
    PREPARING = "preparing"
    STOPPING = "stopping"
    COMMITTING = "committing"
    ROLLING_BACK = "rolling_back"
    FINISHED = "finished"


class CancellationToken:
    def __init__(self, *, allow_rollback: bool = True) -> None:
        self._lock = RLock()
        self._state = CancellationState.PREPARING
        self._allow_rollback = allow_rollback
        self._signal_stopped = False

    def signal_stop(self) -> None:
        """A Python signal handler may interrupt even the commit transition."""
        if self._state == CancellationState.PREPARING:
            self._signal_stopped = True

    @property
    def can_rollback(self) -> bool:
        with self._lock:
            return self._allow_rollback and self._state == CancellationState.COMMITTING

    @property
    def state(self) -> CancellationState:
        with self._lock:
            if self._signal_stopped and self._state == CancellationState.PREPARING:
                self._state = CancellationState.STOPPING
            return self._state

    def request_cancel(self) -> bool:
        with self._lock:
            if self._state != CancellationState.PREPARING:
                return False
            self._state = CancellationState.STOPPING
            return True

    def request_rollback(self) -> bool:
        with self._lock:
            if self._state != CancellationState.COMMITTING or not self._allow_rollback:
                return False
            self._state = CancellationState.ROLLING_BACK
            return True

    def checkpoint(self) -> None:
        state = self.state
        if state == CancellationState.STOPPING:
            raise OperationCancelled("Stopped by the user before any destination changed.")
        if state == CancellationState.ROLLING_BACK:
            raise RollbackRequested()

    def begin_commit(self) -> None:
        with self._lock:
            self.checkpoint()
            if self._state != CancellationState.PREPARING:
                raise RuntimeError("a cancellation token belongs to one operation")
            self._state = CancellationState.COMMITTING
            if self._signal_stopped:
                self._state = CancellationState.STOPPING
                self.checkpoint()

    def complete_commit(self, *, finish: bool = True) -> None:
        with self._lock:
            self.checkpoint()
            self._allow_rollback = False
            if finish:
                self._state = CancellationState.FINISHED

    def finish(self) -> CancellationState:
        with self._lock:
            previous = self._state
            self._state = CancellationState.FINISHED
            return previous
