from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4
import zipfile

from .guardian_models import normalize_machine_id


class BackupManager:
    def __init__(
        self,
        backup_root: Path,
        machine_id: str | None,
        retention_days: int = 30,
        max_backups: int = 0,
        compression: str = "none",
    ) -> None:
        self._backup_root = backup_root
        self._retention_days = retention_days
        self._max_backups = max_backups
        self._compression = compression
        safe_machine = normalize_machine_id(machine_id) or "unknown-machine"
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

    def backup_file(self, file_path: Path, relative_path: str) -> Path | None:
        """
        Backups existing destination file before overwrite.
        Returns backup file path.
        """
        if not file_path.exists() or not file_path.is_file():
            return None

        if self._compression == "zip":
            return self._backup_file_zip(file_path, relative_path)

        backup_path = self._snapshot_path / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self._deduplicate_path(backup_path)
        shutil.copy2(file_path, backup_path)
        self._record_verified(file_path, relative_path, backup_path)
        return backup_path

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
        os.replace(temp, manifest_path)
        return manifest_path

    def prune(self) -> None:
        if not self._backup_root.exists():
            return

        snapshots = [path for path in self._backup_root.iterdir() if path.is_dir() or _is_snapshot_zip(path)]
        snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        now = time.time()
        if self._retention_days > 0:
            cutoff = now - (self._retention_days * 24 * 60 * 60)
            for path in snapshots:
                if path.stat().st_mtime < cutoff:
                    _remove_snapshot(path)

        if self._max_backups > 0:
            snapshots = [path for path in self._backup_root.iterdir() if path.is_dir() or _is_snapshot_zip(path)]
            snapshots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in snapshots[self._max_backups :]:
                _remove_snapshot(stale)

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
