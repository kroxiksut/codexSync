"""The main window and every screen, rendered offscreen.

Skipped wholesale when PySide6 is absent, which is what CI sees: the GUI is an
optional extra and the test matrix does not install it. That makes these tests
the kind this project has been bitten by twice -- code no green suite reaches --
so they run without a display and assert the things a window can get wrong on
its own:

* an undetermined Codex must read as "refused", never as "allowed";
* every refusal the core can raise must produce a headline, on every screen;
* a mutation button must stay locked until its plan (or its dry run) exists,
  must ask for confirmation, and must quote the plan id it was shown;
* a result that arrives while the language is switched is not lost;
* every sentence comes from the catalogue -- assertions compare with the
  catalogue, never with an English literal.

Jobs run synchronously here (`_InlineRunner`), so a test reads the result right
after the click instead of racing a thread pool.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest import mock
import uuid

_HAS_QT = importlib.util.find_spec("PySide6") is not None
if _HAS_QT:
    # Must be set before the first QApplication: it lets the widgets be built
    # and painted with no display attached.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from codexsync.app import (
    BackupSnapshotInfo,
    GuardianAcceptPlan,
    GuardianInventory,
    GuardianRestorePlan,
    JournalInfo,
    ProjectMovePlan,
    ProjectMoveResult,
    RecoveryOutcome,
    RestoreResult,
    ShrinkExplanation,
)
from codexsync.guardian_inventory import GuardianSnapshotInfo
from codexsync.chat_directory import Association, ChatDirectory, ChatEntry, ChatKind, ProjectView
from codexsync.chat_move import ChatMoveAction, ChatMoveKind, ChatMovePlan
from codexsync.gui import BRAND_NAME
from codexsync.gui.controller import (
    CheckRow,
    ConfigInfo,
    Controller,
    Failure,
    MoveScan,
    Outcome,
    RepairScan,
    SessionScan,
    StateView,
    SyncPreview,
    SyncResult,
)
from codexsync.gui.i18n import available_languages, load
from codexsync.recovery import RecoveryAction
from codexsync.repair_plan import RepairAction, RepairActionKind, RepairPlan
from codexsync.semantic_merge import BranchRelation
from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan
from codexsync.session_catalog import SessionState

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


def _view(*, stopped: bool, checks=()) -> StateView:
    rows = checks or (
        CheckRow("config", "PASS", "Loaded config"),
        CheckRow("codex_process", "PASS" if stopped else "WARN", "…"),
        CheckRow("session_catalog", "WARN", "sessions=252"),
    )
    return StateView(
        checks=rows,
        codex_looks_stopped=stopped,
        failures=sum(1 for row in rows if row.status == "FAIL"),
        warnings=sum(1 for row in rows if row.status == "WARN"),
    )


def _directory() -> ChatDirectory:
    projects = {
        "p1": ProjectView("p1", "Alpha", ("D:/alpha",)),
        "p2": ProjectView("p2", "Beta", ("C:/beta",)),
    }
    chats = (
        ChatEntry("aaaa-1", "sessions/a.jsonl", SessionState.ACTIVE, ChatKind.TOP_LEVEL, Association.BOUND, "p1", "2026-09-01T10:00:00Z", "D:/alpha", 10, title="first chat"),
        ChatEntry("bbbb-2", "sessions/b.jsonl", SessionState.ACTIVE, ChatKind.TOP_LEVEL, Association.DERIVED_VIA_MAPPING, "p2", "2026-09-02T10:00:00Z", "D:/beta", 5, title="stranded chat"),
        ChatEntry("cccc-3", "sessions/c.jsonl", SessionState.ACTIVE, ChatKind.TOP_LEVEL, Association.NONE, None, "2026-09-03T10:00:00Z", "E:/other", 3, title="lost chat"),
    )
    return ChatDirectory(projects, chats, "electron-v2")


def _move_plan(plan_id: str = "plan-move-1", kind: ChatMoveKind = ChatMoveKind.BIND) -> ChatMovePlan:
    return ChatMovePlan(1, plan_id, "0" * 64, "electron-v2", "p2", (
        ChatMoveAction("bbbb-2", kind, "p2", "p2", Association.DERIVED_VIA_MAPPING),
    ))


def _transfer_plan(*, conflict: bool, volatile: bool = False) -> TransferPlan:
    items = [
        TransferItem("h" * 64, BranchRelation.FAST_FORWARD_REMOTE, TransferAction.FAST_FORWARD_REMOTE, "a" * 64, "b" * 64, 10, 5, "sessions/x.jsonl.xz"),
        TransferItem("i" * 64, BranchRelation.IDENTICAL, TransferAction.NOOP, "c" * 64, "c" * 64, 3, 3),
    ]
    if conflict:
        items.append(TransferItem("j" * 64, BranchRelation.DIVERGED, TransferAction.BLOCKED_CONFLICT, "d" * 64, "e" * 64, 4, 6, conflict_id="k" * 64))
    return TransferPlan(1, "plan-sessions-1", "2026-09-13T00:00:00Z", "desktop", "laptop", "unproven", "v1", volatile, tuple(items))


def _repair_plan(*, ambiguous: bool = False) -> RepairPlan:
    actions = [
        RepairAction(RepairActionKind.ADD_BINDING, "s" * 64, "r" * 64, "p2", "rule-1", "C:/beta", "bbbb-2"),
        RepairAction(RepairActionKind.KEEP_PROJECT, "t" * 64, "r" * 64, "p1", None, "D:/alpha", "aaaa-1"),
    ]
    if ambiguous:
        actions.append(RepairAction(RepairActionKind.AMBIGUOUS_PROJECT, "u" * 64, "r" * 64, None, None, "X:/y", "cccc-3"))
    return RepairPlan(1, "plan-repair-1", "2026-09-13T00:00:00Z", "desktop", "laptop", "0" * 64, "m" * 64, "electron-v2", False, tuple(actions), ("AMBIGUOUS_PROJECT",) if ambiguous else ())


class FakeController(Controller):
    """Canned answers, and a record of every call that could write."""

    def __init__(self) -> None:
        super().__init__(Path("config.toml"))
        self.calls: list[tuple] = []
        self.outcomes: dict[str, Outcome] = {}

    def config_exists(self) -> bool:
        return True

    def config_info(self) -> Outcome:
        return Outcome(value=ConfigInfo("laptop", ("desktop", "laptop"), None, None, Path("C:/m"), Path("C:/b"), Path("C:/t"), Path("C:/p")))

    def _answer(self, name: str, default: Outcome) -> Outcome:
        return self.outcomes.get(name, default)

    def state(self) -> Outcome:
        return self._answer("state", Outcome(value=_view(stopped=True)))

    def preview_sync(self) -> Outcome:
        return self._answer("preview_sync", Outcome(value=SyncPreview(("a",), ("b", "c"), (), False)))

    def sync(self, *, dry_run: bool) -> Outcome:
        self.calls.append(("sync", dry_run))
        return self._answer("sync", Outcome(value=SyncResult(applied=not dry_run, actions=3, conflicts=0)))

    def chats(self, *, source_machine=None, target_machine=None, progress=None) -> Outcome:
        # A long read reports as it goes; the fake reports once so the window's
        # plumbing is exercised rather than merely tolerated.
        if progress is not None:
            progress("sessions", 1, 2)
        return self._answer("chats", Outcome(value=_directory()))

    def move_chats(self, **kwargs) -> Outcome:
        self.calls.append(("move_chats", kwargs))
        written = 0 if kwargs.get("confirm_plan") is None else 1
        return self._answer("move_chats", Outcome(value=(_move_plan(), written)))

    def scan_sessions(self, *, source_machine, target_machine, progress=None) -> Outcome:
        self.calls.append(("scan_sessions", source_machine, target_machine))
        return self._answer("scan_sessions", Outcome(value=SessionScan(_transfer_plan(conflict=False), Path("C:/p/s.json"), Path("C:/p/r.json"), False)))

    def resolve_session_conflict(self, scan, *, conflict_id, choice) -> Outcome:
        self.calls.append(("resolve", conflict_id, choice))
        return Outcome(value=None)

    def apply_sessions(self, scan, *, confirm_plan, dry_run=False) -> Outcome:
        self.calls.append(("apply_sessions", confirm_plan, dry_run))
        return self._answer("apply_sessions", Outcome(value=1))

    def save_working_set(self, *, projects, chats, source_machine, target_machine, progress=None) -> Outcome:
        from codexsync.session_scope import SessionScope

        self.calls.append(("save_working_set", tuple(projects), tuple(chats), source_machine, target_machine))
        return self._answer("save_working_set", Outcome(value=SessionScope(
            projects=tuple(projects), chats=tuple(chats),
            session_hashes=("h1", "h2"), chat_count=12, total_bytes=3 * 1024 * 1024,
        )))

    def working_set(self, *, source_machine, target_machine) -> Outcome:
        from codexsync.session_scope import SessionScope

        return self._answer("working_set", Outcome(value=SessionScope()))

    def mapping_hints(self, *, progress=None) -> Outcome:
        from codexsync.mapping_hints import MappingHints

        return self._answer("mapping_hints", Outcome(value=MappingHints(
            machines=("desktop", "laptop"),
            chat_roots=(("D:/Projects/NetRuleRouter", 26), ("D:/Projects/Other", 4)),
            unmapped_roots=("D:/Projects/NetRuleRouter",),
            local_project_roots=("C:/Work/NetRuleRouter",),
            remote_project_roots=("D:/Projects/Gone",),
        )))

    def scan_repair(self, *, source_machine, target_machine, progress=None) -> Outcome:
        return self._answer("scan_repair", Outcome(value=RepairScan(_repair_plan(), Path("C:/p/r.json"))))

    def apply_repair(self, scan, *, confirm_plan, dry_run=False) -> Outcome:
        self.calls.append(("apply_repair", confirm_plan, dry_run))
        return Outcome(value=1)

    def guardian_inventory(self) -> Outcome:
        return self._answer("guardian_inventory", Outcome(failure=Failure.CONFIGURATION, message="no guardian"))

    def guardian_snapshot(self) -> Outcome:
        self.calls.append(("guardian_snapshot",))
        return self._answer("guardian_snapshot", Outcome(failure=Failure.STOPPED_SAFELY, message="busy"))

    def backups(self) -> Outcome:
        return self._answer("backups", Outcome(value=[
            BackupSnapshotInfo("m-20260905T100630Z-3fcf46bc84a7.zip", True, True, False, 4, 1024, "2026-09-05T10:09:29Z", "m", "2026-09-05T10:06:30Z", None),
            BackupSnapshotInfo("legacy-dir", False, False, True, None, None, "2026-09-01T00:00:00Z", None, None, None),
        ]))

    def restore(self, *, snapshot_name, target, dry_run) -> Outcome:
        self.calls.append(("restore", snapshot_name, target, dry_run))
        return Outcome(value=RestoreResult(snapshot_name, target, 4))

    def journals(self) -> Outcome:
        return self._answer("journals", Outcome(value=[
            JournalInfo("op-1", "sync", "COMMITTING", "2026-09-05T10:06:30Z", 3, "snap-1", False, True, True, True, True),
        ]))

    def resume_journal(self, operation_id, *, dry_run) -> Outcome:
        self.calls.append(("resume", operation_id, dry_run))
        return Outcome(value=RecoveryOutcome(operation_id, "sync", "COMMITTING", RecoveryAction.WOULD_RECOVER if dry_run else RecoveryAction.RETRY_ALLOWED, "snap-1", 0, "detail"))

    def rollback_journal(self, operation_id, *, target, dry_run) -> Outcome:
        self.calls.append(("rollback", operation_id, target, dry_run))
        return Outcome(value=RecoveryOutcome(operation_id, "sync", "COMMITTING", RecoveryAction.WOULD_RECOVER if dry_run else RecoveryAction.ROLLED_BACK, "snap-1", 2, "detail"))

    def open_config(self) -> Outcome:
        return self._answer("open_config", Outcome(failure=Failure.CONFIGURATION, message="not in this test"))

    def config_history(self) -> Outcome:
        return Outcome(value=[])

    def automation(self) -> Outcome:
        return self._answer("automation", Outcome(failure=Failure.CONFIGURATION, message="not in this test"))

    def session_index(self) -> Outcome:
        return self._answer("session_index", Outcome(value={
            "local": {"present": True, "records": 5, "sessions": 4},
            "cloud": {"present": False, "records": 0, "sessions": 0},
            "only_local": 4, "only_cloud": 0, "differing": 0, "differing_ids": [],
            "contract_proven": False, "codes": ["UNPROVEN_CONSUMER_CONTRACT"],
        }))

    def preview_guardian_restore(self, snapshot_id: str) -> Outcome:
        self.calls.append(("preview_restore", snapshot_id))
        return self._answer("preview_guardian_restore", Outcome(value=GuardianRestorePlan(
            1, "plan-restore-1", "laptop", snapshot_id, 2, "2026-09-05T10:57:48Z", "a" * 64, "b" * 64,
            "electron-v2", 18, 1, 16, 6, False, (),
        )))

    def apply_guardian_restore(self, snapshot_id: str, *, confirm_plan: str, dry_run: bool) -> Outcome:
        self.calls.append(("apply_restore", snapshot_id, confirm_plan, dry_run))
        return Outcome(value=1)

    def preview_guardian_accept(self) -> Outcome:
        self.calls.append(("preview_accept",))
        return self._answer("preview_guardian_accept", Outcome(value=GuardianAcceptPlan(
            1, "plan-accept-1", "laptop", "snap-2", 2, "2026-09-05T10:57:48Z", "electron-v2",
            16, 6, 18, 1, ("BINDING_COUNT_DROP",), ("BINDING_COUNT_DROP",),
            ShrinkExplanation(16, 0, 2, 6, 0, 0), self.outcomes.get("accept_codes", ()),
        )))

    def apply_guardian_accept(self, *, confirm_plan: str) -> Outcome:
        self.calls.append(("apply_accept", confirm_plan))
        return Outcome(value="snap-3")

    def scan_project_move(self, *, project_id: str, new_root: Path) -> Outcome:
        self.calls.append(("scan_move", project_id, str(new_root)))
        plan = ProjectMovePlan(
            1, "plan-move-project-1", "2026-09-13T00:00:00Z", "electron-v2", project_id, "Alpha",
            "D:/alpha", str(new_root), "0" * 64, 12, 4096, "i" * 64, ("aaaa-1",), False, False,
            self.outcomes.get("move_codes", ()),
        )
        return Outcome(value=MoveScan(plan, Path("C:/p/move.json")))

    def apply_project_move(self, scan, *, confirm_plan: str, dry_run: bool) -> Outcome:
        self.calls.append(("apply_move", confirm_plan, dry_run))
        return Outcome(value=ProjectMoveResult(confirm_plan, 12, 4096, 1, "D:/alpha", scan.plan.new_root, dry_run))

    def run_automation_now(self) -> Outcome:
        self.calls.append(("run_automation_now",))
        return Outcome(failure=Failure.CODEX_NOT_STOPPED, message="open")


class _Settings:
    """A QSettings stand-in: a test must not write into the user's registry."""

    def __init__(self, values: dict) -> None:
        self.values = values
        self.stored: dict = {}

    def value(self, key):
        return self.stored.get(key, self.values.get(key))

    def setValue(self, key, value) -> None:  # noqa: N802 - Qt naming
        self.stored[key] = value


