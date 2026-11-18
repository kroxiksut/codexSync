"""How the window's jobs start, finish and overlap (review of 2026-09-27).

`test_gui_window.py` runs every job inline, which is exactly what hides the
bugs here: a job that finishes the moment it starts can never overlap another,
outlive a save, or be clicked twice. `_DeferredRunner` holds each job until the
test finishes it, so the order of events is the test's to choose.
"""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

try:
    from tests.test_gui_window import (
        _HAS_QT,
        FakeController,
        _WindowTestCase,
        _directory,
    )
except ImportError:  # collected with tests/ itself on sys.path
    from test_gui_window import (  # type: ignore[no-redef]
        _HAS_QT,
        FakeController,
        _WindowTestCase,
        _directory,
    )

from codexsync.gui.controller import Controller, Failure, Outcome


class _DeferredRunner:
    """Holds every job until the test says it finished."""

    busy = False

    def __init__(self) -> None:
        self.jobs: list[tuple[object, object, object, object]] = []
        self.abandoned: list[object] = []

    def start(self, call, done, progress=None, *, abandonable=False):
        token = object()
        self.jobs.append((token, call, done, progress))
        return token

    def abandon(self, token) -> bool:
        for index, job in enumerate(self.jobs):
            if job[0] is token:
                del self.jobs[index]
                self.abandoned.append(token)
                return True
        return False

    def finish(self, index: int = 0, *, reports: int = 0) -> None:
        _token, call, done, progress = self.jobs.pop(index)
        for count in range(reports):
            progress("sessions", count, reports + 1)
        done(call(progress=progress) if progress is not None else call())


class _PairController(FakeController):
    """A stored working set per pair of machines, as the real store keeps them."""

    def __init__(self) -> None:
        super().__init__()
        from codexsync.session_scope import SessionScope

        self.sets = {
            ("desktop", "laptop"): SessionScope(projects=("p1",)),
            ("tablet", "laptop"): SessionScope(projects=("p2",)),
        }

    def working_set(self, *, source_machine, target_machine) -> Outcome:
        from codexsync.session_scope import SessionScope

        self.calls.append(("working_set", source_machine, target_machine))
        return Outcome(value=self.sets.get((source_machine, target_machine), SessionScope()))


class _TaskController(FakeController):
    """The OS-task calls, answered here: no test may reach `schtasks`."""

    #: What the next apply/remove answers; a refusal for another config by default off.
    other_config: str | None = None

    def _maybe_refuse(self, replace: bool) -> Outcome | None:
        if self.other_config and not replace:
            return Outcome(
                failure=Failure.STOPPED_SAFELY, message="installed from another config",
                code="AUTOMATION_OTHER_CONFIG", details={"path": self.other_config},
            )
        return None

    def apply_automation(self, *, replace_other_config: bool = False) -> Outcome:
        self.calls.append(("apply_automation", replace_other_config))
        return self._maybe_refuse(replace_other_config) or Outcome(value=None)

    def remove_automation(self, *, replace_other_config: bool = False) -> Outcome:
        self.calls.append(("remove_automation", replace_other_config))
        return self._maybe_refuse(replace_other_config) or Outcome(value=True)

    def automation(self) -> Outcome:
        self.calls.append(("automation",))
        return Outcome(failure=Failure.CONFIGURATION, message="not in this test")


class _JobsCase(_WindowTestCase):
    def deferred(self, controller=None, page: str | None = None):
        """A window whose arrival work ran inline, and whose jobs now wait."""
        window, controller = self.make(controller=controller)
        if page is not None:
            window.go_to(page)
        runner = _DeferredRunner()
        window._jobs = runner
        return window, controller, runner


