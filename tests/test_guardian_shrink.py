from __future__ import annotations

import unittest

from codexsync.guardian_models import GuardianManifest, ValidationReport, ValidationStatus
from codexsync.guardian_shrink import (
    BASELINE_SCHEMA_MISMATCH,
    BINDING_COUNT_DROP,
    PROJECT_COUNT_DROP,
    UNKNOWN_SCHEMA,
    assess_suspicious_shrink,
)


class GuardianShrinkTests(unittest.TestCase):
    def test_first_snapshot_and_small_change_are_not_suspicious(self) -> None:
        candidate = self._report(9, 9)
        self.assertEqual(
            assess_suspicious_shrink(candidate, baseline=None, baseline_verified=True),
            candidate,
        )
        self.assertEqual(
            assess_suspicious_shrink(candidate, baseline=self._baseline(10, 10), baseline_verified=True),
            candidate,
        )

    def test_boundary_drop_and_zero_are_suspicious(self) -> None:
        boundary = assess_suspicious_shrink(
            self._report(6, 8), baseline=self._baseline(8, 8), baseline_verified=True
        )
        self.assertEqual(boundary.status, ValidationStatus.SUSPICIOUS)
        self.assertEqual(boundary.codes, (PROJECT_COUNT_DROP,))

        zero = assess_suspicious_shrink(
            self._report(0, 7), baseline=self._baseline(1, 7), baseline_verified=True
        )
        self.assertEqual(zero.status, ValidationStatus.SUSPICIOUS)
        self.assertEqual(zero.codes, (PROJECT_COUNT_DROP,))

    def test_binding_count_is_compared_independently(self) -> None:
        result = assess_suspicious_shrink(
            self._report(8, 6), baseline=self._baseline(8, 8), baseline_verified=True
        )

        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)
        self.assertEqual(result.codes, (BINDING_COUNT_DROP,))

    def test_growth_and_order_only_change_do_not_trigger_shrink(self) -> None:
        report = self._report(11, 10)
        result = assess_suspicious_shrink(report, baseline=self._baseline(10, 10), baseline_verified=True)

        self.assertEqual(result, report)

    def test_schema_or_baseline_incompatibility_fails_safe(self) -> None:
        mismatch = assess_suspicious_shrink(
            self._report(4, 4, schema_id="other"), baseline=self._baseline(8, 8), baseline_verified=True
        )
        self.assertEqual(mismatch.status, ValidationStatus.INDETERMINATE)
        self.assertIn(BASELINE_SCHEMA_MISMATCH, mismatch.codes)

        unknown = assess_suspicious_shrink(
            self._report(4, 4), baseline=self._baseline(8, 8, schema_id=None), baseline_verified=True
        )
        self.assertEqual(unknown.status, ValidationStatus.INDETERMINATE)
        self.assertIn(UNKNOWN_SCHEMA, unknown.codes)

    def test_configurable_thresholds_are_applied(self) -> None:
        candidate = self._report(7, 10)
        baseline = self._baseline(10, 10)
        self.assertEqual(
            assess_suspicious_shrink(
                candidate,
                baseline=baseline,
                baseline_verified=True,
                shrink_min_count=4,
                shrink_ratio=0.25,
            ),
            candidate,
        )
        result = assess_suspicious_shrink(
            candidate,
            baseline=baseline,
            baseline_verified=True,
            shrink_min_count=3,
            shrink_ratio=0.30,
        )
        self.assertEqual(result.status, ValidationStatus.SUSPICIOUS)

    @staticmethod
    def _report(projects: int, bindings: int, *, schema_id: str | None = "legacy-v1") -> ValidationReport:
        return ValidationReport(
            ValidationStatus.PASS,
            project_count=projects,
            binding_count=bindings,
            schema_id=schema_id,
        )

    @staticmethod
    def _baseline(projects: int, bindings: int, *, schema_id: str | None = "legacy-v1") -> GuardianManifest:
        return GuardianManifest(
            manifest_version=1,
            snapshot_id="snapshot-1",
            generation=1,
            created_at_utc="2026-09-01T00:00:00.000000Z",
            machine_id="machine-a",
            source_name=".codex-global-state.json",
            source_size=2,
            source_mtime_ns_before=1,
            source_mtime_ns_after=1,
            source_file_id_before=None,
            source_file_id_after=None,
            hash_algorithm="sha256",
            sha256="a" * 64,
            validation_status=ValidationStatus.PASS,
            validation_codes=(),
            project_count=projects,
            binding_count=bindings,
            previous_good_snapshot_id=None,
            previous_good_sha256=None,
            producer_version="0.2.0",
            schema_id=schema_id,
        )


if __name__ == "__main__":
    unittest.main()