class _InlineRunner:
    """Runs a job on the spot, so a click's result is readable immediately."""

    busy = False

    def start(self, call, done, progress=None) -> None:
        done(call(progress=progress) if progress is not None else call())


@unittest.skipUnless(_HAS_QT, "PySide6 is an optional extra and is not installed")
class _WindowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # The first-run screen looks for a workspace when it is shown. A test
        # must not walk this machine's cloud folders to do it, so the search is
        # answered here; the test that is about the search replaces this.
        patcher = mock.patch(
            "codexsync.gui.screens.first_run.find_workspaces", return_value=Outcome(value=())
        )
        self.workspace_search = patcher.start()
        self.addCleanup(patcher.stop)

    def pump(self, until=None, *, seconds: float = 3.0) -> None:
        """Run the event loop until ``until()`` holds, or the deadline passes.

        A save is confirmed through ``QTimer.singleShot(0, ...)`` -- the screen
        cannot delete the widget it is being called from -- so the result only
        exists after the loop has turned. A fixed number of ``processEvents``
        calls is a race: it usually wins and occasionally does not, which is
        how this produced an intermittent failure in the full suite while
        passing on its own.
        """
        from PySide6.QtCore import QCoreApplication

        deadline = time.monotonic() + seconds
        while True:
            QCoreApplication.processEvents()
            if until is None or until():
                return
            if time.monotonic() > deadline:
                return
            time.sleep(0.01)

    def make(self, language: str = "en", controller: Controller | None = None, *, confirm: bool = True):
        from codexsync.gui.window import MainWindow

        controller = controller or FakeController()
        # No QSettings: a test must not write into the user's registry.
        window = MainWindow(controller, language=language, runner=_InlineRunner())
        self.confirmations: list[tuple] = []

        def confirm_fn(title, text, label, *, details=None):
            self.confirmations.append((title, text, label, details))
            return confirm

        window.confirm = confirm_fn
        self.addCleanup(window.deleteLater)
        return window, controller


class WindowTests(_WindowTestCase):
    def test_the_window_is_titled_with_the_brand_and_has_its_icon(self) -> None:
        window, _ = self.make()
        self.assertEqual(window.windowTitle(), BRAND_NAME)
        self.assertFalse(window.windowIcon().isNull())

    def test_the_sidebar_lists_every_screen_and_every_screen_is_real(self) -> None:
        from codexsync.gui.window import PAGES, SCREENS

        for language in available_languages():
            with self.subTest(language=language):
                window, _ = self.make(language)
                catalog = load(language)
                self.assertEqual(window._nav.count(), len(PAGES))
                for row, page in enumerate(PAGES):
                    self.assertEqual(window._nav.item(row).text(), catalog.text(f"nav.{page}"))
                    self.assertIs(type(window.screen(page)), SCREENS[page][0])

    def test_choosing_a_screen_shows_it(self) -> None:
        from codexsync.gui.window import PAGES

        window, _ = self.make()
        window.go_to("backups")
        self.assertEqual(window._stack.currentIndex(), PAGES.index("backups"))

    def test_a_missing_config_opens_the_first_run_screen(self) -> None:
        from codexsync.gui.window import PAGES

        controller = FakeController()
        controller.config_exists = lambda: False  # type: ignore[method-assign]
        window, _ = self.make(controller=controller)
        self.assertEqual(window._stack.currentIndex(), PAGES.index("first_run"))

    def test_switching_language_keeps_the_page_and_what_was_loaded(self) -> None:
        from codexsync.gui.window import PAGES

        window, controller = self.make("en")
        window.go_to("sync")
        window.screen("sync").check_plan()
        window.set_language("ru")
        russian = load("ru")
        self.assertEqual(window.catalog.language, "ru")
        self.assertEqual(window._stack.currentIndex(), PAGES.index("sync"))
        self.assertEqual(window._nav.item(0).text(), russian.text("nav.overview"))
        self.assertEqual(window.screen("sync").table.rowCount(), 3)

    def test_a_result_that_lands_after_a_rebuild_is_drawn_by_the_new_widgets(self) -> None:
        window, _ = self.make()
        pending = []
        window._jobs = type("Deferred", (), {"busy": False, "start": lambda self, call, done: pending.append((call, done))})()
        window.go_to("sync")
        window.screen("sync").check_plan()
        window.set_language("ru")
        call, done = pending.pop()
        done(call())
        self.assertEqual(window.screen("sync").table.rowCount(), 3)

    def test_every_screen_draws_every_refusal_in_every_language_and_palette(self) -> None:
        """A blank banner for the one event a user needed explained is a bug."""
        from codexsync.gui import theme
        from codexsync.gui.window import PAGES

        for language in available_languages():
            for palette in (theme.LIGHT, theme.DARK):
                controller = FakeController()
                for failure in Failure:
                    refusal = Outcome(failure=failure, message="because")
                    for name in ("state", "preview_sync", "chats", "scan_sessions", "scan_repair", "guardian_inventory", "backups", "journals", "open_config", "automation"):
                        controller.outcomes[name] = refusal
                    window, _ = self.make(language, controller)
                    window.palette = lambda palette=palette: palette  # type: ignore[method-assign]
                    for page in PAGES:
                        with self.subTest(language=language, dark=palette.dark, failure=failure.value, page=page):
                            window.go_to(page)
                            window.screen(page).render()


