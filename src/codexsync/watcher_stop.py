"""How the handoff watcher is asked to end without ending a write (CS-506).

Removing the watcher's task used to end the running watcher too -- `schtasks
/End`, `systemctl --user stop`, `launchctl bootout` -- because a removed task
leaves its process running (CS-448). But the watcher does a full sync each
time Codex closes, and ending it inside one leaves the journal at
`RECOVERY_REQUIRED`: every later write refuses until `recover`. So nothing
ends it from outside any more. Whoever removes or switches off the task writes
a new token into ``<temp_dir>/handoff-watch/stop-request``; the watcher reads
the token it found at its start and ends, between runs, once the file says
something else. It looks once per poll, so it ends within seconds when idle,
and after the run when busy.

On POSIX a `SIGTERM` (sign-out, `systemctl stop`, `launchctl bootout` by
hand) is taken the same way: noted, and acted on between runs, so a write
that has begun finishes. Windows ends a windowless process with no signal at
all; there the stop request is the only way, which is why the removal never
ends the process.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import threading
import uuid

from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

STOP_DIR = "handoff-watch"
STOP_NAME = "stop-request"

__all__ = ["StopWatch", "WatcherStopping", "request_watcher_stop", "stop_request_path"]


class WatcherStopping(Exception):
    """Raised from a wait inside a run to end it before anything is written."""


def stop_request_path(temp_dir: Path) -> Path:
    return Path(temp_dir) / STOP_DIR / STOP_NAME


def _read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def request_watcher_stop(temp_dir: Path) -> Path:
    """Ask a watcher using ``temp_dir`` to end once it is idle. Returns the file written."""
    path = stop_request_path(temp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.codexsync.tmp")
    try:
        with staged.open("x", encoding="utf-8") as handle:
            handle.write(uuid.uuid4().hex + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    LOG.warning("handoff watcher: asked to end once it is idle (%s)", path)
    return path


class StopWatch:
    """True once the watcher should end: a new stop request, or a SIGTERM."""

    def __init__(self, temp_dir: Path) -> None:
        self._path = stop_request_path(temp_dir)
        self._start = _read_token(self._path)
        self._signalled = False
        self._previous: object = None

    def __call__(self) -> bool:
        if self._signalled:
            return True
        token = _read_token(self._path)
        return token is not None and token != self._start

    def defer_sigterm(self) -> bool:
        """Take SIGTERM as a request to end between runs. Main thread, POSIX only."""
        if os.name == "nt" or threading.current_thread() is not threading.main_thread():
            return False

        def noted(_signum, _frame) -> None:
            if not self._signalled:
                LOG.warning("handoff watcher: SIGTERM received; ending after the current step")
            self._signalled = True

        self._previous = signal.signal(signal.SIGTERM, noted)
        return True

    def restore_sigterm(self) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGTERM, self._previous)
            self._previous = None
