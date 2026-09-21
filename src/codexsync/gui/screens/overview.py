"""Overview: what the environment looks like, and whether anything may run.

Every judgement here -- what passed, what warned, whether Codex is closed --
comes back from `run_preflight`, the same diagnostic `doctor` prints. The
banner is deliberately large and deliberately advisory: a mutation is refused
inside `app.py` against a check taken at the moment of the write, so a stale
green banner cannot authorise anything.

There is no refresh timer. A reading that quietly replaced itself would hide
the one event the user most needs to notice -- the moment Codex opened or
closed underneath them -- behind an animation they did not ask for.
"""
from __future__ import annotations

from .. import theme
from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, row, set_tone, table
from .base import Model, Screen


class OverviewModel(Model):
    def __init__(self) -> None:
        self.state: Outcome | None = None
        self.busy = False
        self.dry_run: Outcome | None = None
        self.dry_run_busy = False
        #: Read alongside the diagnostic: open journals block every change, and
        #: the automation line is what the mock-up puts under the dry run.
        self.journals: Outcome | None = None
        self.automation: Outcome | None = None


class OverviewScreen(Screen):
    page = "overview"

    def build(self) -> None:
        self.banner = Banner()
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.banner)

        self.recovery = Banner()
        self.open_recovery = button(self.t("overview.open_recovery"))
        self.open_recovery.clicked.connect(lambda: self.host.go_to("recovery"))
        self.recovery.actions.addWidget(self.open_recovery)
        self.body.addWidget(self.recovery)

        self.summary = label()
        frame, inner = card(self.t("checks.title"), self.summary)
        self.table = table([
            self.t("checks.column.check"),
            self.t("checks.column.status"),
            self.t("checks.column.details"),
        ])
        inner.addWidget(self.table, stretch=1)
        self.body.addWidget(frame, stretch=1)

        self.dry_run_button = button(self.t("action.dry_run"), primary=True)
        self.dry_run_button.clicked.connect(self.start_dry_run)
        self.go_sync = button(self.t("overview.open_sync"))
        self.go_sync.clicked.connect(lambda: self.host.go_to("sync"))
        self.body.addLayout(row(self.dry_run_button, self.go_sync))
        self.dry_run_result = label("", "muted", wrap=True)
        self.body.addWidget(self.dry_run_result)

        automation, automation_layout = card()
        self.automation_line = label("", wrap=True)
        self.automation_state = label("", "cardSummary")
        self.open_automation = button(self.t("overview.open_automation"))
        self.open_automation.clicked.connect(self._open_automation)
        line = row(self.automation_line, self.automation_state, self.open_automation, stretch_last=False, spacing=18)
        line.setStretchFactor(self.automation_line, 1)
        automation_layout.addLayout(line)
        self.body.addWidget(automation)

    def activated(self) -> None:
        if self.model.state is None and not self.model.busy:
            self.refresh()

    # --- the environment reading ---------------------------------------------

    def refresh(self) -> None:
        """Ask for a fresh reading, unless one is already on its way."""
        if self.model.busy:
            return
        self.model.busy = True
        self.model.state = None
        self.render()
        controller = self.host.controller

        def go() -> Outcome:
            state = controller.state()
            if not state.ok:
                return Outcome(value=(state, None, None))
            return Outcome(value=(state, controller.journals(), controller.automation()))

        self.read(go, _apply_state)

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        outcome = model.state
        if outcome is None:
            self.banner.show_message(
                "neutral", self.t("banner.reading") if model.busy else "", "", palette
            )
            self.table.setRowCount(0)
            self.summary.setText("")
        elif outcome.ok:
            self._show_state(outcome.value)
        else:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
            if not self.banner.isVisible():
                self.banner.setVisible(True)
            self.table.setRowCount(0)
            self.summary.setText("")
        self._render_dry_run()
        self._render_side()

    def _open_automation(self) -> None:
        self.host.go_to("settings")
        screen = self.host.screen("settings")
        if "automation" in screen._tab_ids:
            screen.tabs.setCurrentIndex(screen._tab_ids.index("automation"))

    def _render_side(self) -> None:
        palette = self.palette_
        journals = self.model.journals
        open_ = [j for j in journals.value if not j.terminal] if journals is not None and journals.ok else []
        if open_:
            self.recovery.show_message(
                "danger", self.p("recovery.blocked.title", len(open_)), self.t("recovery.blocked.detail"), palette
            )
        else:
            self.recovery.setVisible(False)

        outcome = self.model.automation
        view = outcome.value if outcome is not None and outcome.ok else None
        if view is None:
            self.automation_line.setText(self.t("overview.automation.unknown"))
            self.automation_state.setText("")
            return
        if not view.enabled:
            self.automation_line.setText(self.t("overview.automation.off"))
            self.automation_state.setText(self.t("overview.automation.state.off"))
            set_tone(self.automation_state, None, palette)
            return
        mode_key = f"settings.choice.scheduler.mode.{view.mode}"
        seconds = view.interval_seconds
        every = self.p("common.minutes", seconds // 60) if seconds % 60 == 0 else self.p("common.seconds", seconds)
        login = self.t("automation.summary.login" if view.run_at_login else "automation.summary.no_login")
        self.automation_line.setText(self.t(
            "overview.automation.on",
            mode=self.t(mode_key) if self.host.catalog.has(mode_key) else view.mode,
            every=every,
            login=login,
        ))
        status = view.status
        if status is not None and status.installed and status.definition_matches is not False:
            self.automation_state.setText(self.t("overview.automation.state.on"))
            set_tone(self.automation_state, "ok", palette)
        else:
            self.automation_state.setText(self.t("overview.automation.state.not_applied"))
            set_tone(self.automation_state, "attention", palette)

    def _show_state(self, view) -> None:
        palette = self.palette_
        if view.codex_looks_stopped:
            self.banner.show_message(
                "ok", self.t("banner.stopped.title"), self.t("banner.stopped.detail"), palette
            )
        else:
            # One sentence for two situations, because they permit the same
            # thing: nothing. An undetermined process is never read as a
            # stopped one. What differs is *why*, and the check's own text says
            # it -- including which process was found, which is the difference
            # between "Codex is open" and "an unrelated service is running"
            # (CS-260). Without it, an empty tray makes the banner look broken.
            detail = self.t("banner.running.detail")
            found = next(
                (check.details for check in view.checks if check.name == "codex_process"), ""
            )
            if found:
                named = self.t("banner.running.processes", processes=found)
                detail = f"{detail}\n{named}"
            self.banner.show_message(
                "attention", self.t("banner.running.title"), detail, palette
            )
        rows = []
        for check in view.checks:
            key = f"check.{check.name}"
            name = self.t(key) if self.host.catalog.has(key) else check.name
            status_key = f"status.{check.status}"
            status = self.t(status_key) if self.host.catalog.has(status_key) else check.status
            rows.append([
                Cell(name, tooltip=check.name),
                Cell(f"●  {status}", tone=theme.STATUS_TONES.get(check.status, "neutral")),
                Cell(check.details, muted=True),
            ])
        fill_table(self.table, rows, palette)
        passed = len(view.checks) - view.warnings - view.failures
        parts = [self.p("checks.summary.passed", passed)]
        if view.warnings:
            parts.append(self.p("checks.summary.warnings", view.warnings))
        if view.failures:
            parts.append(self.p("checks.summary.failures", view.failures))
        self.summary.setText(self.join(parts))

    # --- the dry run -------------------------------------------------------------

    def start_dry_run(self) -> None:
        """Build and check the sync plan through the gated path; write nothing."""
        if self.model.dry_run_busy:
            return
        self.model.dry_run_busy = True
        self.model.dry_run = None
        self.render()
        controller = self.host.controller
        self.run(lambda: controller.sync(dry_run=True), _apply_dry_run)

    def _render_dry_run(self) -> None:
        model = self.model
        self.dry_run_button.setEnabled(not model.dry_run_busy)
        outcome = model.dry_run
        if outcome is None:
            key = "dry_run.running" if model.dry_run_busy else "dry_run.caption"
            self.dry_run_result.setText(self.t(key))
            set_tone(self.dry_run_result, None, self.palette_)
        elif outcome.ok:
            result = outcome.value
            plan = self.join([
                self.p("dry_run.plan.actions", result.actions),
                self.p("dry_run.plan.conflicts", result.conflicts),
            ])
            self.dry_run_result.setText(f"{self.t('dry_run.done')} {plan}")
            set_tone(self.dry_run_result, "ok", self.palette_)
        else:
            self.dry_run_result.setText(self.failure_text(outcome))
            set_tone(self.dry_run_result, "danger", self.palette_)


def _apply_state(model: OverviewModel, outcome: Outcome) -> None:
    model.busy = False
    if not outcome.ok:
        # Waiting was abandoned (or the whole reading failed): show why.
        model.state = outcome
        return
    model.state, model.journals, model.automation = outcome.value


def _apply_dry_run(model: OverviewModel, outcome: Outcome) -> None:
    model.dry_run_busy = False
    model.dry_run = outcome