class PlacementTests(_WindowTestCase):
    """Where the window opens. Measured against this machine's monitors.

    Both are 1536x864 logical with 824 of usable height, and the second one
    starts at x=1536. The default 1240x820 plus a title bar is taller than 824,
    which is how the title bar ended up above the top edge.
    """

    #: A generous Windows frame: 8px borders and a 31px title bar.
    FRAME = (16, 39)
    WORK_AREA = (1536, 0, 1536, 824)

    def _placed(self, window, *, available=None):
        from PySide6.QtCore import QRect

        area = QRect(*(available or self.WORK_AREA))
        window.place_within_screen(available=area, frame=self.FRAME)
        # ``pos`` is the frame's top-left for a window, so this is the frame.
        return area, QRect(
            window.pos().x(), window.pos().y(),
            window.width() + self.FRAME[0], window.height() + self.FRAME[1],
        )

    def test_the_frame_ends_up_inside_the_work_area_and_below_its_top(self) -> None:
        window, _ = self.make()
        area, frame = self._placed(window)
        self.assertTrue(area.contains(frame), f"{frame} is not inside {area}")
        self.assertGreaterEqual(frame.top(), area.top(), "the title bar must be visible")

    def test_a_size_taller_than_the_screen_is_shrunk_rather_than_pushed_off(self) -> None:
        window, _ = self.make()
        window.resize(1240, 820)
        area, frame = self._placed(window)
        self.assertLess(window.height(), 820, "820 plus a title bar does not fit in 824")
        self.assertTrue(area.contains(frame))

    def test_a_remembered_size_larger_than_the_screen_is_shrunk(self) -> None:
        from codexsync.gui.window import MainWindow

        settings = _Settings({"window/size": ["3000", "2000"]})
        window = MainWindow(FakeController(), language="en", runner=_InlineRunner(), settings=settings)
        self.addCleanup(window.deleteLater)
        area, frame = self._placed(window)
        self.assertTrue(area.contains(frame))

    def test_the_second_monitor_is_not_where_it_opens(self) -> None:
        """Nothing remembered can carry a position: only the size is kept."""
        window, _ = self.make()
        window.move(2000, 400)
        area, frame = self._placed(window, available=(0, 0, 1536, 824))
        self.assertTrue(area.contains(frame), "a window left on the second monitor comes back")

    def test_the_minimum_size_with_a_frame_still_fits_this_screen(self) -> None:
        from codexsync.gui.window import MINIMUM_SIZE

        self.assertLessEqual(MINIMUM_SIZE[1] + self.FRAME[1], 824)
        self.assertLessEqual(MINIMUM_SIZE[0] + self.FRAME[0], 1536)

    def test_a_screen_smaller_than_the_minimum_keeps_the_minimum(self) -> None:
        """Shrinking below the minimum would only hide the sidebar instead."""
        from codexsync.gui.window import MINIMUM_SIZE

        window, _ = self.make()
        self._placed(window, available=(0, 0, 800, 600))
        self.assertEqual((window.width(), window.height()), MINIMUM_SIZE)

    def test_closing_remembers_the_size_and_never_a_position(self) -> None:
        from PySide6.QtGui import QCloseEvent
        from codexsync.gui.window import MainWindow

        settings = _Settings({})
        window = MainWindow(FakeController(), language="en", runner=_InlineRunner(), settings=settings)
        self.addCleanup(window.deleteLater)
        window.resize(1100, 700)
        window.move(1700, 30)
        window.closeEvent(QCloseEvent())
        self.assertEqual(settings.stored["window/size"], [1100, 700])
        self.assertEqual(
            [key for key in settings.stored if "geometry" in key or "pos" in key], [],
            "a position is never kept",
        )


class OverviewTests(_WindowTestCase):
    def test_a_stopped_codex_reads_as_allowed(self) -> None:
        window, _ = self.make()
        screen = window.screen("overview")
        self.assertEqual(screen.banner.title.text(), window.catalog.text("banner.stopped.title"))
        self.assertEqual(screen.table.rowCount(), 3)

    def test_an_undetermined_codex_reads_as_refused_not_as_allowed(self) -> None:
        """UNKNOWN is not optimistically a stopped process, here as everywhere."""
        for language in available_languages():
            with self.subTest(language=language):
                controller = FakeController()
                controller.outcomes["state"] = Outcome(value=_view(stopped=False))
                window, _ = self.make(language, controller)
                title = window.screen("overview").banner.title.text()
                self.assertEqual(title, window.catalog.text("banner.running.title"))
                self.assertNotEqual(title, window.catalog.text("banner.stopped.title"))

    def test_checks_are_labelled_and_counted_in_the_language_s_plural(self) -> None:
        window, _ = self.make("ru")
        screen = window.screen("overview")
        catalog = window.catalog
        self.assertEqual(screen.table.item(0, 0).text(), catalog.text("check.config"))
        self.assertIn(catalog.text("status.PASS"), screen.table.item(1, 1).text())
        self.assertIn(catalog.plural("checks.summary.passed", 2), screen.summary.text())
        self.assertIn(catalog.plural("checks.summary.warnings", 1), screen.summary.text())

    def test_a_check_without_a_label_keeps_its_own_name(self) -> None:
        controller = FakeController()
        controller.outcomes["state"] = Outcome(value=_view(stopped=True, checks=(CheckRow("brand_new_check", "PASS", "ok"),)))
        window, _ = self.make(controller=controller)
        self.assertEqual(window.screen("overview").table.item(0, 0).text(), "brand_new_check")

    def test_a_second_refresh_is_ignored_while_one_is_running(self) -> None:
        window, _ = self.make()
        screen = window.screen("overview")
        screen.model.busy = True
        screen.model.state = None
        screen.refresh()
        self.assertIsNone(screen.model.state, "no second job may start")

    def test_the_dry_run_reports_the_plan_and_that_nothing_was_written(self) -> None:
        window, controller = self.make()
        screen = window.screen("overview")
        screen.start_dry_run()
        self.assertIn(("sync", True), controller.calls)
        self.assertIn(window.catalog.text("dry_run.done"), screen.dry_run_result.text())
        self.assertIn(window.catalog.plural("dry_run.plan.actions", 3), screen.dry_run_result.text())


class OverviewSideTests(_WindowTestCase):
    def test_an_open_journal_is_announced_on_the_overview(self) -> None:
        window, _ = self.make()
        screen = window.screen("overview")
        self.assertTrue(screen.recovery.isVisibleTo(screen))
        self.assertEqual(screen.recovery.title.text(), window.catalog.plural("recovery.blocked.title", 1))

    def test_no_open_journal_shows_no_alert(self) -> None:
        controller = FakeController()
        controller.outcomes["journals"] = Outcome(value=[])
        window, _ = self.make(controller=controller)
        self.assertFalse(window.screen("overview").recovery.isVisibleTo(window.screen("overview")))

    def test_automation_that_is_on_but_not_installed_reads_as_not_applied(self) -> None:
        from codexsync.app import AutomationView
        from codexsync.system_scheduler import SchedulerStatus

        controller = FakeController()
        controller.outcomes["automation"] = Outcome(value=AutomationView(
            True, "sync_dry_run", 900, True, 0, 0, ("py",), (), True, SchedulerStatus(installed=False),
        ))
        window, _ = self.make(controller=controller)
        screen = window.screen("overview")
        self.assertEqual(screen.automation_state.text(), window.catalog.text("overview.automation.state.not_applied"))
        self.assertIn(window.catalog.plural("common.minutes", 15), screen.automation_line.text())


class SyncTests(_WindowTestCase):
    def test_a_real_sync_asks_first_and_does_nothing_when_declined(self) -> None:
        window, controller = self.make(confirm=False)
        screen = window.screen("sync")
        screen.start_run(dry_run=False)
        self.assertEqual(len(self.confirmations), 1)
        self.assertNotIn(("sync", False), controller.calls)

    def test_a_confirmed_sync_runs_and_discards_the_stale_preview(self) -> None:
        window, controller = self.make()
        window.go_to("sync")
        screen = window.screen("sync")
        screen.start_run(dry_run=False)
        self.assertIn(("sync", False), controller.calls)
        self.assertIsNone(screen.model.preview)
        self.assertIn(window.catalog.text("sync.done.apply"), screen.result.text())

    def test_a_dry_run_never_asks(self) -> None:
        window, controller = self.make(confirm=False)
        window.screen("sync").start_run(dry_run=True)
        self.assertEqual(self.confirmations, [])
        self.assertIn(("sync", True), controller.calls)


