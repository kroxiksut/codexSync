"""The main window: a sidebar of screens over the controller.

The window holds no knowledge of its own. It owns the controller, the language
catalogue, the palette, one model per screen and the job runner, and hands
screens a small host interface: run a job, ask for confirmation, go to another
screen, know this machine's name. Every judgement a screen draws comes back
from the core; every sentence comes from the catalogue (`i18n.py`); every
colour from the design tokens (`theme.py`).

Widgets are disposable and models are not. Switching the language, the theme
or the config rebuilds every screen from its model, so nothing a user loaded is
lost and a job that finishes during the rebuild is drawn by the new widgets.
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Any, Callable

from PySide6.QtCore import QLocale, QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from . import BRAND_NAME
from . import theme
from .controller import ConfigInfo, Controller, Failure, Outcome
from .i18n import Catalog, available_languages, load, pick_language
from .locations import (
    CONFIG_NAME,
    ConfigChoice,
    choose_config_path,
    frozen_executable_dir,
    read_config_pointer,
    write_config_pointer,
)
from .screens.about import AboutModel, AboutScreen
from .screens.automation import AutomationModel, AutomationScreen
from .screens.backups import BackupsModel, BackupsScreen
from .screens.base import Model, Screen
from .screens.chats import ChatsModel, ChatsScreen
from .screens.first_run import FirstRunModel, FirstRunScreen
from .screens.guardian import GuardianModel, GuardianScreen
from .screens.home import HomeModel, HomeScreen
from .screens.overview import OverviewModel, OverviewScreen
from .screens.projects import ProjectsModel, ProjectsScreen
from .screens.journals import RecoveryModel, RecoveryScreen
from .screens.no_config import NoConfigScreen
from .screens.sessions import SessionsModel, SessionsScreen
from .screens.settings import SettingsModel, SettingsScreen
from .screens.sync import SyncModel, SyncScreen
from .widgets import JobRunner, button, install_wheel_guard, label, set_tone

RESOURCES = Path(__file__).resolve().parent / "resources"
APP_ICON = RESOURCES / "codexsync.ico"
BRAND_MARK = RESOURCES / "brand-mark.png"
#: The mark's "<>" is dark navy; on the dark sidebar it would vanish.
BRAND_MARK_DARK = RESOURCES / "brand-mark-dark.png"

#: Screens in sidebar order. The id names the catalogue keys (``nav.<id>``,
#: ``page.<id>.subtitle``) and is what the window remembers as the last tab.
PAGES = (
    "home",
    "overview",
    "first_run",
    "sync",
    "chats",
    "sessions",
    "projects",
    "guardian",
    "backups",
    "recovery",
    "automation",
    "settings",
    "about",
)

SCREENS: dict[str, tuple[type[Screen], type[Model]]] = {
    "home": (HomeScreen, HomeModel),
    "overview": (OverviewScreen, OverviewModel),
    "first_run": (FirstRunScreen, FirstRunModel),
    "sync": (SyncScreen, SyncModel),
    "chats": (ChatsScreen, ChatsModel),
    "sessions": (SessionsScreen, SessionsModel),
    "projects": (ProjectsScreen, ProjectsModel),
    "guardian": (GuardianScreen, GuardianModel),
    "backups": (BackupsScreen, BackupsModel),
    "recovery": (RecoveryScreen, RecoveryModel),
    "automation": (AutomationScreen, AutomationModel),
    "settings": (SettingsScreen, SettingsModel),
    "about": (AboutScreen, AboutModel),
}
__all__ = ["MainWindow", "PAGES", "SCREENS", "launch"]

#: The only things the window keeps outside config.toml. None of them changes
#: what CodexSync does; see `UI-SPEC-RU.md`, "Источник истины".
#:
#: The size is kept, the position is not. A restored position is what put the
#: window off screen: both monitors here are 1536x864 with 824 of usable
#: height, the second one starts at x=1536, and a window closed there reopened
#: sideways -- or, at the default height plus a title bar, with its title bar
#: above the top edge. A size can be shrunk to fit the screen it opens on; a
#: remembered position cannot be trusted at all, because the screen it was
#: taken from may not be attached any more.
SETTING_SIZE = "window/size"
SETTING_PAGE = "window/page"
SETTING_LANGUAGE = "interface/language"
#: Where earlier builds kept the config path. Read once, then removed.
LEGACY_SETTING_CONFIG = "config/path"

#: Pages that mean something without a config. Every other page shows
#: `NoConfigScreen` until one is opened or created: a screen running against a
#: path nobody chose is how every page came to talk about a file in %APPDATA%
#: that did not exist (CS-268).
WITHOUT_CONFIG = ("first_run", "about")

#: Opening size before anything is remembered, and the floor under it.
DEFAULT_SIZE = (1240, 820)
MINIMUM_SIZE = (980, 640)
#: Kept clear around the frame so the window never touches the work area edge.
SCREEN_MARGIN = 12


class MainWindow(QMainWindow):
    """The sidebar, the screens, and what they share."""

    def __init__(
        self,
        controller: Controller,
        *,
        settings: QSettings | None = None,
        language: str | None = None,
        runner: Any = None,
        choice: ConfigChoice | None = None,
        remember: Callable[[Path], object] | None = None,
    ) -> None:
        super().__init__()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            install_wheel_guard(app)
        self._controller = controller
        self._settings = settings
        #: Records the opened config for the next start and for the command
        #: line (`config_locations.write_config_pointer`). ``None`` records
        #: nothing -- which is what a test or a smoke run must pass, or it
        #: repoints the user's real window at its scratch file.
        self._remember = remember
        #: How the opened config was found. Shown, never decided from: a window
        #: that does not say which file it is running against cannot be argued
        #: with when it turns out to be the wrong one (CS-263).
        self._config_choice = choice
        #: Anything with ``start(call, done)``; tests pass one that runs inline.
        self._jobs = runner if runner is not None else JobRunner(self)
        self._models: dict[str, Model] = {page: SCREENS[page][1]() for page in PAGES}
        self._screens: dict[str, Screen] = {}
        self._config_info: ConfigInfo | None = None
        #: Replaced in tests; a real window asks with a dialog.
        self.confirm: Callable[..., bool] = self._confirm_with_dialog

        if language is None:
            stored = self._setting(SETTING_LANGUAGE)
            language = stored if stored in available_languages() else pick_language(
                QLocale.system().uiLanguages()
            )
        self._catalog: Catalog = load(language)

        self.setWindowTitle(BRAND_NAME)
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setMinimumSize(*MINIMUM_SIZE)
        self.resize(*self._remembered_size())

        self._remember_config()
        self.setStatusBar(QStatusBar())
        #: token -> (page, apply, cancellable, started, model); what the status
        #: bar counts. The model is the one the job started with: after a switch
        #: to another config file a late result must not land on the new pages.
        self._active: dict[Any, tuple[str, Callable[[Any, Outcome], None], bool, float, Model]] = {}
        #: token -> (phase, done, total); the last report a running read made.
        self._progress: dict[Any, tuple[str, int, int]] = {}
        #: Bumped whenever the same config file is saved. A read started under
        #: an older generation built its result from the previous rules, and
        #: is not delivered as if it had not (see `run`).
        self._generation = 0
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._update_activity)
        #: What `config check` says about the open file, read when it is opened.
        #: Drives the banner that offers the upgrade; never a gate -- the refusal
        #: itself stays in core (`_require_mutation_compatible_config`).
        self._config_check: Outcome | None = None
        self._load_config_info()
        stored_page = self._setting(SETTING_PAGE)
        if not controller.config_exists():
            start = "first_run"
        else:
            start = stored_page if stored_page in PAGES else "home"
        self._build(PAGES.index(start))
        self._check_config()

        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self._apply_theme())

    # --- what screens use ------------------------------------------------------

    @property
    def controller(self) -> Controller:
        return self._controller

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    def palette(self) -> theme.Palette:  # type: ignore[override]
        hints = QGuiApplication.styleHints()
        scheme = getattr(hints, "colorScheme", None)
        if scheme is not None and scheme() == Qt.ColorScheme.Dark:
            return theme.DARK
        return theme.LIGHT

    def run(
        self,
        page: str,
        call: Callable[..., Outcome],
        apply: Callable[[Any, Outcome], None],
        *,
        cancellable: bool = False,
        progress: bool = False,
    ) -> None:
        """Run ``call`` off the UI thread and apply its outcome to the page's model.

        ``cancellable`` is for read-only work only: the person may stop waiting,
        which applies a "stopped, nothing changed" outcome at once and drops the
        real one when it arrives. A write is never offered that, because a
        write that is merely no longer watched has not stopped.

        ``progress`` says the call takes a ``progress=`` callback and will
        report how far it has got. Reports arrive on this thread through the
        runner's slot and are kept per job, so the status bar and the page can
        both show the same number without either of them polling.
        """
        key: list[Any] = [None]
        model = self._models[page]
        generation = self._generation

        def done(outcome: Outcome) -> None:
            self._active.pop(key[0], None)
            self._progress.pop(key[0], None)
            stale = cancellable and generation != self._generation
            if stale:
                # The file was saved while this read ran: `config_changed`
                # already forgot every plan, and this one was built under the
                # old rules. It still has to be applied -- only the result
                # clears the page's busy flag -- but as a refusal, and the
                # page forgets whatever it held from before the save.
                outcome = Outcome(failure=Failure.STOPPED_SAFELY, message=self._catalog.text("activity.stale"))
            apply(model, outcome)
            if stale:
                model.reset_plans()
            screen = self._screens.get(page)
            if screen is not None and self._models[page] is model:
                screen.render()
                if stale and self._stack.currentWidget() is screen:
                    # A page that reads on arrival reads again now.
                    screen.activated()
            self._update_activity()

        def report(phase: str, count: int, total: int) -> None:
            if key[0] not in self._active:
                return
            self._progress[key[0]] = (phase, count, total)
            self._update_activity()
            screen = self._screens.get(page)
            if screen is not None:
                screen.render()

        key[0] = object()
        self._active[key[0]] = (page, apply, cancellable, time.monotonic(), model)
        started = self._jobs.start(call, done, report) if progress else self._jobs.start(call, done)
        token = started
        if key[0] in self._active:
            if token is not None:
                entry = self._active.pop(key[0])
                carried = self._progress.pop(key[0], None)
                key[0] = token
                self._active[token] = entry
                if carried is not None:
                    self._progress[token] = carried
            self._update_activity()

    def cancel_waiting(self) -> None:
        """Stop waiting for every cancellable (read-only) job."""
        for token, (page, apply, cancellable, _, model) in list(self._active.items()):
            if not cancellable or not self._jobs.abandon(token):
                continue
            self._active.pop(token, None)
            self._progress.pop(token, None)
            apply(model, Outcome(failure=Failure.STOPPED_SAFELY, message=self._catalog.text("activity.cancelled")))
            screen = self._screens.get(page)
            if screen is not None and self._models[page] is model:
                screen.render()
        self._update_activity()

    def progress_text(self, page: str) -> str:
        """The latest "reading 120 of 252" line for this page, or nothing."""
        for token, (running_page, _apply, _cancellable, _started, model) in self._active.items():
            report = self._progress.get(token)
            if running_page != page or report is None or self._models[page] is not model:
                continue
            phase, count, total = report
            key = f"progress.phase.{phase}"
            name = self._catalog.text(key) if self._catalog.has(key) else phase
            if total <= 0:
                return name
            return self._catalog.text("progress.of", phase=name, done=count, total=total)
        return ""

    def _update_activity(self) -> None:
        if not hasattr(self, "_activity_label"):
            return
        count = len(self._active)
        if count == 0:
            self._activity_timer.stop()
            self._activity_label.setText("")
            # Back to indeterminate: a left-over range would make the next
            # unreported job look like a finished one.
            self._activity_bar.setRange(0, 0)
            self._activity_bar.setVisible(False)
            self._activity_cancel.setVisible(False)
            return
        if not self._activity_timer.isActive():
            self._activity_timer.start()
        oldest = min(started for _, _, _, started, _ in self._active.values())
        reported = next(iter(sorted(self._progress.values(), key=lambda item: -item[2])), None)
        if reported is not None and reported[2] > 0:
            phase, done, total = reported
            key = f"progress.phase.{phase}"
            name = self._catalog.text(key) if self._catalog.has(key) else phase
            self._activity_bar.setRange(0, total)
            self._activity_bar.setValue(done)
            self._activity_label.setText(
                self._catalog.text("progress.of", phase=name, done=done, total=total)
            )
        else:
            self._activity_bar.setRange(0, 0)
            self._activity_label.setText(self._catalog.plural(
                "activity.running", count, seconds=int(time.monotonic() - oldest)
            ))
        self._activity_bar.setVisible(True)
        self._activity_cancel.setVisible(any(cancellable for _, _, cancellable, _, _ in self._active.values()))

    def go_to(self, page: str, target: str | None = None) -> None:
        """Show ``page``; with ``target``, also the place on it a message named.

        A sentence that sends the person somewhere -- "Settings → Sync → New
        chats from another machine" -- comes with a button that calls this, so
        the person lands on the very field, not on a page to search.
        """
        if page in PAGES:
            self._nav.setCurrentRow(PAGES.index(page))
            if target:
                self._screens[page].reveal(target)

    def screen(self, page: str) -> Screen:
        return self._screens[page]

    def model(self, page: str) -> Model:
        return self._models[page]

    def machine_id(self) -> str | None:
        return self._config_info.machine_id if self._config_info else None

    def known_machines(self) -> tuple[str, ...]:
        return self._config_info.machines if self._config_info else ()

    def usual_source(self) -> str | None:
        """The other machine a full sync pairs chats with, if any has handed off."""
        return self._config_info.usual_source if self._config_info else None

    def config_info(self) -> ConfigInfo | None:
        return self._config_info

    def quoted_config(self) -> str:
        if self._controller.config_path is None:
            return CONFIG_NAME
        path = str(self._controller.config_path)
        return f'"{path}"' if " " in path else path

    def config_changed(self, path: Path | None = None) -> None:
        """The config file was saved, created or swapped: redraw everything from it.

        Saving the same file forgets every plan -- one computed under the
        previous rules would otherwise stay on screen looking applicable.
        Another file forgets every page's state: a form, a chosen machine or
        a reading taken for the old file describes that file, and showing it
        next to the new one reads as if the new file had said it.
        """
        if path is not None and Path(path) != self._controller.config_path:
            self._controller = Controller(Path(path))
            # Whatever the start-up search found no longer describes this file.
            self._config_choice = None
            self._models = {page: SCREENS[page][1]() for page in PAGES}
        else:
            self._generation += 1
            for model in self._models.values():
                model.reset_plans()
        self._remember_config()
        self._load_config_info()
        self._build(self._stack.currentIndex())
        self._check_config()

    def set_language(self, language: str) -> None:
        """Redraw every screen in another language, keeping what is shown."""
        if language == self._catalog.language:
            return
        self._catalog = load(language)
        self._store(SETTING_LANGUAGE, self._catalog.language)
        self._build(self._stack.currentIndex())

    def refresh(self) -> None:
        """Take a fresh environment reading on the overview."""
        self._screens["overview"].refresh()

    # --- settings the window may keep ------------------------------------------

    def _setting(self, key: str):
        return None if self._settings is None else self._settings.value(key)

    def _store(self, key: str, value) -> None:
        if self._settings is not None:
            self._settings.setValue(key, value)

    def _remember_config(self) -> None:
        """Keep where this config is, so the next start opens the same one.

        Only when the file is really there: remembering a path that was only
        proposed would send the next start to a file nobody created. The path
        goes into the pointer file rather than `QSettings`, so a terminal
        command without `-c` opens the same file as the window.
        """
        if self._remember is not None and self._controller.config_exists():
            self._remember(self._controller.config_path)

    def open_config(self, path: Path) -> Outcome:
        """Switch only after the selected file proves it is a CodexSync config."""
        candidate = Controller(Path(path))
        outcome = candidate.validate_config_candidate()
        if not outcome.ok:
            return outcome
        self._controller = candidate
        self._config_choice = None
        self._models = {page: SCREENS[page][1]() for page in PAGES}
        self._remember_config()
        self._load_config_info()
        self._build(self._stack.currentIndex())
        self._check_config()
        return outcome

    def _remembered_size(self) -> tuple[int, int]:
        """The size to open at: what was kept, never smaller than the minimum.

        Stored as two plain integers rather than ``saveGeometry`` bytes, so
        that what comes back cannot carry a position with it.
        """
        stored = self._setting(SETTING_SIZE)
        try:
            width, height = (int(value) for value in stored)  # type: ignore[misc]
        except (TypeError, ValueError):
            return DEFAULT_SIZE
        return max(width, MINIMUM_SIZE[0]), max(height, MINIMUM_SIZE[1])

    def place_within_screen(
        self,
        *,
        available: Any = None,
        frame: tuple[int, int] | None = None,
    ) -> None:
        """Shrink to fit the work area and centre there. Call after ``show()``.

        Only then is the frame's size known, and the frame is the whole point:
        a window sized exactly to the work area is taller than it by its title
        bar, and Windows resolves that by pushing the title bar off the top.

        ``available`` and ``frame`` are injected by tests; a real window asks
        the screen it is on and measures its own decorations.
        """
        # ``QWidget.screen`` explicitly: this class names a *page* ``screen``.
        screen = QWidget.screen(self) or QGuiApplication.primaryScreen()
        if available is None:
            if screen is None:  # pragma: no cover - a headless host with no screen
                return
            available = screen.availableGeometry()
        if frame is None:
            outer, inner = self.frameGeometry(), self.geometry()
            frame = (outer.width() - inner.width(), outer.height() - inner.height())
        frame_width, frame_height = max(0, frame[0]), max(0, frame[1])

        width = min(
            self.width(),
            max(MINIMUM_SIZE[0], available.width() - frame_width - 2 * SCREEN_MARGIN),
        )
        height = min(
            self.height(),
            max(MINIMUM_SIZE[1], available.height() - frame_height - 2 * SCREEN_MARGIN),
        )
        self.resize(width, height)

        outer_width, outer_height = width + frame_width, height + frame_height
        left = available.x() + max(0, (available.width() - outer_width) // 2)
        top = available.y() + max(0, (available.height() - outer_height) // 2)
        # ``move`` positions the frame, so this is the frame's top-left.
        self.move(left, top)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # A maximised window would otherwise be remembered at the screen's
        # size, and reopen shrunk to that screen minus its frame for good.
        size = self.normalGeometry().size() if self.isMaximized() or self.isFullScreen() else self.size()
        self._store(SETTING_SIZE, [size.width(), size.height()])
        super().closeEvent(event)

    # --- building ----------------------------------------------------------------

    def _load_config_info(self) -> None:
        outcome = self._controller.config_info() if self._controller.config_exists() else None
        self._config_info = outcome.value if outcome is not None and outcome.ok else None

    def _build(self, page_index: int) -> None:
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())

        self._stack = QStackedWidget()
        self._stack.setObjectName("content")
        self.setStyleSheet(theme.stylesheet(self.palette(), RESOURCES))
        self._screens = {}
        has_config = self._controller.config_exists()
        for page in PAGES:
            screen_class, _ = SCREENS[page]
            if has_config or page in WITHOUT_CONFIG:
                screen = screen_class(self, self._models[page])
            else:
                screen = NoConfigScreen(self, self._models[page], page)
            self._screens[page] = screen
            self._stack.addWidget(screen)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_config_banner())
        column.addWidget(self._stack, stretch=1)
        row.addWidget(content, stretch=1)

        self.setCentralWidget(root)
        self._nav.currentRowChanged.connect(self._show_page)
        self._nav.setCurrentRow(page_index)
        self._show_page(page_index)
        self._update_status_bar()

    # --- an outdated config ------------------------------------------------------

    def _check_config(self) -> None:
        """Read what `config check` says about the file just opened. Reads only.

        Opening a config written by an earlier version must say so at once,
        not when the first sync is refused with core's English sentence.
        """
        self._config_check = None
        self._render_config_banner()
        if not self._controller.config_exists():
            return
        controller = self._controller

        def done(outcome: Outcome) -> None:
            if self._controller is not controller:
                return  # another file was opened meanwhile
            self._config_check = outcome
            self._render_config_banner()

        self._jobs.start(lambda: controller.config_migration(), done)

    def config_upgrade_plan(self):
        """The migration plan to offer, or None when there is nothing to push.

        Optional findings alone are not pushed: a person may have kept them on
        purpose, and a banner on every start for that would be nagging. They
        stay on the Settings card.
        """
        outcome = self._config_check
        if outcome is None or not outcome.ok or not outcome.value:
            return None
        plan, diff = outcome.value
        if plan.is_current or not diff:
            return None
        if not any(not finding.optional for finding in plan.fixable):
            return None
        return plan

    def _build_config_banner(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("configBanner")
        row = QHBoxLayout(frame)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(12)
        self._banner_text = label(wrap=True)
        row.addWidget(self._banner_text, stretch=1)
        self._banner_button = button(self._catalog.text("banner.config.open"), primary=True)
        self._banner_button.clicked.connect(self.offer_config_upgrade)
        row.addWidget(self._banner_button)
        self._banner = frame
        self._render_config_banner()
        return frame

    def _render_config_banner(self) -> None:
        if not hasattr(self, "_banner"):
            return
        plan = self.config_upgrade_plan()
        self._banner.setVisible(plan is not None)
        if plan is None:
            return
        blocked = bool(plan.blockers)
        self._banner_text.setText(
            self._catalog.text("banner.config.outdated" if blocked else "banner.config.differs")
        )
        set_tone(self._banner_text, "danger" if blocked else "attention", self.palette())

    def offer_config_upgrade(self) -> None:
        """Open the Settings card with this very plan, unfolded."""
        if self.config_upgrade_plan() is None:
            return
        settings = self._models["settings"]
        settings.migration = self._config_check
        settings.migration_skip = set()
        settings.migration_details = True
        settings.migration_result = None
        self.go_to("settings")
        self._screens["settings"].render()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(16, 22, 16, 14)
        column.setSpacing(16)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        mark = QLabel()
        brand_mark = BRAND_MARK_DARK if self.palette().dark and BRAND_MARK_DARK.is_file() else BRAND_MARK
        if brand_mark.is_file():
            mark.setPixmap(QIcon(str(brand_mark)).pixmap(34, 34))
        brand.addWidget(mark)
        name = QLabel(BRAND_NAME)
        name.setObjectName("brandName")
        brand.addWidget(name, stretch=1)
        column.addLayout(brand)

        self._nav = QListWidget()
        self._nav.setObjectName("nav")
        self._nav.setFocusPolicy(Qt.NoFocus)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for page in PAGES:
            self._nav.addItem(self._catalog.text(f"nav.{page}"))
        column.addWidget(self._nav, stretch=1)

        machine = QLabel(self._sidebar_machine_text())
        machine.setObjectName("muted")
        machine.setWordWrap(True)
        column.addWidget(machine)
        return sidebar

    def _sidebar_machine_text(self) -> str:
        if self._config_info is None or not self._config_info.machine_id:
            return self._catalog.text("sidebar.no_config")
        return self._catalog.text("sidebar.machine", machine=self._config_info.machine_id)

    def _config_line(self) -> str:
        """``Config: <path> (found in the current folder)``.

        The reason is half the sentence. A path alone still leaves "why this
        one" unanswered, and that question cost a whole session once.
        """
        path = self._controller.config_path
        if path is None:
            return self._catalog.text("statusbar.no_config")
        choice = self._config_choice
        if choice is None or choice.path is None or Path(choice.path) != Path(path):
            return self._catalog.text("statusbar.config", path=path)
        return self._catalog.text(
            "statusbar.config_from",
            path=path,
            source=self._catalog.text(f"config.source.{choice.source}"),
        )

    def rejected_remembered(self) -> Path | None:
        """A remembered config that was deliberately not opened, if there was one."""
        choice = self._config_choice
        return None if choice is None else choice.rejected_remembered

    def _show_page(self, index: int) -> None:
        if 0 <= index < len(PAGES):
            self._stack.setCurrentIndex(index)
            self._store(SETTING_PAGE, PAGES[index])
            self._screens[PAGES[index]].activated()

    def _update_status_bar(self) -> None:
        bar = self.statusBar()
        bar.showMessage(self._config_line())
        for name in ("_activity_bar", "_activity_label", "_activity_cancel"):
            old = getattr(self, name, None)
            if old is not None:
                bar.removeWidget(old)
                old.deleteLater()
        self._activity_bar = QProgressBar()
        self._activity_bar.setRange(0, 0)
        self._activity_bar.setMaximumWidth(120)
        self._activity_bar.setMaximumHeight(12)
        self._activity_bar.setTextVisible(False)
        self._activity_label = QLabel("")
        self._activity_cancel = QPushButton(self._catalog.text("activity.cancel"))
        self._activity_cancel.setObjectName("statusAction")
        self._activity_cancel.clicked.connect(self.cancel_waiting)
        bar.addPermanentWidget(self._activity_label)
        bar.addPermanentWidget(self._activity_bar)
        bar.addPermanentWidget(self._activity_cancel)
        self._update_activity()

    # --- theme -------------------------------------------------------------------

    def _apply_theme(self) -> None:
        self._build(self._stack.currentIndex())

    # --- dialogs -----------------------------------------------------------------

    def _confirm_with_dialog(
        self, title: str, text: str, confirm_label: str, *, details: str | None = None
    ) -> bool:  # pragma: no cover - modal
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(text)
        if details:
            box.setDetailedText(details)
        yes = box.addButton(confirm_label, QMessageBox.AcceptRole)
        cancel = box.addButton(self._catalog.text("common.cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is yes


def _claim_taskbar_identity() -> None:
    """Make Windows group the window under its own icon, not python.exe's."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodexSync.GUI")
    except (AttributeError, OSError):
        pass


