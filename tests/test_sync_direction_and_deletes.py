"""One-way directions (`D-012`) and propagated deletions (`D-013`).

Both settings are easy to implement in a way that quietly corrupts the next
run, and both failures are invisible at the moment they happen:

* a one-way run that records the side it skipped as synchronised makes the
  following bidirectional run believe the two sides agreed -- the older file
  then wins for good;
* a deletion propagated without proof removes a file that was never
  synchronised, or one the other side edited meanwhile.

So the tests below are mostly about the manifest and about what is *not* done.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.backup import BackupManager
from codexsync.manifest import build_manifest
from codexsync.models import FileMeta, ManifestEntry, SnapshotFingerprint, SyncManifest
from codexsync.planner import build_sync_plan
from codexsync.sync_engine import SyncEngine

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


def meta(path: Path, mtime_ns: int, size: int) -> FileMeta:
    return FileMeta(relative_path=path.name, abs_path=path, mtime_ns=mtime_ns, size=size)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"direction-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        self.local.mkdir(parents=True)
        self.cloud.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, side: Path, name: str, text: str, mtime_ns: int = 1_000) -> Path:
        path = side / name
        path.write_text(text, encoding="utf-8")
        return path

    def index(self, side: Path, *names: tuple[str, int, int]) -> dict[str, FileMeta]:
        return {
            name: meta(side / name, mtime_ns, size) for name, mtime_ns, size in names
        }

    def manifest(self, **entries: ManifestEntry) -> SyncManifest:
        return SyncManifest(data_version=1, files=dict(entries))

    @staticmethod
    def entry(local: tuple[int, int] | None, cloud: tuple[int, int] | None) -> ManifestEntry:
        return ManifestEntry(
            local=SnapshotFingerprint(mtime_ns=local[0], size=local[1]) if local else None,
            cloud=SnapshotFingerprint(mtime_ns=cloud[0], size=cloud[1]) if cloud else None,
        )


class DirectionTests(_Case):
    def _both_sides_changed_differently(self):
        local = self.index(self.local, ("a.txt", 3_000, 3), ("b.txt", 1_000, 1))
        cloud = self.index(self.cloud, ("a.txt", 1_000, 1), ("b.txt", 3_000, 3))
        return local, cloud

    def test_bidirectional_is_unchanged(self) -> None:
        local, cloud = self._both_sides_changed_differently()
        plan = build_sync_plan(local, cloud, self.local, self.cloud)
        self.assertEqual([item.relative_path for item in plan.to_cloud], ["a.txt"])
        self.assertEqual([item.relative_path for item in plan.to_local], ["b.txt"])
        self.assertEqual(plan.skipped, [])

    def test_to_cloud_writes_nothing_into_the_local_side(self) -> None:
        local, cloud = self._both_sides_changed_differently()
        plan = build_sync_plan(local, cloud, self.local, self.cloud, direction="to_cloud")
        self.assertEqual(plan.to_local, [])
        self.assertEqual([item.relative_path for item in plan.to_cloud], ["a.txt"])
        self.assertEqual(plan.skipped, ["b.txt"], "and it remembers what it did not do")

    def test_to_local_is_the_mirror_image(self) -> None:
        local, cloud = self._both_sides_changed_differently()
        plan = build_sync_plan(local, cloud, self.local, self.cloud, direction="to_local")
        self.assertEqual(plan.to_cloud, [])
        self.assertEqual([item.relative_path for item in plan.to_local], ["b.txt"])
        self.assertEqual(plan.skipped, ["a.txt"])

    def test_an_unknown_direction_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(ValueError):
            build_sync_plan({}, {}, self.local, self.cloud, direction="sideways")

    def test_a_conflict_stays_a_conflict_in_a_one_way_run(self) -> None:
        """The direction says what may be written, not what counts as agreement."""
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("a.txt", 5_000, 5))
        cloud = self.index(self.cloud, ("a.txt", 7_000, 7))
        plan = build_sync_plan(
            local, cloud, self.local, self.cloud, previous_manifest=previous,
            conflict_policy="manual_abort", direction="to_cloud",
        )
        self.assertEqual(plan.conflicts, ["a.txt"])
        self.assertEqual(plan.to_cloud, [])

    def test_the_skipped_side_keeps_its_old_manifest_entry(self) -> None:
        """The whole point: the next run must still see a difference here."""
        previous = self.manifest(**{"b.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("b.txt", 1_000, 1))
        cloud = self.index(self.cloud, ("b.txt", 3_000, 3))
        built = build_manifest(local, cloud, 1, previous=previous, skipped=["b.txt"])
        self.assertEqual(built.files["b.txt"], previous.files["b.txt"])

    def test_a_skipped_path_with_no_history_gets_no_entry_at_all(self) -> None:
        """"Never synchronised" is the truth, and it keeps the next run honest."""
        local = self.index(self.local, ("c.txt", 1_000, 1))
        cloud = self.index(self.cloud, ("c.txt", 3_000, 3))
        built = build_manifest(local, cloud, 1, previous=None, skipped=["c.txt"])
        self.assertNotIn("c.txt", built.files)

    def test_after_a_one_way_run_the_next_bidirectional_run_still_finds_the_change(self) -> None:
        """The regression this is all for, played out in full."""
        previous = self.manifest(**{"b.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("b.txt", 1_000, 1))
        cloud = self.index(self.cloud, ("b.txt", 3_000, 3))

        one_way = build_sync_plan(
            local, cloud, self.local, self.cloud, previous_manifest=previous, direction="to_cloud",
        )
        after = build_manifest(local, cloud, 1, previous=previous, skipped=one_way.skipped)

        later = build_sync_plan(local, cloud, self.local, self.cloud, previous_manifest=after)
        self.assertEqual([item.relative_path for item in later.to_local], ["b.txt"])

    def test_recording_the_skipped_side_would_have_lost_the_change(self) -> None:
        """The bug the carry-over prevents, played out so the rule cannot be dropped.

        The cloud side was edited by a machine whose clock is behind -- which
        is the case this project exists for. Its file is different and older.
        A manifest that recorded that state as "synchronised" leaves the next
        run with no evidence of a change on either side, so it falls back to
        mtime, prefers the local file, and overwrites the cloud edit.
        """
        previous = self.manifest(**{"b.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("b.txt", 1_000, 1))
        cloud = self.index(self.cloud, ("b.txt", 500, 5))

        naive = build_manifest(local, cloud, 1)
        lost = build_sync_plan(local, cloud, self.local, self.cloud, previous_manifest=naive)
        self.assertEqual([item.relative_path for item in lost.to_cloud], ["b.txt"])
        self.assertEqual(lost.to_local, [], "the cloud edit is about to be overwritten")

        one_way = build_sync_plan(
            local, cloud, self.local, self.cloud, previous_manifest=previous, direction="to_cloud",
        )
        carried = build_manifest(local, cloud, 1, previous=previous, skipped=one_way.skipped)
        kept = build_sync_plan(local, cloud, self.local, self.cloud, previous_manifest=carried)
        self.assertEqual([item.relative_path for item in kept.to_local], ["b.txt"])


class DeletePolicyTests(_Case):
    def test_never_copies_the_file_back_as_before(self) -> None:
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("a.txt", 1_000, 1))
        plan = build_sync_plan(local, {}, self.local, self.cloud, previous_manifest=previous)
        self.assertEqual([item.relative_path for item in plan.to_cloud], ["a.txt"])
        self.assertEqual(plan.deletions, [])

    def test_a_proven_deletion_removes_the_surviving_copy(self) -> None:
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("a.txt", 1_000, 1))
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=previous, delete_policy="propagate",
        )
        self.assertEqual([item.relative_path for item in plan.deletions], ["a.txt"])
        self.assertEqual(plan.deletions[0].side, "local")
        self.assertEqual(plan.to_cloud, [])

    def test_the_first_run_has_no_manifest_and_deletes_nothing(self) -> None:
        local = self.index(self.local, ("a.txt", 1_000, 1))
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=None, delete_policy="propagate",
        )
        self.assertEqual(plan.deletions, [])
        self.assertEqual([item.relative_path for item in plan.to_cloud], ["a.txt"])

    def test_a_path_the_manifest_never_saw_on_the_missing_side_is_not_a_deletion(self) -> None:
        """It was never there to delete: this is a new file, and it is copied."""
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), None)})
        local = self.index(self.local, ("a.txt", 1_000, 1))
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=previous, delete_policy="propagate",
        )
        self.assertEqual(plan.deletions, [])
        self.assertEqual([item.relative_path for item in plan.to_cloud], ["a.txt"])

    def test_a_side_edited_since_the_manifest_is_a_conflict_not_a_deletion(self) -> None:
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("a.txt", 9_000, 9))
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=previous, delete_policy="propagate",
        )
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.conflicts, ["a.txt"])

    def test_a_deletion_towards_the_skipped_side_is_dropped_by_the_direction(self) -> None:
        previous = self.manifest(**{"a.txt": self.entry((1_000, 1), (1_000, 1))})
        local = self.index(self.local, ("a.txt", 1_000, 1))
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=previous,
            delete_policy="propagate", direction="to_cloud",
        )
        self.assertEqual(plan.deletions, [], "to_cloud may not remove a local file")
        self.assertEqual(plan.skipped, ["a.txt"])


class DeleteExecutionTests(_Case):
    def _engine(self) -> tuple[SyncEngine, BackupManager]:
        manager = BackupManager(
            backup_root=self.root / "backups", machine_id="laptop",
            retention_days=30, max_backups=0, compression="none",
        )
        engine = SyncEngine(
            backup_manager=manager, temp_dir=self.root / "tmp",
            backup_before_overwrite=True, fail_on_unknown=True,
        )
        return engine, manager

    def _plan_for(self, path: Path):
        """A file both sides held and the other side deleted, unchanged here."""
        size = path.stat().st_size
        previous = self.manifest(**{"a.txt": self.entry((1_000, size), (1_000, size))})
        local = {"a.txt": meta(path, 1_000, size)}
        plan = build_sync_plan(
            local, {}, self.local, self.cloud, previous_manifest=previous, delete_policy="propagate",
        )
        assert [item.relative_path for item in plan.deletions] == ["a.txt"], plan
        return plan

    def test_a_dry_run_deletes_nothing(self) -> None:
        path = self.write(self.local, "a.txt", "keep me")
        engine, _ = self._engine()
        engine.execute(self._plan_for(path), dry_run=True)
        self.assertTrue(path.is_file())

    def test_the_backup_exists_before_the_file_is_removed(self) -> None:
        path = self.write(self.local, "a.txt", "the only copy")
        engine, manager = self._engine()
        engine.execute(self._plan_for(path), dry_run=False)
        self.assertFalse(path.exists(), "the deletion happened")
        copies = list((self.root / "backups").rglob("a.txt"))
        self.assertTrue(copies, "and the content is still somewhere")
        self.assertEqual(copies[0].read_text(encoding="utf-8"), "the only copy")

    def test_the_backup_manifest_records_the_deleted_file(self) -> None:
        """Which is what `recover --rollback` restores from."""
        import json

        path = self.write(self.local, "a.txt", "restore me")
        engine, _ = self._engine()
        engine.execute(self._plan_for(path), dry_run=False)
        manifests = list((self.root / "backups").rglob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        payload = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual([entry["relative_path"] for entry in payload["entries"]], ["a.txt"])
        self.assertTrue(payload["committed"])

    def test_the_gate_is_re_proved_before_each_removal(self) -> None:
        calls: list[str] = []
        path = self.write(self.local, "a.txt", "x")
        manager = BackupManager(
            backup_root=self.root / "backups", machine_id="laptop",
            retention_days=30, max_backups=0, compression="none",
        )
        engine = SyncEngine(
            backup_manager=manager, temp_dir=self.root / "tmp",
            backup_before_overwrite=True, fail_on_unknown=True,
            before_replace_check=lambda: calls.append("checked"),
        )
        engine.execute(self._plan_for(path), dry_run=False)
        self.assertEqual(calls, ["checked"])

    def test_a_file_that_cannot_be_backed_up_is_not_deleted(self) -> None:
        from codexsync.exceptions import FailSafeError
        from codexsync.models import DeleteAction, SyncPlan

        missing = self.local / "gone.txt"
        plan = SyncPlan(deletions=[DeleteAction(missing, "gone.txt", "local")])
        engine, _ = self._engine()
        with self.assertRaises(FailSafeError):
            engine.execute(plan, dry_run=False)


if __name__ == "__main__":
    unittest.main()