class WorkingSetJobTests(_JobsCase):
    def test_carrying_everything_again_does_not_run_on_the_ui_thread(self) -> None:
        """CS-305: "Clear" stored the set synchronously, behind a full chat scan."""
        window, controller, runner = self.deferred(page="sessions")
        screen = window.screen("sessions")
        screen.model.scope_projects = ("p1",)
        screen.render()
        screen.clear_working_set()
        self.assertFalse(
            [call for call in controller.calls if call[0] == "save_working_set"],
            "the store ran before the job did",
        )
        self.assertTrue(screen.model.scope_busy)
        self.assertFalse(screen.scope_clear.isEnabled())
        runner.finish()
        self.assertIn(("save_working_set", (), (), "desktop", "laptop"), controller.calls)
        self.assertEqual(screen.model.scope_projects, ())
        self.assertEqual(window.screen("sessions").scope_summary.text(), window.catalog.text("sessions.scope.all"))

    def test_a_clear_that_could_not_be_stored_says_so(self) -> None:
        window, controller, runner = self.deferred(page="sessions")
        controller.outcomes["save_working_set"] = Outcome(failure=Failure.CONFIGURATION, message="read-only")
        screen = window.screen("sessions")
        screen.model.scope_projects = ("p1",)
        screen.clear_working_set()
        runner.finish()
        screen = window.screen("sessions")
        self.assertEqual(screen.model.scope_projects, ("p1",), "the set still in force stays on screen")
        self.assertIn("read-only", screen.scope_summary.text())
        self.assertNotEqual(screen.scope_summary.text(), window.catalog.text("sessions.scope.all"))

    def test_storing_an_empty_set_reads_no_chat(self) -> None:
        with mock.patch(
            "codexsync.gui.controller.build_working_set", side_effect=AssertionError("scanned")
        ), mock.patch("codexsync.gui.controller.write_working_set") as write:
            outcome = Controller(Path("config.toml")).save_working_set(
                projects=(), chats=(), source_machine="desktop", target_machine="laptop",
            )
        self.assertTrue(outcome.ok, outcome.message)
        self.assertTrue(outcome.value.is_empty)
        write.assert_called_once()

    def test_taking_a_set_is_a_write_nobody_can_stop_waiting_for(self) -> None:
        window, controller, runner = self.deferred(page="sessions")
        screen = window.screen("sessions")
        screen.model.scope_projects = ("p1",)
        screen.take_working_set()
        self.assertEqual(len(runner.jobs), 1)
        window.cancel_waiting()
        self.assertEqual(runner.abandoned, [], "a store that is no longer watched has not stopped")
        self.assertTrue(screen.model.scope_busy)
        runner.finish()
        self.assertFalse(window.screen("sessions").model.scope_busy)


class WorkingSetPairTests(_JobsCase):
    """CS-308: the set shown is the set of the pair that is chosen."""

    def _screen(self):
        window, controller = self.make(controller=_PairController())
        window.go_to("sessions")
        return window, controller, window.screen("sessions")

    def _choose_source(self, screen, name: str, *, signal: bool = True) -> None:
        screen.source.setEditText(name)
        if signal:
            screen.source.lineEdit().editingFinished.emit()

    def test_the_set_is_read_again_when_the_pair_changes(self) -> None:
        _window, _controller, screen = self._screen()
        self.assertEqual(screen.model.scope_projects, ("p1",))
        self._choose_source(screen, "tablet")
        self.assertEqual(screen.model.scope_projects, ("p2",))
        self.assertEqual(screen.model.scope_pair, ("tablet", "laptop"))

    def test_take_never_stores_one_pairs_set_under_another(self) -> None:
        _window, controller, screen = self._screen()
        self._choose_source(screen, "tablet", signal=False)
        screen.take_working_set()
        self.assertFalse(
            [call for call in controller.calls if call[0] == "save_working_set"],
            "the ticks were read for desktop -> laptop",
        )
        self.assertEqual(screen.model.scope_projects, ("p2",), "the page now shows the pair's own set")
        screen.take_working_set()
        self.assertIn(("save_working_set", ("p2",), (), "tablet", "laptop"), controller.calls)

    def test_a_scan_shows_the_set_it_will_use(self) -> None:
        _window, controller, screen = self._screen()
        self._choose_source(screen, "tablet", signal=False)
        screen.scan()
        self.assertIn(("scan_sessions", "tablet", "laptop"), controller.calls)
        self.assertEqual(screen.model.scope_projects, ("p2",))


