"""An append-only, operation-owned JSONL stream outside Codex state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import BinaryIO, Iterator
from uuid import uuid4

from .config import codex_folder_holding
from .cancellation import OperationCancelled, OperationRolledBack
from .fs_replace import replace_with_retry
from .progress import PHASES, ProgressEvent, ProgressEventCallback

_BATCH_BYTES = 64 * 1024
_OPERATIONS = frozenset({"sync", "sessions"})


def progress_log_dir(temp_dir: Path, state_dir: Path) -> Path | None:
    folder = temp_dir / "progress"
    try:
        resolved = folder.resolve(strict=False)
        # A component may have been replaced by a junction or symlink since
        # the config was loaded. Resolve the owned folder before writing.
        if resolved != temp_dir.resolve(strict=False) / "progress":
            return None
        if codex_folder_holding(resolved, state_dir) is None:
            return resolved
    except (OSError, RuntimeError):
        pass
    return None


class ProgressLog:
    """Deliver events to an observer and durably publish one log per run.

    Each operation exclusively creates its own file. Flush bounded batches
    there, retaining complete records if a process stops before publication.
    Consumers may open either filename and disregard an incomplete tail.
    """

    def __init__(self, folder: Path | None, operation: str, observer: ProgressEventCallback | None = None) -> None:
        self.folder = folder
        self.operation = operation
        self.observer = observer
        self.started_at = time.time()
        self._file: BinaryIO | None = None
        self._partial: Path | None = None
        self._complete: Path | None = None
        self._phase: str | None = None
        self._write_ok = True
        self.recovery_required = False

    def __enter__(self) -> ProgressLog:
        if self.folder is not None and self.operation in _OPERATIONS:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                stem = f"{self.operation}-{uuid4().hex}"
                self._partial = self.folder / f"{stem}.partial.jsonl"
                self._complete = self.folder / f"{stem}.complete.jsonl"
                self._file = self._partial.open("xb", buffering=_BATCH_BYTES)
            except OSError:
                pass
        return self

    def report(self, phase: str, completed: int, total: int | None, **metadata) -> None:
        if self.operation not in _OPERATIONS or phase not in PHASES:
            return
        try:
            event = ProgressEvent(
                self.operation, phase, completed, total, **metadata,
                started_at=self.started_at, at=time.time(), monotonic_at=time.monotonic(),
            )
        except (TypeError, ValueError):
            return
        if self._file is not None and self._write_ok:
            try:
                self._file.write(progress_record(event).encode("ascii") + b"\n")
                if phase != self._phase:
                    self._file.flush()
                    self._phase = phase
            except (OSError, ValueError):
                self._write_ok = False
        if self.observer is not None:
            try:
                self.observer(event)
            except Exception:
                pass

    def __exit__(self, exc_type, value, _traceback) -> None:
        status = "failed" if exc_type else "completed"
        if self.recovery_required:
            status = "recovery_required"
        elif isinstance(value, OperationCancelled):
            status = "cancelled"
        elif isinstance(value, OperationRolledBack):
            status = "rolled_back"
        self.report("transfer", 1, 1, status=status)
        if self._file is None:
            return
        try:
            with self._file as handle:
                handle.flush()
                os.fsync(handle.fileno())
            if self._write_ok:
                assert self._partial is not None and self._complete is not None
                replace_with_retry(self._partial, self._complete)
        except OSError:
            pass


def progress_record(event: ProgressEvent) -> str:
    return json.dumps({
        "version": 1,
        "operation": event.operation,
        "phase": event.phase,
        "completed": event.completed,
        "total": event.total,
        "bytes_completed": event.bytes_completed,
        "bytes_total": event.bytes_total,
        "item_bytes": event.item_bytes,
        "item_id": event.item_id,
        "relative_path": event.relative_path,
        "direction": event.direction,
        "status": event.status,
        "started_at": event.started_at,
        "at": event.at,
    }, allow_nan=False)


def iter_progress_records(path: Path) -> Iterator[dict]:
    """Read complete records from a published or interrupted operation."""
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            yield json.loads(line)
