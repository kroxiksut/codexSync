"""Settings: `config.toml`, edited in place, and the interface language.

`config.toml` is the only source of truth, and the screen behaves like it. The
file is read into a draft; nothing on screen is a second copy of a setting. A
change becomes an edit only when it differs from what the file says, so saving
one field rewrites that one value and leaves every comment and every other
line exactly as the user wrote it. Before anything is written the edited text
goes through the same loader the command line uses, the difference is shown,
and the save refuses if the file changed on disk since it was opened. The
replaced version goes into the config history first, and every open plan in
the window is discarded afterwards.

Two settings are displayed and deliberately not editable: requiring Codex to be
stopped and failing on uncertainty. A window whose checkbox could switch off the
safety spine would be a second authority over it.

The interface language is not in `config.toml`: it changes how CodexSync talks,
never what it does. It sits in the page header rather than in a tab of its own:
a tab is a group of settings, and a tab holding one control that is not even a
setting reads as one.

Which config file is open is also decided here. The window remembers the path
-- the path only -- so the next start opens the same file from anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..controller import (
    BROKEN_TASK_CODES,
    FOREIGN_TASK,
    LEGACY_TASK,
    PATH_SUBSTITUTIONS,
    REMOVE,
    ConfigEdits,
    Outcome,
    default_background_process_names,
    default_process_names,
    resolve_path_preview,
)
from ..i18n import available_languages, load
from ..widgets import (
    Banner,
    Cell,
    PathField,
    PathTreeDialog,
    button,
    card,
    field,
    fill_table,
    label,
    machine_combo,
    row,
    set_tone,
    table,
)
from .base import Model, Screen


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    kind: str  # text | path | int | float | bool | choice | lines
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: float = 0
    maximum: float = 10**9
    locked: bool = False
    step: float = 1
    #: Values shown in the list but not selectable, each for a stated reason.
    #: A rule the user cannot change is still a rule they should be able to
    #: see; leaving the value out would make the screen look like the option
    #: does not exist, and they would go looking for it in the file.
    blocked: tuple[str, ...] = ()

    @property
    def id(self) -> tuple[str, str]:
        return (self.section, self.key)


TABS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    ("general", (
        Field("identity", "machine_id", "text", ""),
        Field("paths", "workspace_root_dir", "path", ""),
        Field("paths", "local_state_dir", "path", ""),
        Field("paths", "cloud_root_dir", "path", ""),
        Field("paths", "backup_dir", "path", ""),
        Field("paths", "temp_dir", "path", ""),
    )),
    ("sync", (
        Field("sync", "compare", "choice", "mtime", ("mtime", "mtime_hash_fallback")),
        Field("sync", "time_tolerance_seconds", "int", 0, maximum=3600),
        Field("sync", "equal_mtime_action", "choice", "skip", ("skip", "prefer_local", "prefer_cloud", "manual_abort")),
        Field("conflict", "policy", "choice", "manual_abort", ("manual_abort", "prefer_cloud", "prefer_local", "prefer_newer_mtime")),
        Field("conflict", "report_conflicts", "bool", True),
        Field("sync", "dry_run_default", "bool", True),
        Field("targets", "include_roots", "lines", []),
        Field("filters", "exclude_globs", "lines", []),
        Field("sync", "direction", "choice", "bidirectional", ("bidirectional", "to_cloud", "to_local")),
        Field("sync", "delete_policy", "choice", "never", ("never", "propagate")),
        Field(
            "sync", "session_mode", "choice", "all", ("all", "last_date_only"),
            blocked=("last_date_only",),
        ),
        Field("sync", "mode", "choice", "cold", ("cold", "hot"), blocked=("hot",), locked=True),
    )),
    ("protection", (
        Field("safety", "require_codex_stopped", "bool", True, locked=True),
        Field("safety", "fail_on_unknown", "bool", True, locked=True),
        # The lists come from process_knowledge so the screen, the template and
        # the loader cannot drift apart (CS-256).
        Field("process_detection", "process_names", "lines", default_process_names()),
        Field("process_detection", "grace_period_seconds", "int", 2, maximum=600),
        Field(
            "process_detection.background_process_names", "windows", "lines",
            default_background_process_names()["windows"],
        ),
        Field(
            "process_detection.background_process_names", "macos", "lines",
            default_background_process_names()["macos"],
        ),
        Field(
            "process_detection.background_process_names", "linux", "lines",
            default_background_process_names()["linux"],
        ),
        Field("guardian", "root_dir", "path", ""),
        Field("guardian", "retention_days", "int", 30, maximum=36500),
        Field("guardian", "max_snapshots", "int", 100, maximum=100000),
        Field("guardian", "quarantine_retention_days", "int", 30, maximum=36500),
        Field("guardian", "poll_interval_seconds", "float", 3.0, minimum=0.1, maximum=3600, step=0.5),
        Field("guardian", "staging_retention_hours", "int", 24, maximum=87600),
        Field("guardian", "max_state_bytes", "int", 67108864, minimum=1048576, maximum=1073741824, step=1048576),
        Field("guardian", "shrink_min_count", "int", 2, minimum=1, maximum=100000),
        Field("guardian", "shrink_ratio", "float", 0.25, minimum=0.0, maximum=1.0, step=0.05),
        Field("guardian", "debounce_seconds", "float", 2.0, minimum=0.1, maximum=3600, step=0.5),
        Field("guardian", "stable_reads", "int", 3, minimum=3, maximum=100),
        Field("guardian", "stable_read_interval_seconds", "float", 0.5, minimum=0.05, maximum=60, step=0.1),
        Field("guardian", "fallback_scan_seconds", "float", 60.0, minimum=1, maximum=86400, step=10),
        Field("guardian", "once_timeout_seconds", "float", 120.0, minimum=1, maximum=86400, step=10),
    )),
    ("automation", (
        Field("scheduler", "enabled", "bool", False),
        Field("scheduler", "mode", "choice", "guardian_snapshot", ("guardian_snapshot", "preflight", "sync_dry_run")),
        Field("scheduler", "interval_seconds", "int", 60, minimum=60, maximum=7 * 24 * 3600, step=60),
        Field("scheduler", "run_at_login", "bool", True),
        Field("scheduler", "startup_delay_seconds", "int", 0, maximum=24 * 3600),
        Field("scheduler", "jitter_seconds", "int", 0, maximum=24 * 3600),
    )),
    ("mappings", ()),
    ("service", (
        Field("backup", "retention_days", "int", 30, maximum=36500),
        Field("backup", "max_backups", "int", 0, maximum=100000),
        Field("backup", "compression", "choice", "none", ("none", "zip")),
        Field("semantic", "root_dir", "path", ""),
        Field("semantic", "mirror_compression", "choice", "xz", ("none", "gzip", "xz")),
        Field("semantic", "max_jsonl_line_bytes", "int", 67108864, minimum=1048576, maximum=1073741824, step=1048576),
        Field("state", "manifest_file", "path", ""),
        Field("state", "data_version", "int", 1, minimum=0, maximum=1000, locked=True),
        Field("logging", "level", "choice", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
        Field("logging", "file", "path", ""),
        Field("logging", "format", "choice", "text", ("text", "json", "logfmt")),
        Field("logging", "retention_days", "int", 7, maximum=36500),
        Field("logging", "archive_mode", "choice", "zip", ("text", "zip")),
        Field("logging", "max_file_size_mb", "int", 10, minimum=1, maximum=100000),
    )),
)

#: Keys an older `[scheduler]` section carries that the current contract
#: replaced. Dropped when the automation settings are saved, so the file does
#: not keep saying something nothing reads.
LEGACY_SCHEDULER_KEYS = ("kind", "interval_minutes")
MAPPING_COLUMNS = ("rule_id", "source_machine", "target_machine", "from", "to", "case_sensitive")


def _mapping_cell(entry: dict[str, Any], column: str) -> str:
    """One rule's column, as text. `case_sensitive` is a tri-state, not a bool."""
    value = entry.get(column)
    if column == "case_sensitive":
        return "" if value is None else ("true" if value else "false")
    return "" if value is None else str(value)