class AutomationTaskTests(_JobsCase):
    """CS-306: one OS-task write at a time, and a status read never unlocks it."""

    def test_a_second_task_does_not_start_while_one_runs(self) -> None:
        window, controller, runner = self.deferred(controller=_TaskController(), page="automation")
        screen = window.screen("automation")
        screen._start_task("apply")
        screen._start_task("apply")
        self.assertEqual(len(runner.jobs), 1)

    def test_a_status_read_does_not_unlock_a_running_apply(self) -> None:
        window, controller, runner = self.deferred(controller=_TaskController(), page="automation")
        screen = window.screen("automation")
        screen.model.automation = None
        screen.refresh_task()  # the read a rebuild starts on arrival
        screen._start_task("apply")  # what a save continues with
        self.assertEqual(len(runner.jobs), 2)
        runner.finish(0)  # the status read comes back first
        screen = window.screen("automation")
        self.assertTrue(screen.model.task_busy, "the apply is still running")
        self.assertFalse(screen.task_apply.isEnabled())
        self.assertFalse(screen.task_run.isEnabled())
        self.assertIsNone(screen.model.automation, "a reading from before the apply is not kept")
        screen.apply_task()
        self.assertEqual(len(runner.jobs), 1, "no second apply")
        runner.finish(0)
        self.assertFalse(window.screen("automation").model.task_busy)


class AutomationOtherConfigTests(_JobsCase):
    """CS-545: tasks another config installed are taken over only when the person says so."""

    def test_a_refusal_offers_to_take_the_tasks_over_and_asks_first(self) -> None:
        controller = _TaskController()
        controller.other_config = "D:/Other/config.toml"
        window, controller, runner = self.deferred(controller=controller, page="automation")
        screen = window.screen("automation")
        self.assertFalse(screen.task_replace.isVisibleTo(window))
        screen._start_task("apply")
        runner.finish(0)
        screen = window.screen("automation")
        self.assertTrue(screen.task_replace.isVisibleTo(window))
        self.assertIn("D:/Other/config.toml", screen.task_status.text())
        asked: list[str] = []
        window.confirm = lambda title, body, action: asked.append(body) or False
        screen.replace_tasks()
        self.assertEqual(len(runner.jobs), 0, "declined: nothing started")
        window.confirm = lambda title, body, action: asked.append(body) or True
        screen.replace_tasks()
        self.assertIn("D:/Other/config.toml", asked[-1])
        runner.finish(0)
        self.assertEqual(
            [call for call in controller.calls if call[0] == "apply_automation"],
            [("apply_automation", False), ("apply_automation", True)],
        )
        self.assertFalse(window.screen("automation").task_replace.isVisibleTo(window))


class ConfigMigrationReloadTests(_JobsCase):
    def test_an_upgraded_file_is_what_every_page_reads_next(self) -> None:
        """CS-307: after the upgrade the pages kept the old file."""
        window, _controller = self.make()
        window.go_to("settings")
        screen = window.screen("settings")
        self.pump(lambda: screen.model.migration is not None)
        with mock.patch.object(window, "config_changed", wraps=window.config_changed) as changed:
            screen.apply_migration()
        changed.assert_called_once_with()
        screen = window.screen("settings")
        self.assertTrue(screen.model.migration_result.ok)
        self.assertEqual(screen.migration_status.text(), window.catalog.text("settings.migration.done"))


class RecoveryResumeTests(_JobsCase):
    def test_choosing_a_rollback_target_keeps_resume_unlocked(self) -> None:
        window, controller = self.make()
        window.go_to("recovery")
        screen = window.screen("recovery")
        screen.table.selectRow(0)
        screen.act("resume", dry_run=True)
        self.assertTrue(screen.resume_apply.isEnabled())
        screen.target.setCurrentIndex(screen.target.findData("cloud"))
        self.assertTrue(screen.resume_apply.isEnabled(), "resume takes no target")
        screen.act("resume", dry_run=False)
        self.assertIn(("resume", "op-1", False), controller.calls)


