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

from typing import Callable

__all__ = ["PHASES", "ProgressCallback", "report"]

#: ``(phase, done, total)``. ``total`` may be 0 while it is still unknown.
ProgressCallback = Callable[[str, int, int], None]

#: Every phase id a core scan reports, which is also what a catalogue of
#: labels has to cover. Adding one here without a label makes the i18n test
#: fail, which is the point.
PHASES: tuple[str, ...] = (
    "sessions",
    "sessions_cloud",
    "chats",
)


def report(progress: ProgressCallback | None, phase: str, done: int, total: int) -> None:
    """Tell the caller where the read is, and never fail because of it."""
    if progress is None:
        return
    try:
        progress(phase, done, total)
    except Exception:  # noqa: BLE001 - a broken indicator must not break a read
        pass
