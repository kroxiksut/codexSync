from __future__ import annotations

from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import ProcessSnapshot
from codexsync.preflight import run_preflight
from codexsync.work_scheduler import WorkScheduler

try:
    from tests.test_preflight import _write_config
except ImportError:
    from test_preflight import _write_config


class ParallelPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"parallel-preflight-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = _write_config(self.root)
        self.stopped = ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False)

    def _run(self, scheduler=None, facts=None):
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped):
            return run_preflight(self.config, facts=facts, scheduler=scheduler)

    def test_parallel_checks_overlap_within_injected_scheduler_bound_and_match_serial_report(self) -> None:
        serial_scheduler = WorkScheduler(io_workers=1)
        self.addCleanup(serial_scheduler.close)
        serial_facts = {}
        serial = self._run(serial_scheduler, serial_facts)
        entered = 0
        peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        from codexsync import preflight
        original = preflight._check_sync_rules
        original_registry = preflight._check_project_registry

        def overlapping(cfg):
            nonlocal entered, peak
            with lock:
                entered += 1
                peak = max(peak, entered)
            barrier.wait(timeout=3)
            try:
                return original(cfg)
            finally:
                with lock:
                    entered -= 1

        def overlapping_registry():
            nonlocal entered, peak
            with lock:
                entered += 1
                peak = max(peak, entered)
            barrier.wait(timeout=3)
            try:
                return original_registry()
            finally:
                with lock:
                    entered -= 1

        scheduler = WorkScheduler(io_workers=2)
        self.addCleanup(scheduler.close)
        parallel_facts = {}
        with patch("codexsync.preflight._check_sync_rules", side_effect=overlapping), patch(
            "codexsync.preflight._check_project_registry", side_effect=overlapping_registry,
        ):
            parallel = self._run(scheduler, parallel_facts)
        self.assertGreaterEqual(peak, 2, "independent observation checks must overlap")
        self.assertLessEqual(peak, 2, "work must stay within the injected scheduler bound")
        self.assertEqual(parallel.checks, serial.checks)
        for key in ("cfg", "local_dir", "cloud_dir", "catalog", "sandbox"):
            self.assertEqual(parallel_facts[key], serial_facts[key], f"report fact {key} changed")

    def test_result_order_does_not_follow_inverted_worker_completion(self) -> None:
        from codexsync import preflight
        original_rules, original_registry = preflight._check_sync_rules, preflight._check_project_registry
        done: list[str] = []

        def slow_rules(cfg):
            time.sleep(0.08)
            result = original_rules(cfg)
            done.append("rules")
            return result

        def fast_registry():
            result = original_registry()
            done.append("registry")
            return result

        scheduler = WorkScheduler(io_workers=4)
        self.addCleanup(scheduler.close)
        with patch("codexsync.preflight._check_sync_rules", side_effect=slow_rules), patch(
            "codexsync.preflight._check_project_registry", side_effect=fast_registry,
        ):
            report = self._run(scheduler)
        self.assertEqual(done[:2], ["registry", "rules"])
        names = [item.name for item in report.checks]
        self.assertLess(names.index("sync_rules"), names.index("project_registry"))

    def test_worker_exception_becomes_local_warn_and_other_checks_complete(self) -> None:
        scheduler = WorkScheduler(io_workers=3)
        self.addCleanup(scheduler.close)
        with patch("codexsync.preflight._check_sync_rules", side_effect=RuntimeError("worker broke")):
            report = self._run(scheduler)
        by_name = {item.name: item for item in report.checks}
        self.assertEqual(by_name["sync_rules"].status, "WARN")
        self.assertIn("worker broke", by_name["sync_rules"].details)
        self.assertEqual(by_name["config_compat"].status, "PASS")
        self.assertEqual(by_name["plugin_inventory"].status, "PASS")
        self.assertEqual(len(report.checks), 24)

    def test_preflight_observes_once_and_does_not_change_any_file_or_mtime(self) -> None:
        scheduler = WorkScheduler(io_workers=4)
        self.addCleanup(scheduler.close)

        def snapshot():
            return {p.relative_to(self.root).as_posix():
                    (p.is_dir(), p.stat().st_mtime_ns, None if p.is_dir() else p.read_bytes())
                    for p in self.root.rglob("*")}

        before = snapshot()
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped), patch(
            "codexsync.preflight.collect_preflight_observations",
            wraps=__import__("codexsync.preflight_observations", fromlist=["collect_preflight_observations"])
            .collect_preflight_observations,
        ) as collect:
            report = run_preflight(self.config, facts={}, scheduler=scheduler)
        self.assertEqual(len(report.checks), 24)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