class ProgressRedrawTests(_JobsCase):
    def test_job_progress_estimates_rate_and_resets_at_phase_boundary(self) -> None:
        from codexsync.gui.jobs import JobBook

        now = [10.0]
        book = JobBook(clock=lambda: now[0])
        book.start("job", page="sync", kind="full_sync", apply=lambda *_: None,
                   model=object(), cancellable=False)
        self.assertTrue(book.report("job", "stage_chats", 1, 10))
        self.assertIsNone(book.running["job"].progress_estimate.items_per_second)
        now[0] = 12.0
        book.report("job", "stage_chats", 5, 10)
        estimate = book.running["job"].progress_estimate
        self.assertEqual(estimate.items_per_second, 2.0)
        self.assertEqual(estimate.remaining_seconds, 2.5)
        now[0] = 13.0
        book.report("job", "apply_chats", 1, 10)
        self.assertIsNone(book.running["job"].progress_estimate.items_per_second)
        self.assertEqual(book.running["job"].progress, ("apply_chats", 1, 10))

    def test_large_task_list_filters_without_changing_the_plan(self) -> None:
        from codexsync.gui.task_items import task_rows, filter_task_rows
        from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
        from codexsync.semantic_merge import BranchRelation

        items = tuple(TransferItem(str(i), BranchRelation.IDENTICAL, TransferAction.FAST_FORWARD_REMOTE,
                                   "a", "b", 1, 1, f"sessions/{i}.jsonl") for i in range(1200))
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False, items)
        rows = task_rows(plan, {"staged": 1200, "applied": 205, "relative_path": "sessions/205.jsonl",
                                "phase": "apply_chats"})
        self.assertEqual(len(filter_task_rows(rows, "applied")), 205)
        self.assertEqual(len(filter_task_rows(rows, "failed")), 1)
        self.assertEqual(len(filter_task_rows(rows, "not_applied")), 995)
        self.assertEqual(plan.items, items)

    def test_not_applied_filter_includes_blocked_and_skipped_chats(self) -> None:
        from codexsync.gui.task_items import task_rows, filter_task_rows
        from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
        from codexsync.semantic_merge import BranchRelation

        items = tuple(TransferItem(str(i), BranchRelation.IDENTICAL, action, "a", "b", 1, 1)
                      for i, action in enumerate((TransferAction.BLOCKED_INVALID_BRANCH, TransferAction.NOOP)))
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False, items)
        self.assertEqual(len(filter_task_rows(task_rows(plan, {}), "not_applied")), 2)

    def test_failed_item_is_identified_by_branch_not_ambiguous_path(self) -> None:
        from codexsync.gui.task_items import task_rows
        from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
        from codexsync.semantic_merge import BranchRelation

        items = (
            TransferItem("first", BranchRelation.IDENTICAL, TransferAction.FAST_FORWARD_LOCAL,
                         "a", "b", 1, 1, "sessions/chat.jsonl"),
            TransferItem("second", BranchRelation.IDENTICAL, TransferAction.FAST_FORWARD_REMOTE,
                         "a", "b", 1, 1, "sessions/chat.jsonl"),
        )
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False, items)
        params = {"staged": 2, "applied": 1, "phase": "apply_chats",
                  "relative_path": "sessions/chat.jsonl", "session_hash": "second"}
        self.assertEqual([row.state for row in task_rows(plan, params)], ["applied", "failed"])
        params.update(phase="delete_chat_files", relative_path="archived_sessions/old.jsonl")
        self.assertEqual([row.state for row in task_rows(plan, params)], ["applied", "failed"])

    def test_tasks_dialog_virtualizes_and_filters_failed_transfer(self) -> None:
        from types import SimpleNamespace
        from codexsync.gui.screens.tasks import TasksDialog
        from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
        from codexsync.semantic_merge import BranchRelation

        items = tuple(TransferItem(str(i), BranchRelation.IDENTICAL, TransferAction.FAST_FORWARD_REMOTE,
                                   "a", "b", 1, 1, f"sessions/{i}.jsonl") for i in range(1200))
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False, items)
        journal = SimpleNamespace(state="RECOVERY_REQUIRED", failure_params={
            "staged": 1200, "applied": 205, "relative_path": "sessions/205.jsonl", "phase": "apply_chats",
        })
        window, _ = self.make()
        dialog = TasksDialog(window)
        dialog.set_task_data(plan, journal)
        self.assertEqual(dialog.items_model.rowCount(), 1200)
        dialog.items_filter.setCurrentIndex(dialog.items_filter.findData("failed"))
        self.assertEqual(dialog.items_proxy.rowCount(), 1)
        self.assertEqual(plan.items, items)
        dialog.close()

    def test_tasks_dialog_tracks_live_applied_count(self) -> None:
        from types import SimpleNamespace
        from codexsync.gui.screens.tasks import TasksDialog
        from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
        from codexsync.semantic_merge import BranchRelation

        items = tuple(TransferItem(str(i), BranchRelation.IDENTICAL, TransferAction.FAST_FORWARD_REMOTE,
                                   "a", "b", 1, 1, f"sessions/{i}.jsonl") for i in range(3))
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False, items)
        window, _ = self.make()
        dialog = TasksDialog(window)
        dialog.set_task_data(plan, SimpleNamespace(state="COMMITTING", failure_params=None))
        dialog.update_task_progress(("apply_chats", 2, 3))
        self.assertEqual([row.state for row in dialog.items_model.rows], ["applied", "applied", "staged"])
        dialog.close()

    def test_tasks_live_events_keep_only_the_latest_500_and_filter_them(self) -> None:
        from codexsync.gui.screens.tasks import TasksDialog
        from codexsync.progress import ProgressEvent

        window, _ = self.make()
        dialog = TasksDialog(window)
        book = window.jobs()
        book.start("transfer", page="sync", kind="full_sync", apply=lambda *_: None,
                   model=object(), cancellable=False)
        for i in range(1500):
            book.report_events("transfer", [ProgressEvent(
                "sync", "apply_chats", i, 1500, relative_path=f"settings/{i}.toml",
                status=("pending", "applied", "failed")[i % 3],
            )])
        dialog.render()
        self.assertEqual(dialog.events_model.rowCount(), 500)
        self.assertEqual(dialog.events_model.rows[0].relative_path, "settings/1000.toml")
        self.assertEqual(dialog.events_model.rows[-1].relative_path, "settings/1499.toml")
        for category, count in (("errors", 167), ("applied", 167), ("pending", 166)):
            dialog.events_filter.setCurrentIndex(dialog.events_filter.findData(category))
            self.assertEqual(dialog.events_proxy.rowCount(), count)
        dialog.close()

    def test_a_fast_worker_flushes_bounded_event_batches_before_finishing(self) -> None:
        from codexsync.gui.widgets import _Job
        from codexsync.progress import ProgressEvent

        def work(progress):
            for i in range(1500):
                progress.event(ProgressEvent("sync", "apply_chats", i, 1500, status="applied"))
            return Outcome(value=True)

        job = _Job(1, work, with_progress=True)
        batches = []
        job.signals.events.connect(batches.append)
        with mock.patch("codexsync.gui.widgets.time.monotonic", return_value=100.0):
            job.outcome()
        self.assertEqual(len(batches), 2)
        token, latest = batches[-1]
        self.assertEqual(token, 1)
        self.assertEqual([row.completed for row in latest], list(range(1000, 1500)))

    def test_open_tasks_reloads_journal_after_transfer_stops(self) -> None:
        from types import SimpleNamespace
        from codexsync.gui.screens.tasks import TasksDialog
        from codexsync.semantic_transfer import TransferPlan

        window, _ = self.make()
        dialog = TasksDialog(window)
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False)
        dialog.set_task_data(plan, SimpleNamespace(state="COMMITTING", failure_params=None,
                                                    machine_id=window.machine_id()))
        book = window.jobs()
        book.start("transfer", page="sync", kind="full_sync", apply=lambda *_: None,
                   model=object(), cancellable=False)
        dialog.render()
        book.finish("transfer", Outcome(failure=Failure.STOPPED_SAFELY))
        with mock.patch.object(dialog, "_load_items") as reload_items:
            dialog.render()
        reload_items.assert_called_once()
        dialog.close()

    def test_failed_transfer_items_offer_the_matching_recovery_journal(self) -> None:
        from types import SimpleNamespace
        from codexsync.gui.screens.tasks import TasksDialog
        from codexsync.semantic_transfer import TransferPlan

        window, _ = self.make()
        dialog = TasksDialog(window)
        plan = TransferPlan(1, "p", "now", "a", "b", "layout", "v1", False)
        dialog.set_task_data(plan, SimpleNamespace(state="RECOVERY_REQUIRED", failure_params={}, operation_id="op-1"))
        self.assertFalse(dialog.items_recovery.isHidden())
        with mock.patch.object(window, "go_to") as go:
            dialog.items_recovery.click()
        go.assert_called_once_with("recovery", "op-1")
        dialog.close()

    def test_failed_task_keeps_last_applied_count(self) -> None:
        from codexsync.gui.jobs import JobBook

        book = JobBook()
        book.start(1, page="sync", kind="full_sync", apply=lambda *_: None,
                   model=object(), cancellable=False)
        book.report(1, "apply_chats", 2, 3)
        book.finish(1, Outcome(failure=Failure.STOPPED_SAFELY))
        self.assertEqual(book.recent[0].progress, ("apply_chats", 2, 3))

    def test_fast_failure_flushes_the_latest_progress_before_result(self) -> None:
        from codexsync.gui.widgets import _Job

        def work(progress):
            progress("stage_chats", 3, 3)
            progress("apply_chats", 0, 3)
            progress("apply_chats", 2, 3)
            return Outcome(failure=Failure.STOPPED_SAFELY)

        job = _Job(1, work, with_progress=True)
        reports = []
        job.signals.progress.connect(reports.append)
        with mock.patch("codexsync.gui.widgets.time.monotonic", return_value=100.0):
            job.outcome()
        self.assertEqual(reports[-1], (1, "apply_chats", 2, 3))

    """A scan redraws the page per report; the trees are drawn once per result."""

    def _count(self, widget, name: str) -> list[int]:
        calls = [0]
        original = getattr(widget, name)

        def counted(*args, **kwargs):
            calls[0] += 1
            return original(*args, **kwargs)

        setattr(widget, name, counted)
        return calls

    def test_the_chat_tree_is_not_rebuilt_while_a_scan_reports(self) -> None:
        window, _controller, runner = self.deferred(page="chats")
        screen = window.screen("chats")
        screen.refresh()
        runner.finish()
        fills = self._count(screen, "_fill_tree")
        screen.refresh()
        runner.finish(reports=5)
        self.assertEqual(fills[0], 1, "one redraw for the new result, none per report")
        self.assertEqual(screen.tree.topLevelItemCount(), 3)

    def test_the_scope_tree_is_not_rebuilt_while_a_scan_reports(self) -> None:
        window, _controller, runner = self.deferred(page="sessions")
        screen = window.screen("sessions")
        screen.load_projects()
        runner.finish()
        clears = self._count(screen.scope_tree, "clear")
        screen.scan()
        self.assertEqual(len(runner.jobs), 1, "the directory is already there")
        runner.finish(reports=5)
        self.assertEqual(clears[0], 0)
        self.assertEqual(screen.scope_tree.topLevelItemCount(), len(_directory().projects))