class SyncFilterTests(_WindowTestCase):
    """The filters narrow the view. They never narrow the plan."""

    PREVIEW = SyncPreview(
        ("skills/a.md", "plugins/p.json"),
        ("skills/b.md", "notes.txt"),
        ("plugins/c.json",),
        False,
    )

    def _screen(self):
        window, controller = self.make()
        controller.outcomes["preview_sync"] = Outcome(value=self.PREVIEW)
        window.go_to("sync")
        screen = window.screen("sync")
        screen.check_plan()
        return window, screen

    def _paths(self, screen) -> list[str]:
        return [screen.table.item(row, 1).text() for row in range(screen.table.rowCount())]

    def test_every_action_is_listed_when_nothing_is_filtered(self) -> None:
        _window, screen = self._screen()
        self.assertEqual(len(self._paths(screen)), 5)
        self.assertEqual(screen.shown.text(), "", "no count while everything is shown")

    def test_the_search_is_a_case_insensitive_substring_of_the_path(self) -> None:
        _window, screen = self._screen()
        screen.search.setText("SKILLS")
        self.assertEqual(sorted(self._paths(screen)), ["skills/a.md", "skills/b.md"])

    def test_the_direction_filter_separates_the_two_sides_and_the_conflicts(self) -> None:
        window, screen = self._screen()
        screen.direction.setCurrentIndex(screen.direction.findData("to_cloud"))
        self.assertEqual(sorted(self._paths(screen)), ["notes.txt", "skills/b.md"])
        screen.direction.setCurrentIndex(screen.direction.findData("conflict"))
        self.assertEqual(self._paths(screen), ["plugins/c.json"])
        self.assertIn(window.catalog.text("sync.direction.conflict"), screen.table.item(0, 0).text())

    def test_the_folder_filter_offers_the_roots_the_plan_touches(self) -> None:
        _window, screen = self._screen()
        offered = [screen.root.itemData(i) for i in range(screen.root.count())]
        self.assertEqual(offered, ["", "plugins", "skills"])
        screen.root.setCurrentIndex(screen.root.findData("plugins"))
        self.assertEqual(sorted(self._paths(screen)), ["plugins/c.json", "plugins/p.json"])

    def test_the_counter_says_how_much_is_hidden(self) -> None:
        window, screen = self._screen()
        screen.search.setText("skills")
        self.assertEqual(
            screen.shown.text(), window.catalog.text("sync.filter.shown", shown=2, total=5),
        )

    def test_the_summary_still_counts_the_whole_plan(self) -> None:
        """A filtered table must not look like a smaller plan."""
        window, screen = self._screen()
        screen.search.setText("skills")
        self.assertIn(window.catalog.plural("sync.count.conflicts", 1), screen.summary.text())
        self.assertTrue(screen.whole_plan.isVisible() or not screen.isVisible())
        self.assertEqual(screen.whole_plan.text(), window.catalog.text("sync.filter.whole_plan"))

    def test_a_search_that_matches_nothing_says_the_plan_is_unchanged(self) -> None:
        window, screen = self._screen()
        screen.search.setText("no-such-path")
        self.assertEqual(self._paths(screen), [])
        self.assertEqual(screen.empty.text(), window.catalog.text("sync.filter.nothing_shown"))

    def test_the_view_survives_a_language_change(self) -> None:
        window, screen = self._screen()
        screen.search.setText("skills")
        screen.direction.setCurrentIndex(screen.direction.findData("to_local"))
        window.set_language("ru")
        screen = window.screen("sync")
        self.assertEqual(screen.search.text(), "skills")
        self.assertEqual(screen.direction.currentData(), "to_local")
        self.assertEqual(self._paths(screen), ["skills/a.md"])


class ChatsTests(_WindowTestCase):
    def _preview(self, window):
        from PySide6.QtCore import Qt

        window.go_to("chats")
        screen = window.screen("chats")
        root = screen.tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            for j in range(group.childCount()):
                if group.child(j).data(0, Qt.UserRole) == "bbbb-2":
                    group.child(j).setSelected(True)
        screen.target.setCurrentIndex(screen.target.findData("p2"))
        screen.preview_selected()
        return screen

    def test_the_tree_groups_chats_under_projects_and_the_unassigned(self) -> None:
        window, _ = self.make()
        window.go_to("chats")
        screen = window.screen("chats")
        self.assertEqual(screen.tree.topLevelItemCount(), 3)
        self.assertEqual(screen.tree.topLevelItem(2).text(0), window.catalog.text("chats.no_project"))

    def test_the_stranded_chats_are_announced_and_offered_for_auto_repair(self) -> None:
        window, _ = self.make()
        window.go_to("chats")
        screen = window.screen("chats")
        self.assertEqual(screen.banner.title.text(), window.catalog.plural("chats.stranded.title", 1))
        self.assertEqual(screen.autofix.currentData(), "p2")

    def test_move_is_locked_until_a_preview_and_then_quotes_its_id(self) -> None:
        window, controller = self.make()
        window.go_to("chats")
        screen = window.screen("chats")
        self.assertFalse(screen.move_button.isEnabled())
        screen = self._preview(window)
        self.assertTrue(screen.move_button.isEnabled())
        screen.move(dry_run=False)
        applied = [kwargs for name, kwargs in controller.calls if name == "move_chats" and kwargs.get("confirm_plan")]
        self.assertEqual(applied[-1]["confirm_plan"], "plan-move-1")
        self.assertEqual(applied[-1]["chat_refs"], ["bbbb-2"])
        self.assertIn("plan-move-1", self.confirmations[-1][1])

    def test_auto_repair_previews_exactly_the_stranded_chats(self) -> None:
        window, controller = self.make()
        window.go_to("chats")
        window.screen("chats").preview_autofix()
        previews = [kwargs for name, kwargs in controller.calls if name == "move_chats" and not kwargs.get("confirm_plan")]
        self.assertEqual(previews[-1]["chat_refs"], ["bbbb-2"])
        self.assertEqual(previews[-1]["to_project"], "p2")


class SessionsTests(_WindowTestCase):
    def test_apply_is_locked_while_a_branch_needs_a_decision(self) -> None:
        controller = FakeController()
        controller.outcomes["scan_sessions"] = Outcome(value=SessionScan(_transfer_plan(conflict=True), Path("C:/p/s.json"), Path("C:/p/r.json"), False))
        window, _ = self.make(controller=controller)
        screen = window.screen("sessions")
        screen.scan()
        self.assertFalse(screen.apply_button.isEnabled())
        self.assertEqual(screen.banner.title.text(), window.catalog.plural("sessions.decide.title", 1))

    def test_a_volatile_plan_cannot_be_applied(self) -> None:
        controller = FakeController()
        controller.outcomes["scan_sessions"] = Outcome(value=SessionScan(_transfer_plan(conflict=False, volatile=True), Path("C:/p/s.json"), Path("C:/p/r.json"), False))
        window, _ = self.make(controller=controller)
        screen = window.screen("sessions")
        screen.scan()
        self.assertFalse(screen.apply_button.isEnabled())

    def test_a_clean_plan_applies_with_its_exact_id_after_confirmation(self) -> None:
        window, controller = self.make()
        screen = window.screen("sessions")
        screen.scan()
        self.assertTrue(screen.apply_button.isEnabled())
        screen.apply(dry_run=False)
        self.assertIn(("apply_sessions", "plan-sessions-1", False), controller.calls)

    def test_a_decision_is_recorded_for_the_selected_conflict_and_the_scan_repeats(self) -> None:
        controller = FakeController()
        controller.outcomes["scan_sessions"] = Outcome(value=SessionScan(_transfer_plan(conflict=True), Path("C:/p/s.json"), Path("C:/p/r.json"), False))
        window, _ = self.make(controller=controller)
        screen = window.screen("sessions")
        screen.scan()
        for row in range(screen.table.rowCount()):
            from PySide6.QtCore import Qt

            if screen.table.item(row, 0).data(Qt.UserRole).conflict_id:
                screen.table.selectRow(row)
        screen.choice.setCurrentIndex(screen.choice.findData("KEEP_REMOTE"))
        screen.resolve()
        self.assertIn(("resolve", "k" * 64, "KEEP_REMOTE"), controller.calls)
        self.assertEqual(sum(1 for call in controller.calls if call[0] == "scan_sessions"), 2)


class ProjectsTests(_WindowTestCase):
    def test_an_ambiguous_plan_cannot_be_applied(self) -> None:
        controller = FakeController()
        controller.outcomes["scan_repair"] = Outcome(value=RepairScan(_repair_plan(ambiguous=True), Path("C:/p/r.json")))
        window, _ = self.make(controller=controller)
        screen = window.screen("projects")
        screen.scan()
        self.assertFalse(screen.apply_button.isEnabled())

    def test_a_clean_plan_applies_with_its_exact_id(self) -> None:
        window, controller = self.make()
        screen = window.screen("projects")
        screen.scan()
        self.assertTrue(screen.apply_button.isEnabled())
        screen.apply(dry_run=False)
        self.assertIn(("apply_repair", "plan-repair-1", False), controller.calls)

    def test_the_project_move_scenario_has_no_button(self) -> None:
        """It is a missing capability, and must not be imitated by a control."""
        from PySide6.QtWidgets import QPushButton

        window, _ = self.make()
        screen = window.screen("projects")
        texts = {button.text() for button in screen.findChildren(QPushButton)}
        self.assertNotIn(window.catalog.text("projects.move.title"), texts)


class BackupsTests(_WindowTestCase):
    def test_restore_unlocks_only_after_a_dry_run_of_the_same_choice(self) -> None:
        window, controller = self.make()
        window.go_to("backups")
        screen = window.screen("backups")
        screen.table.selectRow(0)
        self.assertFalse(screen.apply_button.isEnabled())
        screen.restore(dry_run=True)
        self.assertTrue(screen.apply_button.isEnabled())
        screen.target.setCurrentIndex(screen.target.findData("cloud"))
        self.assertFalse(screen.apply_button.isEnabled(), "a dry run for local proves nothing about cloud")

    def test_a_legacy_backup_cannot_be_restored_from_the_window(self) -> None:
        window, controller = self.make()
        window.go_to("backups")
        screen = window.screen("backups")
        screen.table.selectRow(1)
        self.assertFalse(screen.dry_button.isEnabled())
        screen.restore(dry_run=True)
        self.assertEqual([call for call in controller.calls if call[0] == "restore"], [])


class RecoveryTests(_WindowTestCase):
    def test_an_open_journal_blocks_and_rollback_needs_an_explicit_target(self) -> None:
        window, controller = self.make()
        window.go_to("recovery")
        screen = window.screen("recovery")
        self.assertEqual(screen.banner.title.text(), window.catalog.plural("recovery.blocked.title", 1))
        screen.table.selectRow(0)
        self.assertFalse(screen.rollback_dry.isEnabled(), "the target is never guessed")
        screen.target.setCurrentIndex(screen.target.findData("local"))
        self.assertTrue(screen.rollback_dry.isEnabled())
        self.assertFalse(screen.rollback_apply.isEnabled())
        screen.act("rollback", dry_run=True)
        self.assertTrue(screen.rollback_apply.isEnabled())
        screen.act("rollback", dry_run=False)
        self.assertIn(("rollback", "op-1", "local", False), controller.calls)


