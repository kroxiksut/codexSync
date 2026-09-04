from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import UUID

from codexsync.exceptions import GuardianIntegrityError
from codexsync.guardian_manifest import (
    GUARDIAN_MANIFEST_VERSION,
    build_guardian_manifest,
    parse_guardian_manifest,
    serialize_guardian_manifest,
    validate_guardian_manifest,
    verify_guardian_snapshot,
)
from codexsync.guardian_models import (
    GuardianSnapshot,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
    build_snapshot_id,
)


class GuardianManifestTests(unittest.TestCase):
    def test_round_trip_and_deterministic_serialization(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            manifest = self._build(snapshot, observation)

            first = serialize_guardian_manifest(manifest)
            second = serialize_guardian_manifest(manifest)
            parsed = parse_guardian_manifest(first)

            self.assertEqual(first, second)
            self.assertFalse(first.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(first.endswith(b"\n"))
            self.assertEqual(parsed, manifest)

    def test_unknown_fields_are_ignored(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            raw = json.loads(serialize_guardian_manifest(self._build(snapshot, observation)))
            raw["future_field"] = {"safe": True}

            parsed = parse_guardian_manifest(json.dumps(raw).encode("utf-8"))

            self.assertEqual(parsed.snapshot_id, snapshot.snapshot_id)

    def test_missing_and_wrong_typed_required_fields_fail(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            raw = json.loads(serialize_guardian_manifest(self._build(snapshot, observation)))
            del raw["sha256"]
            with self.assertRaisesRegex(GuardianIntegrityError, "missing required fields"):
                parse_guardian_manifest(json.dumps(raw).encode("utf-8"))

            raw = json.loads(serialize_guardian_manifest(self._build(snapshot, observation)))
            raw["generation"] = "1"
            with self.assertRaisesRegex(GuardianIntegrityError, "generation"):
                parse_guardian_manifest(json.dumps(raw).encode("utf-8"))

    def test_incompatible_version_is_not_accepted(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            raw = json.loads(serialize_guardian_manifest(self._build(snapshot, observation)))
            raw["manifest_version"] = GUARDIAN_MANIFEST_VERSION + 1

            with self.assertRaisesRegex(GuardianIntegrityError, "Unsupported Guardian manifest version"):
                parse_guardian_manifest(json.dumps(raw).encode("utf-8"))

    def test_verify_rejects_tampered_payload_and_directory_id_mismatch(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            manifest = self._build(snapshot, observation)
            snapshot.directory.mkdir(parents=True)
            snapshot.payload_path.write_bytes(observation.payload)
            snapshot.manifest_path.write_bytes(serialize_guardian_manifest(manifest))
            self.assertEqual(verify_guardian_snapshot(snapshot), manifest)

            snapshot.payload_path.write_bytes(b"x" * len(observation.payload))
            with self.assertRaisesRegex(GuardianIntegrityError, "SHA-256"):
                verify_guardian_snapshot(snapshot)

            snapshot.payload_path.write_bytes(observation.payload)
            wrong_manifest = replace(manifest, snapshot_id="other-snapshot")
            snapshot.manifest_path.write_bytes(serialize_guardian_manifest(wrong_manifest))
            with self.assertRaisesRegex(GuardianIntegrityError, "directory does not match"):
                verify_guardian_snapshot(snapshot)

    def test_generation_and_predecessor_must_be_consistent(self) -> None:
        with self._snapshot_case() as (first, observation):
            previous = self._build(first, observation)
            successor = GuardianSnapshot(
                root_dir=first.root_dir,
                machine_id=first.machine_id,
                snapshot_id=build_snapshot_id(
                    "b" * 64,
                    operation_id=UUID("22222222-2222-2222-2222-222222222222"),
                    now=datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
                generation=2,
            )
            report = ValidationReport(ValidationStatus.PASS, project_count=1, binding_count=1)
            second = build_guardian_manifest(
                successor,
                observation,
                report,
                producer_version="0.2.0",
                previous_good=previous,
            )

            self.assertEqual(second.previous_good_snapshot_id, previous.snapshot_id)
            self.assertEqual(second.previous_good_sha256, previous.sha256)
            with self.assertRaisesRegex(GuardianIntegrityError, "generation is already in use"):
                validate_guardian_manifest(second, predecessor=previous, known_generations=[2])

    def test_manifest_never_contains_source_payload_or_absolute_source_path(self) -> None:
        with self._snapshot_case() as (snapshot, observation):
            manifest = self._build(snapshot, observation)
            encoded = serialize_guardian_manifest(manifest)

            self.assertNotIn(observation.payload, encoded)
            self.assertNotIn(str(snapshot.root_dir).encode("utf-8"), encoded)

    def _build(self, snapshot: GuardianSnapshot, observation: SourceObservation):
        return build_guardian_manifest(
            snapshot,
            observation,
            ValidationReport(ValidationStatus.PASS, codes=("VALID",), project_count=1, binding_count=1),
            producer_version="0.2.0",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

    def _snapshot_case(self):
        return _SnapshotCase()


class _SnapshotCase:
    def __enter__(self) -> tuple[GuardianSnapshot, SourceObservation]:
        self._root = Path(tempfile.mkdtemp()) / "guardian"
        payload = b'{"projects":[]}'
        snapshot_id = build_snapshot_id(
            "a" * 64,
            operation_id=UUID("11111111-1111-1111-1111-111111111111"),
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        return (
            GuardianSnapshot(self._root, "machine-a", snapshot_id, 1),
            SourceObservation(
                payload=payload,
                source_name=".codex-global-state.json",
                source_size_before=len(payload),
                source_size_after=len(payload),
                source_mtime_ns_before=10,
                source_mtime_ns_after=10,
                source_file_id_before="file-id",
                source_file_id_after="file-id",
                is_stable=True,
            ),
        )

    def __exit__(self, *_args) -> None:
        shutil.rmtree(self._root.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
