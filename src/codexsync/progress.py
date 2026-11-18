"""How a long read says how far it has got.

A scan of this machine's sessions takes about thirteen seconds -- 270 chats
over 864 MiB -- and until now a window could only spin an indeterminate bar at
it. The cost is entirely in hashing; walking the tree to find the files is
cheap, so the total is known before the expensive part begins and the report
can be a real "120 of 252" rather than a guess.

The contract is three values and no object: a phase id, how many items are
done, and how many there are. It is deliberately the smallest thing that can
cross from the core into a toolkit the core knows nothing about -- the callback
is plain and is called from whatever thread the scan runs on.

`report` swallows whatever the callback raises. A progress bar is not allowed
to be the reason a read fails: the caller asked for a catalogue, and a screen
that throws while drawing a number has not made the catalogue wrong.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Callable

__all__ = [
    "PHASES", "ProgressCallback", "ProgressEstimate", "ProgressEstimator", "ProgressEvent", "report",
]

#: ``(phase, done, total)``. ``total`` may be 0 while it is still unknown.
ProgressCallback = Callable[[str, int, int], None]

#: Every phase id a core scan reports, which is also what a catalogue of
#: labels has to cover. Adding one here without a label makes the i18n test
#: fail, which is the point.
PHASES: tuple[str, ...] = (
    "sessions",
    "sessions_cloud",
    "chats",
    "state_backup",
    # A full sync (`run_handoff`) says which half it is in (CS-339).
    "sync_settings",
    "sync_chats",
    "stage_chats",
    "apply_chats",
    "delete_chat_files",
    "sync_projects",
    "follow_chat_archive",
    "sync_chat_names",
    "refresh_catalogue",
    "check_project_files",
)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One toolkit-neutral progress observation.

    ``total`` is unknown rather than zero while discovery is still in progress.
    Paths and messages are deliberately absent from the first version: callers
    must not accidentally turn a useful counter into a payload or secret log.
    Item metadata is added only together with the redaction boundary in CS-561.
    """

    operation: str
    phase: str
    completed: int
    total: int | None
    bytes_completed: int | None = None
    bytes_total: int | None = None

    def __post_init__(self) -> None:
        if not self.operation or not self.phase:
            raise ValueError("progress operation and phase must not be empty")
        if self.completed < 0:
            raise ValueError("progress completed must not be negative")
        if self.total is not None and (self.total < 0 or self.completed > self.total):
            raise ValueError("progress total must be non-negative and not below completed")
        if self.bytes_completed is not None and self.bytes_completed < 0:
            raise ValueError("progress bytes_completed must not be negative")
        if self.bytes_total is not None:
            if self.bytes_total < 0:
                raise ValueError("progress bytes_total must not be negative")
            if self.bytes_completed is not None and self.bytes_completed > self.bytes_total:
                raise ValueError("progress bytes_total must not be below bytes_completed")


@dataclass(frozen=True, slots=True)
class ProgressEstimate:
    """Rates derived from observations of one operation phase."""

    items_per_second: float | None
    bytes_per_second: float | None
    remaining_seconds: float | None


@dataclass(frozen=True, slots=True)
class _TimedProgress:
    at: float
    completed: int
    bytes_completed: int | None


class ProgressEstimator:
    """Estimate a phase from a bounded window without affecting the work.

    Samples never cross an operation or phase boundary. A decreasing counter
    means a new attempt reused the same name, so its old rate is discarded.
    ``now`` is injectable for deterministic tests; production uses monotonic
    time and is therefore unaffected by wall-clock corrections between PCs.
    """

    def __init__(self, *, window: int = 40) -> None:
        if window < 2:
            raise ValueError("progress estimate window must hold at least two samples")
        self._window = window
        self._samples: dict[tuple[str, str], deque[_TimedProgress]] = {}

    def update(self, event: ProgressEvent, *, now: float | None = None) -> ProgressEstimate:
        at = time.monotonic() if now is None else float(now)
        key = event.operation, event.phase
        samples = self._samples.setdefault(key, deque(maxlen=self._window))
        if samples and (
            event.completed < samples[-1].completed
            or (
                event.bytes_completed is not None
                and samples[-1].bytes_completed is not None
                and event.bytes_completed < samples[-1].bytes_completed
            )
        ):
            samples.clear()
        samples.append(_TimedProgress(at, event.completed, event.bytes_completed))
        if len(samples) < 2:
            return ProgressEstimate(None, None, None)

        first, last = samples[0], samples[-1]
        elapsed = last.at - first.at
        completed = last.completed - first.completed
        item_rate = completed / elapsed if elapsed > 0 and completed > 0 else None
        byte_rate = None
        if (
            elapsed > 0
            and first.bytes_completed is not None
            and last.bytes_completed is not None
            and last.bytes_completed > first.bytes_completed
        ):
            byte_rate = (last.bytes_completed - first.bytes_completed) / elapsed

        remaining = None
        if event.total is not None and item_rate is not None:
            remaining = max(0.0, (event.total - event.completed) / item_rate)
        return ProgressEstimate(item_rate, byte_rate, remaining)


def report(progress: ProgressCallback | None, phase: str, done: int, total: int) -> None:
    """Tell the caller where the read is, and never fail because of it."""
    if progress is None:
        return
    try:
        progress(phase, done, total)
    except Exception:  # noqa: BLE001 - a broken indicator must not break a read
        pass
