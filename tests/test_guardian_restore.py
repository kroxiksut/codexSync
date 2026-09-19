"""Planning a Guardian restore must prove the snapshot and change nothing.

Every snapshot here is produced by ``GuardianStore.commit`` from an electron-v2
state, the shape the desktop build actually writes — fixtures in an assumed
shape once let Guardian pass every test while being inert on real data. Damage
is applied on top of the committed layout, the way a disk fault or a crash
would leave it.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from codexsync.exceptions import ConfigError, ConflictError, FailSafeError
from codexsync.guardian_models import SourceObservation, ValidationStatus
from codexsync.guardian_restore import (
    NOTHING_TO_RESTORE,
    PROJECT_IDS_REPLACED,
    SCHEMA_CHANGED,
    SNAPSHOT_INVALID,
    SNAPSHOT_NOT_FOUND,
    SNAPSHOT_UNVERIFIED,
    build_guardian_restore_plan,
    verify_restore_still_valid,
)
from codexsync.guardian_schema import ELECTRON_V2_SCHEMA, validate_global_state_references
from codexsync.guardian_store import GuardianStore


HOST = "local:C:\\Users\\user\\.codex"


def _electron_state(**overrides) -> dict:
    state = {
        "local-projects": {
            "p1": {"id": "p1", "name": "one", "rootPaths": ["C:/a"], "createdAt": 1, "updatedAt": 2},
            "p2": {"id": "p2", "name": "two", "rootPaths": ["C:/b"], "createdAt": 1, "updatedAt": 2},
        },
        "project-order": ["p1", "p2"],
        "thread-project-assignments": {
            "t1": {"projectKind": "local", "projectId": "p1"},
            "t2": {"projectKind": "app-server", "projectId": "as2"},
        },
        "app-server-project-id-by-legacy-project-id-by-host": {HOST: {"p1": "as1", "p2": "as2"}},
        "selected-project": {"projectId": "p1", "type": "local"},
    }
    state.update(overrides)
    return state


def _payload(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False).encode("utf-8")


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


def _tree(root: Path) -> dict[str, tuple[bool, int, int]]:
    """Names, sizes and mtimes under ``root``; parent mtimes catch a created-then-removed file."""
    if not root.exists():
        return {}
    result = {".": (True, 0, root.stat().st_mtime_ns)}
    for path in root.rglob("*"):
        stat_result = path.stat()
        result[path.relative_to(root).as_posix()] = (
            path.is_dir(),
            0 if path.is_dir() else stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    return result


class GuardianRestorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        # A system temp directory like tests/test_guardian_store.py: snapshot
        # paths carry a ~70-character id and cross Windows' path limit under a
        # deep checkout, and nothing here needs the repo volume.
        self.root = Path(tempfile.mkdtemp(prefix="cs-grest-"))
        self.guardian_root = self.root / "guardian"
        self.good = _payload(_electron_state())
        # Codex dropped a project and a binding since the snapshot was taken.
        self.current = _payload(
            _electron_state(
                **{
                    "local-projects": {
                        "p1": {"id": "p1", "name": "one", "rootPaths": ["C:/a"], "createdAt": 1, "updatedAt": 3},
                    },
                    "project-order": ["p1"],
                    "thread-project-assignments": {},
                }
            )
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _commit(self, payload: bytes, *, machine: str = "machine-a"):
        report = validate_global_state_references(payload)
        if report.status is not ValidationStatus.PASS:
            # A payload that fails today's rules can only have been committed
            # under older ones; stand in for that report.
            report = replace(report, status=ValidationStatus.PASS, codes=(), project_count=report.project_count or 0,
                             binding_count=report.binding_count or 0)
        store = GuardianStore(self.guardian_root, machine, producer_version="0.2.0")
        snapshot = store.commit(_observation(payload), report).snapshot
        assert snapshot is not None
        return snapshot

    def _plan(self, snapshot_id: str, current: bytes | None, machine: str = "machine-a"):
        return build_guardian_restore_plan(
            root_dir=self.guardian_root, machine_id=machine, snapshot_id=snapshot_id, current_state=current
        )

    # -- building ---------------------------------------------------------------

    def test_a_verified_snapshot_plans_a_restore_with_counts_on_both_sides(self) -> None:
        snapshot = self._commit(self.good)

        plan, payload = self._plan(snapshot.snapshot_id, self.current)

        self.assertEqual(plan.codes, ())
        self.assertEqual(payload, self.good)
        self.assertFalse(plan.identical)
        self.assertEqual(plan.machine_id, "machine-a")
        self.assertEqual(plan.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(plan.generation, 1)
        self.assertTrue(plan.snapshot_created_at_utc.endswith("Z"))
        self.assertEqual(plan.schema_id, ELECTRON_V2_SCHEMA)
        self.assertEqual((plan.projects_in_snapshot, plan.bindings_in_snapshot), (2, 2))
        self.assertEqual((plan.projects_now, plan.bindings_now), (1, 0))
        self.assertEqual(len(plan.plan_id), 64)

    def test_identical_bytes_are_nothing_to_restore(self) -> None:
        snapshot = self._commit(self.good)

        plan, payload = self._plan(snapshot.snapshot_id, self.good)

        self.assertTrue(plan.identical)
        self.assertEqual(plan.codes, (NOTHING_TO_RESTORE,))
        self.assertEqual(payload, self.good)

    def test_a_snapshot_of_another_schema_is_refused_over_a_recognised_state(self) -> None:
        """A legacy-shaped file handed to a desktop build that now writes Electron state."""
        legacy = json.dumps({"local-projects": {"a": {"root": "C:/a"}}, "project-order": ["a"]}).encode("utf-8")
        snapshot = self._commit(legacy)

        plan, _ = self._plan(snapshot.snapshot_id, self.current)

        self.assertIn(SCHEMA_CHANGED, plan.codes)

    def test_a_snapshot_whose_project_ids_were_all_replaced_is_refused(self) -> None:
        """Codex re-created every project since the snapshot: restoring it would drop them all."""
        snapshot = self._commit(self.good)
        rekeyed = _payload(_electron_state(**{
            "local-projects": {
                "n1": {"id": "n1", "name": "one", "rootPaths": ["C:/a"], "createdAt": 5, "updatedAt": 6},
            },
            "project-order": ["n1"],
            "thread-project-assignments": {},
        }))

        plan, _ = self._plan(snapshot.snapshot_id, rekeyed)

        self.assertIn(PROJECT_IDS_REPLACED, plan.codes)

    def test_a_partly_changed_project_set_is_still_restorable(self) -> None:
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.current)
        self.assertNotIn(PROJECT_IDS_REPLACED, plan.codes)

    def test_an_unknown_snapshot_id_is_not_found(self) -> None:
        self._commit(self.good)

        plan, payload = self._plan("20260101T000000.000000Z-missing", self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_NOT_FOUND,))
        self.assertIsNone(payload)
        self.assertEqual((plan.generation, plan.snapshot_sha256), (0, ""))

    def test_a_missing_guardian_root_is_not_found_and_is_not_created(self) -> None:
        plan, payload = self._plan("20260101T000000.000000Z-missing", self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_NOT_FOUND,))
        self.assertIsNone(payload)
        self.assertFalse(self.guardian_root.exists())

    def test_a_snapshot_id_that_leaves_the_machine_directory_is_a_config_error(self) -> None:
        snapshot = self._commit(self.good)
        for unsafe in (
            f"../machine-b/{snapshot.snapshot_id}",
            f"..\\{snapshot.snapshot_id}",
            "..",
            ".",
            "",
            "a/b",
            "x..y",
            "C:evil",
        ):
            with self.subTest(snapshot_id=unsafe), self.assertRaises(ConfigError):
                self._plan(unsafe, self.current)

    def test_another_machines_snapshot_is_not_reachable(self) -> None:
        foreign = self._commit(self.good, machine="machine-b")

        plan, payload = self._plan(foreign.snapshot_id, self.current, machine="machine-a")

        self.assertEqual(plan.codes, (SNAPSHOT_NOT_FOUND,))
        self.assertIsNone(payload)

    def test_a_tampered_payload_is_unverified(self) -> None:
        snapshot = self._commit(self.good)
        tampered = self.good.replace(b'"two"', b'"TWO"')
        self.assertEqual(len(tampered), len(self.good))
        snapshot.payload_path.write_bytes(tampered)

        plan, payload = self._plan(snapshot.snapshot_id, self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_UNVERIFIED,))
        self.assertIsNone(payload)

    def test_a_missing_committed_marker_is_unverified(self) -> None:
        snapshot = self._commit(self.good)
        snapshot.committed_path.unlink()

        plan, payload = self._plan(snapshot.snapshot_id, self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_UNVERIFIED,))
        self.assertIsNone(payload)

    def test_a_marker_for_another_snapshot_is_unverified(self) -> None:
        snapshot = self._commit(self.good)
        marker = json.loads(snapshot.committed_path.read_text(encoding="utf-8"))
        marker["sha256"] = "0" * 64
        snapshot.committed_path.write_text(json.dumps(marker), encoding="utf-8")

        plan, payload = self._plan(snapshot.snapshot_id, self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_UNVERIFIED,))
        self.assertIsNone(payload)

    def test_a_committed_snapshot_that_fails_todays_rules_is_invalid(self) -> None:
        broken = _payload(
            _electron_state(**{"thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "gone"}}})
        )
        snapshot = self._commit(broken)

        plan, payload = self._plan(snapshot.snapshot_id, self.current)

        self.assertEqual(plan.codes, (SNAPSHOT_INVALID,))
        self.assertIsNone(payload)
        self.assertFalse(plan.identical)

    def test_an_absent_current_state_is_restorable(self) -> None:
        snapshot = self._commit(self.good)

        plan, payload = self._plan(snapshot.snapshot_id, None)

        self.assertEqual(plan.codes, ())
        self.assertEqual(payload, self.good)
        self.assertIsNone(plan.current_state_sha256)
        self.assertEqual((plan.projects_now, plan.bindings_now), (None, None))

    def test_an_invalid_current_state_does_not_block_a_restore(self) -> None:
        snapshot = self._commit(self.good)
        for broken in (b"{not json", _payload({"something": "else"}), b"\x00"):
            with self.subTest(current=broken):
                plan, payload = self._plan(snapshot.snapshot_id, broken)

                self.assertEqual(plan.codes, ())
                self.assertEqual(payload, self.good)
                self.assertEqual((plan.projects_now, plan.bindings_now), (None, None))

    def test_the_plan_id_follows_the_current_state(self) -> None:
        snapshot = self._commit(self.good)

        first, _ = self._plan(snapshot.snapshot_id, self.current)
        again, _ = self._plan(snapshot.snapshot_id, self.current)
        moved, _ = self._plan(snapshot.snapshot_id, self.current + b" ")
        absent, _ = self._plan(snapshot.snapshot_id, None)

        self.assertEqual(first.plan_id, again.plan_id)
        self.assertNotEqual(first.plan_id, moved.plan_id)
        self.assertNotEqual(first.plan_id, absent.plan_id)

    def test_the_plan_id_follows_the_snapshot(self) -> None:
        first = self._commit(self.good)
        second = self._commit(self.current + b" ")

        one, _ = self._plan(first.snapshot_id, self.current)
        two, _ = self._plan(second.snapshot_id, self.current)

        self.assertNotEqual(one.plan_id, two.plan_id)
        self.assertEqual(two.generation, 2)

    def test_planning_writes_nothing(self) -> None:
        snapshot = self._commit(self.good)
        damaged = self._commit(_payload(_electron_state(**{"selected-project": None})))
        damaged.committed_path.unlink()
        # A missing pointer is what the writer would rebuild; the planner must not.
        pointer = self.guardian_root / "latest-good" / "machine-a.json"
        pointer.unlink()
        before = _tree(self.root)

        plan, _ = self._plan(snapshot.snapshot_id, self.current)
        self._plan(damaged.snapshot_id, self.current)
        self._plan("20260101T000000.000000Z-missing", None)
        verify_restore_still_valid(plan, root_dir=self.guardian_root, current_state=self.current,
                                   confirm_plan=plan.plan_id)

        self.assertEqual(_tree(self.root), before)
        self.assertFalse(pointer.exists())

    # -- re-proving before the write ------------------------------------------

    def test_verify_returns_the_payload_for_an_unchanged_plan(self) -> None:
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.current)

        payload = verify_restore_still_valid(
            plan, root_dir=self.guardian_root, current_state=self.current, confirm_plan=plan.plan_id
        )

        self.assertEqual(payload, self.good)

    def test_verify_refuses_a_confirmation_for_another_plan(self) -> None:
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.current)

        with self.assertRaises(ConfigError):
            verify_restore_still_valid(
                plan, root_dir=self.guardian_root, current_state=self.current, confirm_plan="0" * 64
            )

    def test_verify_refuses_when_the_state_moved_after_the_preview(self) -> None:
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.current)

        with self.assertRaisesRegex(FailSafeError, "changed since the preview"):
            verify_restore_still_valid(
                plan, root_dir=self.guardian_root, current_state=self.current + b" ", confirm_plan=plan.plan_id
            )

    def test_verify_refuses_a_plan_that_carries_codes(self) -> None:
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.good)

        with self.assertRaisesRegex(ConflictError, NOTHING_TO_RESTORE):
            verify_restore_still_valid(
                plan, root_dir=self.guardian_root, current_state=self.good, confirm_plan=plan.plan_id
            )

    def test_verify_refuses_a_snapshot_damaged_after_the_preview(self) -> None:
        # The manifest, and so the plan id, is unchanged; only a rebuild sees it.
        snapshot = self._commit(self.good)
        plan, _ = self._plan(snapshot.snapshot_id, self.current)
        snapshot.payload_path.write_bytes(self.good.replace(b'"two"', b'"TWO"'))

        with self.assertRaisesRegex(ConflictError, SNAPSHOT_UNVERIFIED):
            verify_restore_still_valid(
                plan, root_dir=self.guardian_root, current_state=self.current, confirm_plan=plan.plan_id
            )


if __name__ == "__main__":
    unittest.main()
