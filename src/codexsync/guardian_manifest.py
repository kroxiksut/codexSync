from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .exceptions import GuardianIntegrityError
from .guardian_models import (
    GUARDIAN_MANIFEST_NAME,
    GUARDIAN_STORAGE_VERSION,
    GuardianManifest,
    GuardianSnapshot,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
    require_guardian_machine_id,
)


GUARDIAN_MANIFEST_VERSION = 1
SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_REQUIRED_FIELDS = frozenset(
    {
        "manifest_version",
        "snapshot_id",
        "generation",
        "created_at_utc",
        "machine_id",
        "source_name",
        "source_size",
        "source_mtime_ns_before",
        "source_mtime_ns_after",
        "source_file_id_before",
        "source_file_id_after",
        "hash_algorithm",
        "sha256",
        "validation_status",
        "validation_codes",
        "project_count",
        "binding_count",
        "previous_good_snapshot_id",
        "previous_good_sha256",
        "producer_version",
    }
)


def build_guardian_manifest(
    snapshot: GuardianSnapshot,
    observation: SourceObservation,
    validation: ValidationReport,
    *,
    producer_version: str,
    previous_good: GuardianManifest | None = None,
    created_at: datetime | None = None,
) -> GuardianManifest:
    """Create an in-memory manifest; callers still must verify it after writing."""
    if not observation.is_stable:
        raise GuardianIntegrityError("Guardian manifest requires a stable source observation")
    if observation.source_size_before is None or observation.source_size_after is None:
        raise GuardianIntegrityError("Guardian manifest requires source sizes before and after reading")
    if observation.source_mtime_ns_before is None or observation.source_mtime_ns_after is None:
        raise GuardianIntegrityError("Guardian manifest requires source mtimes before and after reading")
    if snapshot.generation < 1:
        raise GuardianIntegrityError("Guardian snapshot generation must be >= 1")
    if previous_good is None:
        if snapshot.generation != 1:
            raise GuardianIntegrityError("A generation greater than 1 requires a previous good snapshot")
        previous_id = None
        previous_sha256 = None
    else:
        if previous_good.generation + 1 != snapshot.generation:
            raise GuardianIntegrityError("Guardian generation must advance exactly by one")
        previous_id = previous_good.snapshot_id
        previous_sha256 = previous_good.sha256

    created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    manifest = GuardianManifest(
        manifest_version=GUARDIAN_MANIFEST_VERSION,
        snapshot_id=snapshot.snapshot_id,
        generation=snapshot.generation,
        created_at_utc=_format_utc(created),
        machine_id=require_guardian_machine_id(snapshot.machine_id),
        source_name=observation.source_name,
        source_size=len(observation.payload),
        source_mtime_ns_before=observation.source_mtime_ns_before,
        source_mtime_ns_after=observation.source_mtime_ns_after,
        source_file_id_before=observation.source_file_id_before,
        source_file_id_after=observation.source_file_id_after,
        hash_algorithm="sha256",
        sha256=hashlib.sha256(observation.payload).hexdigest(),
        validation_status=validation.status,
        validation_codes=tuple(validation.codes),
        project_count=_require_count(validation.project_count, "project_count"),
        binding_count=_require_count(validation.binding_count, "binding_count"),
        previous_good_snapshot_id=previous_id,
        previous_good_sha256=previous_sha256,
        producer_version=producer_version,
        schema_id=validation.schema_id,
    )
    validate_guardian_manifest(manifest, predecessor=previous_good)
    return manifest


