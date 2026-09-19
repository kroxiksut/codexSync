"""Sessions: how every branch compares across two machines, and applying a plan.

The screen groups branches the way a person decides about them: identical,
transferable, needing a decision, not supported. A divergence is never merged,
sorted or newer-wins -- it is shown with its conflict id and a choice that is
recorded against the exact bytes of both branches, after which the scan is
repeated so the plan reflects the choice.

Apply always quotes a saved plan and its exact id, and the core rebuilds the
plan from the current state and refuses if the id moved. The window saves the
plan beside the workspace (never in `.codex`), so nothing here holds a plan only
in memory.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QTreeWidget, QTreeWidgetItem

from ..controller import Failure, Outcome
from ..widgets import Banner, Cell, button, card, command, fill_table, label, machine_combo, row, selected_data, set_tone, table
from .base import Model, Screen

CATEGORIES = {
    "OUT_OF_SCOPE": "out_of_scope",
    "NOOP": "same",
    "FAST_FORWARD_LOCAL": "transfer",
    "FAST_FORWARD_REMOTE": "transfer",
    "ARCHIVE_TRANSITION": "transfer",
    "BLOCKED_CONFLICT": "decide",
    "BLOCKED_TARGET_COLLISION": "decide",
    "BLOCKED_UNPROVEN_LAYOUT": "unsupported",
    "BLOCKED_UNSUPPORTED_BACKEND": "unsupported",
}
CATEGORY_ORDER = ("decide", "transfer", "unsupported", "out_of_scope", "same")
CATEGORY_TONES = {
    "decide": "danger", "transfer": "ok", "unsupported": "attention",
    "out_of_scope": None, "same": None,
}
CHOICES = ("KEEP_LOCAL", "KEEP_REMOTE", "DEFER")


class SessionsModel(Model):
    def __init__(self) -> None:
        self.source = ""
        self.target = ""
        self.category = ""
        self.scan: Outcome | None = None
        self.busy = False
        self.action_busy = False
        self.action_kind = ""
        self.action_result: Outcome | None = None
        self.index: Outcome | None = None
        self.index_busy = False
        #: The working set: chosen project ids and chat ids, and what the last
        #: expansion of them covered. Empty means "carry everything".
        self.scope_projects: tuple[str, ...] = ()
        self.scope_chats: tuple[str, ...] = ()
        self.scope: Outcome | None = None
        self.scope_busy = False
        #: Whether the stored set has been read back into this model yet.
        self.scope_loaded = False
        #: The chat directory the tree of projects is drawn from.
        self.directory: Outcome | None = None
        self.directory_busy = False

    def reset_plans(self) -> None:
        self.scan = None
        self.action_result = None
        self.scope = None
        self.directory = None
        self.scope_loaded = False


class SessionsScreen(Screen):
    page = "sessions"
    scrollable = True

    def build(self) -> None:
        m = self.model
        machines = self.host.known_machines()
        if not m.target:
            m.target = self.host.machine_id() or ""
        if not m.source:
            m.source = next((name for name in machines if name != m.target), "")
        self.source = machine_combo(machines, m.source, placeholder=self.t("machines.source"))
        self.target = machine_combo(machines, m.target, placeholder=self.t("machines.target"))
        self.scan_button = button(self.t("sessions.scan"), primary=True)
        self.scan_button.clicked.connect(self.scan)
        self.body.addLayout(row(
            label(self.t("machines.source_label")), self.source,
            label(self.t("machines.target_label")), self.target,
            self.scan_button,
        ))
        self.body.addWidget(label(self.t("sessions.caption"), "muted", wrap=True))

        self.banner = Banner()
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(self.t("sessions.branches.title"), self.summary)
        self.category = QComboBox()
        self.category.addItem(self.t("sessions.category.all"), "")
        for category in CATEGORY_ORDER:
            self.category.addItem(self.t(f"sessions.category.{category}"), category)
        self.category.setCurrentIndex(max(0, self.category.findData(m.category)))
        self.category.currentIndexChanged.connect(self._category_changed)
        # Wraps: unwrapped, the Russian counts alone were wider than the default
        # window and pushed the whole page into a horizontal scroll.
        self.counts = label("", "muted", wrap=True)
        counts_row = row(label(self.t("sessions.show")), self.category, self.counts, stretch_last=False)
        counts_row.setStretch(2, 1)
        inner.addLayout(counts_row)
        self.table = table([
            self.t("sessions.column.category"),
            self.t("sessions.column.action"),
            self.t("sessions.column.relation"),
            self.t("sessions.column.records"),
            self.t("sessions.column.chat"),
            self.t("sessions.column.target"),
        ], selectable=True, stretch=4)
        self.table.itemSelectionChanged.connect(self._render_resolution)
        self.table.setMinimumHeight(300)
        inner.addWidget(self.table, stretch=1)

        self.choice = QComboBox()
        for choice in CHOICES:
            self.choice.addItem(self.t(f"sessions.choice.{choice}"), choice)
        self.resolve_button = button(self.t("sessions.resolve"))
        self.resolve_button.clicked.connect(self.resolve)
        self.resolution_hint = label("", "muted", wrap=True)
        inner.addLayout(row(label(self.t("sessions.choice.label")), self.choice, self.resolve_button))
        inner.addWidget(self.resolution_hint)
        self.body.addWidget(frame, stretch=1)

        self.body.addWidget(self._build_working_set())

        plan, plan_inner = card(self.t("sessions.plan.title"))
        self.plan_id = command("")
        plan_inner.addWidget(self.plan_id)
        self.plan_note = label("", "muted", wrap=True)
        plan_inner.addWidget(self.plan_note)
        self.dry_button = button(self.t("action.dry_run"))
        self.dry_button.clicked.connect(lambda: self.apply(dry_run=True))
        self.apply_button = button(self.t("sessions.apply"), primary=True)
        self.apply_button.clicked.connect(lambda: self.apply(dry_run=False))
        plan_inner.addLayout(row(self.dry_button, self.apply_button))
        self.status = label("", wrap=True)
        plan_inner.addWidget(self.status)
        self.body.addWidget(plan)

        self.index_summary = label()
        index, index_inner = card(self.t("sessions.index.title"), self.index_summary)
        index_inner.addWidget(label(self.t("sessions.index.caption"), "muted", wrap=True))
        self.index_text = label("", wrap=True)
        self.index_refresh = button(self.t("action.refresh"))
        self.index_refresh.clicked.connect(self.refresh_index)
        line = row(self.index_text, self.index_refresh, stretch_last=False)
        line.setStretchFactor(self.index_text, 1)
        index_inner.addLayout(line)
        self.body.addWidget(index)

    def refresh_index(self) -> None:
        if self.model.index_busy:
            return
        self.model.index_busy = True
        self._render_index()

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.index_busy = False
            model.index = outcome

        self.read(self.host.controller.session_index, apply)

    def _render_index(self) -> None:
        model = self.model
        palette = self.palette_
        self.index_refresh.setEnabled(not model.index_busy)
        outcome = model.index
        if model.index_busy:
            self.index_text.setText(self.t("sessions.index.loading"))
            set_tone(self.index_text, None, palette)
            self.index_summary.setText("")
            return
        if outcome is None:
            self.index_text.setText("")
            self.index_summary.setText("")
            return
        if not outcome.ok:
            self.index_text.setText(self.failure_text(outcome))
            set_tone(self.index_text, "danger", palette)
            self.index_summary.setText("")
            return
        report = outcome.value

        def side(name: str) -> str:
            data = report[name]
            if not data["present"]:
                return self.t(f"sessions.index.{name}.missing")
            return self.t(f"sessions.index.{name}", records=data["records"], sessions=data["sessions"])

        lines = [
            self.join([side("local"), side("cloud")]),
            self.join([
                self.t("sessions.index.only_local", count=report["only_local"]),
                self.t("sessions.index.only_cloud", count=report["only_cloud"]),
                self.t("sessions.index.differing", count=report["differing"]),
            ]),
        ]
        codes = [code for code in report["codes"] if code != "UNPROVEN_CONSUMER_CONTRACT"]
        if codes:
            lines.append(self.t("common.codes", codes=", ".join(codes)))
        if not report["contract_proven"]:
            lines.append(self.t("sessions.index.not_written"))
        self.index_text.setText("\n".join(lines))
        set_tone(self.index_text, "attention" if report["differing"] else None, palette)
        self.index_summary.setText(
            self.t("sessions.index.state.differ") if report["differing"] else self.t("sessions.index.state.agree")
        )

    # --- the working set -----------------------------------------------------------

    def _build_working_set(self):
        """Projects and chats this machine should carry, as a tree of checkboxes."""
        frame, inner = card(self.t("sessions.scope.title"))
        inner.addWidget(label(self.t("sessions.scope.caption"), "muted", wrap=True))
        self.scope_tree = QTreeWidget()
        self.scope_tree.setColumnCount(2)
        self.scope_tree.setHeaderLabels([
            self.t("sessions.scope.title"), self.t("chats.column.chats"),
        ])
        self.scope_tree.setMinimumHeight(200)
        self.scope_tree.itemChanged.connect(self._scope_changed)
        inner.addWidget(self.scope_tree, stretch=1)
        self.scope_summary = label("", "muted", wrap=True)
        inner.addWidget(self.scope_summary)
        self.scope_take = button(self.t("sessions.scope.take"), primary=True)
        self.scope_take.clicked.connect(self.take_working_set)
        self.scope_clear = button(self.t("sessions.scope.clear"))
        self.scope_clear.clicked.connect(self.clear_working_set)
        inner.addLayout(row(self.scope_take, self.scope_clear))
        return frame

    def activated(self) -> None:
        if self.model.index is None and not self.model.index_busy:
            self.refresh_index()
        if self.model.directory is None and not self.model.directory_busy:
            self.load_projects()
        if not self.model.scope_loaded:
            self.load_working_set()

    def load_working_set(self) -> None:
        """Show the set that is already stored for this pair of machines.

        Without this the screen would say "carry everything" while a scan
        quietly used a stored set -- the screen has to show what will actually
        happen, not what was chosen in this window since it opened.
        """
        model = self.model
        model.scope_loaded = True
        source = self.source.currentText().strip()
        target = self.target.currentText().strip()
        if not source or not target:
            return
        outcome = self.host.controller.working_set(
            source_machine=source, target_machine=target,
        )
        if outcome.ok and outcome.value is not None:
            model.scope_projects = tuple(outcome.value.projects)
            model.scope_chats = tuple(outcome.value.chats)
        self.render()

    def load_projects(self) -> None:
        """Read the chats once, to draw the tree of projects."""
        self.model.directory_busy = True

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.directory_busy = False
            model.directory = outcome

        self.read(
            lambda progress=None: self.host.controller.chats(progress=progress),
            apply, progress=True,
        )

    def _scope_changed(self, item, column: int) -> None:
        if column != 0 or getattr(self, "_filling_scope", False):
            return
        chosen: list[str] = []
        root = self.scope_tree.invisibleRootItem()
        for index in range(root.childCount()):
            child = root.child(index)
            if child.checkState(0) == Qt.Checked and child.data(0, Qt.UserRole):
                chosen.append(child.data(0, Qt.UserRole))
        self.model.scope_projects = tuple(chosen)
        self.model.scope = None
        self._render_scope()

    def take_working_set(self) -> None:
        """Expand the chosen projects and remember the set for this pair."""
        model = self.model
        if model.scope_busy:
            return
        model.source = self.source.currentText().strip()
        model.target = self.target.currentText().strip()
        model.scope_busy = True
        self.render()
        controller = self.host.controller
        projects, chats = model.scope_projects, model.scope_chats
        source, target = model.source, model.target

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.scope_busy = False
            model.scope = outcome

        self.read(
            lambda progress=None: controller.save_working_set(
                projects=projects, chats=chats,
                source_machine=source, target_machine=target, progress=progress,
            ),
            apply, progress=True,
        )

    def clear_working_set(self) -> None:
        """Carry everything again."""
        model = self.model
        model.scope_projects = ()
        model.scope_chats = ()
        model.scope = None
        model.source = self.source.currentText().strip()
        model.target = self.target.currentText().strip()
        self.host.controller.save_working_set(
            projects=(), chats=(),
            source_machine=model.source, target_machine=model.target,
        )
        self.render()

    def _fill_scope_tree(self) -> None:
        outcome = self.model.directory
        if outcome is None or not outcome.ok:
            return
        directory = outcome.value
        counts: dict[str, int] = {}
        for chat in directory.chats:
            if chat.project_id:
                counts[chat.project_id] = counts.get(chat.project_id, 0) + 1
        self._filling_scope = True
        try:
            self.scope_tree.clear()
            chosen = set(self.model.scope_projects)
            for view in sorted(
                directory.projects.values(), key=lambda v: (v.name or v.project_id).casefold()
            ):
                item = QTreeWidgetItem([
                    view.name or view.project_id,
                    self.p("chats.count", counts.get(view.project_id, 0)),
                ])
                item.setData(0, Qt.UserRole, view.project_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if view.project_id in chosen else Qt.Unchecked)
                self.scope_tree.addTopLevelItem(item)
        finally:
            self._filling_scope = False

    def _render_scope(self) -> None:
        model = self.model
        self.scope_take.setEnabled(bool(model.scope_projects or model.scope_chats) and not model.scope_busy)
        self.scope_clear.setEnabled(bool(model.scope_projects or model.scope_chats))
        if model.scope_busy:
            text = self.t("sessions.scope.counting")
        elif model.scope is not None and model.scope.ok and model.scope.value is not None:
            scope = model.scope.value
            text = self.join([
                self.p("sessions.scope.count", scope.chat_count),
                self.t("sessions.scope.size", size=_size(scope.total_bytes)),
                self.t(
                    "sessions.scope.saved",
                    source=model.source or "?", target=model.target or "?",
                ),
                # The limit is stated, never silently skipped: a chat this
                # machine's catalogue does not place will not appear in Codex
                # here, however complete the mirror is.
                self.p("sessions.scope.not_in_catalog", len(scope.not_in_catalog))
                if scope.not_in_catalog else "",
            ])
        elif model.scope is not None and not model.scope.ok:
            text = self.failure_text(model.scope)
        elif not model.scope_projects and not model.scope_chats:
            text = self.t("sessions.scope.all")
        else:
            text = ""
        self.scope_summary.setText(text)

    # --- actions -------------------------------------------------------------------

    def scan(self) -> None:
        model = self.model
        if model.busy or model.action_busy:
            return
        model.source = self.source.currentText().strip()
        model.target = self.target.currentText().strip()
        if not model.source or not model.target:
            model.scan = Outcome(failure=Failure.CONFIGURATION, message=self.t("machines.required"))
            self.render()
            return
        model.busy = True
        model.action_result = None
        self.render()
        controller = self.host.controller
        source, target = model.source, model.target

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.busy = False
            model.scan = outcome

        self.read(
            lambda progress=None: controller.scan_sessions(
                source_machine=source, target_machine=target, progress=progress,
            ),
            apply, progress=True,
        )

    def resolve(self) -> None:
        model = self.model
        scan = self._scan()
        items = selected_data(self.table)
        if scan is None or not items or model.action_busy:
            return
        item = items[0]
        if not item.conflict_id:
            return
        choice = self.choice.currentData()
        model.action_busy = True
        model.action_kind = "resolve"
        model.action_result = None
        self.render()
        controller = self.host.controller
        source, target = model.source, model.target

        def go() -> Outcome:
            recorded = controller.resolve_session_conflict(scan, conflict_id=item.conflict_id, choice=choice)
            if not recorded.ok:
                return recorded
            # A decision changes the plan; show the plan it produces.
            return controller.scan_sessions(source_machine=source, target_machine=target)

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.action_busy = False
            if outcome.ok:
                model.scan = outcome
                model.action_result = Outcome(value="resolved")
            else:
                model.action_result = outcome

        self.run(go, apply)

    def apply(self, *, dry_run: bool) -> None:
        model = self.model
        scan = self._scan()
        if scan is None or model.action_busy:
            return
        plan = scan.plan
        if not dry_run and not self.host.confirm(
            self.t("sessions.confirm.title"),
            self.t("sessions.confirm.body", count=len(plan.writable_items), plan_id=plan.plan_id),
            self.t("sessions.apply"),
        ):
            return
        model.action_busy = True
        model.action_kind = "dry" if dry_run else "apply"
        model.action_result = None
        self.render()
        controller = self.host.controller

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.action_result = outcome
            if outcome.ok and not dry_run:
                model.scan = None

        self.run(lambda: controller.apply_sessions(scan, confirm_plan=plan.plan_id, dry_run=dry_run), apply)

    def _category_changed(self) -> None:
        self.model.category = self.category.currentData() or ""
        self._fill()

    def _scan(self):
        outcome = self.model.scan
        return outcome.value if outcome is not None and outcome.ok else None

    # --- drawing ------------------------------------------------------------------------

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        busy = model.busy or model.action_busy
        self.scan_button.setEnabled(not busy)
        scan = self._scan()
        self._fill_scope_tree()
        self._render_scope()

        if model.busy:
            self.banner.show_message("neutral", self.t("sessions.scanning"), self.progress_text(), palette)
        elif model.scan is None:
            self.banner.show_message("neutral", "", "", palette)
        elif not model.scan.ok:
            self.banner.show_message("danger", self.headline(model.scan.failure), model.scan.message, palette)
        else:
            plan = scan.plan
            decide = sum(1 for item in plan.items if CATEGORIES.get(item.action.value) == "decide")
            if plan.volatile:
                self.banner.show_message("attention", self.t("sessions.volatile.title"), self.t("sessions.volatile.detail"), palette)
            elif decide:
                self.banner.show_message("attention", self.p("sessions.decide.title", decide), self.t("sessions.decide.detail"), palette)
            elif plan.codes:
                self.banner.show_message("attention", self.t("common.codes", codes=", ".join(plan.codes)), "", palette)
            else:
                self.banner.show_message("ok", self.t("sessions.clean.title"), "", palette)
        self._fill()
        self._render_plan()
        self._render_resolution()
        self._render_index()

    def _fill(self) -> None:
        scan = self._scan()
        palette = self.palette_
        if scan is None:
            self.table.setRowCount(0)
            self.summary.setText("")
            self.counts.setText("")
            return
        plan = scan.plan
        counts = {category: 0 for category in CATEGORY_ORDER}
        for item in plan.items:
            counts[CATEGORIES.get(item.action.value, "unsupported")] += 1
        self.summary.setText(self.p("sessions.count", len(plan.items)))
        self.counts.setText(self.join([
            f"{self.t(f'sessions.category.{category}')}: {counts[category]}" for category in CATEGORY_ORDER
        ]))
        wanted = self.model.category
        items = [
            item for item in plan.items
            if not wanted or CATEGORIES.get(item.action.value, "unsupported") == wanted
        ]
        items.sort(key=lambda item: (CATEGORY_ORDER.index(CATEGORIES.get(item.action.value, "unsupported")), item.target_relative_path or ""))
        rows = []
        for item in items:
            category = CATEGORIES.get(item.action.value, "unsupported")
            rows.append([
                Cell(self.t(f"sessions.category.{category}"), tone=CATEGORY_TONES[category], data=item),
                Cell(self.t(f"transfer.action.{item.action.value}"), tooltip=item.action.value),
                Cell(self.t(f"transfer.relation.{item.relation.value}"), tooltip=item.relation.value, muted=True),
                Cell(f"{item.local_records} / {item.remote_records}", tooltip=self.t("sessions.records.tip")),
                Cell(scan.titles.get(item.session_hash) or item.session_hash[:12], tooltip=item.session_hash,
                     muted=item.session_hash not in scan.titles),
                Cell(item.target_relative_path or "—", tooltip=", ".join(item.codes) or None),
            ])
        fill_table(self.table, rows, palette)

    def _render_resolution(self) -> None:
        scan = self._scan()
        items = selected_data(self.table) if scan is not None else []
        conflict = items[0].conflict_id if items else None
        enabled = bool(conflict) and not self.model.action_busy
        self.choice.setEnabled(enabled)
        self.resolve_button.setEnabled(enabled)
        if conflict:
            self.resolution_hint.setText(self.t("sessions.resolve.hint", conflict_id=conflict[:16]))
        else:
            self.resolution_hint.setText(self.t("sessions.resolve.select"))

    def _render_plan(self) -> None:
        model = self.model
        palette = self.palette_
        scan = self._scan()
        if scan is None:
            self.plan_id.setVisible(False)
            self.plan_note.setText(self.t("sessions.plan.none"))
            ready = False
        else:
            plan = scan.plan
            self.plan_id.setText(self.t("common.plan_id", plan_id=plan.plan_id))
            self.plan_id.setVisible(True)
            decide = any(CATEGORIES.get(item.action.value) == "decide" for item in plan.items)
            notes = [
                self.p("sessions.plan.writes", len(plan.writable_items)),
                self.t("sessions.plan.saved", path=scan.plan_path),
            ]
            if scan.used_resolutions:
                notes.append(self.t("sessions.plan.with_resolutions"))
            self.plan_note.setText(self.join(notes))
            ready = not plan.volatile and not decide and bool(plan.writable_items)
        busy = model.busy or model.action_busy
        self.dry_button.setEnabled(ready and not busy)
        self.apply_button.setEnabled(ready and not busy)

        text, tone = "", None
        if model.action_busy:
            text = self.t("sessions.working")
        elif model.action_result is not None:
            result = model.action_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
            elif model.action_kind == "resolve":
                text, tone = self.t("sessions.resolved"), "ok"
            elif model.action_kind == "dry":
                text, tone = self.p("sessions.done.dry", int(result.value)), "ok"
            else:
                text, tone = self.p("sessions.done.apply", int(result.value)), "ok"
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))


def _size(size: int) -> str:
    from .guardian import _size as size_text

    return size_text(size)
