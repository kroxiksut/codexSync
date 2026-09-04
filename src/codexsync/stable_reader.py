"""Read the Codex global-state file without placing a lock on it."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Callable

from .guardian_models import SourceObservation


class SourceMissingError(FileNotFoundError):
    pass


class SourceUnstableError(RuntimeError):
    pass


class SourceTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceSignature:
    exists: bool
    size: int | None
    mtime_ns: int | None
    file_id: str | None


@dataclass(frozen=True, slots=True)
class ReadSample:
    observation: SourceObservation
    sha256: str


class StableReader:
    """Performs bounded, repeatable read-only samples of one JSON source."""

    def __init__(self, path: Path, *, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def signature(self) -> SourceSignature:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return SourceSignature(False, None, None, None)
        except OSError as exc:
            raise SourceUnstableError("Cannot inspect Guardian source metadata") from exc
        return SourceSignature(True, stat.st_size, stat.st_mtime_ns, _file_id(stat))

    def read_once(self) -> ReadSample:
        before = self.signature()
        if not before.exists:
            raise SourceMissingError(self.path.name)
        if before.size is not None and before.size > self.max_bytes:
            raise SourceTooLargeError("Guardian source exceeds configured size limit")
        try:
            # No write mode and no advisory lock: Codex remains free to update.
            with self.path.open("rb") as handle:
                payload = handle.read(self.max_bytes + 1)
        except FileNotFoundError as exc:
            raise SourceMissingError(self.path.name) from exc
        except OSError as exc:
            raise SourceUnstableError("Cannot read Guardian source") from exc
        if len(payload) > self.max_bytes:
            raise SourceTooLargeError("Guardian source exceeds configured size limit")
        after = self.signature()
        observation = SourceObservation(
            payload=payload,
            source_name=self.path.name,
            source_size_before=before.size,
            source_size_after=after.size,
            source_mtime_ns_before=before.mtime_ns,
            source_mtime_ns_after=after.mtime_ns,
            source_file_id_before=before.file_id,
            source_file_id_after=after.file_id,
            is_stable=before == after,
        )
        if not observation.is_stable:
            raise SourceUnstableError("Guardian source changed while being read")
        return ReadSample(observation=observation, sha256=hashlib.sha256(payload).hexdigest())

    def read_stable(
        self,
        *,
        reads: int,
        interval_seconds: float,
        sleep: Callable[[float], None],
    ) -> SourceObservation:
        if reads < 3:
            raise ValueError("stable reads must be at least three")
        samples: list[ReadSample] = []
        for number in range(reads):
            sample = self.read_once()
            samples.append(sample)
            if number + 1 < reads:
                sleep(interval_seconds)
        first = samples[0]
        if any(
            sample.sha256 != first.sha256
            or sample.observation.source_size_before != first.observation.source_size_before
            or sample.observation.source_mtime_ns_before != first.observation.source_mtime_ns_before
            or sample.observation.source_file_id_before != first.observation.source_file_id_before
            for sample in samples[1:]
        ):
            raise SourceUnstableError("Guardian source changed across stable reads")
        return samples[-1].observation


def _file_id(stat: os.stat_result) -> str | None:
    # st_ino can be zero on some Windows filesystems; expose identity only when
    # the platform supplied one, never synthesize a misleading replacement.
    inode = getattr(stat, "st_ino", 0)
    device = getattr(stat, "st_dev", 0)
    if not inode:
        return None
    return f"{device:x}:{inode:x}"
