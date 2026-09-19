"""Projects: roots, the chats each one holds, and repair after a machine handoff.

Two scenarios the spec keeps apart, and this screen keeps apart too.

*Repair after moving to another machine* is the existing exact plan: sessions'
recorded directories run through `[[path_mappings]]`, and the plan pins chats
or remaps a moved project's root. A remap never travels alone -- the plan also
pins every chat still living under the old root -- and the screen shows both
halves so nobody is surprised by the bindings.

*Moving a project to a new folder* copies the files into a folder that does not
exist yet, verifies every byte, and only then points Codex at it, pinning the
chats that reached the project by path. The old folder is never modified or
deleted -- the screen says where it is and leaves that decision to the person.
Preview (which hashes the whole project), dry run and move, in that order; the
move unlocks only after a dry run of the same plan id passed.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QComboBox, QMenu

from ..controller import Failure, Outcome
from ..widgets import (
    Banner,
    Cell,
    PathField,
    button,
    card,
    command,
    fill_table,
    label,
    machine_combo,
    row,
    selected_data,
    set_tone,
    table,
)
from .base import Model, Screen

WRITING_KINDS = {"ADD_PROJECT", "REMAP_ROOT", "ADD_BINDING"}
BLOCKING_KINDS = {"AMBIGUOUS_PROJECT", "UNSUPPORTED_BACKEND"}
QUIET_KINDS = {"KEEP_PROJECT", "KEEP_BINDING", "SKIP_UNMAPPED"}
KIND_TONES = {"REMAP_ROOT": "attention", "ADD_BINDING": "ok", "ADD_PROJECT": "ok", "AMBIGUOUS_PROJECT": "danger", "UNSUPPORTED_BACKEND": "danger"}


class ProjectsModel(Model):
    def __init__(self) -> None:
        self.directory: Outcome | None = None
        self.busy = False
        self.source = ""
        self.target = ""
        self.show_all = False
        self.scan: Outcome | None = None
        self.scan_busy = False
        self.action_busy = False
        self.action_kind = ""
        self.action_result: Outcome | None = None
        self.move_project = ""
        self.move_target = ""
        #: The project row chosen in the list, kept across rebuilds.
        self.selected = ""
        self.move_scan: Outcome | None = None
        self.move_busy = False
        self.move_kind = ""
        self.move_checked: str | None = None
        self.move_result: Outcome | None = None

    def reset_plans(self) -> None:
        self.directory = None
        self.scan = None
        self.action_result = None
        self.move_scan = None
        self.move_checked = None
        self.move_result = None


class ProjectsScreen(Screen):
    page = "projects"
    scrollable = True

    def build(self) -> None:
        m = self.model
        self.banner = Banner()
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(self.t("projects.list.title"), self.summary)
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        inner.addWidget(label(self.t("projects.list.caption"), "muted", wrap=True))
        self.projects = table([
            self.t("projects.column.name"),
            self.t("projects.column.bound"),
            self.t("projects.column.derived"),
            self.t("projects.column.mapped"),
            self.t("projects.column.root"),
        ], selectable=True)
        self.projects.setMinimumHeight(300)
        self.projects.itemSelectionChanged.connect(self._project_selected)
        self.projects.setContextMenuPolicy(Qt.CustomContextMenu)
        self.projects.customContextMenuRequested.connect(self._project_menu)
        inner.addWidget(self.projects, stretch=1)

        # Only what can actually be done today. Deleting, merging and
        # repointing a project wait for `PROVEN_PROJECT_REGISTRY`: Codex keeps
        # projects in `state_5.sqlite` as well, and codexSync does not write
        # there, so a button for any of them would be a button that might do
        # nothing at all. A dead button is worse than no button.
        self.show_chats_button = button(self.t("projects.action.show_chats"))
        self.show_chats_button.clicked.connect(self.show_chats)
        self.move_here_button = button(self.t("projects.action.move"))
        self.move_here_button.clicked.connect(self.move_selected)
        self.open_folder_button = button(self.t("projects.action.open"))
        self.open_folder_button.clicked.connect(self.open_folder)
        inner.addLayout(row(
            self.show_chats_button, self.move_here_button, self.open_folder_button,
        ))
        self.body.addWidget(frame)

        repair, repair_inner = card(self.t("projects.repair.title"))
        repair_inner.addWidget(label(self.t("projects.repair.caption"), "muted", wrap=True))
        machines = self.host.known_machines()
        if not m.target:
            m.target = self.host.machine_id() or ""
        if not m.source:
            m.source = next((name for name in machines if name != m.target), "")
        self.source = machine_combo(machines, m.source, placeholder=self.t("machines.source"))
        self.target = machine_combo(machines, m.target, placeholder=self.t("machines.target"))
        self.scan_button = button(self.t("projects.repair.scan"), primary=True)
        self.scan_button.clicked.connect(self.scan)
        repair_inner.addLayout(row(
            label(self.t("machines.source_label")), self.source,
            label(self.t("machines.target_label")), self.target,
            self.scan_button,
        ))
        self.repair_summary = label("", "muted", wrap=True)
        self.show_all = QCheckBox(self.t("projects.repair.show_all"))
        self.show_all.setChecked(m.show_all)
        self.show_all.toggled.connect(self._toggle_all)
        repair_inner.addLayout(row(self.repair_summary, self.show_all, stretch_last=False))
        self.actions = table([
            self.t("projects.repair.column.action"),
            self.t("projects.column.name"),
            self.t("projects.repair.column.root"),
            self.t("projects.repair.column.from"),
            self.t("projects.repair.column.rule"),
            self.t("sessions.column.session"),
        ], stretch=2)
        self.actions.setMinimumHeight(280)
        repair_inner.addWidget(self.actions, stretch=1)
        self.plan_id = command("")
        repair_inner.addWidget(self.plan_id)
        self.dry_button = button(self.t("action.dry_run"))
        self.dry_button.clicked.connect(lambda: self.apply(dry_run=True))
        self.apply_button = button(self.t("projects.repair.apply"), primary=True)
        self.apply_button.clicked.connect(lambda: self.apply(dry_run=False))
        repair_inner.addLayout(row(self.dry_button, self.apply_button))
        self.status = label("", wrap=True)
        repair_inner.addWidget(self.status)
        self.body.addWidget(repair)

        move, move_inner = card(self.t("projects.move.title"))
        move_inner.addWidget(label(self.t("projects.move.body"), "muted", wrap=True))
        self.move_project = QComboBox()
        self.move_project.setMinimumWidth(460)
        self.move_project.currentIndexChanged.connect(self._move_inputs_changed)
        self.move_target = PathField(self.t("common.browse"))
        self.move_target.edit.setPlaceholderText(self.t("projects.move.target.placeholder"))
        self.move_target.setText(m.move_target)
        self.move_target.picked = self._target_inside
        self.move_target.edit.textChanged.connect(self._move_inputs_changed)
        move_inner.addLayout(row(label(self.t("projects.move.project")), self.move_project))
        target_row = row(label(self.t("projects.move.target")), self.move_target, stretch_last=False)
        target_row.setStretchFactor(self.move_target, 1)
        move_inner.addLayout(target_row)
        self.move_scan_button = button(self.t("projects.move.scan"))
        self.move_scan_button.clicked.connect(self.scan_move)
        self.move_dry = button(self.t("action.dry_run"))
        self.move_dry.clicked.connect(lambda: self.apply_move(dry_run=True))
        self.move_apply = button(self.t("projects.move.apply"), primary=True)
        self.move_apply.clicked.connect(lambda: self.apply_move(dry_run=False))
        move_inner.addLayout(row(self.move_scan_button, self.move_dry, self.move_apply))
        self.move_summary = label("", wrap=True)
        move_inner.addWidget(self.move_summary)
        self.move_plan_id = command("")
        move_inner.addWidget(self.move_plan_id)
        self.move_status = label("", wrap=True)
        move_inner.addWidget(self.move_status)
        self.body.addWidget(move)

    # --- what can be done to the selected project ----------------------------

    def selected_project(self) -> str | None:
        """The project id of the chosen row, or None for "no project"/no row."""
        chosen = selected_data(self.projects)
        return chosen[0] if chosen and chosen[0] else None

    def _project_selected(self) -> None:
        chosen = self.selected_project()
        if chosen:
            self.model.selected = chosen
        self._render_actions()

    def _project_menu(self, point) -> None:  # pragma: no cover - opens a native menu
        if self.selected_project() is None:
            return
        menu = QMenu(self)
        for text, handler in (
            (self.t("projects.action.show_chats"), self.show_chats),
            (self.t("projects.action.move"), self.move_selected),
            (self.t("projects.action.open"), self.open_folder),
        ):
            menu.addAction(text, handler)
        menu.exec(self.projects.viewport().mapToGlobal(point))

    def show_chats(self) -> None:
        """Open the chats screen already filtered to this project."""
        project = self.selected_project()
        if project is None:
            return
        chats = self.host.model("chats")
        chats.project = project
        self.host.go_to("chats")

    def move_selected(self) -> None:
        """Fill the move card below with the selected project."""
        project = self.selected_project()
        if project is None:
            return
        index = self.move_project.findData(project)
        if index >= 0:
            self.move_project.setCurrentIndex(index)

    def open_folder(self) -> None:
        """Show the project's folder in the file manager. Opens nothing else."""
        view = self._project_view(self.selected_project())
        root = view.roots[0] if view is not None and view.roots else ""
        if root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(root))

    def _render_actions(self) -> None:
        view = self._project_view(self.selected_project())
        chosen = view is not None
        self.show_chats_button.setEnabled(chosen)
        self.move_here_button.setEnabled(chosen)
        self.open_folder_button.setEnabled(bool(chosen and view.roots))

    def _restore_selection(self) -> None:
        """Keep the chosen row across a redraw, a rescan and a language change."""
        wanted = self.model.selected
        if not wanted:
            self._render_actions()
            return
        for r in range(self.projects.rowCount()):
            item = self.projects.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == wanted:
                blocked = self.projects.blockSignals(True)
                try:
                    self.projects.selectRow(r)
                finally:
                    self.projects.blockSignals(blocked)
                break
        self._render_actions()

    def activated(self) -> None:
        if self.model.directory is None and not self.model.busy:
            self.refresh()

    # --- actions -----------------------------------------------------------------------

    def refresh(self) -> None:
        if self.model.busy:
            return
        self.model.busy = True
        self.render()

        def apply(model: ProjectsModel, outcome: Outcome) -> None:
            model.busy = False
            model.directory = outcome

        self.read(
            lambda progress=None: self.host.controller.chats(progress=progress),
            apply, progress=True,
        )

    def scan(self) -> None:
        model = self.model
        if model.scan_busy or model.action_busy:
            return
        model.source = self.source.currentText().strip()
        model.target = self.target.currentText().strip()
        model.action_result = None
        if not model.source or not model.target:
            model.scan = Outcome(failure=Failure.CONFIGURATION, message=self.t("machines.required"))
            self.render()
            return
        model.scan_busy = True
        self.render()
        controller = self.host.controller
        source, target = model.source, model.target

        def apply(model: ProjectsModel, outcome: Outcome) -> None:
            model.scan_busy = False
            model.scan = outcome

        self.read(
            lambda progress=None: controller.scan_repair(
                source_machine=source, target_machine=target, progress=progress,
            ),
            apply, progress=True,
        )

    def apply(self, *, dry_run: bool) -> None:
        model = self.model
        scan = model.scan.value if model.scan is not None and model.scan.ok else None
        if scan is None or model.action_busy:
            return
        plan = scan.plan
        writes = sum(1 for action in plan.actions if action.kind.value in WRITING_KINDS)
        if not dry_run and not self.host.confirm(
            self.t("projects.repair.confirm.title"),
            self.t("projects.repair.confirm.body", count=writes, plan_id=plan.plan_id),
            self.t("projects.repair.apply"),
        ):
            return
        model.action_busy = True
        model.action_kind = "dry" if dry_run else "apply"
        model.action_result = None
        self.render()
        controller = self.host.controller

        def apply(model: ProjectsModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.action_result = outcome
            if outcome.ok and not dry_run:
                model.scan = None
                model.directory = None

        self.run(lambda: controller.apply_repair(scan, confirm_plan=plan.plan_id, dry_run=dry_run), apply)

    # --- moving a project ------------------------------------------------------------------

    def _target_inside(self, parent: str) -> str:
        """A picked folder means "put the project in here", under its own name."""
        project = self._project_view(self.move_project.currentData())
        name = Path(project.roots[0]).name if project is not None and project.roots else ""
        return str(Path(parent) / name) if name else parent

    def _project_view(self, project_id):
        outcome = self.model.directory
        if outcome is None or not outcome.ok or not project_id:
            return None
        return outcome.value.projects.get(project_id)

    def _move_inputs_changed(self, *_: object) -> None:
        model = self.model
        project = self.move_project.currentData() or ""
        target = self.move_target.text()
        if (project, target) != (model.move_project, model.move_target):
            model.move_project, model.move_target = project, target
            model.move_scan = None
            model.move_checked = None
            model.move_result = None
        self._render_move()

    def scan_move(self) -> None:
        model = self.model
        if model.move_busy or not model.move_project or not model.move_target:
            return
        model.move_busy = True
        model.move_kind = "scan"
        model.move_scan = None
        model.move_checked = None
        model.move_result = None
        self._render_move()
        controller = self.host.controller
        project, target = model.move_project, model.move_target

        def apply(model: ProjectsModel, outcome: Outcome) -> None:
            model.move_busy = False
            if (model.move_project, model.move_target) == (project, target):
                model.move_scan = outcome

        self.read(lambda: controller.scan_project_move(project_id=project, new_root=Path(target)), apply)

    def apply_move(self, *, dry_run: bool) -> None:
        model = self.model
        scan = model.move_scan.value if model.move_scan is not None and model.move_scan.ok else None
        if scan is None or model.move_busy:
            return
        plan = scan.plan
        if not dry_run and not self.host.confirm(
            self.t("projects.move.confirm.title"),
            self.t(
                "projects.move.confirm.body",
                old=plan.old_root, new=plan.new_root, files=plan.file_count,
                size=_size(plan.total_bytes), chats=len(plan.bindings), plan_id=plan.plan_id,
            ),
            self.t("projects.move.apply"),
        ):
            return
        model.move_busy = True
        model.move_kind = "dry" if dry_run else "apply"
        model.move_result = None
        self._render_move()
        controller = self.host.controller
        plan_id = plan.plan_id

        def apply(model: ProjectsModel, outcome: Outcome) -> None:
            model.move_busy = False
            model.move_result = outcome
            if outcome.ok and dry_run:
                model.move_checked = plan_id
            if outcome.ok and not dry_run:
                model.move_scan = None
                model.move_checked = None
                model.directory = None

        self.run(lambda: controller.apply_project_move(scan, confirm_plan=plan_id, dry_run=dry_run), apply)

    def _fill_move_projects(self) -> None:
        outcome = self.model.directory
        keep = self.model.move_project
        self.move_project.blockSignals(True)
        self.move_project.clear()
        if outcome is not None and outcome.ok:
            for view in sorted(outcome.value.projects.values(), key=lambda v: (v.name or v.project_id).casefold()):
                root = view.roots[0] if view.roots else ""
                self.move_project.addItem(f"{view.name or view.project_id} — {root}", view.project_id)
            index = self.move_project.findData(keep)
            self.move_project.setCurrentIndex(index if index >= 0 else 0)
        self.move_project.blockSignals(False)
        self.model.move_project = self.move_project.currentData() or ""

    def _render_move(self) -> None:
        model = self.model
        palette = self.palette_
        scan = model.move_scan.value if model.move_scan is not None and model.move_scan.ok else None
        plan = scan.plan if scan is not None else None
        busy = model.move_busy
        self.move_scan_button.setEnabled(bool(model.move_project and model.move_target) and not busy)
        ready = plan is not None and not plan.codes and not plan.volatile
        self.move_dry.setEnabled(ready and not busy)
        self.move_apply.setEnabled(ready and not busy and model.move_checked == plan.plan_id)

        if plan is None:
            self.move_summary.setText("")
            self.move_plan_id.setVisible(False)
        else:
            lines = [self.t(
                "projects.move.summary",
                old=plan.old_root, new=plan.new_root, files=plan.file_count,
                size=_size(plan.total_bytes), chats=len(plan.bindings),
            )]
            if plan.copy_complete:
                lines.append(self.t("projects.move.copy_complete"))
            for code, path in plan.blocked_paths[:8]:
                lines.append(f"  {self._move_code(code)}: {path}")
            self.move_summary.setText("\n".join(lines))
            self.move_plan_id.setText(self.t("common.plan_id", plan_id=plan.plan_id))
            self.move_plan_id.setVisible(True)

        text, tone = "", None
        if busy:
            text = self.t("projects.move.working.scan" if model.move_kind == "scan" else "projects.move.working.copy")
        elif model.move_result is not None:
            result = model.move_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
            elif result.value.dry_run:
                text, tone = self.t("projects.move.done.dry"), "ok"
            else:
                value = result.value
                text = self.t("projects.move.done.apply", files=value.copied_files, chats=value.bindings_written, old=value.old_root_kept)
                tone = "ok"
        elif model.move_scan is not None and not model.move_scan.ok:
            text, tone = self.failure_text(model.move_scan), "danger"
        elif plan is not None:
            if plan.codes:
                text, tone = self.t("common.codes", codes=", ".join(self._move_code(code) for code in plan.codes)), "danger"
            elif plan.volatile:
                text, tone = self.t("projects.move.volatile"), "attention"
            elif model.move_checked != plan.plan_id:
                text = self.t("projects.move.dry_first")
        self.move_status.setText(text)
        set_tone(self.move_status, tone, palette)
        self.move_status.setVisible(bool(text))

    def _move_code(self, code: str) -> str:
        key = f"projects.move.code.{code}"
        return self.t(key) if self.host.catalog.has(key) else code

    def _toggle_all(self, checked: bool) -> None:
        self.model.show_all = checked
        self._fill_actions()

    # --- drawing -----------------------------------------------------------------------

    def _names(self) -> dict[str, str]:
        outcome = self.model.directory
        if outcome is None or not outcome.ok:
            return {}
        return {pid: (view.name or pid) for pid, view in outcome.value.projects.items()}

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        self.scan_button.setEnabled(not (model.scan_busy or model.action_busy))

        outcome = model.directory
        if model.busy:
            self.banner.show_message("neutral", self.t("projects.loading"), self.progress_text(), palette)
            self.projects.setRowCount(0)
            self.summary.setText("")
        elif outcome is None:
            self.banner.show_message("neutral", "", "", palette)
            self.projects.setRowCount(0)
            self.summary.setText("")
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
            self.projects.setRowCount(0)
            self.summary.setText("")
        else:
            directory = outcome.value
            stats: dict[str | None, dict[str, int]] = {}
            for chat in directory.chats:
                if chat.kind.value != "TOP_LEVEL" or chat.state.value in {"INVALID", "AMBIGUOUS"}:
                    continue
                bucket = stats.setdefault(chat.project_id, {"BOUND": 0, "DERIVED": 0, "DERIVED_VIA_MAPPING": 0, "NONE": 0})
                bucket[chat.association.value] = bucket.get(chat.association.value, 0) + 1
            rows = []
            for view in sorted(directory.projects.values(), key=lambda v: (v.name or v.project_id).casefold()):
                bucket = stats.get(view.project_id, {})
                mapped = bucket.get("DERIVED_VIA_MAPPING", 0)
                rows.append([
                    Cell(view.name or view.project_id, tooltip=view.project_id, data=view.project_id),
                    Cell(str(bucket.get("BOUND", 0))),
                    Cell(str(bucket.get("DERIVED", 0))),
                    Cell(str(mapped), tone="attention" if mapped else None),
                    Cell("  ".join(view.roots), muted=True),
                ])
            loose = stats.get(None, {})
            rows.append([
                Cell(self.t("chats.no_project"), tone="danger" if sum(loose.values()) else None),
                Cell("0"), Cell("0"), Cell("0"),
                Cell(self.p("chats.count", sum(loose.values())), muted=True),
            ])
            fill_table(self.projects, rows, palette)
            self._restore_selection()
            lost = sum(bucket.get("DERIVED_VIA_MAPPING", 0) for bucket in stats.values())
            self.summary.setText(self.join([
                self.p("chats.projects", len(directory.projects)),
                self.p("projects.lost", lost),
            ]))
            if lost:
                self.banner.show_message("attention", self.p("chats.stranded.title", lost), self.t("projects.lost.detail"), palette)
            else:
                self.banner.show_message("neutral", "", "", palette)
        self._fill_actions()
        self._render_plan()
        if self.move_project.count() == 0 or getattr(self, "_move_drawn", None) is not model.directory:
            self._fill_move_projects()
            self._move_drawn = model.directory
        self._render_move()

    def _fill_actions(self) -> None:
        model = self.model
        palette = self.palette_
        scan = model.scan.value if model.scan is not None and model.scan.ok else None
        if scan is None:
            self.actions.setRowCount(0)
            if model.scan_busy:
                self.repair_summary.setText(self.t("projects.repair.scanning"))
            elif model.scan is not None:
                self.repair_summary.setText(self.failure_text(model.scan))
            else:
                self.repair_summary.setText("")
            set_tone(self.repair_summary, "danger" if model.scan is not None and not model.scan.ok else None, palette)
            return
        set_tone(self.repair_summary, None, palette)
        plan = scan.plan
        counts: dict[str, int] = {}
        for action in plan.actions:
            counts[action.kind.value] = counts.get(action.kind.value, 0) + 1
        self.repair_summary.setText(self.join([
            f"{self.t(f'repair.kind.{kind}')}: {count}" for kind, count in sorted(counts.items())
        ]) or self.t("projects.repair.empty"))
        names = self._names()
        order = {"REMAP_ROOT": 0, "ADD_PROJECT": 1, "AMBIGUOUS_PROJECT": 2, "UNSUPPORTED_BACKEND": 3, "ADD_BINDING": 4}
        actions = [a for a in plan.actions if model.show_all or a.kind.value not in QUIET_KINDS]
        actions.sort(key=lambda a: (order.get(a.kind.value, 9), names.get(a.project_id or "", "")))
        rows = []
        for action in actions:
            kind = action.kind.value
            rows.append([
                Cell(self.t(f"repair.kind.{kind}"), tone=KIND_TONES.get(kind), tooltip=kind),
                Cell(names.get(action.project_id or "", action.project_id or "—"), tooltip=action.project_id),
                Cell(action.target_root or "—"),
                Cell(action.source_root or "", muted=True),
                Cell(action.rule_id or "", muted=True),
                Cell(action.session_hash[:12], tooltip=action.session_id, muted=True),
            ])
        fill_table(self.actions, rows, palette)

    def _render_plan(self) -> None:
        model = self.model
        palette = self.palette_
        scan = model.scan.value if model.scan is not None and model.scan.ok else None
        ready = False
        if scan is None:
            self.plan_id.setVisible(False)
        else:
            plan = scan.plan
            self.plan_id.setText(self.t("common.plan_id", plan_id=plan.plan_id))
            self.plan_id.setVisible(True)
            kinds = {action.kind.value for action in plan.actions}
            ready = (
                not plan.volatile and not plan.codes and not (kinds & BLOCKING_KINDS)
                and bool(kinds & WRITING_KINDS)
            )
        busy = model.scan_busy or model.action_busy
        self.dry_button.setEnabled(ready and not busy)
        self.apply_button.setEnabled(ready and not busy)

        text, tone = "", None
        if model.action_busy:
            text = self.t("projects.repair.working")
        elif model.action_result is not None:
            result = model.action_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
            else:
                key = "projects.repair.done.dry" if model.action_kind == "dry" else "projects.repair.done.apply"
                text, tone = self.p(key, int(result.value)), "ok"
        elif scan is not None:
            plan = scan.plan
            if plan.volatile:
                text, tone = self.t("projects.repair.volatile"), "attention"
            elif plan.codes:
                text, tone = self.t("common.codes", codes=", ".join(plan.codes)), "danger"
            elif not ready:
                text = self.t("projects.repair.nothing")
            else:
                text = self.t("sessions.plan.saved", path=scan.plan_path)
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))


def _size(size: int) -> str:
    from .guardian import _size as size_text

    return size_text(size)
