"""The Guardian inventory must describe the store without changing it.

Every fixture here is produced by ``GuardianStore.commit``/``quarantine``, not
hand-written, so the inventory is checked against the layout production
actually writes. Damage is then applied on top of that layout, the way a crash
or a disk fault would leave it.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from codexsync.guardian_inventory import read_guardian_inventory
from codexsync.guardian_models import (
    GUARDIAN_LATEST_GOOD_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
)
from codexsync.guardian_pointer import resolve_or_restore_latest_good
from codexsync.guardian_store import GuardianStore


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


def _observation(payload: bytes, *, stable: bool = True) -> SourceObservation:
    return SourceObservation(
        payload=payload,
        source_name=".codex-global-state.json",
        source_size_before=len(payload),
        source_size_after=len(payload),
        source_mtime_ns_before=1,
        source_mtime_ns_after=1,
        source_file_id_before="file-id",
        source_file_id_after="file-id",
        is_stable=stable,
    )


def _passed(projects: int = 0, bindings: int = 0) -> ValidationReport:
    return ValidationReport(ValidationStatus.PASS, project_count=projects, binding_count=bindings, schema_id="legacy-v1")


class GuardianInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        # A system temp directory like tests/test_guardian_store.py, not the
        # repo sandbox: a snapshot path carries a ~70-character id plus a
        # marker temp name, which crosses Windows' 260-character limit under a
        # deep checkout. Nothing here needs staging and target on the repo's
        # volume — Guardian stages inside its own root.
        self.root = Path(tempfile.mkdtemp(prefix="cs-ginv-"))
        self.guardian_root = self.root / "guardian"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                direction = "bidirectional"
                compare = "mtime"

                [paths]
                local_state_dir = "{(self.root / 'local-state').as_posix()}"
                cloud_root_dir = "{(self.root / 'cloud').as_posix()}"
                backup_dir = "{(self.root / 'backups').as_posix()}"
                temp_dir = "{(self.root / '.tmp').as_posix()}"

                [guardian]
                root_dir = "{self.guardian_root.as_posix()}"

                [semantic]
                root_dir = "{(self.root / 'semantic').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.pointer = self.guardian_root / GUARDIAN_LATEST_GOOD_DIR_NAME / "machine-a.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _store(self, root: Path | None = None, machine: str = "machine-a") -> GuardianStore:
        return GuardianStore(root or self.guardian_root, machine, producer_version="0.2.0")

    def test_missing_guardian_root_is_an_empty_inventory_and_creates_nothing(self) -> None:
        before = _tree(self.root)

        inventory = read_guardian_inventory(self.config_path)

        self.assertEqual(inventory.machine_id, "machine-a")
        self.assertIsNone(inventory.latest_good_id)
        self.assertEqual((inventory.snapshots, inventory.quarantine, inventory.problems), ((), (), ()))
        self.assertEqual(_tree(self.root), before)
        self.assertFalse(self.guardian_root.exists())

    def test_committed_snapshots_are_listed_newest_generation_first_with_latest_good(self) -> None:
        store = self._store()
        first = store.commit(_observation(b'{"version":1}'), _passed(projects=2, bindings=1)).snapshot
        second = store.commit(_observation(b'{"version":22}'), _passed(projects=3, bindings=4)).snapshot
        assert first is not None and second is not None

        inventory = read_guardian_inventory(self.config_path)

        self.assertEqual(inventory.root_dir, self.guardian_root.resolve())
        self.assertEqual(inventory.latest_good_id, second.snapshot_id)
        self.assertEqual(inventory.problems, ())
        self.assertEqual([item.snapshot_id for item in inventory.snapshots], [second.snapshot_id, first.snapshot_id])
        newest, older = inventory.snapshots
        self.assertEqual((newest.generation, older.generation), (2, 1))
        self.assertEqual(newest.size, len(b'{"version":22}'))
        self.assertEqual((newest.project_count, newest.binding_count), (3, 4))
        self.assertEqual(newest.validation_status, "PASS")
        self.assertEqual(newest.validation_codes, ())
        self.assertEqual(newest.schema_id, "legacy-v1")
        self.assertTrue(newest.created_at_utc.endswith("Z"))
        self.assertTrue(all(item.committed and item.verified for item in inventory.snapshots))
        self.assertEqual([item.latest_good for item in inventory.snapshots], [True, False])

    def test_absent_pointer_is_reported_and_not_rebuilt(self) -> None:
        store = self._store()
        store.commit(_observation(b'{"version":1}'), _passed())
        self.pointer.unlink()

        inventory = read_guardian_inventory(self.config_path)

        self.assertIsNone(inventory.latest_good_id)
        self.assertFalse(any(item.latest_good for item in inventory.snapshots))
        self.assertTrue(any("latest-good" in problem for problem in inventory.problems))
        self.assertFalse(self.pointer.exists(), "a listing must never rebuild the pointer")
        # The writer path would have rebuilt it here, so the scenario is one
        # where not writing is a real decision rather than a no-op.
        self.assertIsNotNone(resolve_or_restore_latest_good(self.guardian_root, "machine-a"))
        self.assertTrue(self.pointer.exists())

    def test_pointer_at_a_missing_snapshot_is_reported_and_left_untouched(self) -> None:
        store = self._store()
        first = store.commit(_observation(b'{"version":1}'), _passed()).snapshot
        second = store.commit(_observation(b'{"version":2}'), _passed()).snapshot
        assert first is not None and second is not None
        shutil.rmtree(second.directory)
        pointer_bytes = self.pointer.read_bytes()

        inventory = read_guardian_inventory(self.config_path)

        self.assertIsNone(inventory.latest_good_id)
        self.assertEqual([item.snapshot_id for item in inventory.snapshots], [first.snapshot_id])
        self.assertFalse(inventory.snapshots[0].latest_good)
        self.assertTrue(any(second.snapshot_id in problem for problem in inventory.problems))
        self.assertEqual(self.pointer.read_bytes(), pointer_bytes)

    def test_unreadable_pointer_is_reported(self) -> None:
        self._store().commit(_observation(b'{"version":1}'), _passed())
        self.pointer.write_text("not-json", encoding="utf-8")

        inventory = read_guardian_inventory(self.config_path)

        self.assertIsNone(inventory.latest_good_id)
        self.assertTrue(any("unreadable" in problem for problem in inventory.problems))
        self.assertEqual(self.pointer.read_text(encoding="utf-8"), "not-json")

    def test_crash_before_the_marker_leaves_a_verified_but_uncommitted_snapshot(self) -> None:
        store = self._store()
        good = store.commit(_observation(b'{"version":1}'), _passed()).snapshot
        assert good is not None

        def power_cut(stage: str) -> None:
            if stage == "snapshot_published":
                raise RuntimeError("simulated power loss")

        with self.assertRaises(RuntimeError):
            store.commit(_observation(b'{"version":2}'), _passed(), fault_hook=power_cut)

        inventory = read_guardian_inventory(self.config_path)

        crashed, committed = inventory.snapshots
        self.assertEqual(crashed.generation, 2)
        self.assertFalse(crashed.committed)
        self.assertTrue(crashed.verified)
        self.assertFalse(crashed.latest_good)
        self.assertEqual(committed.snapshot_id, good.snapshot_id)
        self.assertTrue(committed.latest_good)
        self.assertEqual(inventory.latest_good_id, good.snapshot_id)

    def test_damaged_payload_is_committed_but_unverified_and_cannot_be_latest_good(self) -> None:
        snapshot = self._store().commit(_observation(b'{"version":1}'), _passed()).snapshot
        assert snapshot is not None
        snapshot.payload_path.write_bytes(b'{"version":9}')

        inventory = read_guardian_inventory(self.config_path)

        [info] = inventory.snapshots
        self.assertTrue(info.committed)
        self.assertFalse(info.verified)
        self.assertFalse(info.latest_good)
        self.assertIsNone(inventory.latest_good_id)
        self.assertTrue(any(snapshot.snapshot_id in problem for problem in inventory.problems))

    def test_quarantine_events_are_listed_from_their_manifests(self) -> None:
        store = self._store()
        saved = store.quarantine(_observation(b"\x00{}"), ValidationReport(ValidationStatus.INVALID, ("NUL_BYTE",)))
        unsaved = store.quarantine(
            _observation(b"{}", stable=False),
            ValidationReport(ValidationStatus.INDETERMINATE, ("READ_CHANGED",)),
        )

        inventory = read_guardian_inventory(self.config_path)

        events = {event.event_id: event for event in inventory.quarantine}
        self.assertEqual(set(events), {saved.quarantine_event_id, unsaved.quarantine_event_id})
        self.assertEqual(events[saved.quarantine_event_id].reason_codes, ("NUL_BYTE",))
        self.assertEqual(events[saved.quarantine_event_id].size, 3)
        self.assertEqual(events[unsaved.quarantine_event_id].reason_codes, ("READ_CHANGED",))
        self.assertIsNone(events[unsaved.quarantine_event_id].size)
        created = [event.created_at_utc or "" for event in inventory.quarantine]
        self.assertEqual(created, sorted(created, reverse=True))
        self.assertEqual(inventory.snapshots, ())
        self.assertEqual(inventory.problems, ())

    def test_duplicate_generations_are_reported(self) -> None:
        self._store().commit(_observation(b'{"version":1}'), _passed())
        elsewhere = self.root / "guardian-elsewhere"
        stray = self._store(elsewhere).commit(_observation(b'{"version":2}'), _passed()).snapshot
        assert stray is not None
        target = self.guardian_root / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a" / stray.snapshot_id
        shutil.move(str(stray.directory), str(target))

        inventory = read_guardian_inventory(self.config_path)

        self.assertEqual([item.generation for item in inventory.snapshots], [1, 1])
        self.assertTrue(any(problem.startswith("generation 1 ") for problem in inventory.problems))

    def test_other_machines_are_not_listed(self) -> None:
        self._store(machine="machine-b").commit(_observation(b'{"version":1}'), _passed())
        mine = self._store().commit(_observation(b'{"version":2}'), _passed()).snapshot
        assert mine is not None

        inventory = read_guardian_inventory(self.config_path)

        self.assertEqual([item.snapshot_id for item in inventory.snapshots], [mine.snapshot_id])

    def test_listing_is_capped_and_says_so(self) -> None:
        store = self._store()
        for version in range(3):
            store.commit(_observation(f'{{"version":{version}}}'.encode()), _passed())

        with patch("codexsync.guardian_inventory.MAX_LISTED_SNAPSHOTS", 2):
            inventory = read_guardian_inventory(self.config_path)

        self.assertEqual([item.generation for item in inventory.snapshots], [3, 2])
        self.assertTrue(any("truncated" in problem for problem in inventory.problems))

    def test_reading_a_populated_store_creates_nothing(self) -> None:
        store = self._store()
        store.commit(_observation(b'{"version":1}'), _passed())
        store.commit(_observation(b'{"version":2}'), _passed())
        store.quarantine(_observation(b"\x00"), ValidationReport(ValidationStatus.INVALID, ("NUL_BYTE",)))

        def power_cut(stage: str) -> None:
            if stage == "manifest_written":
                raise RuntimeError("simulated power loss")

        with self.assertRaises(RuntimeError):
            store.commit(_observation(b'{"version":3}'), _passed(), fault_hook=power_cut)
        # And a dangling pointer, the state in which the writer would rebuild.
        self.pointer.unlink()
        before = _tree(self.root)

        forbidden = AssertionError("the inventory must not rewrite the latest-good pointer")
        with patch("codexsync.guardian_pointer._write_pointer", side_effect=forbidden):
            inventory = read_guardian_inventory(self.config_path)

        self.assertEqual(len(inventory.snapshots), 2)
        self.assertEqual(len(inventory.quarantine), 1)
        self.assertEqual(_tree(self.root), before)
        for untouched in (".tmp", "backups", "semantic", "local-state", "cloud"):
            self.assertFalse((self.root / untouched).exists(), untouched)


if __name__ == "__main__":
    unittest.main()