def raw_value(raw: dict[str, Any], section: str, key: str) -> Any:
    node: Any = raw
    for part in section.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node.get(key) if isinstance(node, dict) else None


class SettingsModel(Model):
    def __init__(self) -> None:
        self.opened: Outcome | None = None
        self.busy = False
        self.tab = "general"
        #: Field id -> value typed on screen, kept across language changes.
        self.draft: dict[tuple[str, str], Any] = {}
        self.mappings: list[dict[str, Any]] | None = None
        #: Folders and machine names the rule form offers; read in the background.
        self.hints: Outcome | None = None
        self.hints_busy = False
        self.action_busy = False
        self.action_kind = ""
        self.result: Outcome | None = None
        self.history: Outcome | None = None
        self.automation: Outcome | None = None
        self.task_busy = False
        self.task_kind = ""
        self.task_result: Outcome | None = None
        #: What this version would change in the config file itself, the diff
        #: it would produce, and which optional findings the user unticked.
        self.migration: Outcome | None = None
        self.migration_busy = False
        self.migration_skip: set[str] = set()
        self.migration_open = False
        self.migration_result: Outcome | None = None

    def reset_plans(self) -> None:
        self.opened = None
        self.draft = {}
        self.mappings = None
        self.history = None
        self.automation = None
        self.hints = None
        self.migration = None
        self.migration_skip = set()
        self.migration_open = False
        self.migration_result = None


