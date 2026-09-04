from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import BinaryIO
from uuid import uuid4

from .exceptions import GuardianBusyError
from .guardian_models import GUARDIAN_LOCKS_DIR_NAME, require_guardian_machine_id


class GuardianWriterLock(AbstractContextManager["GuardianWriterLock"]):
    """Non-blocking per-machine OS lock; lock-file timestamps never break locks."""

    def __init__(self, root_dir: Path, machine_id: str, *, operation_id: str | None = None) -> None:
        self._root_dir = root_dir
        self.machine_id = require_guardian_machine_id(machine_id)
        self.operation_id = operation_id or str(uuid4())
        self.path = root_dir / GUARDIAN_LOCKS_DIR_NAME / f"{self.machine_id}.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> "GuardianWriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            _lock_nonblocking(handle)
        except OSError as exc:
            handle.close()
            raise GuardianBusyError(f"Guardian writer lock is busy for machine {self.machine_id}") from exc
        self._handle = handle
        self._write_metadata()
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "GuardianWriterLock":
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.release()

    def _write_metadata(self) -> None:
        assert self._handle is not None
        metadata = {
            "machine_id": self.machine_id,
            "operation_id": self.operation_id,
            "pid": os.getpid(),
            "acquired_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        encoded = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(encoded)
        self._handle.flush()
        os.fsync(self._handle.fileno())


class GuardianRunnerLock(GuardianWriterLock):
    """Long-lived watcher lock, deliberately independent from short writer locks."""

    def __init__(self, root_dir: Path, machine_id: str, *, operation_id: str | None = None) -> None:
        super().__init__(root_dir, machine_id, operation_id=operation_id)
        self.path = root_dir / GUARDIAN_LOCKS_DIR_NAME / f"{self.machine_id}.runner.lock"


def _lock_nonblocking(handle: BinaryIO) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