class GuardianRestoreScreenTests(_WindowTestCase):
    def _window(self, **outcomes):
        controller = FakeController()
        controller.outcomes.update(outcomes)
        controller.outcomes["guardian_inventory"] = Outcome(value=GuardianInventory(
            "laptop", Path("C:/g"), "snap-2",
            (
                GuardianSnapshotInfo("snap-2", 2, "2026-09-05T10:57:48Z", 10, 16, 6, "PASS", (), "electron-v2", True, True, True),
                GuardianSnapshotInfo("snap-1", 1, "2026-09-02T12:08:21Z", 10, 16, 2, "PASS", (), "electron-v2", True, True, False),
            ),
            (), (),
        ))
        window, controller = self.make(controller=controller)
        window.go_to("guardian")
        return window, controller, window.screen("guardian")

    def test_restore_is_preview_then_dry_run_then_restore_with_the_plan_id(self) -> None:
        window, controller, screen = self._window()
        self.assertFalse(screen.restore_preview.isEnabled(), "nothing selected yet")
        screen.snapshots.selectRow(1)
        screen.preview_restore()
        self.assertIn(("preview_restore", "snap-1"), controller.calls)
        self.assertFalse(screen.restore_apply.isEnabled(), "locked until a dry run passes")
        self.assertIn("16", screen.restore_selected.text())
        screen.restore(dry_run=True)
        self.assertTrue(screen.restore_apply.isEnabled())
        screen.restore(dry_run=False)
        self.assertIn(("apply_restore", "snap-1", "plan-restore-1", False), controller.calls)
        self.assertIn("plan-restore-1", self.confirmations[-1][1])

    def test_choosing_another_snapshot_forgets_the_preview(self) -> None:
        window, controller, screen = self._window()
        screen.snapshots.selectRow(1)
        screen.preview_restore()
        screen.restore(dry_run=True)
        screen.snapshots.selectRow(0)
        self.assertIsNone(screen.model.preview)
        self.assertFalse(screen.restore_apply.isEnabled())


class GuardianAcceptScreenTests(_WindowTestCase):
    _window = GuardianRestoreScreenTests._window

    def test_accepting_is_preview_then_confirmation_quoting_the_plan_id(self) -> None:
        window, controller, screen = self._window()
        self.assertFalse(screen.accept_apply.isEnabled(), "nothing previewed yet")
        screen.preview_accept()
        self.assertIn(("preview_accept",), controller.calls)
        self.assertIn("16", screen.accept_counts.text())
        self.assertTrue(screen.accept_apply.isEnabled())
        screen.accept()
        self.assertIn("plan-accept-1", self.confirmations[-1][1])
        self.assertIn(("apply_accept", "plan-accept-1"), controller.calls)
        self.assertIsNone(screen.model.accept_preview, "a taken decision is not offered again")

    def test_a_refused_state_cannot_be_accepted(self) -> None:
        window, controller, screen = self._window(accept_codes=("STATE_REJECTED",))
        screen.preview_accept()
        self.assertFalse(screen.accept_apply.isEnabled())
        screen.accept()
        self.assertNotIn(("apply_accept", "plan-accept-1"), controller.calls)
        self.assertEqual(self.confirmations, [])


class ProjectMoveScreenTests(_WindowTestCase):
    def _screen(self, controller=None):
        window, controller = self.make(controller=controller)
        window.go_to("projects")
        screen = window.screen("projects")
        screen.move_project.setCurrentIndex(screen.move_project.findData("p1"))
        screen.move_target.setText("E:/new/alpha")
        return window, controller, screen

    def test_move_is_scan_then_dry_run_then_move(self) -> None:
        window, controller, screen = self._screen()
        self.assertFalse(screen.move_apply.isEnabled())
        screen.scan_move()
        self.assertIn(("scan_move", "p1", str(Path("E:/new/alpha"))), controller.calls)
        self.assertTrue(screen.move_dry.isEnabled())
        self.assertFalse(screen.move_apply.isEnabled(), "locked until a dry run passes")
        screen.apply_move(dry_run=True)
        self.assertTrue(screen.move_apply.isEnabled())
        screen.apply_move(dry_run=False)
        self.assertIn(("apply_move", "plan-move-project-1", False), controller.calls)
        self.assertIn("D:/alpha", screen.move_status.text(), "the kept old folder is named")

    def test_a_blocked_plan_names_its_reason_and_cannot_run(self) -> None:
        controller = FakeController()
        controller.outcomes["move_codes"] = ("TARGET_EXISTS",)
        window, controller, screen = self._screen(controller)
        screen.scan_move()
        self.assertFalse(screen.move_dry.isEnabled())
        self.assertIn(window.catalog.text("projects.move.code.TARGET_EXISTS"), screen.move_status.text())

    def test_changing_the_target_discards_the_plan(self) -> None:
        window, controller, screen = self._screen()
        screen.scan_move()
        screen.apply_move(dry_run=True)
        screen.move_target.setText("E:/other/alpha")
        self.assertIsNone(screen.model.move_scan)
        self.assertFalse(screen.move_apply.isEnabled())


class SessionsExtrasTests(_WindowTestCase):
    def test_branches_show_chat_titles_where_this_machine_knows_them(self) -> None:
        import hashlib
        from PySide6.QtCore import Qt

        controller = FakeController()
        plan = _transfer_plan(conflict=False)
        titles = {plan.items[0].session_hash: "first chat"}
        controller.outcomes["scan_sessions"] = Outcome(value=SessionScan(plan, Path("C:/p/s.json"), Path("C:/p/r.json"), False, titles))
        window, _ = self.make(controller=controller)
        screen = window.screen("sessions")
        screen.scan()
        texts = {screen.table.item(r, 4).text() for r in range(screen.table.rowCount())}
        self.assertIn("first chat", texts)
        self.assertIn(plan.items[1].session_hash[:12], texts, "an unknown one keeps its short hash")

    def test_the_session_index_card_reports_both_sides(self) -> None:
        window, _ = self.make()
        window.go_to("sessions")
        text = window.screen("sessions").index_text.text()
        self.assertIn(window.catalog.text("sessions.index.local", records=5, sessions=4), text)
        self.assertIn(window.catalog.text("sessions.index.cloud.missing"), text)


class ActivityTests(_WindowTestCase):
    def test_a_read_only_job_can_be_abandoned_and_a_write_cannot(self) -> None:
        from codexsync.gui.window import MainWindow

        pending = []

        class Deferred:
            busy = False

            def __init__(self):
                self.abandoned = []

            def start(self, call, done):
                pending.append((call, done))
                return len(pending)

            def abandon(self, token):
                self.abandoned.append(token)
                return True

        runner = Deferred()
        window = MainWindow(FakeController(), language="en", runner=runner)
        self.addCleanup(window.deleteLater)
        pending.clear()
        window.go_to("sync")
        sync = window.screen("sync")
        sync.check_plan()                     # read-only: cancellable
        sync.model.run_busy = False
        window.confirm = lambda *a, **k: True
        sync.start_run(dry_run=False)         # writes: not cancellable
        self.assertTrue(window._activity_cancel.isVisibleTo(window))
        window.cancel_waiting()
        self.assertEqual(runner.abandoned, [1], "only the read-only job is abandoned")
        self.assertFalse(sync.model.preview.ok)
        self.assertEqual(sync.model.preview.message, window.catalog.text("activity.cancelled"))
        self.assertTrue(sync.model.run_busy, "the write is still awaited")


@unittest.skipUnless(_HAS_QT, "PySide6 is an optional extra and is not installed")
class ProjectSelectionTests(_WindowTestCase):
    """One row is chosen, and the actions act on that project and no other."""

    def _screen(self):
        window, _ = self.make()
        window.go_to("projects")
        screen = window.screen("projects")
        screen.refresh()
        return window, screen

    def _row_of(self, screen, project_id: str) -> int:
        from PySide6.QtCore import Qt

        for r in range(screen.projects.rowCount()):
            if screen.projects.item(r, 0).data(Qt.UserRole) == project_id:
                return r
        raise AssertionError(f"{project_id} is not listed")

    def test_choosing_a_row_names_that_project(self) -> None:
        _window, screen = self._screen()
        screen.projects.selectRow(self._row_of(screen, "p1"))
        self.assertEqual(screen.selected_project(), "p1")

    def test_the_unassigned_row_is_not_a_project_and_unlocks_nothing(self) -> None:
        """"No project" is a bucket, not something a project action can act on."""
        _window, screen = self._screen()
        screen.projects.selectRow(screen.projects.rowCount() - 1)
        self.assertIsNone(screen.selected_project())
        self.assertFalse(screen.show_chats_button.isEnabled())

    def test_showing_the_chats_goes_there_filtered_to_the_project(self) -> None:
        from codexsync.gui.window import PAGES

        window, screen = self._screen()
        screen.projects.selectRow(self._row_of(screen, "p1"))
        screen.show_chats()
        self.assertEqual(window.model("chats").project, "p1")
        self.assertEqual(window._stack.currentIndex(), PAGES.index("chats"))
        chats = window.screen("chats")
        self.assertTrue(chats.only_project.isVisible() or not chats.isVisible())
        names = [
            chats.tree.topLevelItem(i).text(0) for i in range(chats.tree.topLevelItemCount())
        ]
        self.assertEqual(len(names), 1, "one project is shown, not all of them")

    def test_the_filter_can_be_undone_from_the_chats_screen(self) -> None:
        window, screen = self._screen()
        screen.projects.selectRow(self._row_of(screen, "p1"))
        screen.show_chats()
        chats = window.screen("chats")
        chats.clear_project()
        self.assertEqual(window.model("chats").project, "")
        self.assertGreater(chats.tree.topLevelItemCount(), 1)

    def test_moving_from_the_row_fills_the_move_card(self) -> None:
        _window, screen = self._screen()
        screen.projects.selectRow(self._row_of(screen, "p1"))
        screen.move_selected()
        self.assertEqual(screen.move_project.currentData(), "p1")

    def test_the_selection_survives_a_redraw_and_a_language_change(self) -> None:
        window, screen = self._screen()
        screen.projects.selectRow(self._row_of(screen, "p1"))
        screen.render()
        self.assertEqual(screen.selected_project(), "p1")
        window.set_language("ru")
        screen = window.screen("projects")
        screen.refresh()
        self.assertEqual(screen.selected_project(), "p1")
        self.assertTrue(screen.show_chats_button.isEnabled())

    def test_the_actions_that_wait_for_the_sqlite_answer_are_absent(self) -> None:
        """No delete, merge or repoint button exists while the registry is unproven."""
        _window, screen = self._screen()
        for name in dir(screen):
            self.assertNotIn(
                name, {"delete_button", "merge_button", "relink_button"},
                "a button that might do nothing is worse than no button",
            )