class SettingsScreen(Screen):
    page = "settings"

    def build(self) -> None:
        self.banner = Banner()
        self.reload_button = button(self.t("settings.reload"))
        self.reload_button.clicked.connect(self.reload)
        self.banner.actions.addWidget(self.reload_button)
        self.switch_button = button(self.t("settings.config.switch"))
        self.switch_button.clicked.connect(lambda: self.switch_config())
        self.banner.actions.addWidget(self.switch_button)
        self.body.addWidget(self.banner)
        self._build_language_chooser()
        self.migration_card = self._build_migration()
        self.body.addWidget(self.migration_card)

        self._widgets: dict[tuple[str, str], QWidget] = {}
        #: Field id -> the line under a path box showing what it resolves to.
        self._computed: dict[tuple[str, str], QWidget] = {}
        #: Field id -> the substitution help, hidden until "?" is pressed.
        self._help: dict[tuple[str, str], QWidget] = {}
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self._tab_ids: list[str] = []
        for tab_id, fields in TABS:
            if tab_id == "mappings":
                page = self._build_mappings()
            else:
                page = self._build_form(tab_id, fields)
            self.tabs.addTab(page, self.t(f"settings.tab.{tab_id}"))
            self._tab_ids.append(tab_id)
        if self.model.tab in self._tab_ids:
            self.tabs.setCurrentIndex(self._tab_ids.index(self.model.tab))
        self.tabs.currentChanged.connect(self._tab_changed)
        self.body.addWidget(self.tabs, stretch=1)

        self.check_button = button(self.t("settings.check"))
        self.check_button.clicked.connect(lambda: self.check(save=False))
        self.revert_button = button(self.t("settings.revert"))
        self.revert_button.clicked.connect(self.revert)
        self.save_button = button(self.t("settings.save"), primary=True)
        self.save_button.clicked.connect(lambda: self.check(save=True))
        self.changes = label("", "muted")
        actions = row(self.changes, stretch_last=True)
        for widget in (self.check_button, self.revert_button, self.save_button):
            actions.addWidget(widget)
        self.body.addLayout(actions)
        self.status = label("", wrap=True)
        self.body.addWidget(self.status)

    # --- building forms --------------------------------------------------------------

    def _scroll(self, inner: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("content")
        inner.setObjectName("content")
        scroll.setWidget(inner)
        return scroll

    def _build_form(self, tab_id: str, fields: tuple[Field, ...]) -> QWidget:
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(4, 14, 12, 14)
        column.setSpacing(12)
        column.addWidget(label(self.t(f"settings.tab.{tab_id}.caption"), "muted", wrap=True))
        frame, card_layout = card()
        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        for spec in fields:
            widget = self._field_widget(spec)
            self._widgets[spec.id] = widget
            name = f"settings.field.{spec.section}.{spec.key}"
            title = label(self.t(name))
            title.setToolTip(f"[{spec.section}] {spec.key}")
            hint = f"settings.hint.{spec.section}.{spec.key}"
            cell = field(widget, self.t(hint) if self.host.catalog.has(hint) else None)
            reason = f"settings.reason.{spec.section}.{spec.key}"
            if (spec.locked or spec.blocked) and self.host.catalog.has(reason):
                cell = self._with_reason(cell, self.t(reason))
            if spec.kind == "path":
                cell = self._with_path_help(spec, cell)
            elif spec.id == ("targets", "include_roots"):
                cell = self._with_tree_picker(cell)
            form.addRow(title, cell)
        card_layout.addLayout(form)
        if tab_id == "general":
            column.addWidget(frame)
            column.addWidget(self._build_substitutions())
            frame = QWidget()
        column.addWidget(frame)
        if tab_id == "automation":
            column.addWidget(self._build_automation_status())
        if tab_id == "service":
            column.addWidget(self._build_history())
        column.addStretch(1)
        return self._scroll(inner)

    def _with_path_help(self, spec: Field, cell: QWidget) -> QWidget:
        """A path box, what it resolves to, and a "?" that explains the rules."""
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        ask = button("?")
        # The ordinary 20px side padding leaves a 28px button no room for its
        # own label, which is how this rendered as an empty box.
        ask.setObjectName("compact")
        ask.setFixedWidth(36)
        ask.setToolTip(self._substitution_help())
        line = row(cell, ask, stretch_last=False)
        line.setStretch(0, 1)
        column.addLayout(line)
        computed = label("", "muted", wrap=True)
        self._computed[spec.id] = computed
        column.addWidget(computed)
        explanation = label(self._substitution_help(), "muted", wrap=True)
        explanation.setVisible(False)
        self._help[spec.id] = explanation
        ask.clicked.connect(lambda _checked=False, w=explanation: w.setVisible(not w.isVisible()))
        column.addWidget(explanation)
        return holder

    def _with_reason(self, cell: QWidget, text: str) -> QWidget:
        """A field, and one visible line saying why it is the way it is."""
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(cell)
        column.addWidget(label(text, "muted", wrap=True))
        return holder

    def _with_tree_picker(self, cell: QWidget) -> QWidget:
        """The roots box, plus a button that opens the two state directories."""
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(cell)
        pick = button(self.t("settings.roots.choose"))
        pick.clicked.connect(self.choose_roots)
        column.addLayout(row(pick, stretch_last=True))
        return holder

    def choose_roots(self, dialog_factory=None) -> None:
        """Pick `include_roots` from a tree instead of typing them.

        The text box stays the value: whatever the dialog returns is written
        into it and goes through the same edit path as a typed line, so there
        is one place a root can come from.
        """
        widget = self._widgets[("targets", "include_roots")]
        current = [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]

        def children(relative: str):
            outcome = self.host.controller.sync_candidates(relative)
            return outcome.value if outcome.ok and outcome.value else ()

        texts = {
            "hint": self.t("settings.roots.hint"),
            "column.path": self.t("settings.roots.column.path"),
            "column.note": self.t("settings.roots.column.note"),
            "semantic": self.t("settings.roots.semantic"),
            "excluded": self.t("settings.roots.excluded"),
            "cloud_only": self.t("settings.roots.cloud_only"),
            "local_only": self.t("settings.roots.local_only"),
            "separator": self.t("common.separator"),
            "ok": self.t("common.ok"),
            "cancel": self.t("common.cancel"),
        }
        factory = dialog_factory or (lambda: PathTreeDialog(
            self, title=self.t("settings.roots.choose"), list_children=children,
            selected=current, texts=texts,
        ))
        dialog = factory()
        if dialog.exec():
            widget.setPlainText("\n".join(dialog.chosen()))

    def _build_substitutions(self) -> QWidget:
        """The paragraph that says what may be written in a path field."""
        frame, layout = card(self.t("settings.paths.title"))
        self.substitutions = label(self._substitution_help(), wrap=True)
        layout.addWidget(self.substitutions)
        return frame

    def _workspace_text(self) -> str:
        widget = self._widgets.get(("paths", "workspace_root_dir"))
        return widget.text() if widget is not None else ""

    def _substitution_help(self) -> str:
        root = self._workspace_text()
        listed = self.join([
            self.t("settings.paths.substitution", token=token,
                   value=root or self.t("settings.paths.unset"))
            for token in PATH_SUBSTITUTIONS
        ])
        anchor = root or str(self.host.controller.config_path.resolve().parent)
        return self.t("settings.paths.help", substitutions=listed, anchor=anchor)

    def _render_paths(self) -> None:
        """Recompute every path line from what is typed now."""
        base = self.host.controller.config_path.resolve().parent
        workspace = self._workspace_text()
        for spec_id, widget in self._computed.items():
            box = self._widgets.get(spec_id)
            value = box.text() if box is not None else ""
            if not value:
                widget.setText("")
                widget.setVisible(False)
                continue
            outcome = resolve_path_preview(value, base_dir=base, workspace_root=workspace or None)
            if outcome.ok:
                widget.setText(self.t("settings.paths.computed", path=outcome.value))
                set_tone(widget, None, self.palette_)
            else:
                widget.setText(self.failure_text(outcome))
                set_tone(widget, "danger", self.palette_)
            widget.setVisible(True)
        if hasattr(self, "substitutions"):
            self.substitutions.setText(self._substitution_help())
        for widget in self._help.values():
            widget.setText(self._substitution_help())

    def _field_widget(self, field: Field) -> QWidget:
        kind = field.kind
        if kind == "bool":
            widget = QCheckBox()
            widget.toggled.connect(lambda _v, f=field: self._changed(f))
        elif kind == "choice":
            widget = QComboBox()
            for choice in field.choices:
                key = f"settings.choice.{field.section}.{field.key}.{choice}"
                widget.addItem(self.t(key) if self.host.catalog.has(key) else choice, choice)
                if choice in field.blocked:
                    # Visible, and not selectable. The reason is written out
                    # under the field, not hidden in a tooltip.
                    item = widget.model().item(widget.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
            widget.currentIndexChanged.connect(lambda _v, f=field: self._changed(f))
        elif kind == "int":
            widget = QSpinBox()
            widget.setButtonSymbols(QSpinBox.NoButtons)
            widget.setRange(int(field.minimum), int(field.maximum))
            widget.setSingleStep(int(field.step))
            widget.valueChanged.connect(lambda _v, f=field: self._changed(f))
        elif kind == "float":
            widget = QDoubleSpinBox()
            widget.setButtonSymbols(QDoubleSpinBox.NoButtons)
            widget.setRange(field.minimum, field.maximum)
            widget.setSingleStep(field.step)
            widget.setDecimals(2)
            widget.valueChanged.connect(lambda _v, f=field: self._changed(f))
        elif kind == "lines":
            widget = QPlainTextEdit()
            widget.setFixedHeight(96)
            widget.textChanged.connect(lambda f=field: self._changed(f))
        elif kind == "path":
            widget = PathField(self.t("common.browse"))
            widget.edit.textChanged.connect(lambda _v, f=field: self._changed(f))
        else:
            widget = QLineEdit()
            widget.textChanged.connect(lambda _v, f=field: self._changed(f))
        if field.locked:
            widget.setEnabled(False)
        return widget

    def _build_mappings(self) -> QWidget:
        """The rules, and one form to write them with.

        The table only shows. Editing happened in its cells before, where a
        path got a box a few characters wide and the case switch was a combo
        pushed into a cell; a form has room for a path, and room to say what a
        field means and how many chats a rule would reach.
        """
        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(4, 14, 12, 14)
        column.setSpacing(10)
        column.addWidget(label(self.t("settings.tab.mappings.caption"), "muted", wrap=True))
        self.mapping_table = table(
            [self.t(f"settings.mapping.{c}") for c in MAPPING_COLUMNS], stretch=4, selectable=True,
        )
        self.mapping_table.setMinimumHeight(200)
        self.mapping_table.itemSelectionChanged.connect(self._mapping_selected)
        column.addWidget(self.mapping_table, stretch=1)
        column.addWidget(self._build_mapping_form())
        column.addWidget(label(self.t("settings.mapping.example"), "muted", wrap=True))
        return self._scroll(inner)

    def _build_mapping_form(self) -> QWidget:
        frame, card_layout = card(self.t("settings.mapping.form.title"))
        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        known = self._known_machines()

        self.mapping_id = QLineEdit()
        self.mapping_id.setPlaceholderText(self.t("settings.mapping.id.placeholder"))
        self.mapping_from_machine = machine_combo(known, "")
        self.mapping_to_machine = machine_combo(known, self.host.machine_id() or "")
        self.mapping_from = QComboBox()
        self.mapping_from.setEditable(True)
        self.mapping_from.setMinimumWidth(320)
        self.mapping_to = PathField(self.t("common.browse"))
        self.mapping_case = QComboBox()
        for value, key in (("", "auto"), ("true", "yes"), ("false", "no")):
            self.mapping_case.addItem(self.t(f"settings.mapping.case.{key}"), value)

        for key, widget in (
            ("rule_id", self.mapping_id),
            ("source_machine", self.mapping_from_machine),
            ("target_machine", self.mapping_to_machine),
            ("from", self.mapping_from),
            ("to", self.mapping_to),
            ("case_sensitive", self.mapping_case),
        ):
            hint = f"settings.mapping.hint.{key}"
            form.addRow(
                label(self.t(f"settings.mapping.{key}")),
                field(widget, self.t(hint) if self.host.catalog.has(hint) else None),
            )
        card_layout.addLayout(form)

        self.mapping_from.editTextChanged.connect(self._mapping_form_changed)
        self.mapping_reach = label("", "muted", wrap=True)
        card_layout.addWidget(self.mapping_reach)

        self.mapping_add = button(self.t("settings.mapping.add"), primary=True)
        self.mapping_add.clicked.connect(self.add_mapping)
        self.mapping_save = button(self.t("settings.mapping.save"))
        self.mapping_save.clicked.connect(self.save_mapping)
        self.mapping_remove = button(self.t("settings.mapping.remove"))
        self.mapping_remove.clicked.connect(self.remove_mapping)
        card_layout.addLayout(row(self.mapping_add, self.mapping_save, self.mapping_remove))
        return frame

    def _known_machines(self) -> tuple[str, ...]:
        names = list(self.host.known_machines())
        hints = self.model.hints
        if hints is not None and hints.ok and hints.value is not None:
            names.extend(name for name in hints.value.machines if name not in names)
        return tuple(names)

    def _mapping_form_changed(self, *_args: object) -> None:
        """Say how many chats the folder in the form would reach."""
        hints = self.model.hints
        folder = self.mapping_from.currentText().strip()
        if hints is None or not hints.ok or hints.value is None or not folder:
            self.mapping_reach.setText("")
            self.mapping_reach.setVisible(False)
            return
        count = hints.value.chats_under(folder)
        self.mapping_reach.setText(self.p("settings.mapping.reach", count))
        self.mapping_reach.setVisible(True)
        matches = hints.value.local_matches(folder)
        if matches and not self.mapping_to.text():
            self.mapping_to.edit.setPlaceholderText(matches[0])

    def _fill_mapping_suggestions(self) -> None:
        hints = self.model.hints
        if hints is None or not hints.ok or hints.value is None:
            return
        offered = list(hints.value.unmapped_roots)
        offered += [root for root in hints.value.remote_project_roots if root not in offered]
        blocked = self.mapping_from.blockSignals(True)
        try:
            typed = self.mapping_from.currentText()
            for folder in offered:
                if self.mapping_from.findText(folder) < 0:
                    self.mapping_from.addItem(folder)
            if self.mapping_from.currentText() != typed:
                self.mapping_from.setEditText(typed)
        finally:
            self.mapping_from.blockSignals(blocked)

    # --- the rules themselves ------------------------------------------------

    def _entries(self) -> list[dict[str, Any]]:
        model = self.model
        return [dict(item) for item in (
            model.mappings if model.mappings is not None else self._file_mappings()
        )]

    def _form_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "rule_id": self.mapping_id.text().strip(),
            "source_machine": self.mapping_from_machine.currentText().strip(),
            "target_machine": self.mapping_to_machine.currentText().strip(),
            "from": self.mapping_from.currentText().strip(),
            "to": self.mapping_to.text().strip(),
        }
        case = self.mapping_case.currentData()
        if case in ("true", "false"):
            entry["case_sensitive"] = case == "true"
        return entry

    def _load_form(self, entry: dict[str, Any]) -> None:
        self.mapping_id.setText(str(entry.get("rule_id", "")))
        self.mapping_from_machine.setEditText(str(entry.get("source_machine", "")))
        self.mapping_to_machine.setEditText(str(entry.get("target_machine", "")))
        self.mapping_from.setEditText(str(entry.get("from", "")))
        self.mapping_to.setText(str(entry.get("to", "")))
        case = entry.get("case_sensitive")
        self.mapping_case.setCurrentIndex(
            0 if case is None else self.mapping_case.findData("true" if case else "false")
        )

    def selected_mapping(self) -> int:
        rows = {index.row() for index in self.mapping_table.selectedIndexes()}
        return min(rows) if rows else -1

    def _mapping_selected(self) -> None:
        index = self.selected_mapping()
        entries = self._entries()
        if 0 <= index < len(entries):
            self._load_form(entries[index])
        self._render_mapping_buttons()

    def add_mapping(self) -> None:
        entries = self._entries()
        entries.append(self._form_entry())
        self._set_mappings(entries)

    def save_mapping(self) -> None:
        index = self.selected_mapping()
        entries = self._entries()
        if not (0 <= index < len(entries)):
            return
        entries[index] = self._form_entry()
        self._set_mappings(entries)

    def remove_mapping(self) -> None:
        index = self.selected_mapping()
        entries = self._entries()
        if not (0 <= index < len(entries)):
            return
        del entries[index]
        self._set_mappings(entries)

    def _set_mappings(self, entries: list[dict[str, Any]]) -> None:
        self.model.mappings = None if entries == self._file_mappings() else entries
        self.model.result = None
        self._render_changes()
        self.render()

    def _render_mapping_buttons(self) -> None:
        chosen = 0 <= self.selected_mapping() < len(self._entries())
        self.mapping_save.setEnabled(chosen)
        self.mapping_remove.setEnabled(chosen)

    def _build_language_chooser(self) -> None:
        """The language, beside the page title. Kept in QSettings, never in the config."""
        self.language = QComboBox()
        self.language.setToolTip(self.t("settings.language.note"))
        for code in available_languages():
            self.language.addItem(load(code).native_name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(self.host.catalog.language)))
        # Deferred: the rebuild replaces this very combo box, and deleting a
        # widget from inside its own signal is how a Qt window crashes.
        self.language.currentIndexChanged.connect(
            lambda index: QTimer.singleShot(0, lambda: self.host.set_language(self.language.itemData(index)))
        )
        self.heading_actions.addWidget(label(self.t("settings.language.label"), "muted"))
        self.heading_actions.addWidget(self.language)

    def switch_config(self, path: "Path | None" = None) -> None:
        """Point the whole window at another config file that already exists."""
        if path is None:  # pragma: no cover - opens a native dialog
            chosen, _ = QFileDialog.getOpenFileName(
                self, self.t("settings.config.switch"),
                str(self.host.controller.config_path.parent), "TOML (*.toml)",
            )
            if not chosen:
                return
            path = Path(chosen)
        self.host.open_config(Path(path))

    def _build_automation_status(self) -> QWidget:
        frame, layout = card(self.t("automation.task.title"))
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
        self.task_apply = button(self.t("automation.apply"), primary=True)
        self.task_apply.clicked.connect(self.apply_task)
        self.task_remove = button(self.t("automation.remove"))
        self.task_remove.clicked.connect(self.remove_task)
        self.task_refresh = button(self.t("action.refresh"))
        self.task_refresh.clicked.connect(self.refresh_task)
        layout.addLayout(row(self.task_run, self.task_apply, self.task_remove, self.task_refresh))
        self.task_status = label("", wrap=True)
        layout.addWidget(self.task_status)
        layout.addWidget(label(self.t("automation.task.caption"), "muted", wrap=True))
        return frame

    # --- automation ------------------------------------------------------------------

    def refresh_task(self) -> None:
        if self.model.task_busy or not self.host.controller.config_exists():
            return
        self.model.task_busy = True
        self.model.task_kind = "status"
        self.render()

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.task_busy = False
            model.automation = outcome

        self.read(self.host.controller.automation, apply)

    def _task_codes(self) -> tuple[str, ...]:
        outcome = self.model.automation
        if outcome is None or not outcome.ok or outcome.value is None:
            return ()
        status = outcome.value.status
        return status.codes if status is not None else ()

    def apply_task(self) -> None:
        """Save pending edits first (with the usual review), then update the OS task."""
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
        if self.model.draft or self.model.mappings is not None:
            self.check(save=True, then_apply=True)
            return
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
        model.task_busy = True
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

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.task_busy = False
            model.task_result, model.automation = outcome.value

        self.run(go, apply)

    def _build_migration(self) -> QWidget:
        """The card that appears when the config was written by an older version.

        It is not a dialog: the findings, the exact diff and the plan id stay
        on the screen while the user decides, and an optional finding can be
        left alone with its own tick. Nothing here writes -- the button asks
        for confirmation and the write happens in `app.py`.
        """
        self.migration_summary = label(wrap=True)
        frame, layout = card(self.t("settings.migration.title"), self.migration_summary)
        layout.addWidget(label(self.t("settings.migration.caption"), "muted", wrap=True))
        self.migration_findings = QVBoxLayout()
        self.migration_findings.setSpacing(6)
        layout.addLayout(self.migration_findings)
        self.migration_diff = QPlainTextEdit()
        self.migration_diff.setReadOnly(True)
        self.migration_diff.setMinimumHeight(200)
        self.migration_diff.hide()
        layout.addWidget(self.migration_diff)
        self.migration_toggle = button(self.t("settings.migration.show"))
        self.migration_toggle.clicked.connect(self.toggle_migration_diff)
        self.migration_apply = button(self.t("settings.migration.apply"), primary=True)
        self.migration_apply.clicked.connect(self.apply_migration)
        layout.addLayout(row(self.migration_toggle, self.migration_apply, stretch_last=False))
        self.migration_status = label(wrap=True)
        layout.addWidget(self.migration_status)
        frame.hide()
        return frame

    def refresh_migration(self) -> None:
        if self.model.migration_busy or not self.host.controller.config_exists():
            return
        self.model.migration_busy = True
        skip = tuple(sorted(self.model.migration_skip))

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.migration_busy = False
            model.migration = outcome

        self.read(lambda: self.host.controller.config_migration(skip=skip), apply)

    def toggle_migration_diff(self) -> None:
        self.model.migration_open = not self.model.migration_open
        self._render_migration()

    def set_migration_skip(self, code: str, keep: bool) -> None:
        """Tick off an optional finding, then rebuild the diff without it."""
        if keep:
            self.model.migration_skip.add(code)
        else:
            self.model.migration_skip.discard(code)
        self.model.migration = None
        self.refresh_migration()

    def apply_migration(self) -> None:
        model = self.model
        plan = self._migration_plan()
        if plan is None or model.migration_busy:
            return
        if not self.host.confirm(
            self.t("settings.migration.confirm.title"),
            self.t(
                "settings.migration.confirm.body",
                count=len(plan.fixable) - len(model.migration_skip & set(plan.codes())),
                plan_id=plan.plan_id,
            ),
            self.t("settings.migration.apply"),
        ):
            return
        model.migration_busy = True
        model.migration_result = None
        self._render_migration()
        controller = self.host.controller
        plan_id = plan.plan_id
        skip = tuple(sorted(model.migration_skip))

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.migration_busy = False
            model.migration_result = outcome
            model.migration = None
            model.migration_skip = set()

        def go() -> Outcome:
            return controller.apply_config_migration(confirm_plan=plan_id, skip=skip)

        self.run(lambda: go(), apply)

    def _migration_plan(self):
        outcome = self.model.migration
        if outcome is None or not outcome.ok or outcome.value is None:
            return None
        plan, _diff = outcome.value
        return plan

    def _render_migration(self) -> None:
        plan = self._migration_plan()
        diff = ""
        if self.model.migration is not None and self.model.migration.ok and self.model.migration.value:
            _plan, diff = self.model.migration.value
        showing = plan is not None and not plan.is_current
        self.migration_card.setVisible(showing or self.model.migration_result is not None)
        if self.model.migration_result is not None:
            result = self.model.migration_result
            self.migration_status.setText(
                self.t("settings.migration.done") if result.ok else self.failure_text(result)
            )
        else:
            self.migration_status.setText("")
        while self.migration_findings.count():
            item = self.migration_findings.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not showing:
            self.migration_summary.setText("")
            self.migration_diff.hide()
            self.migration_toggle.setEnabled(False)
            self.migration_apply.setEnabled(False)
            return
        self.migration_summary.setText(
            self.p("settings.migration.summary", len(plan.findings))
        )
        for finding in plan.findings:
            self.migration_findings.addWidget(self._finding_row(finding))
        self.migration_toggle.setEnabled(True)
        self.migration_toggle.setText(
            self.t("settings.migration.hide" if self.model.migration_open else "settings.migration.show")
        )
        self.migration_diff.setPlainText(diff)
        self.migration_diff.setVisible(self.model.migration_open and bool(diff))
        self.migration_apply.setEnabled(
            bool(plan.fixable) and not self.model.migration_busy and bool(diff)
        )

    def _finding_row(self, finding) -> QWidget:
        text = label(
            f"{self.t('settings.migration.level.' + finding.level)} — {finding.detail}",
            wrap=True,
        )
        set_tone(text, "danger" if finding.level == "blocker" else None, self.palette_)
        if not finding.optional:
            return text
        keep = QCheckBox(self.t("settings.migration.keep"))
        keep.setChecked(finding.code in self.model.migration_skip)
        code = finding.code
        keep.toggled.connect(lambda checked, code=code: self.set_migration_skip(code, checked))
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(2)
        inner.addWidget(text)
        inner.addWidget(keep)
        return holder

    def _build_history(self) -> QWidget:
        self.history_summary = label()
        frame, layout = card(self.t("settings.history.title"), self.history_summary)
        layout.addWidget(label(self.t("settings.history.caption"), "muted", wrap=True))
        self.history = table([self.t("settings.history.column.created"), self.t("settings.history.column.size"), self.t("settings.history.column.path")])
        self.history.setMinimumHeight(160)
        layout.addWidget(self.history)
        return frame

    # --- data flow -----------------------------------------------------------------

    def activated(self) -> None:
        exists = self.host.controller.config_exists()
        if self.model.opened is None and not self.model.busy and exists:
            self.reload(keep_result=True)
        if self.model.automation is None and not self.model.task_busy and exists:
            self.refresh_task()
        if self.model.migration is None and not self.model.migration_busy and exists:
            self.refresh_migration()
        if self.model.tab == "mappings":
            self.load_mapping_hints()

    def load_mapping_hints(self) -> None:
        """Read this machine's chat folders, to offer them in the rule form.

        Only when the rules tab is actually open: this is the same scan the
        chats screen runs, thirteen seconds on a real state, and paying it to
        draw a tab nobody looked at would make every visit to the settings
        slow for one list of suggestions.

        A read like every other one -- it reports progress and may be
        abandoned. The form works without it; the suggestions are simply
        absent until it lands.
        """
        if self.model.hints is not None or self.model.hints_busy:
            return
        if not self.host.controller.config_exists():
            return
        self.model.hints_busy = True

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.hints_busy = False
            model.hints = outcome

        self.read(
            lambda progress=None: self.host.controller.mapping_hints(progress=progress),
            apply, progress=True,
        )

    def reload(self, *, keep_result: bool = False) -> None:
        model = self.model
        if model.busy:
            return
        model.busy = True
        model.draft = {}
        model.mappings = None
        if not keep_result:
            model.result = None
        self.render()
        controller = self.host.controller

        def go() -> Outcome:
            opened = controller.open_config()
            history = controller.config_history() if opened.ok else None
            return Outcome(value=(opened, history))

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.busy = False
            if not outcome.ok:
                model.opened = outcome
                return
            model.opened, model.history = outcome.value

        self.read(go, apply)

    def _tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._tab_ids):
            self.model.tab = self._tab_ids[index]
        if self.model.tab == "mappings":
            self.load_mapping_hints()

    def _raw(self) -> dict[str, Any] | None:
        opened = self.model.opened
        return opened.value.raw if opened is not None and opened.ok else None

    def _file_value(self, field: Field) -> Any:
        raw = self._raw() or {}
        value = raw_value(raw, field.section, field.key)
        return field.default if value is None else value

    def _widget_value(self, field: Field) -> Any:
        widget = self._widgets[field.id]
        if field.kind == "bool":
            return widget.isChecked()
        if field.kind == "choice":
            return widget.currentData()
        if field.kind == "int":
            return int(widget.value())
        if field.kind == "float":
            return float(widget.value())
        if field.kind == "lines":
            return [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]
        return widget.text().strip() if field.kind == "path" else widget.text()

    def _set_widget(self, field: Field, value: Any) -> None:
        widget = self._widgets[field.id]
        widget.blockSignals(True)
        if field.kind == "path":
            widget.edit.blockSignals(True)
        try:
            if field.kind == "bool":
                widget.setChecked(bool(value))
            elif field.kind == "choice":
                index = widget.findData(value)
                if index < 0 and value is not None:
                    widget.addItem(str(value), value)
                    index = widget.findData(value)
                widget.setCurrentIndex(max(0, index))
            elif field.kind == "int":
                widget.setValue(int(value or 0))
            elif field.kind == "float":
                widget.setValue(float(value or 0))
            elif field.kind == "lines":
                widget.setPlainText("\n".join(str(item) for item in (value or [])))
            elif field.kind == "path":
                widget.setText("" if value is None else str(value))
            else:
                widget.setText("" if value is None else str(value))
        finally:
            widget.blockSignals(False)
            if field.kind == "path":
                widget.edit.blockSignals(False)

    def _changed(self, field: Field) -> None:
        if self._raw() is None:
            return
        value = self._widget_value(field)
        if _same(value, self._file_value(field)):
            self.model.draft.pop(field.id, None)
        else:
            self.model.draft[field.id] = value
        self.model.result = None
        self._render_changes()
        # A path, or the workspace root every other path may be written
        # against, just changed: the computed lines have to follow the text.
        if field.kind == "path" or field.id == ("paths", "workspace_root_dir"):
            self._render_paths()

    def _file_mappings(self) -> list[dict[str, Any]]:
        raw = self._raw() or {}
        value = raw.get("path_mappings", [])
        return [dict(item) for item in value] if isinstance(value, list) else []

    def edits(self) -> ConfigEdits:
        values: dict[tuple[str, str], Any] = dict(self.model.draft)
        raw = self._raw() or {}
        if any(section == "scheduler" for section, _ in values):
            scheduler = raw.get("scheduler", {}) if isinstance(raw.get("scheduler"), dict) else {}
            for key in LEGACY_SCHEDULER_KEYS:
                if key in scheduler:
                    values[("scheduler", key)] = REMOVE
        return ConfigEdits(values=values, mappings=self.model.mappings)

    def revert(self) -> None:
        self.model.draft = {}
        self.model.mappings = None
        self.model.result = None
        self.render()

    def check(self, *, save: bool, then_apply: bool = False) -> None:
        model = self.model
        opened = model.opened.value if model.opened is not None and model.opened.ok else None
        if opened is None or model.action_busy:
            return
        edits = self.edits()
        if not edits.values and edits.mappings is None:
            return
        model.action_busy = True
        model.action_kind = "check"
        model.result = None
        self.render()
        controller = self.host.controller
        base = opened.document

        def go() -> Outcome:
            rendered = controller.render_config(base.text, edits)
            if not rendered.ok:
                return rendered
            validated = controller.validate_config(rendered.value)
            if not validated.ok:
                return validated
            return Outcome(value=rendered.value)

        host = self.host
        page = self

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.result = outcome
            if outcome.ok and save:
                # Confirmation happens on the UI thread, after the text is proven valid.
                QTimer.singleShot(0, lambda: page._confirm_save(host, base, outcome.value, then_apply))

        self.read(go, apply)

    def _confirm_save(self, host, base, text: str, then_apply: bool = False) -> None:
        diff = host.controller.config_diff(base.text, text)
        if not host.confirm(
            self.t("settings.confirm.title"),
            self.t("settings.confirm.body", path=host.controller.config_path),
            self.t("settings.save"),
            details=diff,
        ):
            return
        model = self.model
        model.action_busy = True
        model.action_kind = "save"
        self.render()
        controller = host.controller

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.result = outcome
            if outcome.ok:
                model.action_kind = "saved"
                saved = outcome
                host.config_changed()
                model.result = saved
                if then_apply:
                    host.screen("settings")._start_task("apply")

        self.run(lambda: controller.save_config(text, expected_sha256=base.sha256), apply)

    # --- drawing ---------------------------------------------------------------------

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.reload_button.setEnabled(not model.busy)
        self._render_migration()
        self._render_paths()
        opened = model.opened
        if not self.host.controller.config_exists():
            self.banner.show_message("attention", self.t("settings.no_config.title"), self.t("settings.no_config.detail"), palette)
        elif model.busy:
            self.banner.show_message("neutral", self.t("settings.loading"), "", palette)
        elif opened is None:
            self.banner.show_message("neutral", "", "", palette)
        elif not opened.ok:
            self.banner.show_message("danger", self.headline(opened.failure), opened.message, palette)
        else:
            self.banner.show_message(
                "neutral", self.t("settings.file", path=opened.value.document.path.resolve()),
                self.t("settings.file.detail"), palette,
            )

        raw = self._raw()
        for tab_id, fields in TABS:
            for field in fields:
                value = model.draft.get(field.id, self._file_value(field) if raw is not None else field.default)
                self._set_widget(field, value)
                if not field.locked:
                    self._widgets[field.id].setEnabled(raw is not None)
        fill_table(
            self.mapping_table,
            [
                [Cell(_mapping_cell(entry, column)) for column in MAPPING_COLUMNS]
                for entry in self._entries()
            ],
            palette,
        )
        self.mapping_table.setEnabled(raw is not None)
        self._fill_mapping_suggestions()
        self._mapping_form_changed()
        self._render_mapping_buttons()
        self._render_history()
        self._render_task()
        self._render_changes()

    def _render_history(self) -> None:
        outcome = self.model.history
        if outcome is None or not outcome.ok:
            self.history.setRowCount(0)
            self.history_summary.setText("")
            return
        rows = [
            [Cell(entry.created_utc.strftime("%Y-%m-%d %H:%M:%S UTC")), Cell(f"{entry.size} B"), Cell(str(entry.path), muted=True)]
            for entry in outcome.value
        ]
        fill_table(self.history, rows, self.palette_)
        self.history_summary.setText(self.p("settings.history.count", len(outcome.value)))

    def _render_task(self) -> None:
        model = self.model
        palette = self.palette_
        values = {field.key: model.draft.get(field.id, self._file_value(field)) for field in TABS[3][1]}
        mode = values.get("mode")
        mode_key = f"settings.choice.scheduler.mode.{mode}"
        if not values.get("enabled"):
            self.task_state.setText(self.t("automation.summary.disabled"))
        else:
            login_key = "automation.summary.login" if values.get("run_at_login") else "automation.summary.no_login"
            self.task_state.setText(self.t(
                "automation.summary.enabled",
                mode=self.t(mode_key) if self.host.catalog.has(mode_key) else mode,
                every=self._interval(int(values.get("interval_seconds") or 60)),
                login=self.t(login_key),
            ))

        outcome = model.automation
        view = outcome.value if outcome is not None and outcome.ok else None
        exists = self.host.controller.config_exists()
        busy = model.task_busy
        self.task_refresh.setEnabled(exists and not busy)
        self.task_run.setEnabled(exists and not busy)
        self.task_apply.setEnabled(exists and not busy)
        installed = view is not None and view.status is not None and view.status.installed
        self.task_remove.setEnabled(exists and not busy and installed)

        if outcome is not None and not outcome.ok:
            self.task_banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
            self.task_details.setText("")
            self.task_command.setVisible(False)
        elif view is None:
            self.task_banner.show_message("neutral", self.t("automation.loading") if busy else "", "", palette)
            self.task_details.setText("")
            self.task_command.setVisible(False)
        else:
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
                lines.append(self.t("automation.last_run", when=_when_or_never(status.last_run_utc, self.t("automation.never"))))
                lines.append(self.t("automation.next_run", when=_when_or_never(status.next_run_utc, self.t("automation.unknown"))))
                if status.last_result is not None:
                    result_key = f"automation.exit.{status.last_result}"
                    meaning = self.t(result_key) if self.host.catalog.has(result_key) else self.t("automation.exit.other")
                    lines.append(self.t("automation.last_result", code=status.last_result, meaning=meaning))
                if not view.reports_run_times:
                    lines.append(self.t("automation.no_run_times"))
            if status is not None and status.owner and status.owned_by_me is False:
                lines.append(self.t("automation.task.owner", owner=status.owner))
            if status is not None and any(code in BROKEN_TASK_CODES for code in status.codes):
                lines.append(self.t(
                    "automation.task.runs",
                    command=" ".join(_quote(part) for part in (status.installed_command or ())),
                ))
            if view.ignored:
                names = ", ".join(self.t(f"settings.field.scheduler.{name}") for name in view.ignored)
                lines.append(self.t("automation.ignored", settings=names))
            self.task_details.setText("\n".join(lines))
            self.task_command.setText(" ".join(_quote(part) for part in view.argv))
            self.task_command.setVisible(True)

        text, tone = "", None
        if busy and model.task_kind != "status":
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

    def _interval(self, seconds: int) -> str:
        if seconds % 3600 == 0:
            return self.p("common.hours", seconds // 3600)
        if seconds % 60 == 0:
            return self.p("common.minutes", seconds // 60)
        return self.p("common.seconds", seconds)

    def _render_changes(self) -> None:
        model = self.model
        palette = self.palette_
        count = len(model.draft) + (1 if model.mappings is not None else 0)
        opened = model.opened is not None and model.opened.ok
        busy = model.busy or model.action_busy
        self.changes.setText(self.p("settings.changes", count) if opened else "")
        self.check_button.setEnabled(opened and count > 0 and not busy)
        self.revert_button.setEnabled(opened and count > 0 and not busy)
        self.save_button.setEnabled(opened and count > 0 and not busy)

        text, tone = "", None
        if model.action_busy:
            text = self.t("settings.saving" if model.action_kind == "save" else "settings.checking")
        elif model.result is not None:
            if not model.result.ok:
                text, tone = self.failure_text(model.result), "danger"
            elif model.action_kind == "saved":
                saved = model.result.value
                text = self.t("settings.saved", path=saved.path)
                if saved.history_entry is not None:
                    text += " " + self.t("settings.saved.history", path=saved.history_entry)
                tone = "ok"
            else:
                text, tone = self.t("settings.valid"), "ok"
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))


def _same(first: Any, second: Any) -> bool:
    if isinstance(first, float) or isinstance(second, float):
        try:
            return abs(float(first) - float(second)) < 1e-9
        except (TypeError, ValueError):
            return False
    if isinstance(first, list) or isinstance(second, list):
        return list(first or []) == list(second or [])
    return first == second


def _when_or_never(iso: str | None, fallback: str) -> str:
    if not iso:
        return fallback
    return iso[:16].replace("T", " ") + " UTC"


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part
