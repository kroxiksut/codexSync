"""A moving Codex state must not give one doctor report two different answers."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
import shutil
import sys
import sqlite3
import unittest
from unittest.mock import patch
import uuid
from types import SimpleNamespace

from codexsync.app import ProcessSnapshot
from codexsync.codex_sandbox import SandboxReport
from codexsync.config import load_config
from codexsync.preflight import (
    _check_codex_compat, _check_codex_startup, _check_global_state_schema, _check_process_state, _project_roots, run_preflight,
)
from codexsync.codex_health import Severity
from codexsync.preflight_observations import GlobalStateStatus, observe_global_state
from codexsync.process_detector import ProcessInfo
from codexsync.safety_gate import OperationKind, ProcessState, SafetyGate
from codexsync.sqlite_audit import BackfillReading, PlacementStatus

try:
    from tests.test_preflight import _write_config
except ImportError:
    from test_preflight import _write_config


class PreflightObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"preflight-observations-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = _write_config(self.root)
        self.state = self.root / "local-state" / ".codex-global-state.json"
        self.payload = json.dumps({
            "local-projects": {"p": {"id": "p", "name": "project", "rootPaths": ["C:/original"]}},
            "project-order": ["p"],
        }).encode()
        self.state.write_bytes(self.payload)
        self.stopped = ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False)

    def test_global_state_replaced_between_checks_is_read_once_for_every_consumer(self) -> None:
        """A cloud replacement must not make sandbox and schema diagnose different projects."""
        original_read = Path.read_bytes
        reads = []

        def changing_read(path):
            if path == self.state:
                reads.append(path)
                return self.payload if len(reads) == 1 else b'{"unexpected": true}'
            return original_read(path)

        with patch.object(Path, "read_bytes", changing_read), patch(
            "codexsync.runtime.collect_process_snapshot", return_value=self.stopped,
        ), patch(
            "codexsync.preflight.check_sandbox_folders", return_value=SandboxReport(applicable=False),
        ) as sandbox:
            report = run_preflight(self.config)

        schema = next(item for item in report.checks if item.name == "global_state_schema")
        self.assertEqual(schema.status, "PASS", "every consumer must use the first captured bytes")
        self.assertEqual(len(reads), 1, "the global state has one source boundary per run")
        self.assertEqual(sandbox.call_args.args[1], ["C:/original"])

    def test_codex_exiting_between_checks_does_not_make_one_report_call_a_live_rebuild_stuck(self) -> None:
        """The process line and startup finding must use the same observation."""
        running = ProcessSnapshot(
            main_processes=[ProcessInfo(pid=123, name="codex.exe")],
            subprocesses=[], sandbox_detected=False,
        )
        reading = BackfillReading(PlacementStatus.AVAILABLE, "state_5.sqlite", "running")
        # This test exercises observation reuse, not the platform proof gate.
        # macOS deliberately reports UNKNOWN until its detector is proven, so
        # mark the current synthetic detector as proven for this one scenario.
        with patch.dict("codexsync.process_detector.PROVEN_DETECTORS", {sys.platform: "test fixture"}), patch(
            "codexsync.runtime.collect_process_snapshot", side_effect=[running, self.stopped],
        ) as process, patch("codexsync.preflight.read_backfill_state", return_value=reading), patch(
            "codexsync.preflight.diagnose_catalogue", return_value=[],
        ) as diagnose:
            report = run_preflight(self.config)

        by_name = {item.name: item for item in report.checks}
        self.assertEqual(by_name["codex_process"].status, "WARN")
        self.assertIs(diagnose.call_args.args[1], ProcessState.RUNNING)
        self.assertEqual(process.call_count, 1)

    def test_stopped_codex_startup_finding_has_translatable_part(self) -> None:
        finding = SimpleNamespace(code="CATALOGUE_REBUILD_STUCK", counts={"chats": 2}, severity=Severity.BROKEN)
        reading = BackfillReading(PlacementStatus.AVAILABLE, "state_5.sqlite", "running")
        with patch("codexsync.preflight.read_backfill_state", return_value=reading), patch(
            "codexsync.preflight.diagnose_catalogue", return_value=[finding],
        ):
            result = _check_codex_startup(load_config(self.config), self.state.parent, process=ProcessState.STOPPED)
        self.assertEqual(result.name, "codex_startup")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.parts, (
            ("codex_startup.finding", {"code": finding.code, "chats": 2}),
            ("codex_startup.broken", {}),
        ))

    def test_issue_report_counts_the_state_that_its_checks_actually_diagnosed(self) -> None:
        from codexsync.issue_report import build_issue_report

        original_read = Path.read_bytes
        reads = []

        def changing_read(path):
            if path == self.state:
                reads.append(path)
                return self.payload if len(reads) == 1 else b'{"unexpected": true}'
            return original_read(path)

        with patch.object(Path, "read_bytes", changing_read), patch(
            "codexsync.runtime.collect_process_snapshot", return_value=self.stopped,
        ), patch("codexsync.issue_report._process", return_value={}):
            report = build_issue_report(self.config)

        self.assertEqual(report["checks"]["global_state_schema"], "PASS")
        self.assertEqual(report["global_state"]["projects"], 1)
        self.assertEqual(len(reads), 1, "doctor --report must not silently start another state reading")

    def test_absent_unreadable_and_oversized_observations_are_not_retried_by_consumers(self) -> None:
        """A refusal or absence must survive a file becoming readable later in the run."""
        from codexsync.issue_report import _global_state

        cfg = load_config(self.config)
        original_read, original_stat = Path.read_bytes, Path.stat

        def denied(path):
            if path == self.state:
                raise PermissionError("state is locked")
            return original_read(path)

        with patch.object(Path, "read_bytes", denied):
            unreadable = observe_global_state(self.state, cfg.guardian.max_state_bytes)
        oversized = observe_global_state(self.state, 1)
        self.state.unlink()
        missing = observe_global_state(self.state, cfg.guardian.max_state_bytes)
        self.state.write_bytes(self.payload)

        def no_stat(path, *args, **kwargs):
            if path == self.state:
                self.fail("a supplied state observation must prohibit every fallback read, even stat")
            return original_stat(path, *args, **kwargs)

        for observation, status, report in (
            (missing, GlobalStateStatus.MISSING, {"present": False}),
            (unreadable, GlobalStateStatus.UNREADABLE, {"present": True, "readable": False}),
            (oversized, GlobalStateStatus.TOO_BIG, {"present": True, "too_large": True}),
        ):
            with self.subTest(status=status), patch.object(Path, "stat", no_stat), patch.object(
                Path, "read_bytes", denied,
            ):
                self.assertIs(observation.status, status)
                self.assertEqual(_project_roots(self.state.parent, 1, global_state=observation), [])
                result = _check_global_state_schema(self.state.parent, cfg, global_state=observation)
                self.assertEqual(result.status, "WARN")
                self.assertNotIn("formats not checked", _check_codex_compat(
                    self.state.parent, None, cfg, global_state=observation,
                ).details)
                self.assertEqual(_global_state(self.state.parent, 1, global_state=observation), report)

    def test_operation_safety_window_and_unknown_state_are_preserved_without_a_second_check(self) -> None:
        """Reusing the state must not replace SYNC's safety window by a DOCTOR sample."""
        now = [0.0]
        samples = []

        def sample():
            samples.append(now[0])
            return ProcessState.STOPPED

        def sleep(seconds):
            now[0] += seconds

        gate = SafetyGate(sample, monotonic=lambda: now[0], sleep=sleep)
        with patch("codexsync.preflight._make_safety_gate", return_value=gate) as factory:
            facts = {}
            report = run_preflight(self.config, operation=OperationKind.SYNC, facts=facts)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(samples, [i / 4 for i in range(9)])
        observation = facts["observations"]
        decision = observation.process.decision
        self.assertIs(decision.operation, OperationKind.SYNC)
        check = next(item for item in report.checks if item.name == "codex_process")
        self.assertEqual(check.details, "continuous stopped process window passed")
        self.assertEqual(check, _check_process_state(facts["cfg"], OperationKind.SYNC, decision=decision))

        unknown = SafetyGate(lambda: ProcessState.UNKNOWN, monotonic=lambda: 0, sleep=lambda _: None)
        with patch("codexsync.preflight._make_safety_gate", return_value=unknown), patch(
            "codexsync.preflight.diagnose_catalogue", return_value=[],
        ) as diagnose:
            report = run_preflight(self.config, operation=OperationKind.SYNC)
        self.assertIs(diagnose.call_args.args[1], ProcessState.UNKNOWN)
        self.assertEqual(next(item.status for item in report.checks if item.name == "codex_process"), "FAIL")

    def test_observations_are_immutable_and_name_their_independent_capture_intervals(self) -> None:
        facts = {}
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped):
            run_preflight(self.config, facts=facts)
        observations = facts["observations"]
        state = observations.global_state
        self.assertEqual(state.payload, self.payload)
        self.assertEqual(state.boundary.source, str(self.state))
        self.assertLessEqual(observations.process.boundary.started_at_utc, observations.process.boundary.completed_at_utc)
        self.assertLessEqual(observations.process.boundary.completed_at_utc, state.boundary.started_at_utc)
        self.assertLessEqual(state.boundary.started_at_utc, state.boundary.completed_at_utc)
        self.assertIsNotNone(state.boundary.started_at_utc.tzinfo)
        for obj, name, replacement in (
            (observations, "global_state", None), (state, "payload", b"changed"),
            (state.boundary, "source", "changed"), (observations.process.decision, "allowed", False),
        ):
            with self.subTest(attribute=name), self.assertRaises(FrozenInstanceError):
                setattr(obj, name, replacement)

    def test_repeated_reports_keep_order_and_leave_all_files_and_missing_runtime_dirs_untouched(self) -> None:
        """Even a WAL database and a missing manifest must never trigger diagnostic bootstrap."""
        connection = sqlite3.connect(self.state.parent / "state_5.sqlite")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.execute("CREATE TABLE backfill_state (id INTEGER, status TEXT)")
            connection.execute("INSERT INTO backfill_state VALUES (1, 'complete')")
            connection.commit()
        finally:
            connection.close()
        (self.root / "backups").rmdir()
        (self.root / ".tmp").rmdir()
        (self.root / "state").rmdir()

        def disk_snapshot():
            return {
                path.relative_to(self.root).as_posix(): (
                    path.is_dir(), path.stat().st_mtime_ns,
                    None if path.is_dir() else path.read_bytes(),
                ) for path in self.root.rglob("*")
            }

        before = disk_snapshot()
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped), patch(
            "codexsync.preflight.check_sandbox_folders", return_value=SandboxReport(applicable=False),
        ):
            first, second = run_preflight(self.config), run_preflight(self.config)
        self.assertEqual(first.checks, second.checks)
        self.assertEqual([item.parts for item in first.checks], [item.parts for item in second.checks])
        self.assertEqual([item.name for item in first.checks], [
            "config", "state_dirs", "codex_compat", "config_compat", "sync_rules", "project_registry",
            "local_state", "cloud_root", "backup_dir", "temp_dir", "manifest", "codex_process",
            "codex_startup", "codex_sandbox", "session_catalog", "session_visibility", "session_format",
            "sqlite_audit", "session_index", "global_state_schema", "plugin_inventory", "guardian_latest_good",
            "orphan_temp_files", "mutation_journal",
        ])
        self.assertEqual(disk_snapshot(), before)

    def test_sqlite_observation_is_collected_once_and_shared_by_every_doctor_consumer(self) -> None:
        """A SQLite change mid-run must not split visibility, startup, audit and compatibility answers."""
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped), patch(
            "codexsync.preflight.collect_preflight_observations", wraps=__import__(
                "codexsync.preflight_observations", fromlist=["collect_preflight_observations"]
            ).collect_preflight_observations,
        ) as collect, patch("codexsync.sqlite_audit.discover_sqlite_sets", side_effect=AssertionError("rediscovered")), patch(
            "codexsync.sqlite_audit._read_only_connect", side_effect=AssertionError("reopened")
        ):
            facts = {}
            report = run_preflight(self.config, facts=facts)
        self.assertEqual(collect.call_count, 1)
        self.assertIsNotNone(facts["observations"].sqlite)
        self.assertEqual([item.name for item in report.checks].count("sqlite_audit"), 1)


if __name__ == "__main__":
    unittest.main()
