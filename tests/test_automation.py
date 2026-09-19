"""`[scheduler]` applied to the OS task, with a fake scheduler.

No test here registers anything with the real operating system: every call
takes an injected adapter that records what it was asked to do.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.app import run_automation_job
from codexsync.automation import apply_automation, automation_status, remove_automation
from codexsync.config_edit import create_config, set_value
from codexsync.exceptions import FailSafeError
from codexsync.system_scheduler import JobDefinition, SchedulerError, SchedulerStatus

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class FakeScheduler:
    platform = "fake"
    reports_run_times = True

    def __init__(self, *, fail: bool = False) -> None:
        self.installed: tuple | None = None
        self.removed = 0
        self.fail = fail

    def ignored_settings(self, job):
        return ("jitter_seconds",) if job.run_at_login else ()

    def install(self, job, *, command, config_path, log_dir) -> None:
        if self.fail:
            raise SchedulerError("access denied")
        self.installed = (job, tuple(command), config_path, log_dir)

    def remove(self) -> bool:
        if self.fail:
            raise SchedulerError("access denied")
        self.removed += 1
        was = self.installed is not None
        self.installed = None
        return was

    def status(self, expected: JobDefinition | None = None) -> SchedulerStatus:
        if self.installed is None:
            return SchedulerStatus(installed=False, detail="not installed")
        job, command, config_path, log_dir = self.installed
        matches = None
        if expected is not None:
            matches = (expected.job, expected.command, expected.config_path, expected.log_dir) == (job, command, config_path, log_dir)
        return SchedulerStatus(installed=True, enabled=True, definition_matches=matches)


class AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"automation-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(
            self.config, machine_id="laptop", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"),
        )

    def _set(self, **values) -> None:
        text = self.config.read_text(encoding="utf-8")
        for key, value in values.items():
            text = set_value(text, "scheduler", key, value)
        self.config.write_text(text, encoding="utf-8")

    def test_an_enabled_scheduler_installs_exactly_the_configured_safe_job(self) -> None:
        self._set(enabled=True, mode="sync_dry_run", interval_seconds=900, run_at_login=True)
        fake = FakeScheduler()
        view = apply_automation(self.config, scheduler=fake)
        job, command, config_path, _log_dir = fake.installed
        self.assertEqual((job.mode, job.interval_seconds, job.run_at_login), ("sync_dry_run", 900, True))
        self.assertEqual(config_path, self.config.resolve())
        self.assertTrue(view.status.installed)
        self.assertTrue(view.status.definition_matches)
        self.assertEqual(view.argv[-2:], ("sync", "--dry-run"))

    def test_a_disabled_scheduler_removes_the_task(self) -> None:
        fake = FakeScheduler()
        fake.installed = ("x", (), Path("c"), Path("l"))
        view = apply_automation(self.config, scheduler=fake)
        self.assertEqual(fake.removed, 1)
        self.assertFalse(view.status.installed)

    def test_a_changed_config_reads_as_an_outdated_task(self) -> None:
        self._set(enabled=True, interval_seconds=600)
        fake = FakeScheduler()
        apply_automation(self.config, scheduler=fake)
        self._set(interval_seconds=1200)
        self.assertFalse(automation_status(self.config, scheduler=fake).status.definition_matches)

    def test_an_os_refusal_is_a_fail_safe_stop_not_a_crash(self) -> None:
        self._set(enabled=True)
        with self.assertRaises(FailSafeError):
            apply_automation(self.config, scheduler=FakeScheduler(fail=True))
        with self.assertRaises(FailSafeError):
            remove_automation(self.config, scheduler=FakeScheduler(fail=True))

    def test_a_status_the_os_cannot_give_is_reported_not_raised(self) -> None:
        fake = FakeScheduler()
        fake.status = mock.Mock(side_effect=SchedulerError("schtasks missing"))  # type: ignore[method-assign]
        view = automation_status(self.config, scheduler=fake)
        self.assertIsNone(view.status)
        self.assertIn("schtasks", view.status_error)

    def test_ignored_settings_are_reported(self) -> None:
        self._set(run_at_login=True, jitter_seconds=30)
        self.assertEqual(automation_status(self.config, scheduler=FakeScheduler()).ignored, ("jitter_seconds",))

    def test_the_task_never_writes_its_logs_inside_codex(self) -> None:
        self._set(enabled=True)
        fake = FakeScheduler()
        apply_automation(self.config, scheduler=fake)
        _, _, _, log_dir = fake.installed
        self.assertNotIn(str((self.root / "codex").resolve()), str(log_dir))

    def test_run_now_calls_what_the_scheduled_command_calls(self) -> None:
        self._set(mode="preflight")
        with mock.patch("codexsync.app.run_preflight") as preflight:
            preflight.return_value = mock.Mock(is_ok=True, failures=[])
            ran = run_automation_job(self.config)
        self.assertEqual((ran.mode, ran.status), ("preflight", "PASSED"))
        self.assertEqual(preflight.call_args.kwargs["operation"].name, "SYNC")

        self._set(mode="sync_dry_run")
        context = mock.Mock()
        context.plan.action_count = 7
        with mock.patch("codexsync.app.build_context", return_value=context) as build, \
                mock.patch("codexsync.app.run_sync") as sync:
            ran = run_automation_job(self.config)
        self.assertEqual(build.call_args.kwargs["enforce_safety"], True, "a scheduled dry run still goes through the gate")
        self.assertEqual(sync.call_args.kwargs["dry_run"], True)
        self.assertEqual((ran.status, ran.actions), ("DRY_RUN_FINISHED", 7))


if __name__ == "__main__":
    unittest.main()
