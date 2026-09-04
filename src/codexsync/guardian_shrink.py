from __future__ import annotations

from .guardian_models import GuardianManifest, ValidationReport, ValidationStatus


PROJECT_COUNT_DROP = "PROJECT_COUNT_DROP"
BINDING_COUNT_DROP = "BINDING_COUNT_DROP"
BASELINE_UNVERIFIED = "BASELINE_UNVERIFIED"
BASELINE_SCHEMA_MISMATCH = "BASELINE_SCHEMA_MISMATCH"
UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"


def assess_suspicious_shrink(
    candidate: ValidationReport,
    *,
    baseline: GuardianManifest | None,
    baseline_verified: bool,
    shrink_min_count: int = 2,
    shrink_ratio: float = 0.25,
) -> ValidationReport:
    """Compare aggregate counts only; project IDs and bindings never leave core."""
    if not _valid_policy(shrink_min_count, shrink_ratio):
        return ValidationReport(ValidationStatus.INDETERMINATE, (BASELINE_UNVERIFIED,))
    if candidate.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        return candidate
    if baseline is None:
        return candidate
    if not baseline_verified or baseline.validation_status not in {
        ValidationStatus.PASS,
        ValidationStatus.PASS_WITH_WARNING,
    }:
        return _indeterminate(candidate, BASELINE_UNVERIFIED)
    if not candidate.schema_id or not baseline.schema_id:
        return _indeterminate(candidate, UNKNOWN_SCHEMA)
    if candidate.schema_id != baseline.schema_id:
        return _indeterminate(candidate, BASELINE_SCHEMA_MISMATCH)
    if candidate.project_count is None or candidate.binding_count is None:
        return _indeterminate(candidate, UNKNOWN_SCHEMA)

    codes: list[str] = list(candidate.codes)
    if _is_suspicious_drop(baseline.project_count, candidate.project_count, shrink_min_count, shrink_ratio):
        codes.append(PROJECT_COUNT_DROP)
    if _is_suspicious_drop(baseline.binding_count, candidate.binding_count, shrink_min_count, shrink_ratio):
        codes.append(BINDING_COUNT_DROP)
    if codes != list(candidate.codes):
        return ValidationReport(
            ValidationStatus.SUSPICIOUS,
            tuple(codes),
            project_count=candidate.project_count,
            binding_count=candidate.binding_count,
            schema_id=candidate.schema_id,
        )
    return candidate


def _is_suspicious_drop(baseline: int, candidate: int, min_count: int, ratio: float) -> bool:
    if baseline <= 0 or candidate >= baseline:
        return False
    if candidate == 0:
        return True
    decrease = baseline - candidate
    return decrease >= min_count and decrease / baseline >= ratio


def _indeterminate(candidate: ValidationReport, code: str) -> ValidationReport:
    return ValidationReport(
        ValidationStatus.INDETERMINATE,
        candidate.codes + (code,),
        project_count=candidate.project_count,
        binding_count=candidate.binding_count,
        schema_id=candidate.schema_id,
    )


def _valid_policy(min_count: int, ratio: float) -> bool:
    return isinstance(min_count, int) and not isinstance(min_count, bool) and min_count >= 1 and 0.0 <= ratio <= 1.0
