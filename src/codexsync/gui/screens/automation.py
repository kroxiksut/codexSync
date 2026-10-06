"""Automation: every task the operating system runs for CodexSync, on one page (CS-276).

Four tasks, four cards, one file. The periodic safe job (`[scheduler]`), the
settings sync after sign-in (`sync_at_login`, `D-016`), the copy of `.codex`
(`[state_backup]`, `D-017`) and the handoff watcher (`[handoff]`, `D-018`)
each have their own OS task, and each card shows
its settings next to what the operating system actually has -- installed or
not, last run, what that run's exit code meant. The settings are edited the
way Settings edits them (`ConfigFormScreen`): `config.toml` stays the only
source of truth, and the task is only its applied form, so "Save and update
the tasks" saves first, with the usual review, and then makes all three match.

The copy of `.codex` also lists the copies that exist and can take one now.
"Now" never waits: a window that held a job for hours until Codex closed would
be a window nobody could use. It refuses while Codex is open, with the same
reason the task would log; the task is the one that waits.
"""
from __future__ import annotations

from typing import Any

from ..controller import (
    BROKEN_TASK_CODES,
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    MAX_SCHEDULER_INTERVAL_SECONDS,
    MIN_SCHEDULER_INTERVAL_SECONDS,
    FOREIGN_TASK,
    LEGACY_TASK,
    MAX_HANDOFF_DELIVERY_WAIT_MINUTES,
    MAX_STATE_BACKUP_INTERVAL_HOURS,
    REMOVE,
    Outcome,
)
from ..widgets import Banner, Cell, button, card, fill_table, human_size, label, row, set_tone, table
from .config_form import ConfigFormModel, ConfigFormScreen, Field, quote_part, when_or_never
from .sync import ResultLinks, full_sync_notes, local_time

PERIODIC_FIELDS: tuple[Field, ...] = (
    Field("scheduler", "enabled", "bool", False),
    Field("scheduler", "mode", "choice", "guardian_snapshot", ("guardian_snapshot", "preflight", "sync_dry_run")),
    Field(
        "scheduler", "interval_seconds", "int", DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        minimum=MIN_SCHEDULER_INTERVAL_SECONDS, maximum=MAX_SCHEDULER_INTERVAL_SECONDS,
        step=300, scale=60,
    ),
    Field("scheduler", "run_at_login", "bool", True),
    Field("scheduler", "jitter_seconds", "int", 0, maximum=24 * 3600),
)
LOGIN_FIELDS: tuple[Field, ...] = (
    Field("scheduler", "startup_delay_seconds", "int", 0, maximum=24 * 3600),
    Field("scheduler", "sync_at_login", "bool", False),
)
BACKUP_FIELDS: tuple[Field, ...] = (
    Field("state_backup", "root_dir", "path", ""),
    Field("state_backup", "at_login", "bool", False),
    Field("state_backup", "interval_hours", "int", 0, maximum=MAX_STATE_BACKUP_INTERVAL_HOURS),
    Field("state_backup", "keep", "int", 5, minimum=1, maximum=1000),
)
HANDOFF_FIELDS: tuple[Field, ...] = (
    Field("handoff", "enabled", "bool", False),
    Field("handoff", "delivery_wait_minutes", "int", 15, maximum=MAX_HANDOFF_DELIVERY_WAIT_MINUTES),
    Field("handoff", "notify", "bool", True),
)

#: Keys an older `[scheduler]` section carries that the current contract
#: replaced. Dropped when the automation settings are saved, so the file does
#: not keep saying something nothing reads.
LEGACY_SCHEDULER_KEYS = ("kind", "interval_minutes")
#: How many copies the table lists; the folder may hold other machines' too.
COPIES_SHOWN = 20


