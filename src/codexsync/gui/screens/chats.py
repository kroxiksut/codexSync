"""Chat bindings: which project each chat is under, why, and moving chats.

This is not a chat window. It shows the directory `chats tree` prints -- every
project, the chats under it, and the chats under none -- with the *reason* each
chat is where it is, because the reasons behave differently: a pinned chat
follows its project, a chat found by path is left behind when the project
moves, and a chat found only by a mapping rule is invisible in Codex until it
is pinned.

A move is always preview, then confirm. The preview id covers the decisions and
the exact bytes of the state they were read from, so the confirm button quotes
an id rather than saying yes; if anything changed in between, the move is
refused and the user is asked to look again.

*Auto-repair* only proposes what is unambiguous: chats a mapping rule already
connects to exactly one project. Everything else stays a manual choice.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QComboBox, QHeaderView, QLineEdit, QTreeWidget, QTreeWidgetItem

from .. import theme
from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, command, fill_table, label, row, set_tone, table
from .base import Model, Screen

ASSOCIATIONS = ("BOUND", "DERIVED", "DERIVED_VIA_MAPPING", "NONE")
_ASSOCIATION_TONES = {"DERIVED_VIA_MAPPING": "attention", "NONE": "danger"}


class ChatsModel(Model):
    def __init__(self) -> None:
        self.directory: Outcome | None = None
        self.busy = False
        self.text = ""
        self.association = ""
        #: Show one project only. Set by the projects screen; cleared here.
        self.project = ""
        self.sub_threads = False
        self.source_machine = ""
        #: (chat ids, project id, include sub-threads) the open preview was built for.
        self.request: tuple[tuple[str, ...], str, bool] | None = None
        self.preview: Outcome | None = None
        self.move_busy = False
        self.move_result: Outcome | None = None
        self.move_kind = ""

    def reset_plans(self) -> None:
        self.directory = None
        self.request = None
        self.preview = None
        self.move_result = None


class ChatsScreen(Screen):
    page = "chats"

    def build(self) -> None:
        m = self.model
        self.search = QLineEdit(m.text)
        self.search.setPlaceholderText(self.t("chats.search"))
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self._filters_changed)
        self.association = QComboBox()
        self.association.addItem(self.t("chats.association.all"), "")
        for value in ASSOCIATIONS:
            self.association.addItem(self.t(f"association.{value}"), value)
        self.association.setCurrentIndex(max(0, self.association.findData(m.association)))
        self.association.currentIndexChanged.connect(self._filters_changed)
        self.sub_threads = QCheckBox(self.t("chats.sub_threads"))
        self.sub_threads.setChecked(m.sub_threads)
        self.sub_threads.toggled.connect(self._filters_changed)
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.lineEdit().setPlaceholderText(self.t("chats.source_machine"))
        self.source.setToolTip(self.t("chats.source_machine.tip"))
        self.source.addItem("")
        for name in self.host.known_machines():
            self.source.addItem(name)
        self.source.setEditText(m.source_machine)
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.only_project = button(self.t("chats.filter.clear_project"))
        self.only_project.clicked.connect(self.clear_project)
        filters = row(
            self.search, self.association, self.only_project, self.refresh_button,
            stretch_last=False,
        )
        filters.setStretchFactor(self.search, 1)
        self.body.addLayout(filters)
        self.body.addLayout(row(self.sub_threads, label(self.t("chats.source_label")), self.source))

        self.banner = Banner()
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(self.t("chats.tree.title"), self.summary)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels([
            self.t("chats.column.title"),
            self.t("chats.column.reason"),
            self.t("chats.column.date"),
            self.t("chats.column.id"),
            self.t("chats.column.records"),
            self.t("chats.column.folder"),
        ])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.resizeSection(0, 380)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        inner.addWidget(self.tree, stretch=1)
        self.body.addWidget(frame, stretch=1)

        move, move_inner = card(self.t("chats.move.title"))
        self.selected = label("", "muted")
        self.target = QComboBox()
        self.target.setMinimumWidth(260)
        self.preview_button = button(self.t("chats.move.preview"))
        self.preview_button.clicked.connect(self.preview_selected)
        move_inner.addLayout(row(self.selected, label(self.t("chats.move.to")), self.target, self.preview_button))

        self.autofix = QComboBox()
        self.autofix.setMinimumWidth(260)
        self.autofix_button = button(self.t("chats.autofix.build"))
        self.autofix_button.clicked.connect(self.preview_autofix)
        move_inner.addLayout(row(label(self.t("chats.autofix.label")), self.autofix, self.autofix_button))
        move_inner.addWidget(label(self.t("chats.autofix.note"), "muted", wrap=True))

        self.plan_table = table([
            self.t("chats.column.title"),
            self.t("chats.plan.column.action"),
            self.t("chats.plan.column.from"),
            self.t("chats.column.reason"),
        ], stretch=0)
        self.plan_table.setMaximumHeight(170)
        move_inner.addWidget(self.plan_table)
        self.plan_id = command("")
        move_inner.addWidget(self.plan_id)
        self.dry_button = button(self.t("action.dry_run"))
        self.dry_button.clicked.connect(lambda: self.move(dry_run=True))
        self.move_button = button(self.t("chats.move.apply"), primary=True)
        self.move_button.clicked.connect(lambda: self.move(dry_run=False))
        move_inner.addLayout(row(self.dry_button, self.move_button))
        self.move_status = label("", wrap=True)
        move_inner.addWidget(self.move_status)
        self.body.addWidget(move)

    def activated(self) -> None:
        if self.model.directory is None and not self.model.busy:
            self.refresh()

    # --- loading -----------------------------------------------------------------

    def refresh(self) -> None:
        if self.model.busy:
            return
        self.model.busy = True
        self.model.source_machine = self.source.currentText().strip()
        self.render()
        controller = self.host.controller
        source = self.model.source_machine or None
        target = self.host.machine_id() if source else None

        def apply(model: ChatsModel, outcome: Outcome) -> None:
            model.busy = False
            model.directory = outcome

        self.read(
            lambda progress=None: controller.chats(
                source_machine=source, target_machine=target, progress=progress,
            ),
            apply, progress=True,
        )

    def clear_project(self) -> None:
        """Stop showing one project only."""
        self.model.project = ""
        self.render()

    def _filters_changed(self, *_: object) -> None:
        self.model.text = self.search.text()
        self.model.association = self.association.currentData() or ""
        self.model.sub_threads = self.sub_threads.isChecked()
        self._fill_tree()

    def _selection_changed(self) -> None:
        count = len(self._selected_ids())
        self.selected.setText(self.p("chats.selected", count))
        self.preview_button.setEnabled(count > 0 and not self.model.move_busy and self.target.count() > 0)

    def _selected_ids(self) -> list[str]:
        return [
            item.data(0, Qt.UserRole)
            for item in self.tree.selectedItems()
            if item.data(0, Qt.UserRole)
        ]

    # --- moving ----------------------------------------------------------------------

    def preview_selected(self) -> None:
        ids = tuple(self._selected_ids())
        project = self.target.currentData()
        if not ids or not project:
            return
        directory = self.model.directory.value
        kinds = {chat.session_id: chat.kind.value for chat in directory.chats}
        self._preview(ids, project, any(kinds.get(i) != "TOP_LEVEL" for i in ids))

    def preview_autofix(self) -> None:
        project = self.autofix.currentData()
        if not project or self.model.directory is None or not self.model.directory.ok:
            return
        ids = tuple(
            chat.session_id for chat in self.model.directory.value.chats
            if chat.association.value == "DERIVED_VIA_MAPPING" and chat.project_id == project
            and chat.kind.value == "TOP_LEVEL"
        )
        if ids:
            self._preview(ids, project, False)

    def _preview(self, ids: tuple[str, ...], project: str, sub_threads: bool) -> None:
        if self.model.move_busy:
            return
        self.model.request = (ids, project, sub_threads)
        self.model.preview = None
        self.model.move_result = None
        self.model.move_busy = True
        self.model.move_kind = "preview"
        self.render()
        controller = self.host.controller
        source = self.model.source_machine or None
        target = self.host.machine_id() if source else None

        def apply(model: ChatsModel, outcome: Outcome) -> None:
            model.move_busy = False
            model.preview = outcome

        self.read(lambda: controller.move_chats(
            chat_refs=list(ids), to_project=project, include_sub_threads=sub_threads,
            source_machine=source, target_machine=target,
        ), apply)

    def move(self, *, dry_run: bool) -> None:
        model = self.model
        if model.move_busy or model.request is None or model.preview is None or not model.preview.ok:
            return
        plan, _ = model.preview.value
        if not dry_run and not self.host.confirm(
            self.t("chats.move.confirm.title"),
            self.t("chats.move.confirm.body", count=len(plan.writing_actions), plan_id=plan.plan_id),
            self.t("chats.move.apply"),
        ):
            return
        ids, project, sub_threads = model.request
        model.move_busy = True
        model.move_kind = "dry" if dry_run else "apply"
        model.move_result = None
        self.render()
        controller = self.host.controller
        source = model.source_machine or None
        target = self.host.machine_id() if source else None

        def apply(model: ChatsModel, outcome: Outcome) -> None:
            model.move_busy = False
            model.move_result = outcome
            if outcome.ok and not dry_run:
                # The state moved on; both the tree and the preview are stale now.
                model.preview = None
                model.request = None
                model.directory = None

        self.run(lambda: controller.move_chats(
            chat_refs=list(ids), to_project=project, confirm_plan=plan.plan_id, dry_run=dry_run,
            include_sub_threads=sub_threads, source_machine=source, target_machine=target,
        ), apply)

    # --- drawing -----------------------------------------------------------------------

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        # Visible and undoable: a list narrowed to one project must not look
        # like the whole list with chats missing.
        self.only_project.setVisible(bool(model.project))
        outcome = model.directory
        if model.busy:
            self.banner.show_message("neutral", self.t("chats.loading"), self.progress_text(), palette)
        elif outcome is None:
            self.banner.show_message("neutral", "", "", palette)
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
        else:
            directory = outcome.value
            stranded = sum(1 for chat in directory.chats if chat.association.value == "DERIVED_VIA_MAPPING")
            if directory.volatile:
                self.banner.show_message("attention", self.t("chats.volatile.title"), self.t("chats.volatile.detail"), palette)
            elif stranded:
                self.banner.show_message(
                    "attention", self.p("chats.stranded.title", stranded), self.t("chats.stranded.detail"), palette
                )
            else:
                self.banner.show_message("neutral", "", "", palette)
        if outcome is None or not outcome.ok:
            self.tree.clear()
            self.summary.setText("")
            self.target.clear()
            self.autofix.clear()
        else:
            self._fill_tree()
            self._fill_targets()
        self._render_move()
        self._selection_changed()

    def _fill_targets(self) -> None:
        directory = self.model.directory.value
        keep = self.target.currentData()
        self.target.clear()
        for view in sorted(directory.projects.values(), key=lambda v: (v.name or v.project_id).casefold()):
            self.target.addItem(view.name or view.project_id, view.project_id)
        if keep:
            self.target.setCurrentIndex(max(0, self.target.findData(keep)))

        groups: dict[str, int] = {}
        for chat in directory.chats:
            if chat.association.value == "DERIVED_VIA_MAPPING" and chat.project_id and chat.kind.value == "TOP_LEVEL":
                groups[chat.project_id] = groups.get(chat.project_id, 0) + 1
        self.autofix.clear()
        for project_id, count in sorted(groups.items(), key=lambda item: -item[1]):
            view = directory.projects.get(project_id)
            name = (view.name if view else None) or project_id
            self.autofix.addItem(f"{name} — {self.p('chats.count', count)}", project_id)
        self.autofix.setEnabled(bool(groups))
        self.autofix_button.setEnabled(bool(groups) and not self.model.move_busy)
        if not groups:
            self.autofix.addItem(self.t("chats.autofix.none"), "")

    def _fill_tree(self) -> None:
        outcome = self.model.directory
        if outcome is None or not outcome.ok:
            return
        directory = outcome.value
        palette = self.palette_
        needle = self.model.text.casefold().strip()
        wanted = self.model.association
        selected = set(self._selected_ids())
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)

        groups = sorted(directory.projects.values(), key=lambda v: (v.name or v.project_id).casefold())
        buckets = [(view.name or view.project_id, view.project_id, view.roots) for view in groups]
        buckets.append((self.t("chats.no_project"), None, ()))
        only = self.model.project
        if only:
            buckets = [item for item in buckets if item[1] == only]
        shown = 0
        projects_shown = 0
        for name, project_id, roots in buckets:
            chats = [
                chat for chat in directory.chats
                if chat.project_id == project_id
                and (self.model.sub_threads or chat.kind.value == "TOP_LEVEL")
                and chat.state.value not in {"INVALID", "AMBIGUOUS"}
                and (not wanted or chat.association.value == wanted)
                and (not needle or needle in " ".join(filter(None, (chat.title, chat.cwd))).casefold())
            ]
            if not chats and (needle or wanted):
                continue
            chats.sort(key=lambda chat: chat.timestamp or "", reverse=True)
            parent = QTreeWidgetItem([name, "", "", "", self.p("chats.count", len(chats)), "  ".join(roots)])
            parent.setFont(0, bold)
            parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
            parent.setToolTip(5, "\n".join(roots))
            self.tree.addTopLevelItem(parent)
            if project_id is not None:
                projects_shown += 1
            for chat in chats:
                reason = self.t(f"association.{chat.association.value}")
                item = QTreeWidgetItem([
                    chat.title or self.t("chats.untitled"),
                    reason,
                    (chat.timestamp or "")[:10],
                    chat.short_id,
                    str(chat.record_count),
                    chat.cwd or "",
                ])
                item.setData(0, Qt.UserRole, chat.session_id)
                item.setToolTip(0, chat.title or "")
                item.setToolTip(3, chat.session_id)
                item.setToolTip(5, chat.cwd or "")
                tone = _ASSOCIATION_TONES.get(chat.association.value)
                if tone:
                    item.setForeground(1, QColor(theme.tone_colour(tone, palette)))
                if chat.kind.value != "TOP_LEVEL":
                    item.setForeground(0, self.tree.palette().placeholderText())
                parent.addChild(item)
                if chat.session_id in selected:
                    item.setSelected(True)
                shown += 1
            parent.setExpanded(bool(needle or wanted) or len(chats) <= 25)
        self.summary.setText(self.join([
            self.p("chats.projects", projects_shown),
            self.p("chats.count", shown),
        ]))

    def _render_move(self) -> None:
        model = self.model
        palette = self.palette_
        preview = model.preview
        ready = preview is not None and preview.ok and not preview.value[0].codes and bool(preview.value[0].writing_actions)
        self.dry_button.setEnabled(ready and not model.move_busy)
        self.move_button.setEnabled(ready and not model.move_busy)
        has_plan = preview is not None and preview.ok
        self.plan_table.setVisible(has_plan)
        self.dry_button.setVisible(has_plan)
        self.move_button.setVisible(has_plan)
        if not has_plan:
            self.plan_table.setRowCount(0)
            self.plan_id.setText("")
            self.plan_id.setVisible(False)
        else:
            plan, _ = preview.value
            directory = model.directory.value if model.directory is not None and model.directory.ok else None
            titles = {chat.session_id: chat.title for chat in directory.chats} if directory else {}
            names = {pid: (view.name or pid) for pid, view in directory.projects.items()} if directory else {}
            rows = []
            for action in plan.actions:
                rows.append([
                    Cell(titles.get(action.session_id) or action.session_id.split("-", 1)[0], tooltip=action.session_id),
                    Cell(self.t(f"chats.plan.kind.{action.kind.value}"), tone="ok" if action.kind.writes else None),
                    Cell(names.get(action.from_project_id or "", self.t("chats.no_project")) if action.from_project_id else self.t("chats.no_project")),
                    Cell(self.t(f"association.{action.from_association.value}"), muted=True),
                ])
            fill_table(self.plan_table, rows, palette)
            self.plan_id.setText(self.t("common.plan_id", plan_id=plan.plan_id))
            self.plan_id.setVisible(True)

        status = ""
        tone = None
        if model.move_busy:
            status = self.t("chats.move.working")
        elif model.move_result is not None:
            if model.move_result.ok:
                _, written = model.move_result.value
                key = "chats.move.done.dry" if model.move_kind == "dry" else "chats.move.done.apply"
                status, tone = self.p(key, written), "ok"
            else:
                status, tone = self.failure_text(model.move_result), "danger"
        elif preview is not None:
            if not preview.ok:
                status, tone = self.failure_text(preview), "danger"
            else:
                plan, _ = preview.value
                if plan.codes:
                    status, tone = self.t("chats.move.codes", codes=", ".join(plan.codes)), "danger"
                elif not plan.writing_actions:
                    status = self.t("chats.move.nothing")
                else:
                    status = self.p("chats.move.ready", len(plan.writing_actions))
        self.move_status.setText(status)
        set_tone(self.move_status, tone, palette)
        self.move_status.setVisible(bool(status))
