from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .exceptions import ConfigError
from .models import FileMeta, ManifestEntry, SnapshotFingerprint, SyncManifest


def load_manifest(path: Path | None, data_version: int) -> SyncManifest:
    if path is None or not path.exists():
        return SyncManifest(data_version=data_version, files={})

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Manifest file is not valid JSON: {path}") from exc

    raw_version = int(raw.get("data_version", data_version))
    if raw_version != data_version:
        raise ConfigError(
            f"Manifest version mismatch: got {raw_version}, expected {data_version}. "
            "Please rotate or migrate manifest first."
        )

    files_raw = raw.get("files", {})
    files: dict[str, ManifestEntry] = {}
    for rel_path, entry in files_raw.items():
        if not isinstance(rel_path, str) or not isinstance(entry, dict):
            continue
        files[rel_path] = ManifestEntry(
            local=_parse_fingerprint(entry.get("local")),
            cloud=_parse_fingerprint(entry.get("cloud")),
        )
    return SyncManifest(data_version=raw_version, files=files)


def save_manifest(manifest: SyncManifest, path: Path | None) -> None:
    if path is None:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "data_version": manifest.data_version,
        "files": {
            rel_path: {
                "local": _fingerprint_to_dict(entry.local),
                "cloud": _fingerprint_to_dict(entry.cloud),
            }
            for rel_path, entry in sorted(manifest.files.items())
        },
    }

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def build_manifest(
    local_index: dict[str, FileMeta],
    cloud_index: dict[str, FileMeta],
    data_version: int,
    previous: SyncManifest | None = None,
    skipped: Iterable[str] = (),
) -> SyncManifest:
    """Record what both sides look like now -- except where nothing was synced.

    A one-way run leaves the other side untouched (`D-012`), and writing its
    current fingerprint would claim the two sides had agreed. The next
    bidirectional run would then see no change on either side and keep the
    older file for good. So a skipped path keeps the entry the previous
    manifest held, and a skipped path that has no previous entry gets none:
    "never synchronised" is the truth, and it is also what makes the next run
    treat it as a first sync rather than as an agreement.
    """
    all_paths = sorted(set(local_index) | set(cloud_index))
    skipped_paths = set(skipped)
    previous_files = previous.files if previous else {}
    files: dict[str, ManifestEntry] = {}

    for rel_path in all_paths:
        if rel_path in skipped_paths:
            carried = previous_files.get(rel_path)
            if carried is not None:
                files[rel_path] = carried
            continue
        local_meta = local_index.get(rel_path)
        cloud_meta = cloud_index.get(rel_path)
        files[rel_path] = ManifestEntry(
            local=fingerprint_from_meta(local_meta) if local_meta else None,
            cloud=fingerprint_from_meta(cloud_meta) if cloud_meta else None,
        )

    return SyncManifest(data_version=data_version, files=files)


def fingerprint_from_meta(meta: FileMeta | None) -> SnapshotFingerprint | None:
    if meta is None:
        return None
    return SnapshotFingerprint(mtime_ns=meta.mtime_ns, size=meta.size)


def _parse_fingerprint(raw: Any) -> SnapshotFingerprint | None:
    if not isinstance(raw, dict):
        return None
    if "mtime_ns" not in raw or "size" not in raw:
        return None
    return SnapshotFingerprint(mtime_ns=int(raw["mtime_ns"]), size=int(raw["size"]))


def _fingerprint_to_dict(value: SnapshotFingerprint | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {"mtime_ns": value.mtime_ns, "size": value.size}
