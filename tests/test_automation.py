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
        #: The sign-in task's adapter. Always injected: the real one is schtasks.
        self.login = FakeScheduler()
        #: The copy-of-.codex task's adapter (CS-276). Always injected too.
        self.backup = FakeScheduler()
        #: The handoff watcher's adapter (CS-328). Always injected too.
        self.handoff = FakeScheduler()
        # What the tasks need from this platform is a proven process detector;
        # CI runs on macOS too, where it is not, so the answer is fixed here
        # and the refusal has its own test.
        detectable = mock.patch("codexsync.automation._codex_detectable", return_value=True)
        detectable.start()
        self.addCleanup(detectable.stop)

    def _set(self, **values) -> None:
        text = self.config.read_text(encoding="utf-8")
        for key, value in values.items():
            text = set_value(text, "scheduler", key, value)
        self.config.write_text(text, encoding="utf-8")

    def test_an_enabled_scheduler_installs_exactly_the_configured_safe_job(self) -> None:
        self._set(enabled=True, mode="sync_dry_run", interval_seconds=900, run_at_login=True)
        fake = FakeScheduler()
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        job, command, config_path, _log_dir = fake.installed
        self.assertEqual((job.mode, job.interval_seconds, job.run_at_login), ("sync_dry_run", 900, True))
        self.assertEqual(config_path, self.config.resolve())
        self.assertTrue(view.status.installed)
        self.assertTrue(view.status.definition_matches)
        self.assertEqual(view.argv[-2:], ("sync", "--dry-run"))

    def test_a_disabled_scheduler_removes_the_task(self) -> None:
        fake = FakeScheduler()
        fake.installed = ("x", (), Path("c"), Path("l"))
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self.assertEqual(fake.removed, 1)
        self.assertFalse(view.status.installed)

    def test_a_changed_config_reads_as_an_outdated_task(self) -> None:
        self._set(enabled=True, interval_seconds=600)
        fake = FakeScheduler()
        apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self._set(interval_seconds=1200)
        self.assertFalse(automation_status(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff).status.definition_matches)

    def test_an_os_refusal_is_a_fail_safe_stop_not_a_crash(self) -> None:
        self._set(enabled=True)
        with self.assertRaises(FailSafeError):
            apply_automation(self.config, scheduler=FakeScheduler(fail=True), login_scheduler=FakeScheduler(fail=True), backup_scheduler=FakeScheduler(fail=True), handoff_scheduler=FakeScheduler(fail=True))
        with self.assertRaises(FailSafeError):
            remove_automation(self.config, scheduler=FakeScheduler(fail=True), login_scheduler=FakeScheduler(fail=True), backup_scheduler=FakeScheduler(fail=True), handoff_scheduler=FakeScheduler(fail=True))

    def test_a_status_the_os_cannot_give_is_reported_not_raised(self) -> None:
        fake = FakeScheduler()
        fake.status = mock.Mock(side_effect=SchedulerError("schtasks missing"))  # type: ignore[method-assign]
        view = automation_status(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self.assertIsNone(view.status)
        self.assertIn("schtasks", view.status_error)

    def test_a_task_the_os_cannot_render_is_one_slots_error(self) -> None:
        """CS-476: a ValueError from rendering (e.g. `%X%` in a path) cost the whole view."""
        self.login.status = mock.Mock(side_effect=ValueError("percent in path"))  # type: ignore[method-assign]
        view = automation_status(self.config, scheduler=FakeScheduler(), login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self.assertIsNotNone(view.status)
        self.assertIn("percent", view.login_status_error)

    def test_ignored_settings_are_reported(self) -> None:
        self._set(run_at_login=True, jitter_seconds=30)
        self.assertEqual(automation_status(self.config, scheduler=FakeScheduler(), login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff).ignored, ("jitter_seconds",))

    def test_the_task_never_writes_its_logs_inside_codex(self) -> None:
        self._set(enabled=True)
        fake = FakeScheduler()
        apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        _, _, _, log_dir = fake.installed
        self.assertNotIn(str((self.root / "codex").resolve()), str(log_dir))

    def test_the_login_sync_is_its_own_task_and_off_by_default(self) -> None:
        fake = FakeScheduler()
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self.assertIsNone(self.login.installed)
        self.assertEqual(self.login.removed, 1, "off means the task is removed")
        self.assertFalse(view.sync_at_login)

    def test_switching_the_login_sync_on_installs_exactly_one_unattended_sync(self) -> None:
        self._set(sync_at_login=True, startup_delay_seconds=45)
        fake = FakeScheduler()
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        job, _command, config_path, _log = self.login.installed
        self.assertEqual(
            (job.mode, job.interval_seconds, job.run_at_login, job.startup_delay_seconds),
            ("sync_at_login", None, True, 45),
        )
        self.assertEqual(config_path, self.config.resolve())
        self.assertEqual(view.login_argv[-3:], ("sync", "--apply", "--unattended"))
        self.assertTrue(view.login_status.installed)
        self.assertIsNone(fake.installed, "the periodic task is untouched: [scheduler] enabled is false")

    def test_the_periodic_mode_can_never_be_the_login_sync(self) -> None:
        from codexsync.exceptions import ConfigError

        self._set(mode="sync_at_login")
        with self.assertRaises(ConfigError):
            automation_status(self.config, scheduler=FakeScheduler(), login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)

    def test_one_injected_scheduler_without_the_other_is_refused(self) -> None:
        """Otherwise the missing one is the real OS scheduler."""
        with self.assertRaises(ValueError):
            automation_status(self.config, scheduler=FakeScheduler())
        with self.assertRaises(ValueError):
            apply_automation(self.config, login_scheduler=FakeScheduler())

    def test_the_copy_of_codex_is_its_own_task(self) -> None:
        """[state_backup] (CS-276): installed when scheduled, removed when not."""
        text = self.config.read_text(encoding="utf-8")
        copies = (self.root / "copies").as_posix()
        text = set_value(text, "state_backup", "root_dir", copies)
        text = set_value(text, "state_backup", "interval_hours", 6)
        self.config.write_text(text, encoding="utf-8")
        fake = FakeScheduler()
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        job = self.backup.installed[0]
        self.assertEqual((job.mode, job.interval_seconds, job.run_at_login), ("state_backup", 6 * 3600, False))
        self.assertEqual(view.backup_argv[-3:], ("state-backup", "create", "--wait"))
        self.assertTrue(view.backup_scheduled)
        self.assertTrue(view.backup_status.definition_matches)
        self.assertIsNone(self.login.installed)

        self.config.write_text(set_value(self.config.read_text(encoding="utf-8"), "state_backup", "interval_hours", 0),
                               encoding="utf-8")
        view = apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
        self.assertIsNone(self.backup.installed)
        self.assertFalse(view.backup_scheduled)

    def test_tasks_that_need_codex_closed_are_refused_where_that_cannot_be_proven(self) -> None:
        """CS-279: without a proven detector every run would exit 5 doing nothing."""
        text = set_value(self.config.read_text(encoding="utf-8"), "state_backup", "root_dir",
                         (self.root / "copies").as_posix())
        self.config.write_text(set_value(text, "state_backup", "at_login", True), encoding="utf-8")
        self._set(enabled=True, mode="preflight", sync_at_login=True)
        fake = FakeScheduler()
        with mock.patch("codexsync.automation._codex_detectable", return_value=False):
            with self.assertRaisesRegex(FailSafeError, "state_backup.*sync_at_login"):
                apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
            # Refused before any task was touched.
            self.assertEqual((fake.installed, self.login.installed, self.backup.installed), (None, None, None))
            self.assertEqual((fake.removed, self.login.removed, self.backup.removed), (0, 0, 0))
            # Switched off, the read-only periodic job still applies there.
            text = set_value(self.config.read_text(encoding="utf-8"), "state_backup", "at_login", False)
            self.config.write_text(text, encoding="utf-8")
            self._set(sync_at_login=False)
            apply_automation(self.config, scheduler=fake, login_scheduler=self.login, backup_scheduler=self.backup, handoff_scheduler=self.handoff)
            self.assertEqual(fake.installed[0].mode, "preflight")

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
