"""A page that edits part of `config.toml` in place.

Settings and Automation both edit the same file, and both must behave the way
`config.toml` being the only source of truth demands: the file is read into a
draft, a change becomes an edit only when it differs from what the file says,
the edited text goes through the loader the command line uses before anything
is written, the difference is shown, and the save refuses if the file changed
on disk since it was opened. That behaviour lives here once, so the two pages
cannot drift apart in how carefully they write.

A page lists its fields (`Field`) and draws them with `form_card`; everything
from the draft to the confirmation dialog is this class.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..controller import PATH_SUBSTITUTIONS, ConfigEdits, Outcome, resolve_path_preview
from ..widgets import PathField, button, card, field, label, row, set_tone
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
    #: For an ``int`` field: the file stores ``shown * scale``. The period of
    #: the scheduled job is kept in seconds and edited in minutes; bounds,
    #: step and default are in the stored unit.
    scale: int = 1

    @property
    def id(self) -> tuple[str, str]:
        return (self.section, self.key)


def raw_value(raw: dict[str, Any], section: str, key: str) -> Any:
    node: Any = raw
    for part in section.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node.get(key) if isinstance(node, dict) else None


def same_value(first: Any, second: Any) -> bool:
    if isinstance(first, float) or isinstance(second, float):
        try:
            return abs(float(first) - float(second)) < 1e-9
        except (TypeError, ValueError):
            return False
    if isinstance(first, list) or isinstance(second, list):
        return list(first or []) == list(second or [])
    return first == second


class ConfigFormModel(Model):
    """The opened file, the draft typed over it, and what the last check said."""

    def __init__(self) -> None:
        self.opened: Outcome | None = None
        self.busy = False
        #: Field id -> value typed on screen, kept across language changes.
        self.draft: dict[tuple[str, str], Any] = {}
        #: Whole `[[path_mappings]]` replacement; only Settings edits it.
        self.mappings: list[dict[str, Any]] | None = None
        self.action_busy = False
        self.action_kind = ""
        self.result: Outcome | None = None

    def reset_plans(self) -> None:
        # The draft survives: Settings and Automation edit one file, and a save
        # on one of them must not silently throw away what was typed on the
        # other. The next read rebases it on the new file (`keep_pending`).
        self.opened = None

    def keep_pending(self, fields: Iterable[Field], file_mappings: list[dict[str, Any]] | None) -> None:
        """Drop every draft value the file now says anyway."""
        raw = self.opened.value.raw if self.opened is not None and self.opened.ok else None
        if raw is None:
            return
        for spec in fields:
            if spec.id not in self.draft:
                continue
            value = raw_value(raw, spec.section, spec.key)
            if same_value(self.draft[spec.id], spec.default if value is None else value):
                del self.draft[spec.id]
        if self.mappings is not None and self.mappings == file_mappings:
            self.mappings = None


class ConfigFormScreen(Screen):
    """What every config-editing page shares. A subclass lists its fields."""

    def form_fields(self) -> Iterable[Field]:
        raise NotImplementedError

    # --- building ------------------------------------------------------------

    def init_form(self) -> None:
        """Call first in `build`: the registries the helpers below fill."""
        self._widgets: dict[tuple[str, str], QWidget] = {}
        #: Field id -> the line under a path box showing what it resolves to.
        self._computed: dict[tuple[str, str], QWidget] = {}
        #: Field id -> the substitution help, hidden until "?" is pressed.
        self._help: dict[tuple[str, str], QWidget] = {}

    def form_card(self, fields: Iterable[Field], title: str | None = None) -> tuple[QWidget, QVBoxLayout]:
        """A card holding one row per field, labelled from the catalogue."""
        frame, card_layout = card(title) if title else card()
        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(8)
        for spec in fields:
            widget = self._field_widget(spec)
            self._widgets[spec.id] = widget
            name = f"settings.field.{spec.section}.{spec.key}"
            title_label = label(self.t(name))
            title_label.setToolTip(f"[{spec.section}] {spec.key}")
            hint = f"settings.hint.{spec.section}.{spec.key}"
            cell = field(widget, self.t(hint) if self.host.catalog.has(hint) else None)
            reason = f"settings.reason.{spec.section}.{spec.key}"
            if (spec.locked or spec.blocked) and self.host.catalog.has(reason):
                cell = self._with_reason(cell, self.t(reason))
            cell = self.decorate_cell(spec, cell)
            form.addRow(title_label, cell)
        card_layout.addLayout(form)
        return frame, card_layout

    def field_widget(self, target: str) -> QWidget | None:
        """The box of a ``section.key`` field, if this page has it."""
        section, _, key = target.rpartition(".")
        return self._widgets.get((section, key))

    def reveal(self, target: str) -> None:
        widget = self.field_widget(target)
        if widget is not None:
            self.show_widget(widget)

    def decorate_cell(self, spec: Field, cell: QWidget) -> QWidget:
        """Add what a kind of field needs around its box; paths get their help."""
        if spec.kind == "path":
            return self._with_path_help(spec, cell)
        return cell

    def build_form_actions(self) -> None:
        """Check, revert and save, with the count of pending edits and the result."""
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
            widget.setRange(int(field.minimum) // field.scale, int(field.maximum) // field.scale)
            widget.setSingleStep(max(1, int(field.step) // field.scale))
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

    # --- paths ------------------------------------------------------------------

    def _workspace_text(self) -> str:
        """The workspace root as typed, or as the file says when this page has no box for it."""
        widget = self._widgets.get(("paths", "workspace_root_dir"))
        if widget is not None:
            return widget.text()
        value = raw_value(self._raw() or {}, "paths", "workspace_root_dir")
        return str(value) if isinstance(value, str) else ""

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
        for widget in self._help.values():
            widget.setText(self._substitution_help())

    # --- the draft ----------------------------------------------------------------

    def _raw(self) -> dict[str, Any] | None:
        opened = self.model.opened
        return opened.value.raw if opened is not None and opened.ok else None

    def _file_value(self, field: Field) -> Any:
        raw = self._raw() or {}
        value = raw_value(raw, field.section, field.key)
        return field.default if value is None else value

    def draft_value(self, field: Field) -> Any:
        """What the field says now: the typed value, else the file's."""
        return self.model.draft.get(field.id, self._file_value(field))

    def _widget_value(self, field: Field) -> Any:
        widget = self._widgets[field.id]
        if field.kind == "bool":
            return widget.isChecked()
        if field.kind == "choice":
            return widget.currentData()
        if field.kind == "int":
            return int(widget.value()) * field.scale
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
                widget.setValue(int(value or 0) // field.scale)
            elif field.kind == "float":
                widget.setValue(float(value or 0))
            elif field.kind == "lines":
                widget.setPlainText("\n".join(str(item) for item in (value or [])))
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
        if same_value(value, self._file_value(field)):
            self.model.draft.pop(field.id, None)
        else:
            self.model.draft[field.id] = value
        self.model.result = None
        self._render_changes()
        # A path, or the workspace root every other path may be written
        # against, just changed: the computed lines have to follow the text.
        if field.kind == "path" or field.id == ("paths", "workspace_root_dir"):
            self._render_paths()
        self.field_changed(field)

    def field_changed(self, field: Field) -> None:
        """A field's draft value changed; a page that describes the draft redraws."""

    def edit_values(self) -> dict[tuple[str, str], Any]:
        return dict(self.model.draft)

    def edits(self) -> ConfigEdits:
        return ConfigEdits(values=self.edit_values(), mappings=self.model.mappings)

    def pending_count(self) -> int:
        return len(self.model.draft) + (1 if self.model.mappings is not None else 0)

    def revert(self) -> None:
        self.model.draft = {}
        self.model.mappings = None
        self.model.result = None
        self.render()

    # --- reading and writing the file ---------------------------------------------

    def reload(self, *, keep_result: bool = False) -> None:
        model = self.model
        if model.busy:
            return
        model.busy = True
        if not keep_result:
            model.result = None
        self.render()
        controller = self.host.controller

        def go() -> Outcome:
            opened = controller.open_config()
            return Outcome(value=(opened, self.read_alongside(controller) if opened.ok else None))

        def apply(model: ConfigFormModel, outcome: Outcome) -> None:
            model.busy = False
            if not outcome.ok:
                model.opened = outcome
                return
            model.opened, extra = outcome.value
            model.keep_pending(self.form_fields(), self.file_mappings_of(model))
            self.apply_alongside(model, extra)

        self.read(go, apply)

    def file_mappings_of(self, model: ConfigFormModel) -> list[dict[str, Any]] | None:
        """`[[path_mappings]]` as the file says; only a page that edits them answers."""
        return None

    def read_alongside(self, controller) -> Any:
        """Read on the worker thread together with the file; nothing by default."""
        return None

    def apply_alongside(self, model: ConfigFormModel, value: Any) -> None:
        """Keep what `read_alongside` returned."""

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

        def apply(model: ConfigFormModel, outcome: Outcome) -> None:
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
        page_id = self.page

        def apply(model: ConfigFormModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.result = outcome
            if outcome.ok:
                model.action_kind = "saved"
                saved = outcome
                # What was typed here is now the file; another page's draft
                # is kept and rebased when that page reads the file again.
                model.draft = {}
                model.mappings = None
                host.config_changed()
                model.result = saved
                if then_apply:
                    host.screen(page_id).after_save_apply()

        self.run(lambda: controller.save_config(text, expected_sha256=base.sha256), apply, kind="config_save")

    def after_save_apply(self) -> None:
        """What `check(save=True, then_apply=True)` continues with once saved."""

    # --- drawing -------------------------------------------------------------------

    def render_fields(self) -> None:
        """Put the draft (or the file) into every box this page has."""
        raw = self._raw()
        for spec in self.form_fields():
            value = self.model.draft.get(spec.id, self._file_value(spec) if raw is not None else spec.default)
            self._set_widget(spec, value)
            if not spec.locked:
                self._widgets[spec.id].setEnabled(raw is not None)

    def _render_changes(self) -> None:
        model = self.model
        palette = self.palette_
        count = self.pending_count()
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


def when_or_never(iso: str | None, fallback: str) -> str:
    if not iso:
        return fallback
    return iso[:16].replace("T", " ") + " UTC"


def quote_part(part: str) -> str:
    return f'"{part}"' if " " in part else part