class StaleResultTests(_JobsCase):
    def test_a_plan_read_before_a_save_is_not_delivered_after_it(self) -> None:
        window, _controller, runner = self.deferred(page="sessions")
        window.screen("sessions").scan()  # the project tree and the plan
        window.config_changed()  # the same file, saved
        while runner.jobs:
            runner.finish()
        model = window.model("sessions")
        self.assertFalse(model.busy, "the page is not left waiting")
        self.assertIsNone(model.scan, "a plan built under the old rules must not look applicable")

    def test_a_write_in_flight_still_reports_what_it_did(self) -> None:
        window, controller, runner = self.deferred(page="sync")
        window.screen("sync").run(lambda: controller.sync(dry_run=False), lambda model, outcome: setattr(model, "_seen", outcome))
        window.config_changed()
        runner.finish()
        self.assertTrue(window.model("sync")._seen.ok)


@unittest.skipUnless(_HAS_QT, "PySide6 is an optional extra and is not installed")
class JobFailureTests(_WindowTestCase):
    def test_interrupted_transfer_does_not_show_exception_payload(self) -> None:
        from codexsync.exceptions import FailSafeError
        from codexsync.gui.controller import run

        exc = FailSafeError("private chat payload")
        exc.recovery_operation_id = "op-1"
        exc.recovery_message = "Session transfer stopped; recovery may be required."
        outcome = run(lambda: (_ for _ in ()).throw(exc))
        self.assertNotIn("private chat payload", outcome.message)
        self.assertEqual(outcome.details["recovery_operation_id"], "op-1")
    def test_a_call_that_raises_still_delivers_a_failure(self) -> None:
        """Without a result the page's busy flag would never clear."""
        from codexsync.gui.widgets import JobRunner

        runner = JobRunner()
        self.addCleanup(runner.deleteLater)
        received: list[Outcome] = []

        def boom() -> Outcome:
            raise RuntimeError("wrapper broke")

        runner.start(boom, received.append)
        self.pump(lambda: bool(received), seconds=10)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].failure, Failure.UNEXPECTED)
        self.assertIn("wrapper broke", received[0].message)