def _startup_controller(choice: ConfigChoice) -> tuple[Controller, ConfigChoice]:
    """Adopt an implicit config only after it proves it belongs to CodexSync.

    Explicit `-c` keeps its historical behavior: the user deliberately named
    that path, including a missing or invalid file they may want diagnosed.
    Remembered, current-directory, and executable-adjacent files are only hints.
    A stale pointer to Codex's own `.codex/config.toml` must never make the
    whole window run against the wrong configuration.
    """
    controller = Controller(choice.path)
    if choice.path is None or not choice.exists or choice.source == "explicit":
        return controller, choice
    outcome = controller.validate_config_candidate()
    if outcome.ok:
        return controller, choice
    rejected = choice.path if choice.source == "remembered" else choice.rejected_remembered
    return Controller(None), ConfigChoice(
        path=None,
        source="none",
        exists=False,
        rejected_remembered=rejected,
    )


def launch(config: str | Path | Controller | None = None) -> int:
    """Show the window and run until it closes.

    Takes what ``-c`` said, or nothing: which file that becomes is
    `locations.choose_config_path`, fed with the pointer the window keeps
    for itself and for the command line. A ``Controller`` may be passed instead by a host that already
    built one.

    Reuses an existing ``QApplication`` when there is one, so a host that
    already owns the event loop is not handed a second one.
    """
    _claim_taskbar_identity()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(BRAND_NAME)
    app.setStyle("Fusion")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    settings = QSettings(BRAND_NAME, BRAND_NAME)
    choice: ConfigChoice | None = None
    if isinstance(config, Controller):
        controller = config
    else:
        # Builds before the pointer kept the path in QSettings; it is read once
        # as a fallback and the key dropped, so the pointer is the one record.
        remembered = read_config_pointer() or settings.value(LEGACY_SETTING_CONFIG)
        settings.remove(LEGACY_SETTING_CONFIG)
        choice = choose_config_path(
            config, remembered, executable_dir=frozen_executable_dir(),
        )
        controller, choice = _startup_controller(choice)
    window = MainWindow(controller, settings=settings, choice=choice, remember=write_config_pointer)
    window.show()
    window.place_within_screen()
    return int(app.exec())