class WorkingSetTests(_WindowTestCase):
    """Carrying one project: what the screen offers and what it says."""

    def _screen(self):
        window, controller = self.make()
        window.go_to("sessions")
        return window, window.screen("sessions"), controller

    def _tree_names(self, screen) -> list[str]:
        root = screen.scope_tree.invisibleRootItem()
        return [root.child(i).text(0) for i in range(root.childCount())]

    def _check(self, screen, name: str) -> None:
        from PySide6.QtCore import Qt

        root = screen.scope_tree.invisibleRootItem()
        for index in range(root.childCount()):
            if root.child(index).text(0) == name:
                root.child(index).setCheckState(0, Qt.Checked)
                return
        raise AssertionError(f"{name} is not offered")

    def test_a_set_stored_earlier_is_shown_when_the_screen_opens(self) -> None:
        """Otherwise the screen would say "everything" while a scan narrowed."""
        from codexsync.session_scope import SessionScope

        window, controller = self.make()
        controller.outcomes["working_set"] = Outcome(
            value=SessionScope(projects=("p1",), chats=())
        )
        window.go_to("sessions")
        screen = window.screen("sessions")
        self.assertEqual(screen.model.scope_projects, ("p1",))

    def test_the_projects_are_offered_with_their_chat_counts(self) -> None:
        _window, screen, _ = self._screen()
        self.assertTrue(self._tree_names(screen), "the tree is drawn from the chat directory")

    def test_nothing_chosen_means_everything_is_carried(self) -> None:
        window, screen, _ = self._screen()
        self.assertEqual(screen.scope_summary.text(), window.catalog.text("sessions.scope.all"))
        self.assertFalse(screen.scope_take.isEnabled(), "there is nothing to take yet")

    def test_choosing_a_project_and_taking_it_saves_the_set_and_counts_it(self) -> None:
        window, screen, controller = self._screen()
        self._check(screen, self._tree_names(screen)[0])
        self.assertTrue(screen.scope_take.isEnabled())
        screen.take_working_set()
        saved = [call for call in controller.calls if call[0] == "save_working_set"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(len(saved[0][1]), 1, "one project was chosen")
        self.assertIn(window.catalog.plural("sessions.scope.count", 12), screen.scope_summary.text())

    def test_carrying_everything_again_clears_the_set(self) -> None:
        window, screen, controller = self._screen()
        self._check(screen, self._tree_names(screen)[0])
        screen.take_working_set()
        screen.clear_working_set()
        self.assertEqual(screen.model.scope_projects, ())
        self.assertEqual(screen.scope_summary.text(), window.catalog.text("sessions.scope.all"))
        self.assertEqual(controller.calls[-1][1], (), "the stored set is emptied too")

    def test_the_choice_survives_a_language_change(self) -> None:
        window, screen, _ = self._screen()
        self._check(screen, self._tree_names(screen)[0])
        chosen = screen.model.scope_projects
        window.set_language("ru")
        screen = window.screen("sessions")
        self.assertEqual(screen.model.scope_projects, chosen)

    def test_a_branch_outside_the_set_is_shown_as_held_back_not_as_an_error(self) -> None:
        from codexsync.semantic_transfer import TransferAction
        from codexsync.gui.screens.sessions import CATEGORIES

        window, _screen, _ = self._screen()
        self.assertEqual(CATEGORIES[TransferAction.OUT_OF_SCOPE.value], "out_of_scope")
        self.assertNotEqual(
            window.catalog.text("sessions.category.out_of_scope"),
            window.catalog.text("sessions.category.decide"),
        )


class MappingFormTests(_WindowTestCase):
    """Rules are written in a form, and the table only shows what is written."""

    def _screen(self):
        window, controller = self.make()
        window.go_to("settings")
        screen = window.screen("settings")
        screen.tabs.setCurrentIndex(screen._tab_ids.index("mappings"))
        return window, screen

    def test_the_table_cannot_be_typed_into(self) -> None:
        from PySide6.QtWidgets import QAbstractItemView

        _window, screen = self._screen()
        self.assertEqual(
            screen.mapping_table.editTriggers(), QAbstractItemView.NoEditTriggers,
            "editing happens in the form, where a path gets a real box",
        )

    def test_the_form_adds_a_rule_and_the_table_shows_it(self) -> None:
        _window, screen = self._screen()
        screen.mapping_id.setText("desktop-to-laptop")
        screen.mapping_from_machine.setEditText("desktop")
        screen.mapping_to_machine.setEditText("laptop")
        screen.mapping_from.setEditText("D:/Projects")
        screen.mapping_to.setText("C:/Work/Projects")
        screen.mapping_case.setCurrentIndex(screen.mapping_case.findData("false"))
        screen.add_mapping()
        self.assertEqual(screen.model.mappings, [{
            "rule_id": "desktop-to-laptop", "source_machine": "desktop",
            "target_machine": "laptop", "from": "D:/Projects", "to": "C:/Work/Projects",
            "case_sensitive": False,
        }])
        self.assertEqual(screen.mapping_table.rowCount(), 1)
        self.assertEqual(screen.mapping_table.item(0, 0).text(), "desktop-to-laptop")

    def test_auto_case_writes_no_key_at_all(self) -> None:
        """The config's third state is the key being absent, not a false."""
        _window, screen = self._screen()
        screen.mapping_id.setText("r")
        screen.add_mapping()
        self.assertNotIn("case_sensitive", screen.model.mappings[0])

    def test_choosing_a_row_loads_it_and_saving_replaces_that_row(self) -> None:
        _window, screen = self._screen()
        screen.mapping_id.setText("first")
        screen.mapping_from.setEditText("D:/a")
        screen.add_mapping()
        screen.mapping_id.setText("second")
        screen.add_mapping()

        screen.mapping_table.selectRow(0)
        self.assertEqual(screen.mapping_id.text(), "first")
        self.assertEqual(screen.mapping_from.currentText(), "D:/a")
        screen.mapping_from.setEditText("D:/b")
        screen.save_mapping()
        self.assertEqual([entry["from"] for entry in screen.model.mappings], ["D:/b", "D:/a"])

    def test_removing_takes_the_selected_rule_and_no_other(self) -> None:
        _window, screen = self._screen()
        for name in ("one", "two"):
            screen.mapping_id.setText(name)
            screen.add_mapping()
        screen.mapping_table.selectRow(1)
        screen.remove_mapping()
        self.assertEqual([entry["rule_id"] for entry in screen.model.mappings], ["one"])

    def test_nothing_selected_means_save_and_remove_are_locked(self) -> None:
        _window, screen = self._screen()
        screen.mapping_table.clearSelection()
        screen.render()
        self.assertFalse(screen.mapping_save.isEnabled())
        self.assertFalse(screen.mapping_remove.isEnabled())

    def test_the_folders_this_machine_cannot_find_are_offered(self) -> None:
        _window, screen = self._screen()
        offered = [screen.mapping_from.itemText(i) for i in range(screen.mapping_from.count())]
        self.assertIn("D:/Projects/NetRuleRouter", offered, "a chat names it and it is not here")
        self.assertIn("D:/Projects/Gone", offered, "a project root that is not here either")

    def test_the_form_says_how_many_chats_a_rule_would_reach(self) -> None:
        window, screen = self._screen()
        screen.mapping_from.setEditText("D:/Projects/NetRuleRouter")
        self.assertEqual(
            screen.mapping_reach.text(), window.catalog.plural("settings.mapping.reach", 26),
        )

    def test_an_unknown_folder_reaches_nothing_rather_than_guessing(self) -> None:
        window, screen = self._screen()
        screen.mapping_from.setEditText("D:/Nowhere")
        self.assertEqual(
            screen.mapping_reach.text(), window.catalog.plural("settings.mapping.reach", 0),
        )

    def test_the_machines_offered_include_the_ones_the_hints_found(self) -> None:
        _window, screen = self._screen()
        offered = {screen.mapping_from_machine.itemText(i)
                   for i in range(screen.mapping_from_machine.count())}
        self.assertTrue({"desktop", "laptop"} <= offered)

    def test_the_rules_survive_a_language_change(self) -> None:
        window, screen = self._screen()
        screen.mapping_id.setText("kept")
        screen.add_mapping()
        window.set_language("ru")
        screen = window.screen("settings")
        self.assertEqual([entry["rule_id"] for entry in screen.model.mappings], ["kept"])
        self.assertEqual(screen.mapping_table.item(0, 0).text(), "kept")


class PathPickerTests(_WindowTestCase):
    """Choosing include_roots from a tree writes exactly what typing would."""

    TREE = {
        "": [
            ("skills", True, False, False),
            ("plugins", True, False, False),
            ("sessions", True, True, False),
            ("tmp", True, False, True),
        ],
        "skills": [("pack", True, False, False), ("notes.md", False, False, False)],
    }

    def _children(self, relative: str):
        from codexsync.sync_candidates import SyncCandidate

        return tuple(
            SyncCandidate(
                relative=f"{relative}/{name}" if relative else name,
                name=name, is_dir=is_dir, local=True, cloud=True,
                semantic_owned=semantic, excluded_by_glob=excluded,
            )
            for name, is_dir, semantic, excluded in self.TREE.get(relative, [])
        )

    def _dialog(self, window, selected):
        from codexsync.gui.widgets import PathTreeDialog

        texts = {
            key: window.catalog.text(catalogue_key) for key, catalogue_key in (
                ("hint", "settings.roots.hint"),
                ("column.path", "settings.roots.column.path"),
                ("column.note", "settings.roots.column.note"),
                ("semantic", "settings.roots.semantic"),
                ("excluded", "settings.roots.excluded"),
                ("cloud_only", "settings.roots.cloud_only"),
                ("local_only", "settings.roots.local_only"),
                ("separator", "common.separator"),
                ("ok", "common.ok"),
                ("cancel", "common.cancel"),
            )
        }
        dialog = PathTreeDialog(
            None, title="t", list_children=self._children, selected=selected, texts=texts,
        )
        self.addCleanup(dialog.deleteLater)
        return dialog

    def _item(self, dialog, name):
        root = dialog.tree.invisibleRootItem()
        for index in range(root.childCount()):
            if root.child(index).text(0) == name:
                return root.child(index)
        raise AssertionError(f"{name} is not in the tree")

    def test_what_is_already_chosen_comes_back_ticked(self) -> None:
        from PySide6.QtCore import Qt

        window, _ = self.make()
        dialog = self._dialog(window, ["skills"])
        self.assertEqual(self._item(dialog, "skills").checkState(0), Qt.Checked)
        self.assertEqual(self._item(dialog, "plugins").checkState(0), Qt.Unchecked)

    def test_a_chosen_child_makes_its_parent_partly_ticked(self) -> None:
        from PySide6.QtCore import Qt

        window, _ = self.make()
        dialog = self._dialog(window, ["skills/pack"])
        self.assertEqual(self._item(dialog, "skills").checkState(0), Qt.PartiallyChecked)

    def test_a_semantic_owned_path_is_shown_with_its_reason_and_cannot_be_ticked(self) -> None:
        from PySide6.QtCore import Qt

        window, _ = self.make()
        dialog = self._dialog(window, [])
        sessions = self._item(dialog, "sessions")
        self.assertFalse(sessions.flags() & Qt.ItemIsUserCheckable)
        self.assertIn(window.catalog.text("settings.roots.semantic"), sessions.text(1))

    def test_a_path_under_an_exclude_rule_is_marked(self) -> None:
        window, _ = self.make()
        dialog = self._dialog(window, [])
        self.assertIn(window.catalog.text("settings.roots.excluded"), self._item(dialog, "tmp").text(1))

    def test_ticking_a_folder_replaces_the_children_chosen_under_it(self) -> None:
        from PySide6.QtCore import Qt

        window, _ = self.make()
        dialog = self._dialog(window, ["skills/pack"])
        self._item(dialog, "skills").setCheckState(0, Qt.Checked)
        self.assertEqual(dialog.chosen(), ["skills"], "one root, not a root and its child")

    def test_children_are_listed_only_when_the_folder_is_opened(self) -> None:
        window, _ = self.make()
        asked: list[str] = []
        original = self._children

        def watched(relative: str):
            asked.append(relative)
            return original(relative)

        dialog = PathPickerTests._dialog(self, window, [])
        dialog._list_children = watched
        skills = self._item(dialog, "skills")
        self.assertEqual(skills.childCount(), 1, "a placeholder stands in until it is opened")
        dialog.tree.expandItem(skills)
        self.assertEqual(asked, ["skills"])
        self.assertEqual(
            sorted(skills.child(i).text(0) for i in range(skills.childCount())),
            ["notes.md", "pack"],
        )

    def test_the_settings_screen_writes_the_choice_into_the_text_box(self) -> None:
        window, _controller = self.make()
        window.go_to("settings")
        screen = window.screen("settings")
        box = screen._widgets[("targets", "include_roots")]
        box.setPlainText("plugins")

        class _Chosen:
            def exec(self) -> bool:
                return True

            @staticmethod
            def chosen() -> list[str]:
                return ["plugins", "skills"]

        screen.choose_roots(dialog_factory=_Chosen)
        self.assertEqual(box.toPlainText().splitlines(), ["plugins", "skills"])

    def test_a_cancelled_dialog_changes_nothing(self) -> None:
        window, _ = self.make()
        window.go_to("settings")
        screen = window.screen("settings")
        box = screen._widgets[("targets", "include_roots")]
        box.setPlainText("plugins")

        class _Cancelled:
            def exec(self) -> bool:
                return False

            @staticmethod
            def chosen() -> list[str]:
                raise AssertionError("a cancelled dialog is never read")

        screen.choose_roots(dialog_factory=_Cancelled)
        self.assertEqual(box.toPlainText(), "plugins")


class ProgressTests(_WindowTestCase):
    """A read that takes seconds says how far it has got, in words from the catalogue."""

    class _ReportingRunner:
        """Reports twice, then finishes -- what a real scan does, in order."""

        busy = False

        def __init__(self) -> None:
            self.reports: list[tuple[str, int, int]] = []

        def start(self, call, done, progress=None) -> None:
            if progress is not None:
                progress("sessions", 120, 252)
                self.reports.append(("sessions", 120, 252))
            done(call(progress=progress) if progress is not None else call())

    def test_the_chats_screen_names_the_phase_and_the_count(self) -> None:
        window, _ = self.make()
        window.go_to("chats")
        screen = window.screen("chats")
        seen: list[str] = []
        original = screen.render

        def watch() -> None:
            seen.append(screen.banner.detail.text())
            original()

        screen.render = watch
        screen.refresh()
        expected = window.catalog.text(
            "progress.of", phase=window.catalog.text("progress.phase.sessions"), done=1, total=2,
        )
        self.assertIn(expected, seen, "the phase line is drawn while the read runs")

    def test_the_status_bar_shows_a_determinate_bar_while_a_read_reports(self) -> None:
        window, _ = self.make()
        window.go_to("chats")
        screen = window.screen("chats")
        bars: list[tuple[int, int, str]] = []
        original = window._update_activity

        def watch() -> None:
            original()
            bars.append((window._activity_bar.maximum(), window._activity_bar.value(),
                         window._activity_label.text()))

        window._update_activity = watch
        screen.refresh()
        determinate = [item for item in bars if item[0] == 2]
        self.assertTrue(determinate, "a known total makes the bar determinate")
        self.assertEqual(determinate[-1][1], 1)
        self.assertIn(window.catalog.text("progress.phase.sessions"), determinate[-1][2])

    def test_nothing_is_reported_once_the_read_is_done(self) -> None:
        window, _ = self.make()
        window.go_to("chats")
        window.screen("chats").refresh()
        self.assertEqual(window.progress_text(""), "")
        self.assertEqual(window.progress_text("chats"), "", "a finished read reports nothing")

    def test_an_unlabelled_phase_falls_back_to_its_own_id(self) -> None:
        """A phase without a label is a bug the i18n test catches, not a blank line."""
        window, _ = self.make()
        window._active["t"] = ("chats", lambda *_: None, True, 0.0)
        window._progress["t"] = ("no-such-phase", 1, 2)
        self.assertIn("no-such-phase", window.progress_text("chats"))


class ConfigScreensTests(_WindowTestCase):
    """First run and settings against a real config file in the sandbox."""

    def setUp(self) -> None:
        super().setUp()
        self.root = SANDBOX / f"gui-config-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "codex").mkdir()

    def _create(self) -> Path:
        path = self.root / "config.toml"
        outcome = Controller(path).create_config(
            path, machine_id="laptop", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"), cloud_root_dir=None,
        )
        self.assertTrue(outcome.ok, outcome.message)
        return path

    def test_first_run_creates_the_config_and_switches_the_window_to_it(self) -> None:
        missing = self.root / "config.toml"
        window, _ = self.make(controller=Controller(missing))
        screen = window.screen("first_run")
        screen.machine.setEditText("laptop")
        screen.codex.setText(str(self.root / "codex"))
        screen.workspace.setText(str(self.root / "workspace"))
        screen.create()
        self.assertTrue(missing.is_file())
        self.assertEqual(window.controller.config_path, missing)
        self.assertEqual(window.machine_id(), "laptop")
        self.assertEqual(list((self.root / "codex").iterdir()), [], "nothing is created inside .codex")

    def test_creating_a_config_remembers_where_it_is(self) -> None:
        """The next start has to find this file from anywhere, not only from here."""
        from codexsync.gui.window import SETTING_CONFIG, MainWindow

        settings = _Settings({})
        missing = self.root / "config.toml"
        window = MainWindow(
            Controller(missing), language="en", runner=_InlineRunner(), settings=settings,
        )
        self.addCleanup(window.deleteLater)
        self.assertNotIn(SETTING_CONFIG, settings.stored, "a file that is not there is not remembered")
        screen = window.screen("first_run")
        screen.machine.setEditText("laptop")
        screen.codex.setText(str(self.root / "codex"))
        screen.workspace.setText(str(self.root / "workspace"))
        screen.create()
        self.assertEqual(settings.stored[SETTING_CONFIG], str(missing))

    def test_opening_another_config_switches_the_window_and_is_remembered(self) -> None:
        from codexsync.gui.window import SETTING_CONFIG, MainWindow

        path = self._create()
        settings = _Settings({})
        window = MainWindow(
            Controller(self.root / "absent.toml"), language="en",
            runner=_InlineRunner(), settings=settings,
        )
        self.addCleanup(window.deleteLater)
        window.open_config(path)
        self.assertEqual(window.controller.config_path, path)
        self.assertEqual(settings.stored[SETTING_CONFIG], str(path))
        self.assertEqual(window.machine_id(), "laptop")

    def test_the_first_run_screen_offers_a_workspace_it_found(self) -> None:
        """The form is filled from a folder codexSync itself made, not typed again."""
        from codexsync.gui.locations import WorkspaceCandidate

        workspace = self.root / "cloud" / "codexSync"
        # Not this machine's host name: the suggestion would match by accident.
        self.workspace_search.return_value = Outcome(
            value=(WorkspaceCandidate(workspace, ("guardian",), ("old-laptop",)),)
        )
        window, _ = self.make(controller=Controller(self.root / "config.toml"))
        screen = window.screen("first_run")
        self.assertTrue(screen.model.searched, "the screen looks once when it is shown")
        self.assertEqual(screen.workspace.text(), str(workspace))
        self.assertIn(str(workspace), screen.found.text())
        self.assertIn("old-laptop", screen.machine_warning.text())
        self.assertNotEqual(screen.machine.currentText(), "old-laptop", "a known name is never picked")
        self.assertGreater(screen.machine.count(), 0, "but it is offered")

    def test_the_language_is_chosen_in_the_page_header_and_not_in_a_tab(self) -> None:
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        self.assertNotIn("interface", screen._tab_ids, "a tab holding one control is not a tab")
        self.assertIsNotNone(screen.language.parent(), "the chooser is on screen")
        self.assertEqual(screen.language.currentData(), "en")

        model_before = window.model("settings")
        screen.language.setCurrentIndex(screen.language.findData("ru"))
        self.pump()
        self.assertEqual(window.catalog.language, "ru")
        self.assertIs(window.model("settings"), model_before, "the page keeps its model")
        self.assertEqual(window.screen("settings").language.currentData(), "ru")

    def test_settings_can_open_another_config_and_the_path_is_remembered(self) -> None:
        from codexsync.gui.window import SETTING_CONFIG, MainWindow

        first = self._create()
        second = self.root / "other.toml"
        outcome = Controller(second).create_config(
            second, machine_id="second-machine", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"), cloud_root_dir=None,
        )
        self.assertTrue(outcome.ok, outcome.message)

        settings = _Settings({})
        window = MainWindow(Controller(first), language="en", runner=_InlineRunner(), settings=settings)
        self.addCleanup(window.deleteLater)
        window.go_to("settings")
        window.screen("settings").switch_config(second)
        self.assertEqual(window.controller.config_path, second)
        self.assertEqual(settings.stored[SETTING_CONFIG], str(second))
        self.assertEqual(window.machine_id(), "second-machine")

    def test_settings_save_changes_one_value_keeps_comments_and_keeps_history(self) -> None:
        path = self._create()
        before = path.read_text(encoding="utf-8")
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        self.assertIsNotNone(screen.model.opened)
        spin = screen._widgets[("backup", "retention_days")]
        spin.setValue(45)
        self.assertEqual(screen.edits().values, {("backup", "retention_days"): 45})
        screen.check(save=True)
        self.pump(lambda: "retention_days = 45" in path.read_text(encoding="utf-8"))
        after = path.read_text(encoding="utf-8")
        self.assertIn("retention_days = 45", after)
        self.assertEqual(len(after.splitlines()), len(before.splitlines()), "only a value changed")
        self.assertIn("# Backup retention period (days)", after)
        self.assertIsNotNone(self.confirmations[-1][3], "the difference is shown before saving")
        history = list((self.root / "workspace" / "config-history").iterdir())
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_text(encoding="utf-8"), before)

    def test_an_invalid_edit_is_refused_and_nothing_is_written(self) -> None:
        path = self._create()
        before = path.read_bytes()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        screen._widgets[("paths", "backup_dir")].setText(str(self.root / "codex" / "backups"))
        screen.check(save=True)
        # Nothing to wait for here: the point is that nothing is written.
        self.pump(seconds=0.2)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(screen.model.result.ok)
        self.assertEqual(self.confirmations, [], "nothing to confirm when the text is invalid")

    def test_a_path_field_shows_what_it_resolves_to(self) -> None:
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        computed = screen._computed[("paths", "backup_dir")]
        screen._widgets[("paths", "backup_dir")].setText("${workspace_root}/backups")
        self.assertIn(str(self.root / "workspace" / "backups"), computed.text())

    def test_the_computed_path_follows_the_workspace_root_as_it_is_edited(self) -> None:
        """The line under the box is recomputed by the loader, not guessed."""
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        screen._widgets[("paths", "backup_dir")].setText("${workspace_root}/backups")
        screen._widgets[("paths", "workspace_root_dir")].setText(str(self.root / "elsewhere"))
        self.assertIn(
            str(self.root / "elsewhere" / "backups"),
            screen._computed[("paths", "backup_dir")].text(),
        )

    def test_a_substitution_without_a_workspace_root_is_reported_not_hidden(self) -> None:
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        screen._widgets[("paths", "workspace_root_dir")].setText("")
        screen._widgets[("paths", "backup_dir")].setText("${workspace_root}/backups")
        text = screen._computed[("paths", "backup_dir")].text()
        self.assertIn(window.catalog.text("failure.configuration"), text)

    def test_the_substitutions_are_listed_with_their_current_values(self) -> None:
        from codexsync.gui.controller import PATH_SUBSTITUTIONS

        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        for token in PATH_SUBSTITUTIONS:
            self.assertIn(token, screen.substitutions.text())
        self.assertIn(str(self.root / "workspace"), screen.substitutions.text())
        self.assertIn(str(self.root / "workspace"), screen._help[("paths", "backup_dir")].text())

    def test_every_sync_option_is_offered_including_the_ones_that_are_refused(self) -> None:
        """A rule the user cannot change is still a rule they should be able to see."""
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")

        direction = screen._widgets[("sync", "direction")]
        self.assertEqual(
            [direction.itemData(i) for i in range(direction.count())],
            ["bidirectional", "to_cloud", "to_local"],
        )
        self.assertTrue(direction.isEnabled())

        deletes = screen._widgets[("sync", "delete_policy")]
        self.assertEqual([deletes.itemData(i) for i in range(deletes.count())], ["never", "propagate"])
        self.assertTrue(deletes.isEnabled())

    def test_a_value_the_rules_forbid_is_shown_and_cannot_be_chosen(self) -> None:
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        for (section, key), blocked in (
            (("sync", "mode"), "hot"),
            (("sync", "session_mode"), "last_date_only"),
        ):
            widget = screen._widgets[(section, key)]
            index = widget.findData(blocked)
            with self.subTest(field=key):
                self.assertGreaterEqual(index, 0, "the value is visible")
                self.assertFalse(widget.model().item(index).isEnabled(), "and not selectable")

    def test_each_refused_value_has_its_reason_on_screen(self) -> None:
        """Not in a tooltip: the reason is a line the user can read."""
        from PySide6.QtWidgets import QLabel

        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        drawn = {widget.text() for widget in screen.findChildren(QLabel)}
        for key in (
            "settings.reason.sync.mode",
            "settings.reason.sync.session_mode",
            "settings.reason.safety.require_codex_stopped",
            "settings.reason.safety.fail_on_unknown",
        ):
            with self.subTest(key=key):
                self.assertIn(window.catalog.text(key), drawn)

    def test_choosing_a_one_way_direction_is_saved_and_the_core_accepts_it(self) -> None:
        from codexsync.config import load_config

        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        widget = screen._widgets[("sync", "direction")]
        widget.setCurrentIndex(widget.findData("to_cloud"))
        self.assertEqual(screen.edits().values, {("sync", "direction"): "to_cloud"})
        screen.check(save=True)
        self.pump(lambda: load_config(path).sync.direction == "to_cloud")
        self.assertEqual(load_config(path).sync.direction, "to_cloud")

    def test_the_safety_switches_are_not_editable(self) -> None:
        path = self._create()
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        self.assertFalse(screen._widgets[("safety", "require_codex_stopped")].isEnabled())
        self.assertFalse(screen._widgets[("safety", "fail_on_unknown")].isEnabled())

    def test_changing_automation_drops_the_legacy_scheduler_keys(self) -> None:
        path = self._create()
        text = path.read_text(encoding="utf-8").replace("[scheduler]\n", "[scheduler]\nkind = \"windows_task_scheduler\"\ninterval_minutes = 10\n")
        path.write_text(text, encoding="utf-8")
        window, _ = self.make(controller=Controller(path))
        window.go_to("settings")
        screen = window.screen("settings")
        screen._widgets[("scheduler", "interval_seconds")].setValue(900)
        edits = screen.edits().values
        self.assertEqual(edits[("scheduler", "interval_seconds")], 900)
        self.assertIn(("scheduler", "kind"), edits)
        self.assertIn(("scheduler", "interval_minutes"), edits)


