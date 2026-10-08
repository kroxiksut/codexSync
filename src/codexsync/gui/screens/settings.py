"""Settings: `config.toml`, edited in place, and the interface language.

`config.toml` is the only source of truth, and the screen behaves like it. The
file is read into a draft; nothing on screen is a second copy of a setting. A
change becomes an edit only when it differs from what the file says, so saving
one field rewrites that one value and leaves every comment and every other
line exactly as the user wrote it. Before anything is written the edited text
goes through the same loader the command line uses, the difference is shown,
and the save refuses if the file changed on disk since it was opened. The
replaced version goes into the config history first, and every open plan in
the window is discarded afterwards. That behaviour is `ConfigFormScreen`'s,
shared with the Automation page, which edits `[scheduler]` and
`[state_backup]` the same way (CS-276).

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

from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QFrame,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..controller import (
    Outcome,
    default_background_process_names,
    default_process_names,
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
from .config_form import ConfigFormModel, ConfigFormScreen, Field, raw_value

__all__ = ["Field", "SettingsModel", "SettingsScreen", "TABS", "raw_value"]


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
        # First on the tab: it decides whether a chat started on the other
        # machine reaches this one at all, and messages elsewhere point here.
        Field("semantic", "new_chats", "choice", "same_path", ("same_path", "keep_in_cloud")),
        Field("sync", "compare", "choice", "mtime", ("mtime", "mtime_hash_fallback")),
        Field("sync", "time_tolerance_seconds", "int", 0, maximum=3600),
        Field("sync", "equal_mtime_action", "choice", "skip", ("skip", "prefer_local", "prefer_cloud", "manual_abort")),
        Field("conflict", "policy", "choice", "prefer_newer_mtime", ("prefer_newer_mtime", "prefer_local", "prefer_cloud", "manual_abort")),
        Field("conflict", "report_conflicts", "bool", True),
        Field("sync", "dry_run_default", "bool", True),
        Field("targets", "include_roots", "lines", []),
        Field("filters", "exclude_globs", "lines", []),
        Field("sync", "direction", "choice", "bidirectional", ("bidirectional", "to_cloud", "to_local")),
        Field("sync", "scope", "choice", "full", ("full", "settings")),
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
    ("mappings", ()),
    ("service", (
        # Shown so every key of the template has a place in the window, and
        # locked like `safety.*`: a sync that overwrites without a backup
        # would leave `recover` nothing to put back.
        Field("backup", "backup_before_overwrite", "bool", True, locked=True),
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

#: The tab area never gets less than this, whatever the cards above it hold.
TABS_MIN_HEIGHT = 380
#: An unfolded migration card scrolls inside this height instead of growing.
MIGRATION_DETAILS_MIN_HEIGHT = 220
MIGRATION_DETAILS_MAX_HEIGHT = 320
MAPPING_COLUMNS = ("rule_id", "source_machine", "target_machine", "from", "to", "case_sensitive")


def _mapping_cell(entry: dict[str, Any], column: str) -> str:
    """One rule's column, as text. `case_sensitive` is a tri-state, not a bool."""
    value = entry.get(column)
    if column == "case_sensitive":
        return "" if value is None else ("true" if value else "false")
    return "" if value is None else str(value)


class SettingsModel(ConfigFormModel):
    def __init__(self) -> None:
        super().__init__()
        self.tab = "general"
        #: Folders and machine names the rule form offers; read in the background.
        self.hints: Outcome | None = None
        self.hints_busy = False
        self.history: Outcome | None = None
        #: What this version would change in the config file itself, the diff
        #: it would produce, and which optional findings the user unticked.
        self.migration: Outcome | None = None
        self.migration_busy = False
        self.migration_skip: set[str] = set()
        self.migration_open = False
        #: Findings shown or folded away; ``None`` follows the plan -- open
        #: when something blocks every write, folded otherwise.
        self.migration_details: bool | None = None
        self.migration_result: Outcome | None = None

    def reset_plans(self) -> None:
        super().reset_plans()
        self.history = None
        self.hints = None
        self.migration = None
        self.migration_skip = set()
        self.migration_open = False
        self.migration_details = None
        self.migration_result = None


