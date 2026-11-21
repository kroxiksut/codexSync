"""An interrupted payload preparation must never become a partial sync."""
from __future__ import annotations

import os
import errno
from pathlib import Path
import shutil
from threading import Barrier, Event, Lock, get_ident
import unittest
from unittest.mock import patch
import uuid

from codexsync import app
from codexsync.backup import BackupManager
from codexsync.exceptions import FailSafeError, SafetyPreconditionError
from codexsync.models import DeleteAction, SyncPlan
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.sync_engine import STAGE_SUFFIX, SyncEngine
from codexsync.work_scheduler import WorkScheduler


class _StoppedGate:
    def require(self, *_args, **_kwargs):
        return None


class ParallelSyncStageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / "test-sandbox" / f"parallel-stage-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        for side in ("local", "cloud"):
            (self.root / side / "data").mkdir(parents=True)
            for index in range(4):
                path = self.root / side / "data" / f"{index}.txt"
                path.write_bytes(f"{side}-{index}".encode())
                os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)
        config = self.root / "config.toml"
        self.config = config
        config.write_text(
            '[identity]\nmachine_id = "stage-test"\n'
            '[sync]\ncompare = "mtime_hash_fallback"\nequal_mtime_action = "prefer_local"\n'
            '[paths]\n' + "".join(
                f'{key} = "{(self.root / folder).as_posix()}"\n' for key, folder in (
                    ("local_state_dir", "local"), ("cloud_root_dir", "cloud"),
                    ("backup_dir", "backups"), ("temp_dir", "temp"),
                )
            ) + '[targets]\ninclude_roots = ["data"]\n'
            f'[state]\nmanifest_file = "{(self.root / "manifest.json").as_posix()}"\n',
            encoding="utf-8",
        )
        with patch.object(app, "_make_safety_gate", return_value=_StoppedGate()):
            self.ctx = app.build_context(config)
        self.before = self._targets()
        self.manifest_before = (self.root / "manifest.json").read_bytes()

    def _targets(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for side in ("local", "cloud") for p in (self.root / side).rglob("*.txt")}

    def _apply(self, workers=2, large=2):
        with WorkScheduler(io_workers=workers, large_file_limit=large) as scheduler:
            # Exercise default ownership too: run_sync must create one scheduler
            # for the entire operation, rather than an executor per payload.
            with patch.object(app, "WorkScheduler", return_value=scheduler) as factory:
                app.run_sync(self.ctx, dry_run=False)
                self.assertEqual(factory.call_count, 1)

    def _journal(self):
        paths = list((self.root / "temp" / "journals").glob("*.json"))
        self.assertEqual(len(paths), 1)
        return JournalStore(self.root / "temp").load(paths[0].stem)

    def _unchanged_failure(self, state=JournalState.FAILED):
        self.assertEqual(self._targets(), self.before)
        if os.name == "nt":
            self.assertFalse(list(self.root.rglob(f"*{STAGE_SUFFIX}")))
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.manifest_before)
        self.assertEqual(self._journal().state, state)

    def test_payloads_overlap_but_backup_and_commit_stay_on_caller_in_plan_order(self):
        rendezvous, lock = Barrier(2), Lock()
        active = maximum = 0
        caller, stage_threads, serial = get_ident(), set(), []
        real_stage, real_replace = SyncEngine._stage_verified, SyncEngine._replace_staged
        real_backup, real_finalize = BackupManager.backup_file, BackupManager.finalize
        real_transition = JournalStore.transition

        def stage(*args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                stage_threads.add(get_ident())
            try:
                rendezvous.wait(timeout=5)
                return real_stage(*args)
            finally:
                with lock:
                    active -= 1

        def replace(engine, action, *args):
            serial.append(("replace", action.relative_path, get_ident()))
            return real_replace(engine, action, *args)

        def backup(manager, path, rel, **kwargs):
            serial.append(("backup", rel, get_ident()))
            return real_backup(manager, path, rel, **kwargs)

        def finalize(manager):
            serial.append(("finalize", "", get_ident()))
            return real_finalize(manager)

        def transition(store, journal, state, **kwargs):
            serial.append(("journal", state.value, get_ident()))
            return real_transition(store, journal, state, **kwargs)

        with patch.object(SyncEngine, "_stage_verified", side_effect=stage), patch.object(
            SyncEngine, "_replace_staged", new=replace,
        ), patch.object(BackupManager, "backup_file", new=backup), patch.object(
            BackupManager, "finalize", new=finalize,
        ), patch.object(JournalStore, "transition", new=transition):
            self._apply()
        self.assertEqual(maximum, 2)
        self.assertNotIn(caller, stage_threads)
        self.assertTrue(all(thread == caller for _, _, thread in serial))
        paths = [a.relative_path for a in self.ctx.plan.to_cloud]
        self.assertEqual([(kind, path) for kind, path, _ in serial], [
            *(("backup", path) for path in paths), ("finalize", ""),
            ("journal", "BACKED_UP"), ("journal", "COMMITTING"),
            *(("replace", path) for path in paths), ("journal", "COMMITTED"),
        ])

    def test_large_file_budget_limits_simultaneous_payload_work(self):
        lock = Lock()
        active = maximum = 0
        real = SyncEngine._stage_verified

        def stage(*args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                self.assertNotEqual(get_ident(), caller)
                return real(*args)
            finally:
                with lock:
                    active -= 1

        caller = get_ident()
        with patch.object(SyncEngine, "_stage_verified", side_effect=stage):
            self._apply(workers=4, large=1)
        self.assertEqual(maximum, 1)

    def test_inverted_completion_never_reorders_replacements(self):
        second_done = Event()
        real = SyncEngine._stage_verified
        finished, replaced = [], []
        real_replace = SyncEngine._replace_staged

        def stage(source, *args):
            if source.name == "0.txt":
                self.assertTrue(second_done.wait(5), "second payload must finish while first waits")
            real(source, *args)
            finished.append(source.name)
            if source.name == "1.txt":
                second_done.set()

        def replace(engine, action, *args):
            replaced.append(action.src.name)
            return real_replace(engine, action, *args)

        with patch.object(SyncEngine, "_stage_verified", side_effect=stage), patch.object(
            SyncEngine, "_replace_staged", new=replace,
        ):
            self._apply()
        self.assertLess(finished.index("1.txt"), finished.index("0.txt"))
        self.assertEqual(replaced, [a.src.name for a in self.ctx.plan.to_cloud])

    def test_partial_worker_failure_joins_active_work_and_leaves_targets_and_foreign_stages(self):
        rendezvous = Barrier(2)
        release, joined = Event(), Event()
        started = []
        stale = self.root / "cloud" / "data" / ".foreign.old.codexsync.tmp"
        stale.write_bytes(b"another operation")
        os.utime(stale, (1, 1))
        real = SyncEngine._stage_verified
        failure = OSError("cloud delivery interrupted the stage")

        def stage(source, staged, *args):
            started.append(source.name)
            rendezvous.wait(timeout=5)
            if source.name == "0.txt":
                staged.write_bytes(b"partial")
                release.set()
                raise failure
            self.assertTrue(release.wait(5))
            real(source, staged, *args)
            joined.set()

        with patch.object(SyncEngine, "_stage_verified", side_effect=stage):
            with self.assertRaises(OSError) as raised:
                self._apply()
        self.assertIs(raised.exception, failure)
        self.assertTrue(joined.is_set())
        self.assertEqual(set(started), {"0.txt", "1.txt"})
        self.assertEqual(stale.read_bytes(), b"another operation")
        stale.unlink()
        self._unchanged_failure()
        # A terminal preparation failure does not require rollback to retry.
        self._apply()

    def test_source_truncated_during_copy_is_refused_before_any_replace(self):
        from codexsync.sync_engine import _copy_raw_to_stage

        def copy(source, destination):
            _copy_raw_to_stage(source, destination)
            if source.name == "0.txt":
                Path(source).write_bytes(b"")

        with patch("codexsync.sync_engine._copy_raw_to_stage", side_effect=copy):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.before["local/data/0.txt"] = b""
        self._unchanged_failure()

    @unittest.skipUnless(os.name == "nt", "requires Windows alternate data streams")
    def test_a_file_with_an_alternate_stream_is_not_copied_as_just_its_main_stream(self):
        name = self.root / "local" / "data" / "0.txt"
        stream = Path(f"{name}:codexsync-test-stream")
        try:
            stream.write_bytes(b"non-default data")
        except OSError as exc:
            if getattr(exc, "winerror", None) in (1, 50, 123):
                self.skipTest("test volume does not support alternate data streams")
            raise

        with self.assertRaisesRegex(FailSafeError, "alternate data stream"):
            self._apply()
        self.assertEqual(stream.read_bytes(), b"non-default data")
        self._unchanged_failure()

    def _run_destination_stream_case(self, *, delete=False, after_backup=False):
        if os.name != "nt":
            self.skipTest("requires Windows alternate data streams")
        target = self.root / "cloud" / "data" / "0.txt"
        stream = Path(f"{target}:codexsync-test-stream")
        payload = b"information a ZIP backup cannot retain"
        self.ctx.config.backup.compression = "zip"
        if delete:
            self.ctx.plan = SyncPlan(deletions=[DeleteAction(target, "data/0.txt", "cloud")])

        def deliver():
            info = target.stat()
            try:
                stream.write_bytes(payload)
            except OSError as exc:
                if getattr(exc, "winerror", None) in (1, 50, 123):
                    self.skipTest("test volume does not support alternate data streams")
                raise
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

        if after_backup:
            class Gate:
                final_checks = 0

                def require(self, *_args, final=False):
                    if final:
                        self.final_checks += 1
                        if self.final_checks == 2:
                            deliver()

            self.ctx.safety_gate = Gate()
        else:
            deliver()

        with self.assertRaisesRegex(FailSafeError, "alternate data stream"):
            self._apply()
        self.assertEqual(stream.read_bytes(), payload)
        if not after_backup:
            self.assertFalse(list((self.root / "backups").glob("*.zip")))
        self._unchanged_failure(JournalState.RECOVERY_REQUIRED if after_backup else JournalState.FAILED)

    def test_destination_stream_refuses_overwrite_before_zip_backup(self):
        self._run_destination_stream_case()

    def test_destination_stream_refuses_delete_before_zip_backup(self):
        self._run_destination_stream_case(delete=True)

    def test_destination_stream_delivered_at_the_gate_refuses_overwrite(self):
        self._run_destination_stream_case(after_backup=True)

    def test_destination_stream_delivered_at_the_gate_refuses_delete(self):
        self._run_destination_stream_case(delete=True, after_backup=True)

    def test_source_changed_after_its_worker_finishes_is_refused_after_join(self):
        real = SyncEngine._stage_verified
        first_done = Event()

        def stage(source, *args):
            if source.name == "1.txt":
                self.assertTrue(first_done.wait(5))
            real(source, *args)
            if source.name == "0.txt":
                first_done.set()
            if source.name == "1.txt":
                (source.parent / "0.txt").write_bytes(b"late-source")

        with patch.object(SyncEngine, "_stage_verified", side_effect=stage):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.before["local/data/0.txt"] = b"late-source"
        self._unchanged_failure()

    def test_destination_delivered_during_stage_is_preserved(self):
        real = SyncEngine._stage_verified

        def stage(source, *args):
            real(source, *args)
            if source.name == "0.txt":
                (self.root / "cloud" / "data" / "0.txt").write_bytes(b"cloud-new")

        with patch.object(SyncEngine, "_stage_verified", side_effect=stage):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.before["cloud/data/0.txt"] = b"cloud-new"
        self._unchanged_failure()

    def test_codex_starts_after_staging_refuses_commit(self):
        class Gate:
            def require(_self, *_args, **kwargs):
                if kwargs.get("final"):
                    raise SafetyPreconditionError("Codex started")

        self.ctx.safety_gate = Gate()
        with self.assertRaises(SafetyPreconditionError):
            self._apply()
        self._unchanged_failure()

    def test_existing_stage_is_never_overwritten_or_cleaned(self):
        occupied = self.root / "cloud" / "data" / ".existing.codexsync.tmp"
        occupied.write_bytes(b"foreign")
        with patch("codexsync.sync_engine._stage_path", return_value=occupied):
            with self.assertRaises(FileExistsError):
                self._apply()
        self.assertEqual(occupied.read_bytes(), b"foreign")
        occupied.unlink()
        self._unchanged_failure()

    def test_cloud_renames_stage_and_delivers_identical_foreign_copy_is_refused(self):
        real = SyncEngine._prepare_copy
        moved, foreign = [], []

        def stage(engine, item):
            result = real(engine, item)
            source, staged = result.action.src, result.path
            if source.name == "0.txt":
                renamed = staged.with_suffix(".cloud-renamed")
                staged.rename(renamed)
                moved.append(renamed)
                staged.write_bytes(source.read_bytes())
                foreign.append(staged)
            return result

        with patch.object(SyncEngine, "_prepare_copy", new=stage):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.assertEqual(foreign[0].read_bytes(), b"local-0")
        foreign[0].unlink()
        moved[0].unlink()
        self._unchanged_failure()

    def test_one_and_many_workers_keep_identical_plan_bytes_manifest_and_progress(self):
        observed = []
        for workers in (1, 3):
            # Restore the identical input bytes/times; a fresh scan records
            # whichever file identity the previous atomic replace left here.
            for relative, content in self.before.items():
                path = self.root / relative
                path.write_bytes(content)
                os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)
            (self.root / "manifest.json").write_bytes(self.manifest_before)
            with patch.object(app, "_make_safety_gate", return_value=_StoppedGate()):
                with WorkScheduler(io_workers=1) as serial:
                    one = app.build_context(self.config, scheduler=serial)
                with WorkScheduler(io_workers=3) as many:
                    self.ctx = app.build_context(self.config, scheduler=many)
            self.assertEqual(one.plan, self.ctx.plan)
            progress = []
            with WorkScheduler(io_workers=workers) as scheduler:
                app.run_sync(self.ctx, dry_run=False, scheduler=scheduler, progress=lambda d, t: progress.append((d, t, get_ident())))
                self.assertEqual(scheduler.map_read(lambda n: n + 1, [1]), [2])
            observed.append((
                app._plan_hash(self.ctx.plan), self._targets(),
                (self.root / "manifest.json").read_bytes(), progress,
            ))
        self.assertEqual(observed[0], observed[1])
        self.assertTrue(all(thread == get_ident() for _, _, thread in observed[0][-1]))

    def test_same_size_source_edit_with_restored_mtime_after_first_stage_is_refused(self):
        source = self.root / "local" / "data" / "0.txt"
        first_done = Event()
        real = SyncEngine._prepare_copy

        def prepare(engine, item):
            if item[0].src.name == "1.txt":
                self.assertTrue(first_done.wait(5))
            result = real(engine, item)
            if item[0].src.name == "0.txt":
                first_done.set()
            if item[0].src.name == "1.txt":
                source.write_bytes(b"changed")
                os.utime(source, ns=(1_700_000_000_000_000_000,) * 2)
            return result

        with patch.object(SyncEngine, "_prepare_copy", new=prepare):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.before["local/data/0.txt"] = b"changed"
        self._unchanged_failure()

    def test_transient_target_lock_retries_on_caller_with_the_same_payload(self):
        real = os.replace
        target = self.root / "cloud" / "data" / "0.txt"
        attempts = []

        def replace(source, destination):
            if Path(destination) == target:
                attempts.append((Path(source).read_bytes(), get_ident()))
                if len(attempts) == 1:
                    raise PermissionError(errno.EACCES, "cloud upload holds the target")
            return real(source, destination)

        with patch("codexsync.fs_replace.os.replace", side_effect=replace), patch("codexsync.fs_replace.time.sleep"):
            self._apply()
        self.assertEqual(attempts, [(b"local-0", get_ident())] * 2)
        self.assertEqual(target.read_bytes(), b"local-0")
        self.assertEqual(self._journal().state, JournalState.COMMITTED)

    def test_cloud_renames_target_during_lock_retry_preserves_delivered_file(self):
        real = os.replace
        target = self.root / "cloud" / "data" / "0.txt"
        saved = target.with_suffix(".renamed")
        attempts = []

        def replace(source, destination):
            if Path(destination) == target:
                attempts.append(get_ident())
                target.rename(saved)
                target.write_bytes(b"delivered")
                raise PermissionError(errno.EACCES, "cloud rename holds the target")
            return real(source, destination)

        with patch("codexsync.fs_replace.os.replace", side_effect=replace), patch("codexsync.fs_replace.time.sleep"):
            with self.assertRaises(FailSafeError):
                self._apply()
        self.assertEqual(attempts, [get_ident()])
        self.assertEqual(saved.read_bytes(), b"cloud-0")
        self.before["cloud/data/0.txt"] = b"delivered"
        self._unchanged_failure(JournalState.RECOVERY_REQUIRED)

    def test_failed_terminal_journal_write_after_worker_failure_self_closes_on_retry(self):
        real_transition = JournalStore.transition
        failure = OSError("worker lost its input")

        def transition(store, journal, state, **kwargs):
            if state is JournalState.FAILED:
                raise PermissionError("cloud client locked the terminal journal")
            return real_transition(store, journal, state, **kwargs)

        with patch.object(SyncEngine, "_stage_verified", side_effect=failure), patch.object(
            JournalStore, "transition", new=transition,
        ):
            with self.assertRaises(OSError) as raised:
                self._apply()
        self.assertIs(raised.exception, failure)
        self._unchanged_failure(JournalState.PREPARED)
        stuck_id = self._journal().operation_id
        self._apply()
        self.assertEqual(JournalStore(self.root / "temp").load(stuck_id).state, JournalState.FAILED)

    def test_codex_starting_after_precommit_gate_refuses_first_replace(self):
        running = False
        real_replace = SyncEngine._replace_staged

        class Gate:
            def require(_self, *_args, **kwargs):
                if kwargs.get("final") and running:
                    raise SafetyPreconditionError("Codex started after the precommit gate")

        def replace(engine, *args):
            nonlocal running
            running = True
            return real_replace(engine, *args)

        self.ctx.safety_gate = Gate()
        with patch.object(SyncEngine, "_replace_staged", new=replace):
            with self.assertRaises(SafetyPreconditionError):
                self._apply()
        self._unchanged_failure(JournalState.RECOVERY_REQUIRED)

    def test_cloud_lock_on_owned_stage_cleanup_does_not_mask_worker_failure(self):
        failure = OSError("source copy interrupted")

        with patch.object(SyncEngine, "_stage_verified", side_effect=failure), patch(
            "codexsync.sync_engine._discard_windows_stage", side_effect=PermissionError("cloud client holds operation stage"),
        ):
            with self.assertRaises(OSError) as raised:
                self._apply()
        self.assertIs(raised.exception, failure)
        self.assertEqual(self._targets(), self.before)
        self.assertEqual(self._journal().state, JournalState.FAILED)
        # Only the operation's stages remain, for inspection after the lock
        # clears; no broad orphan sweep may remove another operation's file.
        leftovers = list(self.root.rglob(f"*{STAGE_SUFFIX}"))
        self.assertTrue(leftovers)
        for path in leftovers:
            path.unlink()

    def test_stage_cleanup_never_unlinks_a_path_that_can_be_swapped_after_validation(self):
        real_unlink = Path.unlink
        unsafe_attempts = []
        failure = OSError("staging failed before commit")

        def deliver_on_path_unlink(path, *args, **kwargs):
            if path.name.endswith(STAGE_SUFFIX) and path.exists():
                path.rename(path.with_suffix(".saved"))
                path.write_bytes(b"foreign stage bytes")
                unsafe_attempts.append(path)
            return real_unlink(path, *args, **kwargs)

        with patch.object(SyncEngine, "_stage_verified", side_effect=failure), patch.object(
            Path, "unlink", new=deliver_on_path_unlink,
        ):
            with self.assertRaises(OSError):
                self._apply()
        self.assertEqual(unsafe_attempts, [], "path unlink could delete a replacement, despite an earlier valid stat")
        self.assertEqual(self._targets(), self.before)

    def test_source_and_stage_reverification_overlap_without_caller_payload_reads(self):
        rendezvous = Barrier(2)
        real_verify = SyncEngine._verify_prepared
        from codexsync import sync_engine
        real_hash = sync_engine._sha256_file
        caller = get_ident()

        def verify(prepared):
            rendezvous.wait(timeout=5)
            return real_verify(prepared)

        def digest(path):
            if path.name.endswith(STAGE_SUFFIX) or path.parent == self.root / "local" / "data":
                self.assertNotEqual(get_ident(), caller, "payload proof belongs to bounded workers")
            return real_hash(path)

        with patch.object(SyncEngine, "_verify_prepared", side_effect=verify), patch.object(
            sync_engine, "_sha256_file", side_effect=digest,
        ):
            self._apply()

    def test_stage_stat_failure_cleans_the_exact_successful_reservation(self):
        from codexsync import sync_engine
        real_stamp = sync_engine._stage_stamp_at
        inspected = []
        failure = OSError("cloud client denied staging metadata")

        def stamp(path):
            if path.name.endswith(STAGE_SUFFIX) and not inspected:
                inspected.append(path)
                raise failure
            return real_stamp(path)

        with patch.object(sync_engine, "_stage_stamp_at", side_effect=stamp):
            with self.assertRaises(OSError) as raised:
                self._apply()
        self.assertIs(raised.exception, failure)
        self._unchanged_failure()