class AutomationModel(ConfigFormModel):
    def __init__(self) -> None:
        super().__init__()
        self.automation: Outcome | None = None
        #: A write to the OS tasks (apply, remove, run) is in flight. Only one
        #: at a time: two `schtasks` runs racing over one task leave it in
        #: whichever shape finished last.
        self.task_busy = False
        self.task_kind = ""
        #: A status read is in flight. Kept apart from ``task_busy``: a read
        #: that finishes first must not unlock the buttons while a write runs.
        self.status_busy = False
        #: Counts writes started; a status read taken before the latest one
        #: describes the tasks as they were, and is not kept.
        self.task_generation = 0
        self.task_result: Outcome | None = None
        self.copies: Outcome | None = None
        self.copies_busy = False
        self.copy_busy = False
        self.copy_result: Outcome | None = None
        #: The handoff board (CS-328), read on arrival like the task status.
        self.handoff: Outcome | None = None
        self.handoff_busy = False
        self.handoff_run_busy = False
        self.handoff_result: Outcome | None = None

    def reset_plans(self) -> None:
        super().reset_plans()
        self.automation = None
        self.copies = None
        self.handoff = None


class AutomationScreen(ConfigFormScreen):
    page = "automation"
    scrollable = True

    def form_fields(self):
        return (*PERIODIC_FIELDS, *LOGIN_FIELDS, *BACKUP_FIELDS, *HANDOFF_FIELDS)

    def build(self) -> None:
        self.init_form()
        self.body.addWidget(label(self.t("automation.page.caption"), "muted", wrap=True))

        frame, layout = self.form_card(PERIODIC_FIELDS, self.t("automation.periodic.title"))
        layout.addWidget(label(self.t("automation.periodic.caption"), "muted", wrap=True))
        self.task_state = label("", wrap=True)
        layout.addWidget(self.task_state)
        self.task_banner = Banner()
        layout.addWidget(self.task_banner)
        self.task_details = label("", "muted", wrap=True)
        layout.addWidget(self.task_details)
        self.task_command = label("", "command", wrap=True)
        layout.addWidget(self.task_command)
        self.task_run = button(self.t("automation.run_now"))
        self.task_run.clicked.connect(self.run_task_now)
        layout.addLayout(row(self.task_run))
        self.body.addWidget(frame)

        frame, layout = self.form_card(LOGIN_FIELDS, self.t("automation.login.title"))
        #: The sign-in sync is its own task (CS-267); its state is its own line.
        self.login_task = label("", wrap=True)
        layout.addWidget(self.login_task)
        self.body.addWidget(frame)

        frame, layout = self.form_card(BACKUP_FIELDS, self.t("automation.backup.title"))
        layout.addWidget(label(self.t("automation.backup.caption"), "muted", wrap=True))
        self.backup_task = label("", wrap=True)
        layout.addWidget(self.backup_task)
        self.copies_summary = label("", "muted", wrap=True)
        layout.addWidget(self.copies_summary)
        self.copies_table = table([
            self.t("automation.backup.column.created"),
            self.t("automation.backup.column.size"),
            self.t("automation.backup.column.machine"),
            self.t("automation.backup.column.name"),
        ], stretch=3)
        self.copies_table.setMinimumHeight(150)
        layout.addWidget(self.copies_table)
        self.copy_now = button(self.t("automation.backup.now"))
        self.copy_now.clicked.connect(self.create_copy)
        self.copies_refresh = button(self.t("action.refresh"))
        self.copies_refresh.clicked.connect(self.refresh_copies)
        layout.addLayout(row(self.copy_now, self.copies_refresh))
        self.copy_status = label("", wrap=True)
        layout.addWidget(self.copy_status)
        self.body.addWidget(frame)

        frame, layout = self.form_card(HANDOFF_FIELDS, self.t("automation.handoff.title"))
        layout.addWidget(label(self.t("automation.handoff.caption"), "muted", wrap=True))
        self.handoff_task = label("", wrap=True)
        layout.addWidget(self.handoff_task)
        self.handoff_summary = label("", wrap=True)
        layout.addWidget(self.handoff_summary)
        self.handoff_table = table([
            self.t("automation.handoff.column.machine"),
            self.t("automation.handoff.column.state"),
            self.t("automation.handoff.column.since"),
            self.t("automation.handoff.column.last"),
            self.t("automation.handoff.column.here"),
        ], stretch=0)
        self.handoff_table.setMinimumHeight(110)
        layout.addWidget(self.handoff_table)
        self.handoff_now = button(self.t("automation.handoff.now"))
        self.handoff_now.clicked.connect(self.run_handoff_now)
        self.handoff_refresh = button(self.t("action.refresh"))
        self.handoff_refresh.clicked.connect(self.refresh_handoff)
        layout.addLayout(row(self.handoff_now, self.handoff_refresh))
        self.handoff_status = label("", wrap=True)
        layout.addWidget(self.handoff_status)
        self.handoff_links = ResultLinks(self)
        layout.addLayout(self.handoff_links.layout)
        self.body.addWidget(frame)

        tasks, tasks_layout = card(self.t("automation.task.title"))
        tasks_layout.addWidget(label(self.t("automation.task.caption"), "muted", wrap=True))
        self.task_apply = button(self.t("automation.apply"), primary=True)
        self.task_apply.clicked.connect(self.apply_task)
        self.task_remove = button(self.t("automation.remove"))
        self.task_remove.clicked.connect(self.remove_task)
        self.task_refresh = button(self.t("action.refresh"))
        self.task_refresh.clicked.connect(self.refresh_task)
        tasks_layout.addLayout(row(self.task_apply, self.task_remove, self.task_refresh))
        self.task_status = label("", wrap=True)
        tasks_layout.addWidget(self.task_status)
        self.body.addWidget(tasks)

        self.build_form_actions()

    # --- data flow ---------------------------------------------------------------

    def activated(self) -> None:
        exists = self.host.controller.config_exists()
        if not exists:
            return
        if self.model.opened is None and not self.model.busy:
            self.reload(keep_result=True)
        if self.model.automation is None and not (self.model.task_busy or self.model.status_busy):
            self.refresh_task()
        if self.model.copies is None and not self.model.copies_busy:
            self.refresh_copies()
        if self.model.handoff is None and not self.model.handoff_busy:
            self.refresh_handoff()

    def field_changed(self, field: Field) -> None:
        # The cards describe the draft, not only what was last saved.
        self._render_task()

    def edit_values(self) -> dict[tuple[str, str], Any]:
        values = super().edit_values()
        if any(section == "scheduler" for section, _ in values):
            raw = self._raw() or {}
            scheduler = raw.get("scheduler", {}) if isinstance(raw.get("scheduler"), dict) else {}
            for key in LEGACY_SCHEDULER_KEYS:
                if key in scheduler:
                    values[("scheduler", key)] = REMOVE
        return values

    def refresh_task(self) -> None:
        model = self.model
        if model.task_busy or model.status_busy or not self.host.controller.config_exists():
            return
        model.status_busy = True
        self.render()
        generation = model.task_generation

        def apply(model: AutomationModel, outcome: Outcome) -> None:
            model.status_busy = False
            if model.task_generation == generation:
                model.automation = outcome

        self.read(self.host.controller.automation, apply)

    def refresh_copies(self) -> None:
        if self.model.copies_busy or not self.host.controller.config_exists():
            return
        self.model.copies_busy = True
        self.render()

        def apply(model: AutomationModel, outcome: Outcome) -> None:
            model.copies_busy = False
            model.copies = outcome

        self.read(self.host.controller.codex_backups, apply)

    def refresh_handoff(self) -> None:
        """Read the handoff folder. A config without one is a line, not an error."""
        model = self.model
        if model.handoff_busy or not self.host.controller.config_exists():
            return
        model.handoff_busy = True
        self.render()

        def apply(model: AutomationModel, outcome: Outcome) -> None:
            model.handoff_busy = False
            model.handoff = outcome

        self.read(self.host.controller.handoff, apply)

    def run_handoff_now(self) -> None:
        """Load and hand off now. A write: asked first, never cancellable."""
        model = self.model
        if model.handoff_run_busy:
            return
        if not self.host.confirm(
            self.t("automation.handoff.confirm.title"),
            self.t("automation.handoff.confirm.body"),
            self.t("automation.handoff.now"),
        ):
            return
        model.handoff_run_busy = True
        model.handoff_result = None
        self.render()
        controller = self.host.controller

        def go(progress=None) -> Outcome:
            done = controller.handoff_now(progress=progress)
            return Outcome(value=(done, controller.handoff()))

        def apply(model: AutomationModel, outcome: Outcome) -> None:
            model.handoff_run_busy = False
            if not outcome.ok:
                model.handoff_result = outcome
                return
            model.handoff_result, model.handoff = outcome.value

        self.host.run(self.page, go, apply, progress=True)

    def create_copy(self) -> None:
        """One copy now; the page lists the folder again when it is done."""
        start_copy(self.host)

    def _task_codes(self) -> tuple[str, ...]:
        outcome = self.model.automation
        if outcome is None or not outcome.ok or outcome.value is None:
            return ()
        status = outcome.value.status
        return status.codes if status is not None else ()

    def apply_task(self) -> None:
        """Save pending edits first (with the usual review), then update the OS tasks."""
        if self.model.task_busy:
            return
        codes = self._task_codes()
        if FOREIGN_TASK in codes:
            return
        broken = [code for code in codes if code in BROKEN_TASK_CODES]
        if (broken or LEGACY_TASK in codes) and not self.host.confirm(
            self.t("automation.repair.confirm.title"),
            self.t(
                "automation.repair.confirm.body",
                codes=", ".join(broken or [LEGACY_TASK]),
            ),
            self.t("automation.apply"),
        ):
            return
        if self.model.draft:
            self.check(save=True, then_apply=True)
            return
        self._start_task("apply")

    def after_save_apply(self) -> None:
        self._start_task("apply")

    def remove_task(self) -> None:
        if self.model.task_busy:
            return
        if not self.host.confirm(
            self.t("automation.remove.confirm.title"),
            self.t("automation.remove.confirm.body"),
            self.t("automation.remove"),
        ):
            return
        self._start_task("remove")

    def run_task_now(self) -> None:
        if self.model.task_busy:
            return
        self._start_task("run")

    def _start_task(self, kind: str) -> None:
        model = self.model
        if model.task_busy:
            # Reached without a click too: a save continues here, and the
            # buttons that would have stopped a second start are not in the way.
            return
        model.task_busy = True
        model.task_generation += 1
        model.task_kind = kind
        model.task_result = None
        self.render()
        controller = self.host.controller
        calls = {
            "apply": controller.apply_automation,
            "remove": controller.remove_automation,
            "run": controller.run_automation_now,
        }
        call = calls[kind]

        def go() -> Outcome:
            done = call()
            return Outcome(value=(done, controller.automation()))

        def apply(model: AutomationModel, outcome: Outcome) -> None:
            model.task_busy = False
            if not outcome.ok:
                model.task_result = outcome
                return
            model.task_result, model.automation = outcome.value

        self.run(go, apply)

    # --- drawing -----------------------------------------------------------------

    def render(self) -> None:
        self.render_fields()
        self._render_paths()
        self._render_task()
        self._render_copies()
        self._render_handoff()
        self._render_changes()

    def _value(self, section: str, key: str) -> Any:
        spec = next(spec for spec in self.form_fields() if spec.id == (section, key))
        return self.draft_value(spec)

    def _view(self):
        outcome = self.model.automation
        return outcome.value if outcome is not None and outcome.ok else None

    def _render_task(self) -> None:
        model = self.model
        palette = self.palette_
        mode = self._value("scheduler", "mode")
        mode_key = f"settings.choice.scheduler.mode.{mode}"
        if not self._value("scheduler", "enabled"):
            self.task_state.setText(self.t("automation.summary.disabled"))
        else:
            login_key = (
                "automation.summary.login" if self._value("scheduler", "run_at_login") else "automation.summary.no_login"
            )
            self.task_state.setText(self.t(
                "automation.summary.enabled",
                mode=self.t(mode_key) if self.host.catalog.has(mode_key) else mode,
                every=self._interval(
                    int(self._value("scheduler", "interval_seconds") or DEFAULT_SCHEDULER_INTERVAL_SECONDS)
                ),
                login=self.t(login_key),
            ))

        outcome = model.automation
        view = self._view()
        exists = self.host.controller.config_exists()
        busy = model.task_busy
        self.task_refresh.setEnabled(exists and not busy and not model.status_busy)
        self.task_run.setEnabled(exists and not busy)
        self.task_apply.setEnabled(exists and not busy)
        installed = view is not None and any(
            status is not None and status.installed
            for status in (view.status, view.login_status, view.backup_status, view.handoff_status)
        )
        self.task_remove.setEnabled(exists and not busy and installed)

        if outcome is not None and not outcome.ok:
            self.task_banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
            self.task_details.setText("")
            self.task_command.setVisible(False)
        elif view is None:
            loading = busy or model.status_busy
            self.task_banner.show_message("neutral", self.t("automation.loading") if loading else "", "", palette)
            self.task_details.setText("")
            self.task_command.setVisible(False)
        else:
            self._render_periodic(view)

        text, tone = "", None
        if busy:
            text = self.t(f"automation.working.{model.task_kind}")
        elif model.task_result is not None:
            result = model.task_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
            elif model.task_kind == "run":
                ran = result.value
                ran_key = f"automation.ran.{ran.status}"
                text = self.t(ran_key) if self.host.catalog.has(ran_key) else ran.status
                if ran.actions is not None:
                    text += " " + self.p("sync.count.files", ran.actions)
                if ran.detail:
                    text += " " + ran.detail
                good = {"COMMITTED", "UNCHANGED", "PASSED", "DRY_RUN_FINISHED"}
                tone = "ok" if ran.status in good else "attention"
            elif model.task_kind == "remove":
                text = self.t("automation.removed" if result.value else "automation.nothing_to_remove")
                tone = "ok"
            else:
                text, tone = self.t("automation.applied"), "ok"
        self.task_status.setText(text)
        set_tone(self.task_status, tone, palette)
        self.task_status.setVisible(bool(text))
        self._render_login_task(view, bool(self._value("scheduler", "sync_at_login")))
        self._render_backup_task(view)

    def _render_periodic(self, view) -> None:
        palette = self.palette_
        status = view.status
        if view.status_error:
            tone, title = "danger", self.t("automation.os.error")
        elif status is None or not status.installed:
            tone = "attention" if view.enabled else "neutral"
            title = self.t("automation.os.not_installed")
        elif FOREIGN_TASK in status.codes:
            # Another account's task: shown, never touched.
            tone, title = "attention", self.t("automation.os.foreign")
        elif any(code in BROKEN_TASK_CODES for code in status.codes):
            tone, title = "danger", self.t("automation.os.broken")
        elif LEGACY_TASK in status.codes:
            tone, title = "attention", self.t("automation.os.legacy")
        elif not view.enabled:
            tone, title = "attention", self.t("automation.os.installed_but_disabled")
        elif status.definition_matches is False:
            tone, title = "attention", self.t("automation.os.outdated")
        elif status.enabled is False:
            tone, title = "attention", self.t("automation.os.disabled_in_os")
        else:
            tone, title = "ok", self.t("automation.os.active")
        self.task_banner.show_message(tone, title, view.status_error or "", palette)
        lines = []
        if status is not None and status.installed:
            lines.append(self.t("automation.last_run", when=when_or_never(status.last_run_utc, self.t("automation.never"))))
            lines.append(self.t("automation.next_run", when=when_or_never(status.next_run_utc, self.t("automation.unknown"))))
            if status.last_result is not None:
                lines.append(self.t("automation.last_result", code=status.last_result, meaning=self._exit_meaning(status.last_result)))
            if not view.reports_run_times:
                lines.append(self.t("automation.no_run_times"))
        if status is not None and status.owner and status.owned_by_me is False:
            lines.append(self.t("automation.task.owner", owner=status.owner))
        if status is not None and any(code in BROKEN_TASK_CODES for code in status.codes):
            lines.append(self.t(
                "automation.task.runs",
                command=" ".join(quote_part(part) for part in (status.installed_command or ())),
            ))
        if view.ignored:
            names = ", ".join(self.t(f"settings.field.scheduler.{name}") for name in view.ignored)
            lines.append(self.t("automation.ignored", settings=names))
        self.task_details.setText("\n".join(lines))
        self.task_command.setText(" ".join(quote_part(part) for part in view.argv))
        self.task_command.setVisible(True)

    def _exit_meaning(self, code: int) -> str:
        key = f"automation.exit.{code}"
        return self.t(key) if self.host.catalog.has(key) else self.t("automation.exit.other")

    def _render_login_task(self, view, wanted: bool) -> None:
        """One line on the sign-in sync task: off, waiting to be applied, or its last result."""
        palette = self.palette_
        text, tone = "", None
        if view is not None:
            status = view.login_status
            installed = status is not None and status.installed
            if view.login_status_error:
                text, tone = self.t("automation.login.error", error=view.login_status_error), "danger"
            elif not wanted and not installed:
                text = self.t("automation.login.off")
            elif not wanted:
                text, tone = self.t("automation.login.installed_but_off"), "attention"
            elif not installed:
                text, tone = self.t("automation.login.not_installed"), "attention"
            else:
                result = self.t("automation.never") if status.last_result is None else self._exit_meaning(status.last_result)
                text = self.t(
                    "automation.login.active",
                    when=when_or_never(status.last_run_utc, self.t("automation.never")),
                    result=result,
                )
                tone = "ok"
        elif wanted:
            text = self.t("automation.login.pending")
        self.login_task.setText(text)
        set_tone(self.login_task, tone, palette)
        self.login_task.setVisible(bool(text))

    def _render_backup_task(self, view) -> None:
        """The copy task, described from the draft and from what the OS has."""
        palette = self.palette_
        folder = str(self._value("state_backup", "root_dir") or "").strip()
        at_login = bool(self._value("state_backup", "at_login"))
        hours = int(self._value("state_backup", "interval_hours") or 0)
        wanted = bool(folder) and (at_login or hours > 0)
        when_parts = []
        if at_login:
            when_parts.append(self.t("automation.backup.when.login"))
        if hours > 0:
            when_parts.append(self.t("automation.backup.when.every", every=self.p("common.hours", hours)))
        text, tone = "", None
        status = view.backup_status if view is not None else None
        installed = status is not None and status.installed
        if not folder:
            text = self.t("automation.backup.no_folder")
            tone = "attention" if at_login or hours > 0 else None
        elif view is not None and view.backup_status_error:
            text, tone = self.t("automation.backup.error", error=view.backup_status_error), "danger"
        elif not wanted and not installed:
            text = self.t("automation.backup.off")
        elif not wanted:
            text, tone = self.t("automation.backup.installed_but_off"), "attention"
        elif view is None:
            text = self.t("automation.backup.pending", when=self.join(when_parts))
        elif not installed:
            text, tone = self.t("automation.backup.not_installed", when=self.join(when_parts)), "attention"
        elif status.definition_matches is False:
            text, tone = self.t("automation.backup.outdated"), "attention"
        else:
            result = self.t("automation.never") if status.last_result is None else self._exit_meaning(status.last_result)
            text = self.t(
                "automation.backup.active",
                when=self.join(when_parts),
                last=when_or_never(status.last_run_utc, self.t("automation.never")),
                result=result,
            )
            tone = "ok"
        self.backup_task.setText(text)
        set_tone(self.backup_task, tone, palette)
        self.backup_task.setVisible(bool(text))

    def _render_handoff_task(self, view) -> str:
        """One line on the watcher task, described from the draft and the OS."""
        wanted = bool(self._value("handoff", "enabled"))
        status = view.handoff_status if view is not None else None
        installed = status is not None and status.installed
        if view is not None and view.handoff_status_error:
            return "danger", self.t("automation.handoff.error", error=view.handoff_status_error)
        if not wanted and not installed:
            return "", self.t("automation.handoff.off")
        if not wanted:
            return "attention", self.t("automation.handoff.installed_but_off")
        if view is None:
            return "", self.t("automation.handoff.pending")
        if not installed:
            return "attention", self.t("automation.handoff.not_installed")
        if status.definition_matches is False:
            return "attention", self.t("automation.handoff.outdated")
        return "ok", self.t("automation.handoff.active")

    def _render_handoff(self) -> None:
        model = self.model
        palette = self.palette_
        tone, text = self._render_handoff_task(self._view())
        self.handoff_task.setText(text)
        set_tone(self.handoff_task, tone or None, palette)
        self.handoff_task.setVisible(bool(text))

        exists = self.host.controller.config_exists()
        board = model.handoff
        # The folder is the one beside the sync manifest (CS-335); nobody
        # chooses it, so the button needs only a config.
        self.handoff_now.setEnabled(exists and not model.handoff_run_busy)
        self.handoff_refresh.setEnabled(exists and not model.handoff_busy and not model.handoff_run_busy)

        rows = []
        lines: list[str] = []
        summary_tone = None
        if board is not None and board.ok and board.value is not None:
            status = board.value
            records = ([status.own] if status.own is not None else []) + list(status.others)
            for record in records:
                own = record.machine == status.machine
                if own:
                    here = self.t("automation.handoff.here.own")
                elif record.machine in status.pending:
                    here = self.t("automation.handoff.here.pending")
                elif record.handoff_id:
                    here = self.t("automation.handoff.here.taken")
                else:
                    here = "—"
                rows.append([
                    Cell(record.machine + (" " + self.t("automation.handoff.this_machine") if own else "")),
                    Cell(self.t(f"automation.handoff.state.{record.state}")),
                    Cell(local_time(record.state_since_utc) if record.state_since_utc else "—"),
                    Cell(local_time(record.handed_off_at_utc) if record.handed_off_at_utc else "—"),
                    Cell(here, muted=own),
                ])
            for machine in status.working_elsewhere:
                since = next((r.state_since_utc for r in status.others if r.machine == machine), "")
                lines.append(self.t(
                    "automation.handoff.warn.working", machine=machine,
                    since=local_time(since) if since else "?",
                ))
            for machine in status.pending:
                if machine not in status.working_elsewhere:
                    lines.append(self.t("automation.handoff.warn.pending", machine=machine))
            if status.unreadable:
                lines.append(self.p("automation.handoff.unreadable", len(status.unreadable)))
            if lines:
                summary_tone = "attention"
            elif not records:
                lines.append(self.t("automation.handoff.none"))
        elif board is not None and not board.ok:
            lines.append(self.failure_text(board))
            summary_tone = "danger"
        elif model.handoff_busy:
            lines.append(self.t("automation.loading"))
        self.handoff_summary.setText("\n".join(lines))
        set_tone(self.handoff_summary, summary_tone, palette)
        self.handoff_summary.setVisible(bool(lines))
        fill_table(self.handoff_table, rows, palette)
        self.handoff_table.setVisible(bool(rows))

        text, tone = "", None
        self.handoff_links.show(None)
        if model.handoff_run_busy:
            text = self.progress_text() or self.t("automation.handoff.working")
        elif model.handoff_result is not None:
            result = model.handoff_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
                self.handoff_links.show_stop(result)
            else:
                done = result.value
                parts = [self.t(
                    "automation.handoff.done", files=done.sync_actions, chats=done.session_actions,
                )]
                if done.taken:
                    parts.append(self.t("automation.handoff.done.loaded", machine=", ".join(done.taken)))
                if done.handed_off:
                    parts.append(self.t("automation.handoff.done.handed_off"))
                if done.new_chats_written:
                    parts.append(self.p("automation.handoff.done.new", done.new_chats_written))
                # The same notes and buttons as the Sync page's full sync.
                parts.extend(full_sync_notes(self, done))
                self.handoff_links.show(done)
                text = " ".join(parts)
                tone = "attention" if (
                    done.chats_not_loaded or done.projects_missing_folders or done.chats_codex_ignores
                    or done.project_files_behind or getattr(done, "steps_not_done", ())
                ) else "ok"
        self.handoff_status.setText(text)
        set_tone(self.handoff_status, tone, palette)
        self.handoff_status.setVisible(bool(text))

    def _render_copies(self) -> None:
        model = self.model
        exists = self.host.controller.config_exists()
        file_folder = str(self._file_value(BACKUP_FIELDS[0]) or "").strip()
        self.copy_now.setEnabled(exists and not model.copy_busy and bool(file_folder))
        self.copies_refresh.setEnabled(exists and not model.copy_busy and not model.copies_busy)
        render_copies(self, model, self.copies_summary, self.copies_table, self.copy_status)

    def _interval(self, seconds: int) -> str:
        if seconds % 3600 == 0:
            return self.p("common.hours", seconds // 3600)
        if seconds % 60 == 0:
            return self.p("common.minutes", seconds // 60)
        return self.p("common.seconds", seconds)


def render_copies(screen, model: AutomationModel, summary, copies_table, status) -> None:
    """The copies of `.codex` and the last "copy now", as Automation and Backups both show them.

    Both pages draw from the one Automation model, so a copy started on one
    shows as running on the other and its button is off there too.
    """
    palette = screen.palette_
    copies = model.copies
    rows = []
    if copies is not None and copies.ok:
        listed = list(copies.value or [])
        own = [item for item in listed if item.own]
        for item in listed[:COPIES_SHOWN]:
            rows.append([
                Cell(local_time(item.created_utc)),
                Cell(human_size(item.size)),
                Cell(item.machine if item.own else screen.t("automation.backup.other_machine", machine=item.machine),
                     muted=not item.own),
                Cell(item.name, muted=True),
            ])
        if own:
            summary.setText(screen.p(
                "automation.backup.count", len(own),
                size=human_size(sum(item.size for item in own)),
                latest=local_time(own[0].created_utc),
            ))
        else:
            summary.setText(screen.t("automation.backup.none"))
    elif copies is not None:
        summary.setText(screen.failure_text(copies))
    else:
        summary.setText("")
    summary.setVisible(bool(summary.text()))
    fill_table(copies_table, rows, palette)

    text, tone = "", None
    if model.copy_busy:
        text = screen.host.progress_text("automation") or screen.t("automation.backup.working")
    elif model.copy_result is not None:
        result = model.copy_result
        if not result.ok:
            text, tone = screen.failure_text(result), "danger"
        else:
            made = result.value
            text = screen.t(
                "automation.backup.done",
                name=made.path.name, files=made.files, size=human_size(made.bytes),
            )
            if made.pruned:
                text += " " + screen.p("automation.backup.pruned", len(made.pruned))
            tone = "ok"
    status.setText(text)
    set_tone(status, tone, palette)
    status.setVisible(bool(text))


def start_copy(host) -> None:
    """One copy of `.codex` now, kept on the Automation model whichever page asked."""
    model = host.model("automation")
    if model.copy_busy:
        return
    model.copy_busy = True
    model.copy_result = None
    controller = host.controller

    def go(progress=None) -> Outcome:
        made = controller.create_codex_backup(progress=progress)
        return Outcome(value=(made, controller.codex_backups()))

    def apply(model: AutomationModel, outcome: Outcome) -> None:
        model.copy_busy = False
        if not outcome.ok:
            model.copy_result = outcome
        else:
            model.copy_result, model.copies = outcome.value
        # The window redraws the Automation page; Backups shows the same.
        host.screen("backups").render()

    host.run("automation", go, apply, progress=True)
    host.screen("automation").render()
    host.screen("backups").render()