class SettingsScreen(ConfigFormScreen):
    page = "settings"
    #: The page scrolls as a whole so that nothing above the tabs -- the
    #: migration card above all -- can squeeze them to nothing: on a small
    #: window the page scrolls instead of every label drawing over the next.
    scrollable = True
    scroll_tail_stretch = False

    def form_fields(self):
        for _tab_id, fields in TABS:
            yield from fields

    def build(self) -> None:
        self.init_form()
        self.banner = Banner(actions_below=True)
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
        # Enough for a few fields whatever sits above; the page scrolls past it.
        self.tabs.setMinimumHeight(TABS_MIN_HEIGHT)
        self.body.addWidget(self.tabs, stretch=1)
        self.build_form_actions()

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
        frame, _card_layout = self.form_card(fields)
        column.addWidget(frame)
        if tab_id == "general":
            column.addWidget(self._build_substitutions())
        if tab_id == "service":
            column.addWidget(self._build_history())
        column.addStretch(1)
        return self._scroll(inner)

    def decorate_cell(self, spec: Field, cell: QWidget) -> QWidget:
        if spec.id == ("targets", "include_roots"):
            return self._with_tree_picker(cell)
        return super().decorate_cell(spec, cell)

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

    def _build_migration(self) -> QWidget:
        """The card that appears when the config was written by an older version.

        It is not a dialog: the findings, the exact diff and the plan id stay
        on the screen while the user decides, and an optional finding can be
        left alone with its own tick. Nothing here writes -- the button asks
        for confirmation and the write happens in `app.py`.
        """
        frame, layout = card(self.t("settings.migration.title"))
        self.migration_summary = label(wrap=True)
        layout.addWidget(self.migration_summary)
        # What the card says when unfolded. It scrolls inside a bounded height:
        # a long process list or an open diff must not push the tabs away.
        details = QWidget()
        details.setObjectName("content")
        inner = QVBoxLayout(details)
        inner.setContentsMargins(0, 0, 8, 0)
        inner.setSpacing(8)
        inner.addWidget(label(self.t("settings.migration.caption"), "muted", wrap=True))
        self.migration_findings = QVBoxLayout()
        self.migration_findings.setSpacing(6)
        inner.addLayout(self.migration_findings)
        self.migration_toggle = button(self.t("settings.migration.show"))
        self.migration_toggle.clicked.connect(self.toggle_migration_diff)
        inner.addLayout(row(self.migration_toggle))
        self.migration_diff = QPlainTextEdit()
        self.migration_diff.setReadOnly(True)
        self.migration_diff.setMinimumHeight(200)
        self.migration_diff.hide()
        inner.addWidget(self.migration_diff)
        self.migration_details_box = self._scroll(details)
        # Part of the card, not a page of its own: no grey page ground.
        for part in (self.migration_details_box, self.migration_details_box.viewport(), details):
            part.setObjectName("migrationDetails")
        self.migration_details_box.setStyleSheet("#migrationDetails { background: transparent; }")
        self.migration_details_box.setMinimumHeight(MIGRATION_DETAILS_MIN_HEIGHT)
        self.migration_details_box.setMaximumHeight(MIGRATION_DETAILS_MAX_HEIGHT)
        layout.addWidget(self.migration_details_box)
        self.migration_more = button(self.t("settings.migration.details"))
        self.migration_more.clicked.connect(self.toggle_migration_details)
        self.migration_apply = button(self.t("settings.migration.apply"), primary=True)
        self.migration_apply.clicked.connect(self.apply_migration)
        layout.addLayout(row(self.migration_more, self.migration_apply))
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

    def toggle_migration_details(self) -> None:
        self.model.migration_details = not self._migration_details_open()
        self._render_migration()

    def _migration_details_open(self) -> bool:
        if self.model.migration_details is not None:
            return self.model.migration_details
        plan = self._migration_plan()
        return plan is not None and bool(plan.blockers)

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
        host = self.host
        plan_id = plan.plan_id
        skip = tuple(sorted(model.migration_skip))

        def apply(model: SettingsModel, outcome: Outcome) -> None:
            model.migration_busy = False
            model.migration = None
            model.migration_skip = set()
            if outcome.ok:
                # The file changed under every page, exactly as after a save:
                # without this the form keeps the old values and the next
                # save is refused as "changed on disk" (CS-307).
                host.config_changed()
            model.migration_result = outcome

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
        self.migration_status.setVisible(bool(self.migration_status.text()))
        while self.migration_findings.count():
            item = self.migration_findings.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Out of the layout is not off the screen: until the deferred
                # delete runs, the old row keeps drawing under the new one.
                widget.hide()
                widget.deleteLater()
        if not showing:
            self.migration_summary.setText("")
            self.migration_diff.hide()
            self.migration_details_box.hide()
            self.migration_more.setVisible(False)
            self.migration_toggle.setVisible(False)
            self.migration_apply.setVisible(False)
            return
        self.migration_summary.setText(
            self.p("settings.migration.summary", len(plan.findings))
        )
        set_tone(self.migration_summary, "danger" if plan.blockers else None, self.palette_)
        unfolded = self._migration_details_open()
        self.migration_details_box.setVisible(unfolded)
        self.migration_more.setVisible(True)
        self.migration_more.setText(
            self.t("settings.migration.collapse" if unfolded else "settings.migration.details")
        )
        for finding in plan.findings:
            self.migration_findings.addWidget(self._finding_row(finding))
        # Nothing to write (only notes, or every change kept as it is) means
        # nothing to show or apply: a greyed-out button promised an action
        # that did not exist.
        writable = bool(plan.fixable) and bool(diff)
        self.migration_toggle.setVisible(writable)
        self.migration_toggle.setText(
            self.t("settings.migration.hide" if self.model.migration_open else "settings.migration.show")
        )
        self.migration_diff.setPlainText(diff)
        self.migration_diff.setVisible(writable and self.model.migration_open)
        self.migration_apply.setVisible(writable)
        self.migration_apply.setEnabled(writable and not self.model.migration_busy)

    def _finding_row(self, finding) -> QWidget:
        text = label(
            f"{self.t('settings.migration.level.' + finding.level)} — {self._finding_text(finding)}",
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

    def _finding_text(self, finding) -> str:
        """The finding in the window's language; core's English for a code it does not know."""
        key = f"settings.migration.finding.{finding.code}"
        if not self.host.catalog.has(key):
            return finding.detail
        try:
            return self.t(key, **dict(finding.params))
        except (KeyError, IndexError):
            # A finding without the values its sentence names: say it as core does.
            return finding.detail

    def _build_history(self) -> QWidget:
        self.history_summary = label()
        frame, layout = card(self.t("settings.history.title"), self.history_summary)
        layout.addWidget(label(self.t("settings.history.caption"), "muted", wrap=True))
        self.history = table([self.t("settings.history.column.created"), self.t("settings.history.column.size"), self.t("settings.history.column.path")])
        self.history.setMinimumHeight(160)
        layout.addWidget(self.history)
        return frame

    def _render_paths(self) -> None:
        super()._render_paths()
        if hasattr(self, "substitutions"):
            self.substitutions.setText(self._substitution_help())

    # --- data flow -----------------------------------------------------------------

    def activated(self) -> None:
        exists = self.host.controller.config_exists()
        if self.model.opened is None and not self.model.busy and exists:
            self.reload(keep_result=True)
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

    def read_alongside(self, controller) -> Any:
        return controller.config_history()

    def apply_alongside(self, model: SettingsModel, value: Any) -> None:
        model.history = value

    def reveal(self, target: str) -> None:
        """A tab by its id, or a field: its tab first, then the field itself."""
        tab = target if target in self._tab_ids else next(
            (tab_id for tab_id, fields in TABS if any(f"{spec.section}.{spec.key}" == target for spec in fields)),
            None,
        )
        if tab is None:
            return
        self.tabs.setCurrentIndex(self._tab_ids.index(tab))
        if tab != target:
            super().reveal(target)

    def _tab_changed(self, index: int) -> None:
        if 0 <= index < len(self._tab_ids):
            self.model.tab = self._tab_ids[index]
        if self.model.tab == "mappings":
            self.load_mapping_hints()

    def _file_mappings(self) -> list[dict[str, Any]]:
        return _mappings_in(self._raw() or {})

    def file_mappings_of(self, model) -> list[dict[str, Any]] | None:
        opened = model.opened
        return _mappings_in(opened.value.raw) if opened is not None and opened.ok else None

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
        self.render_fields()
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


def _mappings_in(raw: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw.get("path_mappings", [])
    return [dict(item) for item in value] if isinstance(value, list) else []
