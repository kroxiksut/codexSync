from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4
import zipfile

from .exceptions import FailSafeError
from .guardian_models import normalize_machine_id
from .mutation_journal import JournalStore, snapshot_belongs_to
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)


class BackupManager:
    def __init__(
        self,
        backup_root: Path,
        machine_id: str | None,
        retention_days: int = 30,
        max_backups: int = 0,
        compression: str = "none",
        journal_root: Path | None = None,
    ) -> None:
        self._backup_root = backup_root
        #: Where this machine's mutation journals live (`paths.temp_dir`): a
        #: snapshot an unfinished journal names is never pruned (CS-294).
        self._journal_root = journal_root
        self._retention_days = retention_days
        self._max_backups = max_backups
        self._compression = compression
        safe_machine = normalize_machine_id(machine_id) or "unknown-machine"
        self._machine_id = machine_id
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        operation_suffix = uuid4().hex[:12]
        self._snapshot_path = (
            self._backup_root / f"{safe_machine}-{ts}-{operation_suffix}.zip"
            if self._compression == "zip"
            else self._backup_root / f"{safe_machine}-{ts}-{operation_suffix}"
        )
        self._zip_entries: set[str] = set()
        self._manifest_entries: list[dict[str, object]] = []

    @property
    def snapshot_name(self) -> str:
        """Name this manager will use if it has to back anything up.

        Recorded in the mutation journal before the first destination is
        touched, so an interrupted operation can be rolled back to the exact
        snapshot it produced.
        """
        return self._snapshot_path.name

    def backup_file(self, file_path: Path, relative_path: str, *, side: str | None = None) -> Path | None:
        """
        Backups existing destination file before overwrite.
        Returns backup file path.

        ``side`` (``local`` or ``cloud``) is recorded in the manifest entry.
        One sync backs up files of both sides into one snapshot under their
        relative paths, and without it a rollback could not tell which root a
        file came from -- it wrote the cloud side's backups into ``.codex``
        (CS-293).
        """
        if not file_path.exists() or not file_path.is_file():
            return None

        if self._compression == "zip":
            result = self._backup_file_zip(file_path, relative_path)
        else:
            backup_path = self._snapshot_path / relative_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = self._deduplicate_path(backup_path)
            shutil.copy2(file_path, backup_path)
            self._record_verified(file_path, relative_path, backup_path)
            result = backup_path
        if side is not None:
            self._manifest_entries[-1]["side"] = side
        return result

    def finalize(self) -> Path | None:
        if not self._manifest_entries:
            return None
        manifest_path = self._snapshot_path.with_name(self._snapshot_path.name + ".manifest.json")
        payload = {
            "format": "codexsync-backup-v1",
            "committed": True,
            "snapshot": self._snapshot_path.name,
            "entries": self._manifest_entries,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temp, manifest_path)
        return manifest_path

    def prune(self) -> None:
        """Remove this machine's snapshots past retention -- and nothing else.

        `backup_dir` is often in the shared cloud folder and has neighbours, so
        only a name this manager writes, for this machine, is a candidate
        (CS-290, CS-294): another machine's snapshot may be the one its own
        interrupted journal needs, and a folder that merely sits here is not a
        snapshot at all. A snapshot an unfinished journal of this machine names
        is kept whatever its age, and when the journals cannot be read nothing
        is pruned -- a late prune costs disk space, an early one a rollback.
        """
        if not self._backup_root.exists():
            return
        protected = self._protected_snapshots()
        if protected is None:
            LOG.warning("backup prune skipped: the mutation journals could not be read")
            return

        snapshots = self._own_snapshots(protected)
        now = time.time()
        if self._retention_days > 0:
            cutoff = now - (self._retention_days * 24 * 60 * 60)
            for path in snapshots:
                if path.stat().st_mtime < cutoff:
                    _remove_snapshot(path)

        if self._max_backups > 0:
            snapshots = self._own_snapshots(protected)
            for stale in snapshots[self._max_backups :]:
                _remove_snapshot(stale)

    def _own_snapshots(self, protected: frozenset[str]) -> list[Path]:
        snapshots = [
            path
            for path in self._backup_root.iterdir()
            if snapshot_belongs_to(path.name, self._machine_id)
            and path.name not in protected
            and (path.is_dir() if path.suffix.lower() != ".zip" else _is_snapshot_zip(path))
        ]
        snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return snapshots

    def _protected_snapshots(self) -> frozenset[str] | None:
        """Snapshot names an unfinished journal needs; ``None`` if unknowable."""
        if self._journal_root is None:
            return frozenset()
        try:
            journals = JournalStore(self._journal_root).non_terminal()
        except (FailSafeError, OSError):
            return None
        return frozenset(item.backup_snapshot for item in journals if item.backup_snapshot)

    def _backup_file_zip(self, file_path: Path, relative_path: str) -> Path:
        snapshot_zip = self._snapshot_path
        snapshot_zip.parent.mkdir(parents=True, exist_ok=True)
        source_hash = _sha256_file(file_path)
        with zipfile.ZipFile(snapshot_zip, mode="a", compression=zipfile.ZIP_DEFLATED) as zf:
            if not self._zip_entries:
                self._zip_entries.update(zf.namelist())
            arcname = relative_path.replace("\\", "/")
            arcname = self._deduplicate_zip_entry(arcname)
            zf.write(file_path, arcname=arcname)
        with zipfile.ZipFile(snapshot_zip, mode="r") as zf:
            if hashlib.sha256(zf.read(arcname)).hexdigest() != source_hash:
                raise OSError("Backup zip entry verification failed")
        self._manifest_entries.append(
            {"relative_path": relative_path.replace("\\", "/"), "sha256": source_hash, "size": file_path.stat().st_size}
        )
        return snapshot_zip

    def _record_verified(self, source: Path, relative_path: str, backup_path: Path) -> None:
        source_hash = _sha256_file(source)
        backup_hash = _sha256_file(backup_path)
        if backup_hash != source_hash:
            raise OSError("Backup verification failed")
        self._manifest_entries.append(
            {"relative_path": relative_path.replace("\\", "/"), "sha256": source_hash, "size": source.stat().st_size}
        )

    def _deduplicate_zip_entry(self, arcname: str) -> str:
        if arcname not in self._zip_entries:
            self._zip_entries.add(arcname)
            return arcname

        candidate = arcname
        stem, dot, suffix = candidate.rpartition(".")
        if not dot:
            stem, suffix = candidate, ""
        idx = 1
        while candidate in self._zip_entries:
            if suffix:
                candidate = f"{stem}.{idx}.{suffix}"
            else:
                candidate = f"{stem}.{idx}"
            idx += 1
        self._zip_entries.add(candidate)
        return candidate

    @staticmethod
    def _deduplicate_path(path: Path) -> Path:
        if not path.exists():
            return path
        idx = 1
        candidate = path
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}.{idx}{path.suffix}")
            idx += 1
        return candidate


def _is_snapshot_zip(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".zip"


def _remove_snapshot(path: Path) -> None:
    manifest = path.with_name(path.name + ".manifest.json")
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        manifest.unlink(missing_ok=True)
        return
    if _is_snapshot_zip(path):
        path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
