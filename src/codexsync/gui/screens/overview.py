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

from PySide6.QtCore import Qt

from .. import theme
from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, make_copyable, row, set_tone, table
from .base import Model, Screen
from .sync import full_preview_text, local_time, run_result


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

    def receive(self, kind: str, outcome: Outcome) -> None:
        if kind == "dry_run":
            # Started on Synchronisation: the same dry run, shown here too.
            self.dry_run = outcome


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
        # Sorted by a click on a column -- the status by how bad it is -- and
        # in the diagnostic's own order until then (CS-338).
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.table.setWordWrap(True)
        make_copyable(self.table, copy_row=self.t("checks.copy.row"), copy_all=self.t("checks.copy.all"))
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
        # The last sync, from the journals this page reads anyway; the button
        # opens the full history on the Synchronisation page.
        self.last_sync = label("", wrap=True)
        self.open_history = button(self.t("overview.open_history"))
        self.open_history.clicked.connect(self._open_history)
        last = row(self.last_sync, self.open_history, stretch_last=False, spacing=18)
        last.setStretchFactor(self.last_sync, 1)
        automation_layout.addLayout(last)
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
            # Journals and automation are read whatever the diagnostic says:
            # an unreadable state is exactly when the last sync and an open
            # journal are worth seeing (CS-274).
            return Outcome(value=(controller.state(), controller.journals(), controller.automation()))

        self.read(go, _apply_state, kind="environment")

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
            self.banner.show_message("danger", self.headline(outcome.failure), self.refusal_text(outcome), palette)
            if not self.banner.isVisible():
                self.banner.setVisible(True)
            self.table.setRowCount(0)
            self.summary.setText("")
        self._render_dry_run()
        self._render_side()

    def _open_automation(self) -> None:
        self.host.go_to("automation")

    def _open_history(self) -> None:
        self.host.go_to("sync")
        self.host.screen("sync").show_history()

    def _render_side(self) -> None:
        palette = self.palette_
        journals = self.model.journals
        open_ = [j for j in journals.value if not j.terminal] if journals is not None and journals.ok else []
        self._render_last_sync()
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

    def _render_last_sync(self) -> None:
        journals = self.model.journals
        if journals is None or not journals.ok:
            self.last_sync.setText("")
            self.last_sync.setVisible(False)
            self.open_history.setVisible(False)
            return
        runs = [j for j in journals.value if j.family == "sync"]
        latest = max(runs, key=lambda j: j.created_at_utc or "", default=None)
        self.last_sync.setVisible(True)
        self.open_history.setVisible(True)
        if latest is None:
            self.last_sync.setText(self.t("overview.last_sync.none"))
            set_tone(self.last_sync, None, self.palette_)
            return
        self.last_sync.setText(self.t(
            "overview.last_sync",
            when=local_time(latest.created_at_utc),
            result=run_result(self, latest),
        ))
        tone = None if latest.readable and latest.state == "COMMITTED" else "attention"
        set_tone(self.last_sync, tone, self.palette_)

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
            process = next((check for check in view.checks if check.name == "codex_process"), None)
            # The process names alone (CS-338); the gate's whole English
            # sentence only from a core that does not name them separately.
            found = "" if process is None else next(
                (str(params.get("processes", "")) for code, params in process.parts if code == "codex_process.found"),
                "" if process.parts else process.details,
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
                Cell(
                    f"●  {status}", tone=theme.STATUS_TONES.get(check.status, "neutral"),
                    sort=SEVERITY.get(check.status, len(SEVERITY)),
                ),
                # The English line stays one hover away: it is what `doctor`
                # prints, and what an issue report quotes.
                Cell(detail_text(self, check), muted=True, tooltip=check.details),
            ])
        fill_table(self.table, rows, palette)
        self.table.resizeRowsToContents()
        passed = len(view.checks) - view.warnings - view.failures
        parts = [self.p("checks.summary.passed", passed)]
        if view.warnings:
            parts.append(self.p("checks.summary.warnings", view.warnings))
        if view.failures:
            parts.append(self.p("checks.summary.failures", view.failures))
        self.summary.setText(self.join(parts))

    # --- the dry run -------------------------------------------------------------

    def start_dry_run(self) -> None:
        """What *Synchronise* would do, and nothing written (CS-411): see `Controller.dry_run`."""
        if self.model.dry_run_busy or self.shared_busy("dry_run", "full_sync"):
            return
        self.model.dry_run_busy = True
        self.model.dry_run = None
        self.render()
        controller = self.host.controller
        self.read(
            lambda progress=None: controller.dry_run(progress=progress), _apply_dry_run,
            progress=True, kind="dry_run",
        )

    def _render_dry_run(self) -> None:
        model = self.model
        # Busy too when the Synchronisation page started it, or is syncing (CS-336).
        running = model.dry_run_busy or self.shared_busy("dry_run")
        self.dry_run_button.setEnabled(not running and not self.shared_busy("full_sync"))
        outcome = model.dry_run
        if running:
            self.dry_run_result.setText(self.progress_text("dry_run") or self.t("dry_run.running"))
            set_tone(self.dry_run_result, None, self.palette_)
        elif outcome is None:
            self.dry_run_result.setText(self.t("dry_run.caption"))
            set_tone(self.dry_run_result, None, self.palette_)
        elif outcome.ok and hasattr(outcome.value, "chats_to_codex"):
            self.dry_run_result.setText(full_preview_text(self, outcome.value))
            tone = "attention" if outcome.value.volatile or outcome.value.chats_to_decide else "ok"
            set_tone(self.dry_run_result, tone, self.palette_)
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


#: How a status sorts: the worst first.
SEVERITY = {"FAIL": 0, "WARN": 1, "PASS": 2}

#: Values that are the config's own words, said with the Settings page's label.
PARAM_LABELS = {
    "direction": "settings.choice.sync.direction.{}",
    "delete_policy": "settings.choice.sync.delete_policy.{}",
}


def detail_text(screen: Screen, check) -> str:
    """A check's details in the window's language (CS-338).

    Each part is a sentence code from `preflight.DETAIL_CODES` with its
    values. A part the catalogue has no sentence for -- a newer core than
    this window, say -- makes the whole line fall back to the English one,
    never to half a translation.
    """
    catalog = screen.host.catalog
    if not check.parts:
        return check.details
    sentences = []
    for code, params in check.parts:
        key = f"check.detail.{code}"
        if not catalog.has(key):
            return check.details
        values = dict(params)
        for name, pattern in PARAM_LABELS.items():
            label_key = pattern.format(values.get(name))
            if name in values and catalog.has(label_key):
                values[name] = catalog.text(label_key)
        if code == "codex_startup.finding":
            # The finding's own sentence is the Recovery page's (`codex.finding.*`).
            nested = f"codex.finding.{values.get('code')}"
            if not catalog.has(nested):
                return check.details
            values = {"finding": catalog.text(nested, **{"chats": 0, "megabytes": 0, "journals": 0, **params})}
        if catalog.is_plural(key):
            count = values.pop("count", 0)
            sentences.append(catalog.plural(key, int(count), **values))
        else:
            sentences.append(catalog.text(key, **values))
    # Chinese runs its sentences together; a space after 。 reads as a typo.
    return catalog.text("checks.sentence_separator").join(sentences)


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
