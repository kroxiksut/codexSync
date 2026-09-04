"""OS-backed locks for operations that mutate a Codex state root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .exceptions import OperationBusyError


class OperationLock:
    """A non-stealable lock; file age is never used to break ownership."""

    def __init__(self, root: Path, *, state_root: Path, machine_id: str, family: str) -> None:
        canonical_state = str(state_root.resolve()).casefold() if os.name == "nt" else str(state_root.resolve())
        material = "\0".join((canonical_state, machine_id, family)).encode("utf-8")
        self.path = root / "locks" / f"{hashlib.sha256(material).hexdigest()}.lock"
        self._handle = None

    def __enter__(self) -> "OperationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                if self._handle.tell() == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise OperationBusyError("Another mutation operation already owns this Codex state root") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
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
