from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import shutil
import tempfile
import unittest

from codexsync.exceptions import GuardianBusyError
from codexsync.guardian_lock import GuardianWriterLock
from codexsync.guardian_manifest import verify_guardian_snapshot
from codexsync.guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GUARDIAN_STAGING_DIR_NAME,
    GUARDIAN_QUARANTINE_DIR_NAME,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
)
from codexsync.guardian_store import GuardianStore
from codexsync.guardian_pointer import resolve_or_restore_latest_good


class GuardianStoreTests(unittest.TestCase):
    def test_commit_writes_self_contained_immutable_snapshot_and_advances_generation(self) -> None:
        with self._store_case() as (root, store):
            first = store.commit(self._observation(b'{"version":1}'), self._validation())
            second = store.commit(self._observation(b'{"version":2}'), self._validation())

            assert first.snapshot is not None
            assert second.snapshot is not None
            self.assertTrue(first.snapshot.payload_path.is_file())
            self.assertTrue(first.snapshot.manifest_path.is_file())
            self.assertTrue(first.snapshot.committed_path.is_file())
            self.assertEqual(first.snapshot.generation, 1)
            self.assertEqual(second.snapshot.generation, 2)
            self.assertEqual(verify_guardian_snapshot(first.snapshot).generation, 1)
            self.assertEqual(first.snapshot.payload_path.read_bytes(), b'{"version":1}')
            self.assertTrue((root / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a").is_dir())
            latest = resolve_or_restore_latest_good(root, "machine-a")
            self.assertEqual(latest, second.snapshot)

    def test_active_writer_lock_returns_busy(self) -> None:
        with self._store_case() as (root, store):
            with GuardianWriterLock(root, "machine-a"):
                with self.assertRaises(GuardianBusyError):
                    store.commit(self._observation(b"{}"), self._validation())

    def test_faults_before_commit_never_create_committed_snapshot(self) -> None:
        for fault_stage in ("payload_written", "manifest_written", "validated", "snapshot_published"):
            with self.subTest(fault_stage=fault_stage), self._store_case() as (root, store):
                def fail(stage: str) -> None:
                    if stage == fault_stage:
                        raise RuntimeError("simulated power loss")

                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    store.commit(self._observation(b"{}"), self._validation(), fault_hook=fail)

                committed = list(root.rglob(GUARDIAN_COMMITTED_NAME)) if root.exists() else []
                self.assertEqual(committed, [])
                if fault_stage == "snapshot_published":
                    self.assertTrue((root / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a").exists())
                else:
                    self.assertTrue((root / GUARDIAN_STAGING_DIR_NAME / "machine-a").exists())

    def test_quarantine_saves_only_stable_payload_and_never_commits(self) -> None:
        with self._store_case() as (root, store):
            stable = store.quarantine(
                self._observation(b'{"secret":"not-in-manifest"}'),
                ValidationReport(ValidationStatus.INVALID, ("INVALID_JSON",)),
            )
            assert stable.quarantine_event_id is not None
            event_dir = root / GUARDIAN_QUARANTINE_DIR_NAME / "machine-a" / stable.quarantine_event_id
            manifest = json.loads((event_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue((event_dir / "source.bin").is_file())
            self.assertFalse((event_dir / GUARDIAN_COMMITTED_NAME).exists())
            self.assertNotIn("secret", json.dumps(manifest))

            unstable_observation = self._observation(b"{}")
            unstable_observation = replace(unstable_observation, is_stable=False)
            unstable = store.quarantine(
                unstable_observation,
                ValidationReport(ValidationStatus.INDETERMINATE, ("READ_CHANGED",)),
            )
            assert unstable.quarantine_event_id is not None
            self.assertFalse(
                (root / GUARDIAN_QUARANTINE_DIR_NAME / "machine-a" / unstable.quarantine_event_id / "source.bin").exists()
            )

    def test_every_code_the_validators_can_emit_is_accepted_by_quarantine(self) -> None:
        """A code quarantine does not know turns a rejection into a crash.

        Found on a real state: a warning (`PROJECT_NOT_IN_ORDER`) rode along
        with a suspicious binding drop, and `guardian snapshot` stopped with
        "unsupported reason codes" instead of quarantining. Read the codes from
        the modules that produce them, so a new one cannot be forgotten.
        """
        import ast

        from codexsync import guardian_runner, guardian_schema, guardian_shrink, guardian_validation
        from codexsync.guardian_store import QUARANTINE_REASON_CODES

        emitted: set[str] = set()
        for module in (guardian_validation, guardian_schema, guardian_shrink, guardian_runner):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                    and node.value.value.isupper() and "_" in node.value.value
                    and node.targets[0].id.lstrip("_").isupper()
                    and not node.targets[0].id.endswith("_SCHEMA")
                ):
                    emitted.add(node.value.value)
        self.assertIn("PROJECT_NOT_IN_ORDER", emitted, "the scan stopped seeing the codes")
        self.assertEqual(sorted(emitted - QUARANTINE_REASON_CODES), [])

    def test_a_warning_riding_along_with_a_shrink_is_quarantined(self) -> None:
        with self._store_case() as (_root, store):
            result = store.quarantine(
                self._observation(b"{}"),
                ValidationReport(ValidationStatus.SUSPICIOUS, ("PROJECT_NOT_IN_ORDER", "BINDING_COUNT_DROP")),
            )
            self.assertIsNotNone(result.quarantine_event_id)

    def test_quarantine_deduplicates_same_payload_and_reason(self) -> None:
        with self._store_case() as (_root, store):
            report = ValidationReport(ValidationStatus.INVALID, ("NUL_BYTE",))
            first = store.quarantine(self._observation(b"{}"), report)
            second = store.quarantine(self._observation(b"{}"), report)

            self.assertEqual(first.quarantine_event_id, second.quarantine_event_id)
            self.assertEqual(second.detail, "duplicate")

    def test_damaged_pointer_is_rebuilt_from_highest_verified_generation(self) -> None:
        with self._store_case() as (root, store):
            first = store.commit(self._observation(b'{"version":1}'), self._validation())
            second = store.commit(self._observation(b'{"version":2}'), self._validation())
            assert first.snapshot is not None
            assert second.snapshot is not None
            pointer = root / "latest-good" / "machine-a.json"
            pointer.write_text("not-json", encoding="utf-8")

            recovered = resolve_or_restore_latest_good(root, "machine-a")

            self.assertEqual(recovered, second.snapshot)
            self.assertIn(second.snapshot.snapshot_id, pointer.read_text(encoding="utf-8"))

    def test_same_bytes_with_different_metadata_returns_unchanged(self) -> None:
        with self._store_case() as (_root, store):
            first = store.commit(self._observation(b"{}"), self._validation())
            observation = self._observation(b"{}")
            observation = replace(observation, source_mtime_ns_before=2, source_mtime_ns_after=2)

            duplicate = store.commit(observation, self._validation())

            self.assertEqual(duplicate.status.value, "UNCHANGED")
            self.assertEqual(duplicate.snapshot, first.snapshot)

    def test_retention_keeps_latest_good_and_its_predecessor(self) -> None:
        with self._store_case(max_snapshots=1) as (root, store):
            first = store.commit(self._observation(b'{"version":1}'), self._validation())
            second = store.commit(self._observation(b'{"version":2}'), self._validation())
            third = store.commit(self._observation(b'{"version":3}'), self._validation())
            assert first.snapshot is not None
            assert second.snapshot is not None
            assert third.snapshot is not None

            self.assertFalse(first.snapshot.directory.exists())
            self.assertTrue(second.snapshot.directory.exists())
            self.assertTrue(third.snapshot.directory.exists())
            self.assertEqual(resolve_or_restore_latest_good(root, "machine-a"), third.snapshot)

    @staticmethod
    def _observation(payload: bytes) -> SourceObservation:
        return SourceObservation(
            payload=payload,
            source_name=".codex-global-state.json",
            source_size_before=len(payload),
            source_size_after=len(payload),
            source_mtime_ns_before=1,
            source_mtime_ns_after=1,
            source_file_id_before="file-id",
            source_file_id_after="file-id",
            is_stable=True,
        )

    @staticmethod
    def _validation() -> ValidationReport:
        return ValidationReport(ValidationStatus.PASS, project_count=0, binding_count=0, schema_id="legacy-v1")

    def _store_case(self, *, max_snapshots: int = 100):
        return _StoreCase(max_snapshots=max_snapshots)


class _StoreCase:
    def __init__(self, *, max_snapshots: int) -> None:
        self._max_snapshots = max_snapshots

    def __enter__(self) -> tuple[Path, GuardianStore]:
        self._root = Path(tempfile.mkdtemp()) / "guardian"
        return self._root, GuardianStore(
            self._root,
            "machine-a",
            producer_version="0.2.0",
            max_snapshots=self._max_snapshots,
        )

    def __exit__(self, *_args) -> None:
        shutil.rmtree(self._root.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
