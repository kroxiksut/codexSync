"""Guardian store, pointer, lock and watcher faults found by the 2026-09-27 review.

Each test replays one finding (CS-297, CS-310, CS-313, CS-314 and the Guardian
items of CS-325) and fails on the code before its fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from codexsync import guardian_store
from codexsync.exceptions import GuardianBusyError, GuardianIntegrityError
from codexsync.guardian_lock import GuardianWriterLock
from codexsync.guardian_manifest import load_guardian_manifest, verify_guardian_snapshot
from codexsync.guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_PAYLOAD_NAME,
    GUARDIAN_QUARANTINE_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    GuardianConfig,
    GuardianResultStatus,
    SourceObservation,
    ValidationReport,
    ValidationStatus,
)
from codexsync.guardian_pointer import _write_pointer, resolve_or_restore_latest_good
from codexsync.guardian_retention import prune_uncommitted_snapshots
from codexsync.guardian_runner import GuardianRunner
from codexsync.guardian_store import GuardianStore


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


def _passing() -> ValidationReport:
    return ValidationReport(ValidationStatus.PASS, project_count=0, binding_count=0, schema_id="legacy-v1")


def _age(path: Path, seconds: float) -> None:
    """Move ``path`` and everything under it ``seconds`` into the past."""
    stamp = time.time() - seconds
    for item in sorted(path.rglob("*"), reverse=True):
        os.utime(item, (stamp, stamp))
    os.utime(path, (stamp, stamp))


class _StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # System temp like tests/test_guardian_store.py: snapshot ids are long.
        self.base = Path(tempfile.mkdtemp(prefix="cs-ghard-"))
        self.root = self.base / "guardian"
        self.store = GuardianStore(self.root, "machine-a", producer_version="test")

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def _commit(self, payload: bytes):
        result = self.store.commit(_observation(payload), _passing())
        self.assertEqual(result.status, GuardianResultStatus.COMMITTED)
        assert result.snapshot is not None
        return result.snapshot

    def _committed_generations(self) -> list[int]:
        machine_dir = self.root / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a"
        return sorted(
            load_guardian_manifest(directory / "manifest.json").generation
            for directory in machine_dir.iterdir()
            if (directory / GUARDIAN_COMMITTED_NAME).is_file()
        )


class GenerationNumberingTests(_StoreTestCase):
    """CS-297: a snapshot unreadable for a moment must not give its number away."""

    def test_an_unreadable_manifest_fails_the_commit_instead_of_reusing_its_number(self) -> None:
        self._commit(b'{"v":1}')
        self._commit(b'{"v":2}')
        third = self._commit(b'{"v":3}')
        locked = third.manifest_path
        original = Path.read_bytes

        def held_by_cloud_client(path: Path) -> bytes:
            if Path(path) == locked:
                raise PermissionError(13, "The process cannot access the file")
            return original(path)

        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=held_by_cloud_client):
            with self.assertRaises(GuardianIntegrityError):
                self.store.commit(_observation(b'{"v":4}'), _passing())

        # Readable again: the next commit numbers past it and the store stays usable.
        fourth = self._commit(b'{"v":4}')
        self.assertEqual(fourth.generation, 4)
        self.assertEqual(self._committed_generations(), [1, 2, 3, 4])
        fifth = self._commit(b'{"v":5}')
        self.assertEqual(fifth.generation, 5)
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), fifth)

    def test_a_store_already_holding_duplicate_generations_recovers_through_its_pointer(self) -> None:
        """Stores the old numbering broke: two committed snapshots share the top number."""
        first = self._commit(b'{"v":1}')
        second = self._commit(b'{"v":2}')
        # Replay the old bug: a second snapshot numbered 2 beside the first.
        clone_dir = second.directory.with_name(second.snapshot_id.replace(second.snapshot_id[-12:], "0" * 12))
        shutil.copytree(second.directory, clone_dir)
        manifest = json.loads((clone_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["snapshot_id"] = clone_dir.name
        (clone_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        marker = json.loads((clone_dir / GUARDIAN_COMMITTED_NAME).read_text(encoding="utf-8"))
        marker["snapshot_id"] = clone_dir.name
        (clone_dir / GUARDIAN_COMMITTED_NAME).write_text(json.dumps(marker), encoding="utf-8")
        self.assertEqual(self._committed_generations(), [1, 2, 2])

        third = self._commit(b'{"v":3}')

        self.assertEqual(third.generation, 3)
        self.assertEqual(load_guardian_manifest(third.manifest_path).previous_good_snapshot_id, second.snapshot_id)
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), third)
        self.assertTrue(first.directory.is_dir())

    def test_a_manifest_that_is_not_json_is_named_and_refused(self) -> None:
        self._commit(b'{"v":1}')
        second = self._commit(b'{"v":2}')
        second.manifest_path.write_bytes(b"\x00garbage")

        with self.assertRaisesRegex(GuardianIntegrityError, second.snapshot_id):
            self.store.commit(_observation(b'{"v":3}'), _passing())


class BaselineAndDuplicateTests(_StoreTestCase):
    def test_a_state_equal_to_an_older_snapshot_becomes_a_new_generation(self) -> None:
        """After `guardian restore`, the file holds an older snapshot's bytes (CS-325).

        Answering UNCHANGED kept latest-good on a state the file no longer had,
        so every later shrink was judged against the wrong baseline.
        """
        first = self._commit(b'{"v":1}')
        self._commit(b'{"v":2}')

        again = self.store.commit(_observation(b'{"v":1}'), _passing())

        self.assertEqual(again.status, GuardianResultStatus.COMMITTED)
        assert again.snapshot is not None
        self.assertNotEqual(again.snapshot, first)
        self.assertEqual(again.snapshot.generation, 3)
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), again.snapshot)
        # Equal to latest-good itself is still UNCHANGED.
        self.assertEqual(
            self.store.commit(_observation(b'{"v":1}'), _passing()).status, GuardianResultStatus.UNCHANGED
        )

    def test_the_predecessor_is_latest_good_not_the_highest_generation(self) -> None:
        """What a new snapshot was judged against is what its manifest must name (CS-325).

        Retention keeps an accepted snapshot's predecessor as the baseline a
        person overrode; naming the highest generation instead let the real
        baseline be pruned.
        """
        first = self._commit(b'{"v":1}')
        second = self._commit(b'{"v":2}')
        _write_pointer(self.root.resolve(), first)  # latest-good lags, as after a failed pointer update
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), first)

        third = self._commit(b'{"v":3}')

        manifest = verify_guardian_snapshot(third)
        self.assertEqual(manifest.previous_good_snapshot_id, first.snapshot_id)
        self.assertGreater(third.generation, second.generation)
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), third)


class PointerTests(_StoreTestCase):
    def test_a_pointer_naming_another_machine_is_not_this_machines_baseline(self) -> None:
        other = GuardianStore(self.root, "machine-b", producer_version="test")
        result = other.commit(_observation(b'{"v":"b"}'), _passing())
        assert result.snapshot is not None
        pointers = self.root / "latest-good"
        shutil.copyfile(pointers / "machine-b.json", pointers / "machine-a.json")

        self.assertIsNone(resolve_or_restore_latest_good(self.root, "machine-a"))
        mine = self._commit(b'{"v":"a"}')
        self.assertEqual(mine.generation, 1)
        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), mine)

    def test_a_pointer_with_a_non_string_machine_id_is_treated_as_damaged(self) -> None:
        snapshot = self._commit(b'{"v":1}')
        pointer = self.root / "latest-good" / "machine-a.json"
        raw = json.loads(pointer.read_text(encoding="utf-8"))
        raw["machine_id"] = 7
        pointer.write_text(json.dumps(raw), encoding="utf-8")

        self.assertEqual(resolve_or_restore_latest_good(self.root, "machine-a"), snapshot)


class UncommittedSnapshotTests(_StoreTestCase):
    """CS-310: short marker name; published-but-uncommitted directories are swept."""

    def _crash_after_publish(self, payload: bytes) -> Path:
        def power_loss(stage: str) -> None:
            if stage == "snapshot_published":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.store.commit(_observation(payload), _passing(), fault_hook=power_loss)
        machine_dir = self.root / GUARDIAN_SNAPSHOTS_DIR_NAME / "machine-a"
        return next(d for d in machine_dir.iterdir() if not (d / GUARDIAN_COMMITTED_NAME).exists())

    def test_the_committed_marker_is_never_the_longest_name_in_a_snapshot(self) -> None:
        written: list[str] = []
        original = guardian_store._write_private_file

        def spy(path: Path, data: bytes) -> None:
            written.append(path.name)
            original(path, data)

        with mock.patch.object(guardian_store, "_write_private_file", side_effect=spy):
            self._commit(b'{"v":1}')
        marker_names = [name for name in written if GUARDIAN_COMMITTED_NAME in name]
        self.assertTrue(marker_names)
        self.assertLessEqual(max(len(name) for name in marker_names), len(GUARDIAN_PAYLOAD_NAME))

    def test_an_old_uncommitted_directory_is_swept_by_the_next_commit(self) -> None:
        self._commit(b'{"v":1}')
        leftover = self._crash_after_publish(b'{"v":2}')
        _age(leftover, 2 * 24 * 3600)

        self._commit(b'{"v":3}')

        self.assertFalse(leftover.exists())
        # Swept before numbering: a number no committed snapshot ever held is free.
        self.assertEqual(self._committed_generations(), [1, 2])

    def test_a_young_foreign_or_committed_directory_is_left_alone(self) -> None:
        committed = self._commit(b'{"v":1}')
        young = self._crash_after_publish(b'{"v":2}')
        foreign = self._crash_after_publish(b'{"v":3}')
        (foreign / "notes.txt").write_text("mine", encoding="utf-8")
        _age(foreign, 2 * 24 * 3600)
        _age(committed.directory, 2 * 24 * 3600)
        oddly_named = committed.directory.parent / "not-a-snapshot-id"
        oddly_named.mkdir()
        _age(oddly_named, 2 * 24 * 3600)

        removed = prune_uncommitted_snapshots(self.root, "machine-a", retention_hours=24)

        self.assertEqual(removed, [])
        for kept in (committed.directory, young, foreign, oddly_named):
            self.assertTrue(kept.is_dir(), kept.name)


class QuarantineGrowthTests(_StoreTestCase):
    """CS-313: one drop keeps its first and newest event, not one copy per rewrite."""

    def _suspicious(self) -> ValidationReport:
        return ValidationReport(
            ValidationStatus.SUSPICIOUS, ("BINDING_COUNT_DROP",), project_count=3, binding_count=1, schema_id="electron-v2"
        )

    def _events(self) -> dict[str, dict]:
        base = self.root / GUARDIAN_QUARANTINE_DIR_NAME / "machine-a"
        return {
            d.name: json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            for d in base.iterdir()
        }

    def test_rewrites_of_one_drop_keep_two_events_and_count_the_rest(self) -> None:
        self._commit(b'{"baseline":true}')
        ids = []
        for number in range(6):
            # Codex rewrites window geometry: new bytes, same drop.
            result = self.store.quarantine(_observation(b'{"geometry":%d}' % number), self._suspicious())
            ids.append(result.quarantine_event_id)

        events = self._events()
        self.assertEqual(sorted(events), sorted([ids[0], ids[-1]]))
        newest = events[ids[-1]]
        self.assertEqual(newest["first_event_id"], ids[0])
        self.assertEqual(newest["occurrences"], 5)
        self.assertEqual(events[ids[0]]["occurrences"], 1)
        base = self.root / GUARDIAN_QUARANTINE_DIR_NAME / "machine-a"
        self.assertEqual(len(list(base.rglob("source.bin"))), 2)

    def test_a_different_reason_or_baseline_is_a_different_drop(self) -> None:
        self._commit(b'{"baseline":1}')
        self.store.quarantine(_observation(b'{"a":1}'), self._suspicious())
        self.store.quarantine(_observation(b'{"a":2}'), ValidationReport(ValidationStatus.INVALID, ("INVALID_JSON",)))
        self._commit(b'{"baseline":2}')
        self.store.quarantine(_observation(b'{"a":3}'), self._suspicious())

        self.assertEqual(len(self._events()), 3)


class WriterLockTests(unittest.TestCase):
    """CS-314: the lock never leaks a handle and never raises a raw OSError."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="cs-glock-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def test_a_lock_file_that_cannot_be_opened_is_busy(self) -> None:
        lock = GuardianWriterLock(self.base, "machine-a")
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "sharing violation")):
            with self.assertRaises(GuardianBusyError):
                lock.acquire()

    def test_a_failed_owner_record_releases_the_lock(self) -> None:
        lock = GuardianWriterLock(self.base, "machine-a")
        with mock.patch.object(GuardianWriterLock, "_write_metadata", side_effect=OSError(28, "disk full")):
            with self.assertRaises(GuardianIntegrityError):
                lock.acquire()
        # Nothing leaked: the same machine can take the lock again at once.
        with GuardianWriterLock(self.base, "machine-a"):
            pass


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WatchSurvivalTests(unittest.TestCase):
    """CS-314: a store error costs one step of `watch`, not the watcher."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="cs-gwatch-"))
        self.source = self.base / ".codex-global-state.json"
        self.source.write_text('{"local-projects": {}, "project-order": []}', encoding="utf-8")
        self.config = GuardianConfig(
            root_dir=self.base / "guardian",
            max_state_bytes=1024 * 1024,
            debounce_seconds=0.1,
            stable_read_interval_seconds=0.1,
            poll_interval_seconds=1.0,
            fallback_scan_seconds=600,
        )
        self.store = GuardianStore(self.config.root_dir, "machine-a", producer_version="test")
        self.clock = _Clock()

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def _watch_with_failures(self, *errors: BaseException):
        real_commit = self.store.commit
        remaining = list(errors)
        calls: list[int] = []

        def flaky(observation, validation, **kwargs):
            calls.append(1)
            if remaining:
                raise remaining.pop(0)
            return real_commit(observation, validation, **kwargs)

        runner = GuardianRunner(
            self.source, self.store, self.config, monotonic=self.clock.monotonic, sleep=self.clock.sleep
        )
        with mock.patch.object(self.store, "commit", side_effect=flaky):
            outcome = runner.watch(should_stop=lambda: self.clock.now > 120)
        return outcome, calls

    def test_store_errors_are_backed_off_and_the_watch_keeps_going(self) -> None:
        outcome, calls = self._watch_with_failures(
            GuardianIntegrityError("snapshot held by a cloud client"),
            PermissionError(13, "access denied"),
            GuardianBusyError("writer lock busy"),
        )

        self.assertEqual(len(calls), 4)
        self.assertIsNotNone(resolve_or_restore_latest_good(self.config.root_dir, "machine-a"))
        # Doubling back-off through the injected sleep, not a busy loop.
        self.assertIn(2.0, self.clock.sleeps)
        self.assertIn(4.0, self.clock.sleeps)
        self.assertIn(outcome.status, {GuardianResultStatus.COMMITTED, GuardianResultStatus.UNCHANGED})

    def test_source_gone_then_unreadable_metadata_backs_off(self) -> None:
        # CS-442: the signature taken while reporting a missing source raised
        # SourceUnstableError out of an except clause and ended the watch.
        from codexsync.stable_reader import SourceMissingError, SourceUnstableError

        runner = GuardianRunner(
            self.source, self.store, self.config, monotonic=self.clock.monotonic, sleep=self.clock.sleep
        )
        real_signature = runner.reader.signature
        calls = {"n": 0}

        def signature():
            calls["n"] += 1
            if calls["n"] > 1:
                raise SourceUnstableError("Cannot inspect Guardian source metadata")
            return real_signature()

        with mock.patch.object(runner.reader, "signature", side_effect=signature), \
                mock.patch.object(runner.reader, "read_stable", side_effect=SourceMissingError("gone")):
            outcome = runner._process_candidate(deadline=None, debounce=False)

        self.assertEqual(outcome.status, GuardianResultStatus.FAILED)
        self.assertIsNone(runner._last_signature)


if __name__ == "__main__":
    unittest.main()