class AboutTests(_WindowTestCase):
    """The one screen that asks the core nothing still has to be true."""

    def test_the_facts_name_the_running_version_and_how_it_was_started(self) -> None:
        from codexsync.app import __version__

        window, controller = self.make()
        window.go_to("about")
        screen = window.screen("about")
        facts = dict(screen._rows())
        catalog = load("en")
        self.assertEqual(facts[catalog.text("about.build.version")], __version__)
        # Running from the checkout, so never the packaged-executable wording.
        self.assertEqual(facts[catalog.text("about.build.started")], catalog.text("about.build.source"))
        self.assertEqual(facts[catalog.text("about.build.config")], str(controller.config_path))

    def test_a_config_that_does_not_exist_is_shown_as_missing(self) -> None:
        controller = FakeController()
        controller.config_exists = lambda: False  # type: ignore[method-assign]
        window, _ = self.make(controller=controller)
        window.go_to("about")
        catalog = load("en")
        facts = dict(window.screen("about")._rows())
        self.assertEqual(
            facts[catalog.text("about.build.config")],
            catalog.text("about.build.config_missing", path=str(controller.config_path)),
        )

    def test_copying_puts_every_fact_on_the_clipboard_and_says_so(self) -> None:
        from PySide6.QtGui import QGuiApplication

        window, _ = self.make()
        window.go_to("about")
        screen = window.screen("about")
        screen.copy_facts()
        copied = QGuiApplication.clipboard().text()
        for name, value in screen._rows():
            self.assertIn(f"{name}: {value}", copied)
        self.assertEqual(screen.copied_line.text(), load("en").text("about.build.copied"))

    def test_the_documentation_link_follows_the_language(self) -> None:
        for language in available_languages():
            with self.subTest(language=language):
                window, _ = self.make(language)
                window.go_to("about")
                self.assertEqual(window.screen("about")._documentation_language(), language)

    def test_every_sentence_is_drawn_in_the_chosen_language(self) -> None:
        window, _ = self.make("ru")
        window.go_to("about")
        screen = window.screen("about")
        catalog = load("ru")
        self.assertEqual(screen.copy_button.text(), catalog.text("about.build.copy"))
        self.assertIn(catalog.text("about.build.version"), dict(screen._rows()))


if __name__ == "__main__":
    unittest.main()
