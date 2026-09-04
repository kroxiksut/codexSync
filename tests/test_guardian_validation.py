from __future__ import annotations

import unittest

from codexsync.guardian_models import SourceObservation, ValidationStatus
from codexsync.guardian_validation import (
    DUPLICATE_JSON_KEY,
    INVALID_JSON,
    INVALID_UTF8,
    NUL_BYTE,
    READ_CHANGED,
    ROOT_NOT_OBJECT,
    SOURCE_TOO_LARGE,
    UNSUPPORTED_BOM,
    UTF8_BOM,
    VALIDATOR_ERROR,
    validate_source_observation,
)


class GuardianValidationTests(unittest.TestCase):
    def test_valid_json_and_single_utf8_bom_are_classified_deterministically(self) -> None:
        cases = [
            (b"{}", ValidationStatus.PASS, ()),
            (b"\xef\xbb\xbf{}", ValidationStatus.PASS_WITH_WARNING, (UTF8_BOM,)),
        ]
        for payload, expected_status, expected_codes in cases:
            with self.subTest(payload=payload):
                report = validate_source_observation(self._observation(payload))
                self.assertEqual(report.status, expected_status)
                self.assertEqual(report.codes, expected_codes)

    def test_invalid_byte_and_json_cases_are_rejected(self) -> None:
        cases = [
            (b"", INVALID_JSON),
            (b"\x00{}", NUL_BYTE),
            (b"{}\x00", NUL_BYTE),
            (b"\xff{}", INVALID_UTF8),
            (b"\xff\xfe{\x00}\x00", UNSUPPORTED_BOM),
            (b"\xff\xfe\x00\x00", UNSUPPORTED_BOM),
            (b"{}\xef\xbb\xbf", UNSUPPORTED_BOM),
            (b"\xef\xbb\xbf{}\xef\xbb\xbf", UNSUPPORTED_BOM),
            (b'{"a":1,"a":2}', DUPLICATE_JSON_KEY),
            (b"{} trailing", INVALID_JSON),
            (b"[]", ROOT_NOT_OBJECT),
            (b"null", ROOT_NOT_OBJECT),
        ]
        for payload, expected_code in cases:
            with self.subTest(payload=payload):
                report = validate_source_observation(self._observation(payload))
                self.assertEqual(report.status, ValidationStatus.INVALID)
                self.assertEqual(report.codes, (expected_code,))

    def test_changed_metadata_never_creates_a_partial_snapshot_candidate(self) -> None:
        observation = self._observation(b"{}", size_after=3)

        report = validate_source_observation(observation)

        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertEqual(report.codes, (READ_CHANGED,))

    def test_size_limit_is_checked_before_json_parse(self) -> None:
        limit = 1 * 1024 * 1024
        at_limit = b"{" + (b" " * (limit - 2)) + b"}"
        over_limit = at_limit + b" "

        self.assertEqual(validate_source_observation(self._observation(at_limit), max_state_bytes=limit).status, ValidationStatus.PASS)
        report = validate_source_observation(self._observation(over_limit), max_state_bytes=limit)
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertEqual(report.codes, (SOURCE_TOO_LARGE,))

    def test_unexpected_parser_depth_fails_safe(self) -> None:
        payload = (b"[" * 10_000) + (b"]" * 10_000)

        report = validate_source_observation(self._observation(payload))

        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertEqual(report.codes, (VALIDATOR_ERROR,))

    @staticmethod
    def _observation(payload: bytes, *, size_after: int | None = None) -> SourceObservation:
        size = len(payload)
        return SourceObservation(
            payload=payload,
            source_name=".codex-global-state.json",
            source_size_before=size,
            source_size_after=size if size_after is None else size_after,
            source_mtime_ns_before=1,
            source_mtime_ns_after=1,
            source_file_id_before="file-id",
            source_file_id_after="file-id",
            is_stable=True,
        )


if __name__ == "__main__":
    unittest.main()