def serialize_guardian_manifest(manifest: GuardianManifest) -> bytes:
    """Return deterministic UTF-8/LF manifest bytes without a BOM."""
    validate_guardian_manifest(manifest)
    encoded = (json.dumps(_manifest_to_dict(manifest), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("Guardian manifest serialization must not use a BOM")
    return encoded


def load_guardian_manifest(path: Path) -> GuardianManifest:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise GuardianIntegrityError(f"Cannot read Guardian manifest: {path}") from exc
    return parse_guardian_manifest(raw_bytes)


def parse_guardian_manifest(raw_bytes: bytes) -> GuardianManifest:
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raise GuardianIntegrityError("Guardian manifest must be UTF-8 without BOM")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardianIntegrityError("Guardian manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise GuardianIntegrityError("Guardian manifest root must be an object")
    missing = _REQUIRED_FIELDS.difference(raw)
    if missing:
        raise GuardianIntegrityError(f"Guardian manifest is missing required fields: {', '.join(sorted(missing))}")
    manifest = GuardianManifest(
        manifest_version=_require_int(raw["manifest_version"], "manifest_version", minimum=1),
        snapshot_id=_require_string(raw["snapshot_id"], "snapshot_id"),
        generation=_require_int(raw["generation"], "generation", minimum=1),
        created_at_utc=_require_string(raw["created_at_utc"], "created_at_utc"),
        machine_id=_require_string(raw["machine_id"], "machine_id"),
        source_name=_require_string(raw["source_name"], "source_name"),
        source_size=_require_int(raw["source_size"], "source_size", minimum=0),
        source_mtime_ns_before=_require_int(raw["source_mtime_ns_before"], "source_mtime_ns_before", minimum=0),
        source_mtime_ns_after=_require_int(raw["source_mtime_ns_after"], "source_mtime_ns_after", minimum=0),
        source_file_id_before=_require_optional_string(raw["source_file_id_before"], "source_file_id_before"),
        source_file_id_after=_require_optional_string(raw["source_file_id_after"], "source_file_id_after"),
        hash_algorithm=_require_string(raw["hash_algorithm"], "hash_algorithm"),
        sha256=_require_string(raw["sha256"], "sha256").lower(),
        validation_status=_parse_validation_status(raw["validation_status"]),
        validation_codes=_parse_codes(raw["validation_codes"]),
        project_count=_require_int(raw["project_count"], "project_count", minimum=0),
        binding_count=_require_int(raw["binding_count"], "binding_count", minimum=0),
        previous_good_snapshot_id=_require_optional_string(
            raw["previous_good_snapshot_id"], "previous_good_snapshot_id"
        ),
        previous_good_sha256=_require_optional_string(raw["previous_good_sha256"], "previous_good_sha256"),
        producer_version=_require_string(raw["producer_version"], "producer_version"),
        schema_id=_parse_optional_schema_id(raw.get("schema_id")),
    )
    validate_guardian_manifest(manifest)
    return manifest


def verify_guardian_snapshot(
    snapshot: GuardianSnapshot,
    *,
    predecessor: GuardianManifest | None = None,
    known_generations: Iterable[int] = (),
) -> GuardianManifest:
    """Verify a standalone snapshot without reading the original Codex state."""
    manifest = load_guardian_manifest(snapshot.manifest_path)
    if snapshot.manifest_path.name != GUARDIAN_MANIFEST_NAME:
        raise GuardianIntegrityError("Guardian snapshot manifest has an unexpected filename")
    if snapshot.directory.name != manifest.snapshot_id or snapshot.snapshot_id != manifest.snapshot_id:
        raise GuardianIntegrityError("Guardian snapshot directory does not match manifest snapshot_id")
    if snapshot.directory.parent.name != manifest.machine_id or snapshot.machine_id != manifest.machine_id:
        raise GuardianIntegrityError("Guardian snapshot directory does not match manifest machine_id")
    if snapshot.generation != manifest.generation:
        raise GuardianIntegrityError("Guardian snapshot generation does not match manifest")
    try:
        payload = snapshot.payload_path.read_bytes()
    except OSError as exc:
        raise GuardianIntegrityError("Guardian snapshot payload is unavailable") from exc
    if len(payload) != manifest.source_size:
        raise GuardianIntegrityError("Guardian snapshot payload size does not match manifest")
    if hashlib.sha256(payload).hexdigest() != manifest.sha256:
        raise GuardianIntegrityError("Guardian snapshot payload SHA-256 does not match manifest")
    validate_guardian_manifest(manifest, predecessor=predecessor, known_generations=known_generations)
    return manifest


def validate_guardian_manifest(
    manifest: GuardianManifest,
    *,
    predecessor: GuardianManifest | None = None,
    known_generations: Iterable[int] = (),
) -> None:
    if manifest.manifest_version != GUARDIAN_MANIFEST_VERSION:
        raise GuardianIntegrityError(
            f"Unsupported Guardian manifest version: {manifest.manifest_version} (expected {GUARDIAN_MANIFEST_VERSION})"
        )
    if GUARDIAN_STORAGE_VERSION != GUARDIAN_MANIFEST_VERSION:
        raise GuardianIntegrityError("Guardian storage and manifest versions are inconsistent")
    _validate_snapshot_id(manifest.snapshot_id)
    if require_guardian_machine_id(manifest.machine_id) != manifest.machine_id:
        raise GuardianIntegrityError("Guardian manifest machine_id is not normalized")
    _validate_logical_source_name(manifest.source_name)
    _parse_utc(manifest.created_at_utc)
    if manifest.hash_algorithm != "sha256" or not SHA256_HEX_RE.fullmatch(manifest.sha256):
        raise GuardianIntegrityError("Guardian manifest hash must be SHA-256")
    if manifest.source_size < 0 or manifest.source_mtime_ns_before < 0 or manifest.source_mtime_ns_after < 0:
        raise GuardianIntegrityError("Guardian manifest source metadata cannot be negative")
    if manifest.project_count < 0 or manifest.binding_count < 0:
        raise GuardianIntegrityError("Guardian manifest counts cannot be negative")
    if not manifest.producer_version.strip():
        raise GuardianIntegrityError("Guardian manifest producer_version must not be empty")
    _parse_optional_schema_id(manifest.schema_id)
    if manifest.generation in set(known_generations):
        raise GuardianIntegrityError("Guardian manifest generation is already in use")
    _validate_predecessor(manifest, predecessor)


def _validate_predecessor(manifest: GuardianManifest, predecessor: GuardianManifest | None) -> None:
    previous_id = manifest.previous_good_snapshot_id
    previous_sha = manifest.previous_good_sha256
    if (previous_id is None) != (previous_sha is None):
        raise GuardianIntegrityError("Guardian manifest predecessor id and hash must be set together")
    if manifest.generation == 1:
        if previous_id is not None:
            raise GuardianIntegrityError("First Guardian snapshot must not declare a predecessor")
        if predecessor is not None:
            raise GuardianIntegrityError("First Guardian snapshot cannot have a predecessor")
        return
    if previous_id is None:
        raise GuardianIntegrityError("Guardian snapshot generation > 1 requires a predecessor")
    _validate_snapshot_id(previous_id)
    if previous_sha is None or not SHA256_HEX_RE.fullmatch(previous_sha):
        raise GuardianIntegrityError("Guardian manifest predecessor SHA-256 is invalid")
    if predecessor is not None:
        validate_guardian_manifest(predecessor)
        if predecessor.generation + 1 != manifest.generation:
            raise GuardianIntegrityError("Guardian manifest predecessor generation is inconsistent")
        if predecessor.snapshot_id != previous_id or predecessor.sha256 != previous_sha:
            raise GuardianIntegrityError("Guardian manifest predecessor does not match declared previous good snapshot")


def _manifest_to_dict(manifest: GuardianManifest) -> dict[str, Any]:
    return {
        "manifest_version": manifest.manifest_version,
        "snapshot_id": manifest.snapshot_id,
        "generation": manifest.generation,
        "created_at_utc": manifest.created_at_utc,
        "machine_id": manifest.machine_id,
        "source_name": manifest.source_name,
        "source_size": manifest.source_size,
        "source_mtime_ns_before": manifest.source_mtime_ns_before,
        "source_mtime_ns_after": manifest.source_mtime_ns_after,
        "source_file_id_before": manifest.source_file_id_before,
        "source_file_id_after": manifest.source_file_id_after,
        "hash_algorithm": manifest.hash_algorithm,
        "sha256": manifest.sha256,
        "validation_status": manifest.validation_status.value,
        "validation_codes": list(manifest.validation_codes),
        "project_count": manifest.project_count,
        "binding_count": manifest.binding_count,
        "previous_good_snapshot_id": manifest.previous_good_snapshot_id,
        "previous_good_sha256": manifest.previous_good_sha256,
        "producer_version": manifest.producer_version,
        **({"schema_id": manifest.schema_id} if manifest.schema_id is not None else {}),
    }


def _require_int(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GuardianIntegrityError(f"Guardian manifest field {field_name} must be an integer >= {minimum}")
    return value


def _require_count(value: int | None, field_name: str) -> int:
    if value is None:
        raise GuardianIntegrityError(f"Guardian validation report must include {field_name}")
    return _require_int(value, field_name, minimum=0)


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GuardianIntegrityError(f"Guardian manifest field {field_name} must be a non-empty string")
    return value


def _require_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _parse_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GuardianIntegrityError("Guardian manifest validation_codes must be a list of non-empty strings")
    return tuple(value)


def _parse_validation_status(value: Any) -> ValidationStatus:
    if not isinstance(value, str):
        raise GuardianIntegrityError("Guardian manifest validation_status must be a string")
    try:
        return ValidationStatus(value)
    except ValueError as exc:
        raise GuardianIntegrityError("Guardian manifest validation_status is unsupported") from exc


def _parse_optional_schema_id(value: Any) -> str | None:
    if value is None:
        return None
    schema_id = _require_string(value, "schema_id")
    if "/" in schema_id or "\\" in schema_id:
        raise GuardianIntegrityError("Guardian manifest schema_id is unsafe")
    return schema_id


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id in {".", ".."}:
        raise GuardianIntegrityError("Guardian snapshot_id is unsafe")


def _validate_logical_source_name(source_name: str) -> None:
    if not source_name or "/" in source_name or "\\" in source_name or Path(source_name).is_absolute():
        raise GuardianIntegrityError("Guardian manifest source_name must be a logical filename")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> None:
    if not value.endswith("Z"):
        raise GuardianIntegrityError("Guardian manifest created_at_utc must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GuardianIntegrityError("Guardian manifest created_at_utc is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise GuardianIntegrityError("Guardian manifest created_at_utc must use UTC")
