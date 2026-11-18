"""Parallel preparation must preserve a cold sync's plan and serial commit."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from threading import Barrier, Lock, get_ident
import unittest
from unittest.mock import patch
import uuid

from codexsync import app, planner, runtime
from codexsync.config import load_config
from codexsync.mutation_journal import JournalStore
from codexsync.sync_engine import SyncEngine
from codexsync.work_scheduler import WorkScheduler


class _StoppedGate:
    def require(self, *_args, **_kwargs):
        return None


class ParallelSyncPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"sync-prepare-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _case(self, name: str) -> Path:
        root = self.root / name
        for side in ("local", "cloud"):
            (root / side / "data").mkdir(parents=True)
            for index in range(6):
                path = root / side / "data" / f"{index}.txt"
                path.write_text(f"{side}-{index}", encoding="utf-8")
                # Equal size/time requires content comparison.
                os.utime(path, ns=(1_700_000_000_000_000_000,) * 2)
        (root / "cloud" / "data" / "a-from-cloud.txt").write_text("incoming", encoding="utf-8")
        (root / "local" / "data" / "z-from-local.txt").write_text("outgoing", encoding="utf-8")
        config = root / "config.toml"
        config.write_text(
            f'[identity]\nmachine_id = "test-machine"\n'
            '[sync]\ncompare = "mtime_hash_fallback"\nequal_mtime_action = "prefer_local"\n'
            f'[paths]\nlocal_state_dir = "{(root / "local").as_posix()}"\n'
            f'cloud_root_dir = "{(root / "cloud").as_posix()}"\n'
            f'backup_dir = "{(root / "backups").as_posix()}"\n'
            f'temp_dir = "{(root / "temp").as_posix()}"\n'
            '[targets]\ninclude_roots = ["data"]\n'
            f'[state]\nmanifest_file = "{(root / "manifest.json").as_posix()}"\n',
            encoding="utf-8",
        )
        # Test preparation after the ordinary first-run bootstrap; its
        # infrastructure is outside the mutation guarantee of this step.
        runtime.initialize_runtime_paths(load_config(config))
        return config

    def _context(self, config: Path, scheduler: WorkScheduler):
        with patch.object(app, "_make_safety_gate", return_value=_StoppedGate()):
            return app.build_context(config, scheduler=scheduler)

    def test_hash_reads_overlap_within_the_shared_worker_bound(self) -> None:
        config = self._case("overlap")
        barrier, lock = Barrier(2), Lock()
        active = maximum = 0
        caller = get_ident()
        workers: set[int] = set()
        real = planner._sha256
        scan_barrier = Barrier(2)
        real_scan = runtime.scan_tree

        def scan(*args, **kwargs):
            scan_barrier.wait(timeout=5)
            return real_scan(*args, **kwargs)

        def read(path):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                workers.add(get_ident())
            try:
                # A serial implementation cannot pass this rendezvous.
                barrier.wait(timeout=5)
                return real(path)
            finally:
                with lock:
                    active -= 1

        with WorkScheduler(io_workers=2, large_file_limit=2) as scheduler:
            with patch.object(planner, "_sha256", side_effect=read), patch.object(
                runtime, "scan_tree", side_effect=scan,
            ):
                ctx = self._context(config, scheduler)
            # The caller still owns an injected scheduler after planning.
            self.assertEqual(scheduler.map_read(lambda n: n + 1, [1]), [2])
        self.assertEqual(ctx.plan.action_count, 8)
        self.assertEqual(maximum, 2)
        self.assertNotIn(caller, workers)

    def test_one_and_many_workers_keep_plan_result_and_commit_order(self) -> None:
        observed = []
        caller = get_ident()
        real_replace = SyncEngine._replace_staged
        real_transition = JournalStore.transition
        for count in (1, 3):
            config = self._case(f"workers-{count}")
            events = []

            def replace(engine, action, *args, **kwargs):
                events.append(("replace", action.relative_path, get_ident()))
                return real_replace(engine, action, *args, **kwargs)

            def transition(store, journal, state, *args, **kwargs):
                events.append(("journal", state.value, get_ident()))
                return real_transition(store, journal, state, *args, **kwargs)

            with WorkScheduler(io_workers=count) as scheduler:
                ctx = self._context(config, scheduler)
            with WorkScheduler(io_workers=1) as scheduler:
                baseline = self._context(config, scheduler)
            # Same roots, same identities and all policy/precondition fields,
            # not merely equivalent counts from two separate test folders.
            self.assertEqual(ctx.plan, baseline.plan)
            with patch.object(SyncEngine, "_replace_staged", new=replace), patch.object(
                JournalStore, "transition", new=transition,
            ):
                app.run_sync(ctx, dry_run=False)
            plan = ([a.relative_path for a in ctx.plan.to_local],
                    [a.relative_path for a in ctx.plan.to_cloud],
                    ctx.plan.conflicts, ctx.plan.skipped, ctx.plan.deletions)
            result = {p.relative_to(ctx.cloud_dir).as_posix(): p.read_bytes()
                      for p in ctx.cloud_dir.rglob("*") if p.is_file()}
            self.assertTrue(all(thread == caller for _, _, thread in events))
            expected_paths = ["data/a-from-cloud.txt", *[f"data/{i}.txt" for i in range(6)],
                              "data/z-from-local.txt"]
            self.assertEqual(events, [
                ("journal", "BACKED_UP", caller), ("journal", "COMMITTING", caller),
                *(("replace", path, caller) for path in expected_paths),
                ("journal", "COMMITTED", caller),
            ])
            observed.append((plan, result))
        self.assertEqual(observed[0], observed[1])

    def test_worker_failure_reaches_caller_before_any_mutation_write(self) -> None:
        config = self._case("failure")
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        failure = OSError("cloud client interrupted a content read")
        real = planner._sha256
        threads = []

        def read(path):
            threads.append(get_ident())
            if path.name == "1.txt":
                raise failure
            return real(path)

        with WorkScheduler(io_workers=2) as scheduler:
            with patch.object(planner, "_sha256", side_effect=read):
                with self.assertRaises(OSError) as raised:
                    ctx = self._context(config, scheduler)
                    app.run_sync(ctx, dry_run=False)
        self.assertIs(raised.exception, failure)
        self.assertNotIn(get_ident(), threads)
        after = {p.relative_to(self.root): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

