"""OS-backed locks for operations that mutate a Codex state root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading

from .exceptions import OperationBusyError

#: Lock files this thread holds with ``reentrant=True``, with their depth.
_HELD = threading.local()


def _held() -> dict[str, int]:
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = {}
        _HELD.paths = held
    return held


class OperationLock:
    """A non-stealable lock; file age is never used to break ownership.

    One lock per (state root, machine) for *every* mutation family (CS-304).
    ``family`` used to be part of the key, which let `chats move` and
    `repair-projects apply` -- or a sync and a restore -- run at the same
    time with only the journal's non-atomic check between them, and the later
    writer silently replaced what the earlier one wrote. ``family`` is kept for
    the refusal message only.

    ``reentrant=True`` lets the *same thread* enter the lock again while the
    outer holder is inside, without taking anything new: `recover rollback`
    holds it across closing a journal and the restore that follows, and that
    restore opens the same lock itself. Without the flag a nested attempt is
    refused like any other, and another thread or process always is.
    """

    def __init__(
        self,
        root: Path,
        *,
        state_root: Path,
        machine_id: str,
        family: str,
        reentrant: bool = False,
    ) -> None:
        canonical_state = str(state_root.resolve()).casefold() if os.name == "nt" else str(state_root.resolve())
        material = "\0".join((canonical_state, machine_id)).encode("utf-8")
        self.path = root / "locks" / f"{hashlib.sha256(material).hexdigest()}.lock"
        self.family = family
        self._reentrant = reentrant
        self._handle = None
        self._nested = False

    def __enter__(self) -> "OperationLock":
        key = str(self.path)
        held = _held()
        if key in held:
            held[key] += 1
            self._nested = True
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Opening is inside the try: a lock file a cloud client holds open is
        # a busy lock (exit 5), not an unexpected error (CS-462).
        try:
            self._handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt

                # `tell()` after `seek(0)` is always 0 in append mode, which
                # grew the file by a byte per acquisition; ask the size.
                if os.fstat(self._handle.fileno()).st_size == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
            self._handle = None
            raise OperationBusyError(
                f"Another mutation operation already owns this Codex state root ({self.family} refused)"
            ) from exc
        if self._reentrant:
            held[key] = 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        key = str(self.path)
        held = _held()
        if self._nested:
            self._nested = False
            held[key] -= 1
            return
        if self._handle is None:
            return
        held.pop(key, None)
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
