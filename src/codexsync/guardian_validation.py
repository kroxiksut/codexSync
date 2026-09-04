from __future__ import annotations

import json
from typing import Any

from .guardian_models import SourceObservation, ValidationReport, ValidationStatus


DEFAULT_MAX_STATE_BYTES = 64 * 1024 * 1024
MIN_MAX_STATE_BYTES = 1 * 1024 * 1024
MAX_MAX_STATE_BYTES = 1 * 1024 * 1024 * 1024

NUL_BYTE = "NUL_BYTE"
UNSUPPORTED_BOM = "UNSUPPORTED_BOM"
UTF8_BOM = "UTF8_BOM"
INVALID_UTF8 = "INVALID_UTF8"
INVALID_JSON = "INVALID_JSON"
DUPLICATE_JSON_KEY = "DUPLICATE_JSON_KEY"
ROOT_NOT_OBJECT = "ROOT_NOT_OBJECT"
SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
READ_CHANGED = "READ_CHANGED"
VALIDATOR_ERROR = "VALIDATOR_ERROR"

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")


class _DuplicateJsonKeyError(ValueError):
    pass


class _InvalidJsonConstantError(ValueError):
    pass


def validate_source_observation(
    observation: SourceObservation,
    *,
    max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
) -> ValidationReport:
    """Classify source bytes without normalizing or rewriting them.

    The return value contains only stable machine-readable reason codes; it
    deliberately excludes JSON fragments, source paths, and binding values.
    """
    try:
        _validate_max_state_bytes(max_state_bytes)
        return _validate_source_observation(observation, max_state_bytes)
    except (MemoryError, OverflowError, RecursionError):
        return ValidationReport(ValidationStatus.INDETERMINATE, (VALIDATOR_ERROR,))
    except Exception:
        return ValidationReport(ValidationStatus.INDETERMINATE, (VALIDATOR_ERROR,))


def _validate_source_observation(observation: SourceObservation, max_state_bytes: int) -> ValidationReport:
    metadata_report = _validate_stable_metadata(observation)
    if metadata_report is not None:
        return metadata_report

    payload = observation.payload
    if len(payload) > max_state_bytes:
        return ValidationReport(ValidationStatus.INVALID, (SOURCE_TOO_LARGE,))
    bom_report = _validate_bom(payload)
    if bom_report is not None and bom_report.status is ValidationStatus.INVALID:
        return bom_report
    if b"\x00" in payload:
        return ValidationReport(ValidationStatus.INVALID, (NUL_BYTE,))

    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeError:
        return ValidationReport(ValidationStatus.INVALID, (INVALID_UTF8,))

    try:
        parsed = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_json_constant,
        )
    except _DuplicateJsonKeyError:
        return ValidationReport(ValidationStatus.INVALID, (DUPLICATE_JSON_KEY,))
    except (_InvalidJsonConstantError, json.JSONDecodeError):
        return ValidationReport(ValidationStatus.INVALID, (INVALID_JSON,))

    if not isinstance(parsed, dict):
        return ValidationReport(ValidationStatus.INVALID, (ROOT_NOT_OBJECT,))
    if bom_report is not None:
        return bom_report
    return ValidationReport(ValidationStatus.PASS)


def _validate_stable_metadata(observation: SourceObservation) -> ValidationReport | None:
    if not observation.is_stable:
        return ValidationReport(ValidationStatus.INDETERMINATE, (READ_CHANGED,))
    if not isinstance(observation.payload, bytes):
        return ValidationReport(ValidationStatus.INDETERMINATE, (VALIDATOR_ERROR,))
    if observation.source_size_before is None or observation.source_size_after is None:
        return ValidationReport(ValidationStatus.INDETERMINATE, (READ_CHANGED,))
    if observation.source_mtime_ns_before is None or observation.source_mtime_ns_after is None:
        return ValidationReport(ValidationStatus.INDETERMINATE, (READ_CHANGED,))
    if (
        observation.source_size_before != observation.source_size_after
        or observation.source_size_before != len(observation.payload)
        or observation.source_mtime_ns_before != observation.source_mtime_ns_after
    ):
        return ValidationReport(ValidationStatus.INDETERMINATE, (READ_CHANGED,))
    before_id = observation.source_file_id_before
    after_id = observation.source_file_id_after
    if (before_id is None) != (after_id is None) or (before_id is not None and before_id != after_id):
        return ValidationReport(ValidationStatus.INDETERMINATE, (READ_CHANGED,))
    return None


def _validate_bom(payload: bytes) -> ValidationReport | None:
    if any(payload.find(bom) >= 0 for bom in _UTF32_BOMS):
        return ValidationReport(ValidationStatus.INVALID, (UNSUPPORTED_BOM,))
    if any(payload.find(bom) >= 0 for bom in _UTF16_BOMS):
        return ValidationReport(ValidationStatus.INVALID, (UNSUPPORTED_BOM,))
    bom_index = payload.find(_UTF8_BOM)
    if bom_index == -1:
        return None
    if bom_index != 0 or payload.find(_UTF8_BOM, len(_UTF8_BOM)) != -1:
        return ValidationReport(ValidationStatus.INVALID, (UNSUPPORTED_BOM,))
    return ValidationReport(ValidationStatus.PASS_WITH_WARNING, (UTF8_BOM,))


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> None:
    raise _InvalidJsonConstantError


def _validate_max_state_bytes(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("guardian.max_state_bytes must be an integer")
    if not MIN_MAX_STATE_BYTES <= value <= MAX_MAX_STATE_BYTES:
        raise ValueError("guardian.max_state_bytes must be between 1 MiB and 1 GiB")
