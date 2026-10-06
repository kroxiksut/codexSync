"""Sessions: how every branch compares across two machines, and applying a plan.

The screen groups branches the way a person decides about them: identical,
transferable, needing a decision, not supported. A divergence is never merged.
It is decided by `[conflict] policy` (D-027) when the rule can tell, and what
is left is shown with its conflict id and a choice recorded against the exact
bytes of both branches -- one at a time, or all at once by one rule for this
scan -- after which the scan is repeated so the plan reflects the choice.

Apply always quotes a saved plan and its exact id, and the core rebuilds the
plan from the current state and refuses if the id moved. The window saves the
plan beside the workspace (never in `.codex`), so nothing here holds a plan only
in memory.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QTreeWidget, QTreeWidgetItem

from ..controller import Failure, Outcome
from ..widgets import Banner, Cell, button, card, command, fill_table, human_size, label, machine_combo, row, selected_data, set_tone, table
from .base import Model, Screen

CATEGORIES = {
    "OUT_OF_SCOPE": "out_of_scope",
    "HELD_BY_DIRECTION": "out_of_scope",
    "NOOP": "same",
    "FAST_FORWARD_LOCAL": "transfer",
    "FAST_FORWARD_REMOTE": "transfer",
    "ARCHIVE_TRANSITION": "transfer",
    "BLOCKED_CONFLICT": "decide",
    "BLOCKED_TARGET_COLLISION": "decide",
    "BLOCKED_UNPROVEN_LAYOUT": "unsupported",
    "BLOCKED_UNSUPPORTED_BACKEND": "unsupported",
    "BLOCKED_INVALID_BRANCH": "unsupported",
}
CATEGORY_ORDER = ("decide", "transfer", "unsupported", "out_of_scope", "same")
CATEGORY_TONES = {
    "decide": "danger", "transfer": "ok", "unsupported": "attention",
    "out_of_scope": None, "same": None,
}
CHOICES = ("KEEP_LOCAL", "KEEP_REMOTE", "DEFER")
#: One rule for every conflict of a scan, in button order (`[conflict] policy`).
RULES = ("prefer_newer_mtime", "prefer_local", "prefer_cloud")


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
        #: The (source, target) pair the set above was read for. A set belongs
        #: to one pair: shown under another, it is a set no scan would use,
        #: and taking it would store one pair's choice under the other.
        self.scope_pair: tuple[str, str] | None = None
        #: The chat directory the tree of projects is drawn from.
        self.directory: Outcome | None = None
        self.directory_busy = False
        #: A rule chosen on this page for every conflict of the next scans of
        #: this pair, instead of `[conflict] policy`. ``None`` follows the config.
        self.rule: str | None = None

    def reset_plans(self) -> None:
        self.rule = None
        self.scan = None
        self.action_result = None
        self.scope = None
        self.directory = None
        self.scope_loaded = False
        self.scope_pair = None


class SessionsScreen(Screen):
    page = "sessions"
    scrollable = True

    def build(self) -> None:
        m = self.model
        machines = self.host.known_machines()
        if not m.target:
            m.target = self.host.machine_id() or ""
        if not m.source:
            # The pair a full sync would use, so the page reached by hand shows
            # what the page reached from a stopped sync shows (CS-345).
            usual = self.host.usual_source()
            m.source = usual if usual and usual != m.target else next(
                (name for name in machines if name != m.target), ""
            )
        self.source = machine_combo(machines, m.source, placeholder=self.t("machines.source"))
        self.target = machine_combo(machines, m.target, placeholder=self.t("machines.target"))
        for combo in (self.source, self.target):
            combo.activated.connect(self._pair_changed)
            combo.lineEdit().editingFinished.connect(self._pair_changed)
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
        # Shown when new chats stay in the cloud only because of a setting: the
        # one thing on this page a person can change about it (CS-347).
        self.settings_button = button(self.t("sessions.open_settings"))
        self.settings_button.clicked.connect(lambda: self.host.go_to("settings", "semantic.new_chats"))
        self.settings_button.setVisible(False)
        self.body.addLayout(row(self.settings_button))

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
        # Every conflict at once by one rule: the scan is repeated under it and
        # the plan says which copy each one keeps (D-027).
        self.rule_buttons = {}
        for policy in RULES:
            choice = button(self.t(f"sessions.rule.{policy}"))
            choice.clicked.connect(lambda _=False, policy=policy: self.decide_all(policy))
            self.rule_buttons[policy] = choice
        self.rule_label = label(self.t("sessions.rule.label"))
        inner.addLayout(row(self.rule_label, *self.rule_buttons.values()))
        # Codex rewrote every session into a newer record format once; each old
        # copy is then a conflict of its own, and deciding them one by one is
        # hundreds of clicks for one decision. Shown only when there are some.
        self.format_hint = label("", "muted", wrap=True)
        self.format_button = button(self.t("sessions.format.resolve"))
        self.format_button.clicked.connect(self.resolve_format_migrations)
        format_line = row(self.format_hint, self.format_button, stretch_last=False)
        format_line.setStretchFactor(self.format_hint, 1)
        inner.addLayout(format_line)
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
        """The screen was just shown.

        Only the stored working set loads by itself: it comes from the semantic
        store, touches no `.codex` file and raises no safety gate, and the
        screen would otherwise claim "carry everything" while a scan quietly
        used a stored set. The index and the project tree are reads of `.codex`
        and wait to be asked (CS-262).
        """
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
        source, target = self._pair()
        model.scope_pair = (source, target)
        model.scope = None
        model.scope_projects = ()
        model.scope_chats = ()
        if source and target:
            outcome = self.host.controller.working_set(
                source_machine=source, target_machine=target,
            )
            if outcome.ok and outcome.value is not None:
                model.scope_projects = tuple(outcome.value.projects)
                model.scope_chats = tuple(outcome.value.chats)
            elif not outcome.ok:
                model.scope = outcome
        self.render()

    def _pair(self) -> tuple[str, str]:
        return self.source.currentText().strip(), self.target.currentText().strip()

    def open_for(self, source: str, target: str) -> None:
        """Show the chats waiting for a decision between these two machines.

        Asked for by another page -- a full sync that stopped on chats names
        the pair it was syncing, so the person lands on exactly those (CS-342).
        """
        self.source.setEditText(source)
        self.target.setEditText(target)
        self._pair_changed()
        # Straight to the chats that stopped it.
        self.model.category = "decide"
        self.category.setCurrentIndex(max(0, self.category.findData("decide")))
        self.scan()

    def _pair_changed(self, *_: object) -> None:
        """Another pair of machines was chosen: show the set stored for it."""
        if self.model.scope_busy or self.model.scope_pair == self._pair():
            return
        self.model.source, self.model.target = self._pair()
        self.model.rule = None
        self.load_working_set()

    def load_projects(self) -> None:
        """Read the chats once, to draw the tree of projects."""
        if self.model.directory_busy:
            return
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
        # The tree already shows this choice; redrawing it would only lose
        # the scroll position.
        self._scope_drawn = self._scope_key()
        self._render_scope()

    def take_working_set(self) -> None:
        """Expand the chosen projects and remember the set for this pair.

        A write, so it runs as one: nobody may stop waiting for it, because a
        store that is merely no longer watched has not stopped.
        """
        model = self.model
        if model.scope_busy:
            return
        if model.scope_pair != self._pair():
            # The ticks on screen were chosen for another pair; show the set
            # this pair has before anything is stored under it.
            model.source, model.target = self._pair()
            self.load_working_set()
            return
        model.source, model.target = self._pair()
        model.scope_busy = True
        self.render()
        controller = self.host.controller
        projects, chats = model.scope_projects, model.scope_chats
        source, target = model.source, model.target

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.scope_busy = False
            model.scope = outcome

        self.host.run(
            self.page,
            lambda progress=None: controller.save_working_set(
                projects=projects, chats=chats,
                source_machine=source, target_machine=target, progress=progress,
            ),
            apply, progress=True,
        )

    def clear_working_set(self) -> None:
        """Carry everything again.

        Storing an empty set reads no chat, but it is still a write and runs
        off the UI thread like one; its outcome is kept, so a store that failed
        is said rather than hidden behind "carry everything".
        """
        model = self.model
        if model.scope_busy:
            return
        model.source, model.target = self._pair()
        if model.scope_pair != (model.source, model.target):
            # What is ticked belongs to another pair; clearing is for the pair
            # chosen now, so a failure must leave *its* set on screen.
            self.load_working_set()
        model.scope = None
        model.scope_busy = True
        self.render()
        controller = self.host.controller
        source, target = model.source, model.target

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.scope_busy = False
            if outcome.ok:
                # Stored: the page says "carry everything".
                model.scope_projects = ()
                model.scope_chats = ()
                model.scope = None
            else:
                # Not stored: the set that is still in force stays on screen,
                # with the reason, instead of a "carry everything" that is not so.
                model.scope = outcome

        self.run(
            lambda: controller.save_working_set(
                projects=(), chats=(), source_machine=source, target_machine=target,
            ),
            apply,
        )

    def _scope_key(self) -> tuple:
        """What the scope tree is drawn from; it is redrawn only when this changes."""
        return (self.model.directory, self.model.scope_projects)

    def _fill_scope_tree(self) -> None:
        # A scan redraws the page on every progress report; rebuilding a tree
        # of every project each time is what made the window stutter.
        key = self._scope_key()
        drawn = getattr(self, "_scope_drawn", None)
        # The directory by identity: comparing two scans by value would walk
        # every chat on every redraw, which is the cost this avoids.
        if drawn is not None and drawn[0] is key[0] and drawn[1] == key[1]:
            return
        self._scope_drawn = key
        outcome = self.model.directory
        if outcome is None or not outcome.ok:
            self.scope_tree.clear()
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
        self.scope_clear.setEnabled(bool(model.scope_projects or model.scope_chats) and not model.scope_busy)
        if model.scope_busy:
            text = self.t("sessions.scope.counting")
        elif model.scope is not None and model.scope.ok and model.scope.value is not None:
            scope = model.scope.value
            text = self.join([
                self.p("sessions.scope.count", scope.chat_count),
                self.t("sessions.scope.size", size=human_size(scope.total_bytes)),
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
        model.source, model.target = self._pair()
        if model.scope_pair != (model.source, model.target) and not model.scope_busy:
            # The scan uses the set stored for this pair; the page shows that one.
            self.load_working_set()
        # The scope tree is drawn from the chat directory. Asking for a scan is
        # also asking for the tree, so it loads here rather than on arrival.
        if model.directory is None:
            self.load_projects()
        if not model.source or not model.target:
            model.scan = Outcome(failure=Failure.CONFIGURATION, message=self.t("machines.required"))
            self.render()
            return
        model.busy = True
        model.action_result = None
        self.render()
        controller = self.host.controller
        source, target, rule = model.source, model.target, model.rule

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.busy = False
            model.scan = outcome

        self.read(
            lambda progress=None: controller.scan_sessions(
                source_machine=source, target_machine=target, progress=progress, conflict_policy=rule,
            ),
            apply, progress=True,
        )

    def decide_all(self, policy: str) -> None:
        """Scan again with every conflict decided by ``policy``.

        Writes nothing: the plan shows what each one keeps, and *Apply* is the
        decision, quoting that plan's id like any other.
        """
        if self.model.busy or self.model.action_busy:
            return
        self.model.rule = policy
        self.scan()

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

        rule = model.rule

        def go() -> Outcome:
            recorded = controller.resolve_session_conflict(scan, conflict_id=item.conflict_id, choice=choice)
            if not recorded.ok:
                return recorded
            # A decision changes the plan; show the plan it produces.
            return controller.scan_sessions(source_machine=source, target_machine=target, conflict_policy=rule)

        def apply(model: SessionsModel, outcome: Outcome) -> None:
            model.action_busy = False
            if outcome.ok:
                model.scan = outcome
                model.action_result = Outcome(value="resolved")
            else:
                model.action_result = outcome

        self.run(go, apply)

    def resolve_format_migrations(self) -> None:
        model = self.model
        scan = self._scan()
        if scan is None or model.action_busy or not _format_migrations(scan.plan)[0]:
            return
        model.action_busy = True
        model.action_kind = "resolve"
        model.action_result = None
        self.render()
        controller = self.host.controller
        source, target = model.source, model.target

        rule = model.rule

        def go() -> Outcome:
            recorded = controller.resolve_format_migrations(scan)
            if not recorded.ok:
                return recorded
            return controller.scan_sessions(source_machine=source, target_machine=target, conflict_policy=rule)

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
        decided = sum(1 for item in plan.writable_items if "RESOLVED_BY_RULE" in item.codes)
        body = self.t("sessions.confirm.body", count=len(plan.writable_items), plan_id=plan.plan_id)
        if decided:
            body += "\n\n" + self.p("sessions.confirm.decided", decided)
        if not dry_run and not self.host.confirm(
            self.t("sessions.confirm.title"), body, self.t("sessions.apply"),
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

        self.settings_button.setVisible(False)
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
            else:
                # Sentences, never the plan's raw codes: half of those are not
                # refusals at all (`IN_PLACE`, `CWD_ABSENT_HERE`), CS-346.
                notes = plan_notes(plan)
                left_out = any(key in LEFT_OUT_NOTES for key, _ in notes)
                self.banner.show_message(
                    "attention" if left_out else "ok",
                    self.t("sessions.partial.title" if left_out else "sessions.clean.title"),
                    "\n".join(self.p(f"sessions.note.{key}", count) for key, count in notes),
                    palette,
                )
                self.settings_button.setVisible(any(key == "new_in_cloud" for key, _ in notes))
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
        conflicts = scan is not None and any(
            item.action.value == "BLOCKED_CONFLICT" for item in scan.plan.items
        )
        self.rule_label.setVisible(conflicts)
        for choice in self.rule_buttons.values():
            choice.setVisible(conflicts)
            choice.setEnabled(not (self.model.busy or self.model.action_busy))
        decidable, held = _format_migrations(scan.plan) if scan is not None else (0, 0)
        self.format_button.setVisible(bool(decidable))
        self.format_button.setEnabled(bool(decidable) and not self.model.action_busy)
        self.format_hint.setVisible(bool(decidable or held))
        self.format_hint.setText(self.join([
            self.p("sessions.format.hint", decidable) if decidable else "",
            self.p("sessions.format.held", held) if held else "",
        ]))

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
            # Counted, never listed: the folders are paths on this disk, and the
            # rows already carry the code for whoever wants to see which.
            absent = sum(1 for item in plan.items if "CWD_ABSENT_HERE" in item.codes)
            if absent:
                notes.append(self.p("sessions.plan.cwd_absent", absent))
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


#: Notes about chats a plan does not carry, as opposed to notes about what it
#: does beyond a plain copy.
LEFT_OUT_NOTES = frozenset({"new_in_cloud", "held", "unreadable", "direction"})


def plan_notes(plan) -> list[tuple[str, int]]:
    """What a plan leaves out or does beyond copying: (`sessions.note.*` key, count).

    Counted from the items, so the page says the same thing whatever codes a
    plan happens to collect at its own level.
    """
    counts = {
        "new_in_cloud": 0, "held": 0, "unreadable": 0, "moves": 0, "stale": 0, "decided": 0,
        "direction": 0,
    }
    for item in plan.items:
        action = item.action.value
        if "RESOLVED_BY_RULE" in item.codes and (item.action.writes or action == "HELD_BY_DIRECTION"):
            counts["decided"] += 1
        if action == "HELD_BY_DIRECTION":
            counts["direction"] += 1
        if action == "BLOCKED_UNPROVEN_LAYOUT" and "SESSION_ON_ONE_SIDE_ONLY" in item.codes:
            counts["new_in_cloud"] += 1
        elif action in {"BLOCKED_UNPROVEN_LAYOUT", "BLOCKED_UNSUPPORTED_BACKEND"}:
            counts["held"] += 1
        elif action == "BLOCKED_INVALID_BRANCH":
            counts["unreadable"] += 1
        elif action == "ARCHIVE_TRANSITION" or (item.action.writes and "MOVES_BRANCH" in item.codes):
            counts["moves"] += 1
        if "STALE_DUPLICATE_BY_CATALOG" in item.codes:
            counts["stale"] += 1
    return [(key, count) for key, count in counts.items() if count]


def _format_migrations(plan) -> tuple[int, int]:
    """Conflicts that are only a record-format rewrite: decidable in bulk, and held."""
    rewrites = [
        item for item in plan.items
        if item.action.value == "BLOCKED_CONFLICT" and "FORMAT_MIGRATION" in item.codes
    ]
    held = sum(1 for item in rewrites if "OLDER_FORMAT_HAS_LATER_RECORDS" in item.codes)
    return len(rewrites) - held, held
