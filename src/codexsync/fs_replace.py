"""One atomic replace that waits out a momentary lock on its target.

Every folder codexSync writes into may be watched by a cloud client, a search
indexer or an antivirus, and on Windows a handle any of them holds turns
``os.replace`` into ``WinError 5``/``32``/``33`` for as long as it lives --
usually milliseconds, sometimes a second or two while a cloud client uploads
the file it was just told about. A journal transition failing on that left the
journal open, and an open journal blocks every later mutation until someone
runs ``recover``.

The retry weakens nothing (`D-011`): each attempt is the same atomic replace,
so the target holds either the old bytes or the new ones, never a mix. Only a
transient lock is retried; any other error is raised on the first attempt. A
caller writing where the Codex runtime reads passes ``before_retry`` to re-prove
its safety check before each further attempt, so a Codex that starts during
the wait still stops the write.
"""
from __future__ import annotations

from collections.abc import Callable
import errno
import json
import logging
import os
from pathlib import Path
import secrets
import time

LOG = logging.getLogger(__name__)

#: Attempts in all, the first included. With the backoff below the last one
#: starts about six seconds after the first.
REPLACE_ATTEMPTS = 7
REPLACE_BACKOFF_SECONDS = 0.1
# Windows: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
_TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.ETXTBSY})


def is_transient_lock(exc: OSError) -> bool:
    """True when the target was momentarily held open by another process."""
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return exc.errno in _TRANSIENT_ERRNOS


def replace_with_retry(
    source: Path | str,
    target: Path | str,
    *,
    before_retry: Callable[[], None] | None = None,
) -> None:
    """``os.replace(source, target)``, retried while the target is locked."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if attempt == REPLACE_ATTEMPTS - 1 or not is_transient_lock(exc):
                raise
            LOG.warning(
                "atomic replace blocked by another process (%s); retry %d of %d for %s",
                exc, attempt + 1, REPLACE_ATTEMPTS - 1, target,
            )
            # Looked up at call time, so a test that patches `time.sleep`
            # anywhere silences the wait here too.
            time.sleep(REPLACE_BACKOFF_SECONDS * (2 ** attempt))
            if before_retry is not None:
                before_retry()


def write_json_atomically(path: Path | str, payload: object) -> None:
    """Write a small JSON file of codexSync's own and replace it in one step.

    Staged beside ``path`` under a unique name, so a staging file a killed run
    left behind never blocks the next write, flushed to disk, then put in
    place with :func:`replace_with_retry`. A plain ``write_text`` truncates
    the target first, and a run killed there left a plan or a resolution file
    that every later sync refused to read (CS-517).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.codexsync.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)
