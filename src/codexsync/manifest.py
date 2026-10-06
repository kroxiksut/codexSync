"""The sync manifest: what each machine saw on both sides at its last sync.

The file sits beside the cloud mirror, so every machine reads and writes the
same one. It therefore keeps one baseline per machine (CS-288). A single
shared pair was the bug: machine A syncs, the manifest records A's local
fingerprint, and machine B -- whose own copy is older -- compares its file with
*A's* fingerprint, sees it "changed locally", and copies it over A's newer one
in the cloud.

A baseline is the pair (local, cloud) as one machine saw it, and both halves
are per machine: "has the cloud copy changed since I last looked" is a
question only the machine that looked can answer, and it also holds when a
cloud client does not carry modification times across machines exactly.

The format before CS-288 had one unkeyed ``files`` table. Nothing in it says
whose local side it describes, so it is never attributed to anyone: a machine
without its own baseline plans a first sync -- newer file wins, a file on one
side only is copied, never deleted -- and records its own baseline from then on.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .exceptions import ConfigError
from .guardian_models import normalize_machine_id
from .models import FileMeta, ManifestEntry, SnapshotFingerprint, SyncManifest
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

#: Written into every manifest this version saves.
MANIFEST_FORMAT = "codexsync-sync-manifest-v2"


def manifest_machine_key(machine_id: str | None) -> str:
    """The key a machine's baseline is stored under: the backup-safe id."""
    return normalize_machine_id(machine_id) or "unknown-machine"


def load_manifest(path: Path | None, data_version: int, *, machine_id: str | None) -> SyncManifest:
    """Read the manifest and return ``machine_id``'s baseline.

    Every other machine's baseline is kept in ``others`` so that saving this
    machine's result does not drop theirs. A structurally broken file is a
    ``ConfigError`` (exit 4): guessing what it meant is how a sync overwrites
    the wrong side. A malformed *entry* is dropped, which only makes that path
    look never synchronised -- the conservative reading.
    """
    key = manifest_machine_key(machine_id)
    if path is None or not path.exists():
        return SyncManifest(data_version=data_version, files={}, machine_id=key)

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: Any = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Manifest file is not valid JSON: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Manifest file cannot be read: {path}. {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Manifest file is not a JSON object: {path}")

    raw_version = raw.get("data_version", data_version)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigError(f"Manifest data_version is not an integer: {path}")
    if raw_version != data_version:
        raise ConfigError(
            f"Manifest version mismatch: got {raw_version}, expected {data_version}. "
            "Please rotate or migrate manifest first."
        )

    machines_raw = raw.get("machines")
    if machines_raw is None:
        if raw.get("files"):
            # The pre-CS-288 shape. Whose local side it recorded is unknown,
            # and attributing it to this machine is exactly the bug.
            LOG.warning(
                "manifest %s has no per-machine baselines (written before 0.2); "
                "this run plans a first sync and records this machine's own baseline",
                path,
            )
        machines_raw = {}
    if not isinstance(machines_raw, dict):
        raise ConfigError(f"Manifest machines is not a JSON object: {path}")

    machines: dict[str, dict[str, ManifestEntry]] = {}
    for machine, block in machines_raw.items():
        if not isinstance(machine, str) or not isinstance(block, dict):
            continue
        machines[machine] = _parse_files(block.get("files"))
    own = machines.pop(key, {})
    return SyncManifest(data_version=raw_version, files=own, machine_id=key, others=machines)


def save_manifest(manifest: SyncManifest, path: Path | None) -> None:
    if path is None:
        return
    if manifest.machine_id is None and manifest.files:
        raise ValueError("A manifest with entries must name the machine they belong to")

    machines: dict[str, dict[str, ManifestEntry]] = dict(manifest.others)
    if manifest.machine_id is not None:
        machines[manifest.machine_id] = manifest.files
    payload = {
        "format": MANIFEST_FORMAT,
        "data_version": manifest.data_version,
        "machines": {
            machine: {
                "files": {
                    rel_path: {
                        "local": _fingerprint_to_dict(entry.local),
                        "cloud": _fingerprint_to_dict(entry.cloud),
                    }
                    for rel_path, entry in sorted(files.items())
                }
            }
            for machine, files in sorted(machines.items())
        },
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp_path, path)


def build_manifest(
    local_index: dict[str, FileMeta],
    cloud_index: dict[str, FileMeta],
    data_version: int,
    previous: SyncManifest | None = None,
    skipped: Iterable[str] = (),
    *,
    machine_id: str | None = None,
) -> SyncManifest:
    """Record what both sides look like now -- except where nothing was synced.

    A one-way run leaves the other side untouched (`D-012`), and writing its
    current fingerprint would claim the two sides had agreed. The next
    bidirectional run would then see no change on either side and keep the
    older file for good. So a skipped path keeps the entry the previous
    manifest held, and a skipped path that has no previous entry gets none:
    "never synchronised" is the truth, and it is also what makes the next run
    treat it as a first sync rather than as an agreement.

    Only this machine's baseline is rebuilt; ``previous.others`` -- every other
    machine's -- is carried over untouched.
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

    owner = (
        manifest_machine_key(machine_id)
        if machine_id is not None
        else (previous.machine_id if previous else None)
    )
    return SyncManifest(
        data_version=data_version,
        files=files,
        machine_id=owner,
        others=dict(previous.others) if previous else {},
    )


def fingerprint_from_meta(meta: FileMeta | None) -> SnapshotFingerprint | None:
    if meta is None:
        return None
    return SnapshotFingerprint(mtime_ns=meta.mtime_ns, size=meta.size)


#: Marks a fingerprint that is present but unreadable, as opposed to ``None``.
_MALFORMED = object()


def _parse_files(raw: Any) -> dict[str, ManifestEntry]:
    if not isinstance(raw, dict):
        return {}
    files: dict[str, ManifestEntry] = {}
    for rel_path, entry in raw.items():
        if not isinstance(rel_path, str) or not isinstance(entry, dict):
            continue
        local = _parse_fingerprint(entry.get("local"))
        cloud = _parse_fingerprint(entry.get("cloud"))
        if local is _MALFORMED or cloud is _MALFORMED:
            # Half an entry would read as "this side changed" and pick a
            # direction; no entry reads as never synchronised, which is safe.
            continue
        files[rel_path] = ManifestEntry(local=local, cloud=cloud)  # type: ignore[arg-type]
    return files


def _parse_fingerprint(raw: Any) -> SnapshotFingerprint | None | object:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return _MALFORMED
    mtime_ns, size = raw.get("mtime_ns"), raw.get("size")
    for value in (mtime_ns, size):
        if isinstance(value, bool) or not isinstance(value, int):
            return _MALFORMED
    return SnapshotFingerprint(mtime_ns=mtime_ns, size=size)


def _fingerprint_to_dict(value: SnapshotFingerprint | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {"mtime_ns": value.mtime_ns, "size": value.size}
