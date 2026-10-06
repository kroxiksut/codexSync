"""Synchronisation: the plan in both directions, a dry run, and the real sync.

Three buttons, three different promises. *Check plan* builds the plan the way
`codexsync plan` does -- it runs while Codex is open, writes nothing, and its
result is marked volatile. *Dry run* goes through the gate and the engine and
writes nothing. *Synchronise* never reuses what is on screen: `app.py` builds
the plan again behind the gate at the moment of the write, so a stale table
cannot be what gets applied. The confirmation says so.

The search and the two filters narrow *what is shown* and nothing else. `sync`
has no partial apply -- the plan is taken whole -- so a filtered table is a
view, and the line under it says exactly that. Anything else would be a screen
that looks like it can send half a plan.

The History tab lists past runs from the operation journals -- the same files
the Recovery page reads, so a history needs no store of its own. Reading them
touches neither `.codex` nor the gate, which is why it may load on arrival.

A full sync that stopped on something both machines changed offers the answer
where the question is (D-027): keep the newer copies, this machine's, or the
cloud's, for this run -- and, with the box ticked, as `[conflict] policy` for
every run after it. The run is the same full sync, decided by that rule; the
copy not kept is saved first, as always.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QTabWidget

from ..controller import Failure, Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, row, set_tone, table
from .base import Model, Screen

#: Filter values for the direction box, in the order they are offered.
DIRECTIONS = ("all", "to_local", "to_cloud", "conflict")
#: The views under the buttons, in tab order.
VIEWS = ("plan", "history")
#: How many past runs the History tab shows.
HISTORY_LIMIT = 150
#: Journal failures the History tab names in words; anything else shows its class.
KNOWN_FAILURES = (
    "Abandoned",
    "ConflictError",
    "SafetyPreconditionError",
    "FailSafeError",
    "OperationBusyError",
    "ConfigError",
    "OSError",
    "PermissionError",
    "FileNotFoundError",
)
#: Who may have started a run (`run_sync(origin=...)`).
ORIGINS = ("window", "cli", "unattended", "handoff")
#: The answers a stopped sync offers, in button order: `[conflict] policy`
#: values, each decided for files and chats alike (D-027).
DECIDE_POLICIES = ("prefer_newer_mtime", "prefer_local", "prefer_cloud")


class SyncModel(Model):
    def __init__(self) -> None:
        self.preview: Outcome | None = None
        self.preview_busy = False
        self.result: Outcome | None = None
        self.run_busy = False
        #: Which kind of run produced `run`: "dry" or "apply".
        self.run_kind = ""
        #: What is shown. Kept on the model, so switching language or theme
        #: rebuilds the widgets without losing the view the user set up.
        self.search = ""
        self.direction = "all"
        self.root = ""
        self.view = "plan"
        self.history: Outcome | None = None
        self.history_busy = False

    def reset_plans(self) -> None:
        self.preview = None
        self.result = None
        self.history = None


class SyncScreen(Screen):
    page = "sync"

    def build(self) -> None:
        self.check_button = button(self.t("sync.check"))
        self.check_button.clicked.connect(self.check_plan)
        self.dry_button = button(self.t("action.dry_run"))
        self.dry_button.clicked.connect(lambda: self.start_run(dry_run=True))
        self.apply_button = button(self.t("sync.apply"), primary=True)
        self.apply_button.clicked.connect(lambda: self.start_run(dry_run=False))
        self.body.addLayout(row(self.check_button, self.dry_button, self.apply_button))
        self.caption = label(self.t("sync.caption"), "muted", wrap=True)
        self.body.addWidget(self.caption)

        self.banner = Banner()
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(None, self.summary)
        inner.addLayout(self._build_filters())
        self.table = table([self.t("sync.column.direction"), self.t("sync.column.path")])
        inner.addWidget(self.table, stretch=1)
        self.empty = label("", "muted", wrap=True)
        inner.addWidget(self.empty)
        self.whole_plan = label(self.t("sync.filter.whole_plan"), "muted", wrap=True)
        inner.addWidget(self.whole_plan)

        self.views = QTabWidget()
        self.views.setDocumentMode(True)
        self.views.addTab(frame, self.t("sync.plan.title"))
        self.views.addTab(self._build_history(), self.t("sync.tab.history"))
        self.views.setCurrentIndex(VIEWS.index(self.model.view) if self.model.view in VIEWS else 0)
        self.views.currentChanged.connect(self._view_changed)
        self.body.addWidget(self.views, stretch=1)

        self.result = label("", wrap=True)
        self.body.addWidget(self.result)
        # The answers to a stopped sync, beside the sentence that asks (D-027).
        self.decide_buttons = {}
        for index, policy in enumerate(DECIDE_POLICIES):
            choice = button(self.t(f"sync.decide.{policy}"), primary=index == 0)
            choice.clicked.connect(lambda _=False, policy=policy: self.decide(policy))
            self.decide_buttons[policy] = choice
        self.decide_button = button(self.t("sync.decisions.open"))
        self.decide_button.clicked.connect(self.open_decisions)
        self.remember = QCheckBox(self.t("sync.decide.remember"))
        self.decide_row = row(*self.decide_buttons.values(), self.decide_button)
        self.body.addLayout(self.decide_row)
        self.body.addWidget(self.remember)
        self._show_decisions(False)
        self.links = ResultLinks(self)
        self.body.addLayout(self.links.layout)

    def _show_decisions(self, visible: bool) -> None:
        for widget in (*self.decide_buttons.values(), self.decide_button, self.remember):
            widget.setVisible(visible)

    def _build_history(self):
        self.history_summary = label()
        frame, inner = card(None, self.history_summary)
        inner.addWidget(label(self.t("sync.history.caption"), "muted", wrap=True))
        self.history_table = table([
            self.t("sync.history.column.when"),
            self.t("sync.history.column.kind"),
            self.t("sync.history.column.result"),
            self.t("sync.history.column.origin"),
            self.t("sync.history.column.changes"),
            self.t("sync.history.column.backup"),
        ])
        inner.addWidget(self.history_table, stretch=1)
        self.history_empty = label("", "muted", wrap=True)
        inner.addWidget(self.history_empty)
        self.history_refresh = button(self.t("action.refresh"))
        self.history_refresh.clicked.connect(self.load_history)
        inner.addLayout(row(self.history_refresh))
        return frame

    def _view_changed(self, index: int) -> None:
        self.model.view = VIEWS[index] if 0 <= index < len(VIEWS) else "plan"
        if self.model.view == "history" and self.model.history is None:
            self.load_history()

    def show_history(self) -> None:
        """Open the History tab; the overview's link lands here."""
        self.views.setCurrentIndex(VIEWS.index("history"))
        self.load_history()

    def load_history(self) -> None:
        if self.model.history_busy:
            return
        self.model.history_busy = True
        self.render()

        def apply(model: SyncModel, outcome: Outcome) -> None:
            model.history_busy = False
            model.history = outcome

        self.read(lambda: self.host.controller.sync_history(limit=HISTORY_LIMIT), apply)

    def _build_filters(self):
        """Search, direction, top-level folder, and how much is hidden."""
        self.search = QLineEdit(self.model.search)
        self.search.setPlaceholderText(self.t("sync.filter.search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filters_changed)

        self.direction = QComboBox()
        for value in DIRECTIONS:
            key = "sync.filter.direction.all" if value == "all" else f"sync.direction.{value}"
            self.direction.addItem(self.t(key), value)
        self.direction.setCurrentIndex(max(0, self.direction.findData(self.model.direction)))
        self.direction.currentIndexChanged.connect(self._filters_changed)

        self.root = QComboBox()
        self.root.currentIndexChanged.connect(self._filters_changed)
        self.shown = label("", "muted")
        return row(
            self.search, label(self.t("sync.filter.direction"), "muted"), self.direction,
            label(self.t("sync.filter.root"), "muted"), self.root, self.shown,
            stretch_last=False,
        )

    def _filters_changed(self, *_args: object) -> None:
        self.model.search = self.search.text()
        self.model.direction = self.direction.currentData() or "all"
        self.model.root = self.root.currentData() or ""
        self.render()

    def _rows(self, preview) -> list[tuple[str, str]]:
        """Every action as (direction, path), conflicts first."""
        rows = [("conflict", path) for path in preview.conflicts]
        rows += [("to_local", path) for path in preview.to_local]
        rows += [("to_cloud", path) for path in preview.to_cloud]
        return rows

    @staticmethod
    def top_folder(path: str) -> str:
        head = path.replace("\\", "/").lstrip("/").split("/")[0]
        return head if head and head != path.replace("\\", "/").lstrip("/") else ""

    def _visible(self, rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
        model = self.model
        needle = model.search.strip().casefold()
        return [
            (direction, path) for direction, path in rows
            if (model.direction in ("all", direction))
            and (not model.root or self.top_folder(path) == model.root)
            and (not needle or needle in path.casefold())
        ]

    def _fill_roots(self, rows: list[tuple[str, str]]) -> None:
        """Offer the top-level folders this plan actually touches."""
        folders = sorted({self.top_folder(path) for _direction, path in rows} - {""})
        wanted = [("", self.t("sync.filter.root.all"))] + [(name, name) for name in folders]
        current = [self.root.itemData(i) for i in range(self.root.count())]
        if current == [value for value, _ in wanted]:
            return
        blocked = self.root.blockSignals(True)
        try:
            self.root.clear()
            for value, text in wanted:
                self.root.addItem(text, value)
            index = self.root.findData(self.model.root)
            if index < 0:
                index, self.model.root = 0, ""
            self.root.setCurrentIndex(index)
        finally:
            self.root.blockSignals(blocked)

    def activated(self) -> None:
        """The screen was just shown.

        Nothing that reads `.codex` or raises the safety gate starts by itself:
        a screen that scans on arrival looks like it began working without
        being asked, and on a windowed build every process sample used to flash
        console windows with it (CS-262, CS-259). The button is the request.
        The history is the exception: it reads journals in the working folder,
        nothing of Codex's, and it is what the tab is for.
        """
        if self.model.view == "history" and self.model.history is None:
            self.load_history()

    def check_plan(self) -> None:
        if self.model.preview_busy:
            return
        self.model.preview_busy = True
        self.render()
        self.read(self.host.controller.preview_sync, _apply_preview)

    def start_run(self, *, dry_run: bool) -> None:
        if self.model.run_busy:
            return
        if not dry_run and not self.host.confirm(
            self.t("sync.confirm.title"),
            self.t("sync.confirm.body"),
            self.t("sync.apply"),
        ):
            return
        self.model.run_busy = True
        self.model.run_kind = "dry" if dry_run else "apply"
        self.model.result = None
        self.render()
        controller = self.host.controller

        def apply(model: SyncModel, outcome: Outcome) -> None:
            model.run_busy = False
            model.result = outcome
            if outcome.ok and not dry_run:
                # The picture on screen described the state before the write.
                model.preview = None
            if not dry_run:
                # A real run -- finished or not -- may have left a journal.
                model.history = None

        if dry_run:
            self.run(lambda: controller.sync(dry_run=True), apply)
            return
        # The real thing is the full sync the handoff runs (CS-334): settings,
        # then chats, then projects, one confirmation for all of it -- a button
        # called "Synchronise" that left the chats and the project list behind
        # is how a laptop ended up with its own sidebar after a sync.
        self.host.run(self.page, lambda progress=None: controller.handoff_now(progress=progress), apply, progress=True)

    def decide(self, policy: str) -> None:
        """Run the full sync again with this run's conflicts decided by ``policy``.

        One confirmation, like *Synchronise*. With the box ticked the rule is
        written into the config first -- through the same validated, history-
        keeping save the Settings page uses -- so the next run, from any shell,
        decides the same way; a save that fails stops before the sync.
        """
        if self.model.run_busy:
            return
        remember = self.remember.isChecked()
        if not self.host.confirm(
            self.t("sync.decide.confirm.title"),
            self.t(f"sync.decide.confirm.{policy}")
            + ("\n\n" + self.t("sync.decide.confirm.remember") if remember else ""),
            self.t(f"sync.decide.{policy}"),
        ):
            return
        self.model.run_busy = True
        self.model.run_kind = "apply"
        self.model.result = None
        self.render()
        controller = self.host.controller

        def go(progress=None) -> Outcome:
            if remember:
                saved = controller.remember_conflict_policy(policy)
                if not saved.ok:
                    return saved
            return controller.handoff_now(progress=progress, conflict_policy=policy)

        def apply(model: SyncModel, outcome: Outcome) -> None:
            model.run_busy = False
            model.result = outcome
            model.history = None
            if outcome.ok:
                model.preview = None

        self.host.run(self.page, go, apply, progress=True)

    def open_decisions(self) -> None:
        """Go to the chats the stopped sync named, for the pair it was syncing."""
        run = self.model.result
        if run is None or not isinstance(run.details, dict):
            return
        self.host.go_to("sessions")
        self.host.screen("sessions").open_for(str(run.details["source"]), str(run.details["target"]))

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        busy = model.preview_busy or model.run_busy
        self.check_button.setEnabled(not busy)
        self.dry_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)

        outcome = model.preview
        self.whole_plan.setVisible(bool(outcome is not None and outcome.ok))
        if not (outcome is not None and outcome.ok):
            self.shown.setText("")
        if model.preview_busy:
            self.banner.show_message("neutral", self.t("sync.checking"), "", palette)
            self.summary.setText("")
        elif outcome is None:
            self.banner.show_message("neutral", "", "", palette)
            self.table.setRowCount(0)
            self.summary.setText("")
            self.empty.setText(self.t("sync.not_checked"))
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
            self.table.setRowCount(0)
            self.summary.setText("")
            self.empty.setText("")
        else:
            preview = outcome.value
            if preview.volatile:
                self.banner.show_message(
                    "attention", self.t("sync.volatile.title"), self.t("sync.volatile.detail"), palette
                )
            elif preview.conflicts:
                self.banner.show_message(
                    "attention", self.t("sync.conflicts.title"), self.t("sync.conflicts.detail"), palette
                )
            else:
                self.banner.show_message("neutral", "", "", palette)
            everything = self._rows(preview)
            self._fill_roots(everything)
            visible = self._visible(everything)
            rows = [
                [
                    Cell(f"⚠  {self.t('sync.direction.conflict')}", tone="danger")
                    if direction == "conflict" else Cell(self.t(f"sync.direction.{direction}")),
                    Cell(path),
                ]
                for direction, path in visible
            ]
            fill_table(self.table, rows, palette)
            self.summary.setText(self.join([
                self.p("sync.count.to_local", len(preview.to_local)),
                self.p("sync.count.to_cloud", len(preview.to_cloud)),
                self.p("sync.count.conflicts", len(preview.conflicts)),
            ]))
            self.shown.setText(
                "" if len(visible) == len(everything)
                else self.t("sync.filter.shown", shown=len(visible), total=len(everything))
            )
            if everything and not visible:
                self.empty.setText(self.t("sync.filter.nothing_shown"))
            else:
                self.empty.setText("" if everything else self.t("sync.nothing"))
        self.empty.setVisible(bool(self.empty.text()))

        run = model.result
        self.links.show(None)
        if model.run_busy:
            self.result.setText(self.progress_text() or self.t("sync.running"))
            set_tone(self.result, None, palette)
        elif run is None:
            self.result.setText("")
        elif run.ok and hasattr(run.value, "sync_actions"):
            self.result.setText(full_sync_result(self, run.value))
            self.links.show(run.value)
            attention = (
                run.value.projects_missing_folders or run.value.chats_codex_ignores
                or run.value.project_files_behind or getattr(run.value, "steps_not_done", ())
            )
            set_tone(self.result, "attention" if attention else "ok", palette)
        elif run.ok:
            value = run.value
            key = "sync.done.apply" if value.applied else "sync.done.dry"
            self.result.setText(self.t(key) + " " + self.join([
                self.p("sync.count.files", value.actions),
                self.p("sync.count.conflicts", value.conflicts),
            ]))
            set_tone(self.result, "ok", palette)
        elif run.code == DECISIONS_CODE and isinstance(run.details, dict):
            self.result.setText(decisions_text(self, run.details))
            set_tone(self.result, "attention", palette)
        else:
            self.result.setText(self.failure_text(run))
            set_tone(self.result, "danger", palette)
            self.links.show_stop(run)
        self.result.setVisible(bool(self.result.text()))
        # A stop on something both machines changed -- chats or settings files
        # -- is answered right here; only chats also have a page to go to.
        stopped = (
            not model.run_busy and run is not None and not run.ok
            and run.failure is Failure.NEEDS_A_DECISION
        )
        self._show_decisions(stopped)
        self.decide_button.setVisible(stopped and run.code == DECISIONS_CODE and isinstance(run.details, dict))
        self._render_history()
        if (
            model.view == "history" and model.history is None
            and not (model.history_busy or model.run_busy) and self.isVisible()
        ):
            # Dropped by a run while the tab was open: read it again.
            self.load_history()

    # --- the history -----------------------------------------------------------------

    def _render_history(self) -> None:
        model = self.model
        palette = self.palette_
        self.history_refresh.setEnabled(not model.history_busy)
        outcome = model.history
        if model.history_busy and outcome is None:
            self.history_table.setRowCount(0)
            self.history_summary.setText("")
            self.history_empty.setText(self.t("banner.reading"))
        elif outcome is None:
            self.history_table.setRowCount(0)
            self.history_summary.setText("")
            self.history_empty.setText("")
        elif not outcome.ok:
            self.history_table.setRowCount(0)
            self.history_summary.setText("")
            self.history_empty.setText(self.failure_text(outcome))
        else:
            runs = outcome.value
            fill_table(self.history_table, [
                [
                    Cell(local_time(run.created_at_utc), tooltip=run.operation_id, data=run),
                    Cell(self._kind(run.family)),
                    Cell(run_result(self, run), tone=_result_tone(run)),
                    Cell(self._origin(run.origin)),
                    Cell(self._changes(run)),
                    Cell(run.backup_snapshot or "—", muted=True),
                ]
                for run in runs
            ], palette)
            self.history_summary.setText(self.p("sync.history.count", len(runs)) if runs else "")
            self.history_empty.setText("" if runs else self.t("sync.history.empty"))
        self.history_empty.setVisible(bool(self.history_empty.text()))

    def _origin(self, origin: str | None) -> str:
        return self.t(f"sync.history.origin.{origin}") if origin in ORIGINS else "—"

    def _kind(self, family: str | None) -> str:
        """What a run wrote: settings, chats, projects ... (one full sync is three runs)."""
        key = f"sync.history.family.{family}"
        return self.t(key) if family and self.host.catalog.has(key) else (family or "—")

    def _changes(self, run) -> str:
        counts = run.counts
        if counts and run.family == "project-sync":
            parts = []
            if counts.get("projects_added"):
                parts.append(self.p("sync.count.projects_added", counts["projects_added"]))
            if counts.get("changes"):
                parts.append(self.p("sync.count.changes", counts["changes"]))
            return self.join(parts) if parts else self.t("sync.history.no_changes")
        if counts:
            if not any(counts.values()):
                return self.t("sync.history.no_changes")
            parts = [
                self.p("sync.count.to_cloud", counts.get("to_cloud", 0)),
                self.p("sync.count.to_local", counts.get("to_local", 0)),
            ]
            if counts.get("deletions"):
                parts.append(self.p("sync.count.deleted", counts["deletions"]))
            return self.join(parts)
        # A journal from before the counts were recorded knows only the total.
        if run.action_count is None:
            return "—"
        if run.family == "sync":
            return self.p("sync.count.files", run.action_count)
        if run.family == "sessions":
            return self.p("sync.count.chats", run.action_count)
        return self.p("sync.count.changes", run.action_count)


#: A full sync stopped on chats a person has to decide (`ChatDecisionsNeeded`).
DECISIONS_CODE = "CHAT_DECISIONS_NEEDED"
#: A run refused because an earlier journal is still open (`RecoveryPendingError`).
RECOVERY_CODE = "RECOVERY_PENDING"


def decisions_text(screen: Screen, details: dict) -> str:
    """Why the sync stopped, what kinds of decisions wait, and that nothing was written."""
    lines = [screen.t("sync.decisions.title")]
    for key in ("format_migrations", "held_migrations", "divergences", "collisions"):
        count = int(details.get(key) or 0)
        if count:
            lines.append("• " + screen.p(f"sync.decisions.{key}", count))
    lines.append(screen.t("sync.decisions.next"))
    return "\n".join(lines)


def full_sync_result(screen: Screen, result) -> str:
    """What a full sync (`app.HandoffResult`) did, as one or more sentences."""
    text = screen.t("sync.done.apply") + " " + screen.join([
        screen.p("sync.count.files", result.sync_actions),
        screen.p("sync.count.chats", result.session_actions),
        screen.p("sync.count.projects_added", result.projects_added),
    ])
    return "\n".join([text, *full_sync_notes(screen, result)])


def full_sync_notes(screen: Screen, result) -> list[str]:
    """What a full sync left for a person, one sentence each; `ResultLinks` leads there."""
    notes = []
    # Said every time: a choice nobody saw is still a choice (D-027).
    if getattr(result, "chats_decided_by_rule", 0):
        notes.append(screen.p("sync.note.decided_by_rule", result.chats_decided_by_rule))
    if result.projects_missing_folders:
        notes.append(screen.p("sync.note.missing_folders", result.projects_missing_folders))
    # The part a setting changes is said apart from the rest (CS-347).
    if result.new_chats_kept_in_cloud:
        notes.append(screen.p("sync.note.new_chats_in_cloud", result.new_chats_kept_in_cloud))
    others = result.chats_not_loaded - result.new_chats_kept_in_cloud
    if others > 0:
        notes.append(screen.p("sync.note.chats_not_loaded", others))
    # Written is not shown: Codex lists a chat only once its catalogue has it (D-024).
    if result.chats_codex_will_list:
        notes.append(screen.p("sync.note.codex_will_list", result.chats_codex_will_list))
    if result.chats_codex_ignores:
        notes.append(screen.p("sync.note.codex_ignores", result.chats_codex_ignores))
    # Names live only in Codex's catalogue, so they travel separately (D-025).
    if result.chat_names_set:
        notes.append(screen.p("sync.note.names_set", result.chat_names_set))
    if result.chat_names_waiting:
        notes.append(screen.p("sync.note.names_waiting", result.chat_names_waiting))
    if result.chat_names_kept:
        notes.append(screen.p("sync.note.names_kept", result.chat_names_kept))
    # A step after the settings and chats that failed; the handoff still happened.
    for name in getattr(result, "steps_not_done", ()):
        notes.append(screen.t(f"sync.note.not_done.{name}"))
    # Chats travel, project folders do not (D-026).
    for item in result.project_files_behind:
        text = screen.t(
            f"sync.note.files.{item.verdict.value}", project=item.name, machine=item.peer,
            newer=len(item.newer_there), missing=len(item.missing_here), removed=len(item.removed_there),
        )
        if item.chats_there_at:
            when = datetime.fromtimestamp(item.chats_there_at).strftime("%Y-%m-%d %H:%M")
            text += " " + screen.t("sync.note.files.chats_there", when=when)
        notes.append(text)
    return notes


class ResultLinks:
    """A button for every place a full sync's notes send the person.

    A sentence such as "add a path mapping" or "Settings → Sync → New chats
    from another machine" is only half an instruction; the button is the
    other half and lands on the very tab or field (`MainWindow.go_to`). Used
    by every page that shows a full sync's result, so they cannot drift.
    """

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        self.result = None
        self.journal: str | None = None
        self.recovery = button(screen.t("sync.link.recovery"))
        self.recovery.clicked.connect(lambda: screen.host.go_to("recovery", self.journal))
        self.mappings = button(screen.t("sync.link.mappings"))
        self.mappings.clicked.connect(lambda: screen.host.go_to("settings", "mappings"))
        self.new_chats = button(screen.t("sync.link.new_chats"))
        self.new_chats.clicked.connect(lambda: screen.host.go_to("settings", "semantic.new_chats"))
        self.sessions = button(screen.t("sync.decisions.open"))
        self.sessions.clicked.connect(self.open_sessions)
        self.files = button(screen.t("sync.link.files"))
        self.files.clicked.connect(self.open_files)
        self.layout = row(self.recovery, self.mappings, self.new_chats, self.sessions, self.files)
        self.show(None)

    def show_stop(self, outcome) -> None:
        """A run that did not happen: offer the journal that stopped it, if one did."""
        self.show(None)
        details = outcome.details if isinstance(outcome.details, dict) else {}
        if outcome.code == RECOVERY_CODE and details.get("operation_id"):
            self.journal = str(details["operation_id"])
            self.recovery.setVisible(True)

    def show(self, result) -> None:
        self.result = result
        self.journal = None
        self.recovery.setVisible(False)
        self.mappings.setVisible(bool(result is not None and result.projects_missing_folders))
        self.new_chats.setVisible(bool(result is not None and result.new_chats_kept_in_cloud))
        self.sessions.setVisible(bool(
            result is not None and result.chats_not_loaded - result.new_chats_kept_in_cloud > 0
        ))
        self.files.setVisible(bool(result is not None and result.project_files_behind))

    def open_files(self) -> None:
        """Projects, at the folders that did not come along, checked afresh."""
        host = self.screen.host
        host.go_to("projects", "files")
        host.screen("projects").check_files()

    def open_sessions(self) -> None:
        """Sessions, for the pair of machines the sync paired chats between."""
        host = self.screen.host
        host.go_to("sessions")
        result = self.result
        if result is not None and getattr(result, "source", None):
            host.screen("sessions").open_for(str(result.source), str(result.machine))


def run_result(screen: Screen, run) -> str:
    """`done`, or what stopped a run and why, in `screen`'s language."""
    if not run.readable or run.state is None:
        return screen.t("sync.history.result.unreadable")
    result = screen.t(f"sync.history.result.{run.state}")
    if run.state == "COMMITTED" or not run.failure:
        return result
    reason = (
        screen.t(f"sync.history.failure.{run.failure}")
        if run.failure in KNOWN_FAILURES
        else screen.t("sync.history.failure.other", name=run.failure)
    )
    return screen.t("sync.history.result.with_reason", result=result, reason=reason)


def local_time(iso: str | None) -> str:
    """A journal's UTC stamp in this machine's time: `2026-09-24 16:36`."""
    if not iso:
        return "—"
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if moment.tzinfo is None:
        return iso
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _result_tone(run) -> str | None:
    if not run.readable or run.state is None:
        return "danger"
    if run.state == "COMMITTED":
        return "ok"
    if run.state == "FAILED":
        return "attention"
    return "danger"


def _apply_preview(model: SyncModel, outcome: Outcome) -> None:
    model.preview_busy = False
    model.preview = outcome
