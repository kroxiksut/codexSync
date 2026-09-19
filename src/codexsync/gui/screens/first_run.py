"""First run: create `config.toml` for this machine.

It writes one file -- the packaged template with this machine's name, the local
`.codex` and the synced workspace filled in, every comment kept -- through the
same loader the command line uses. It does not create, touch or even look inside
the Codex state directory, and it does not create the workspace folders: those
appear when an operation that needs them runs.

A machine name is permanent in practice. Backups, Guardian snapshots and
session plans are all filed under it, and `[[path_mappings]]` rules on the
other machine refer to it, so the screen says so instead of generating one
silently on every start. Names already filed in the workspace are offered as
choices with a warning attached and never selected for the user: two machines
writing under one name mix their Guardian snapshots together.

The screen also answers the other half of "there is no config here": this
machine may well have one, and a workspace besides. "Open an existing
config.toml" switches the whole window to a file that already exists, and the
workspace search (`gui/locations.py`, read-only and shallow) fills the form
with a folder codexSync itself made.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QFormLayout, QLineEdit

from ..controller import Outcome, find_workspaces, suggested_codex_dir, suggested_machine_id
from ..widgets import Banner, PathField, button, card, field, label, machine_combo, row, set_tone
from .base import Model, Screen


class FirstRunModel(Model):
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.busy = False
        self.result: Outcome | None = None
        #: What the workspace search found, and whether it has run at all.
        self.workspaces: tuple = ()
        self.searched = False
        self.searching = False


class FirstRunScreen(Screen):
    page = "first_run"
    scrollable = True

    def build(self) -> None:
        m = self.model
        controller = self.host.controller
        self.banner = Banner()
        self.body.addWidget(self.banner)

        frame, inner = card(self.t("first_run.form.title"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        browse = self.t("common.browse")

        self.config_path = PathField(browse, directory=False, save_file=True)
        self.config_path.setText(m.values.get("config", str(controller.config_path.resolve())))
        self.machine = machine_combo(
            self._known_machines(), m.values.get("machine", suggested_machine_id())
        )
        self.codex = PathField(browse)
        self.codex.setText(m.values.get("codex", str(suggested_codex_dir())))
        self.workspace = PathField(browse)
        self.workspace.setText(m.values.get("workspace", ""))
        self.workspace.edit.setPlaceholderText(self.t("first_run.workspace.placeholder"))
        self.mirror = PathField(browse)
        self.mirror.setText(m.values.get("mirror", ""))
        self.mirror.edit.setPlaceholderText("${workspace_root}/sync")

        for key, widget in (
            ("config", self.config_path),
            ("machine", self.machine),
            ("codex", self.codex),
            ("workspace", self.workspace),
            ("mirror", self.mirror),
        ):
            form.addRow(label(self.t(f"first_run.field.{key}")), field(widget, self.t(f"first_run.hint.{key}")))
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(lambda _text, key=key: self._remember(key))
            elif isinstance(widget, PathField):
                widget.edit.textChanged.connect(lambda _text, key=key: self._remember(key))
            else:
                widget.editTextChanged.connect(lambda _text, key=key: self._remember(key))
        inner.addLayout(form)

        self.machine_warning = label("", wrap=True)
        inner.addWidget(self.machine_warning)
        self.found = label("", wrap=True)
        inner.addWidget(self.found)

        self.create_button = button(self.t("first_run.create"), primary=True)
        self.create_button.clicked.connect(self.create)
        self.open_button = button(self.t("first_run.open_existing"))
        self.open_button.clicked.connect(self.open_existing)
        self.status = label("", wrap=True)
        inner.addWidget(label(self.t("first_run.template"), wrap=True))
        inner.addLayout(row(self.create_button, self.open_button))
        inner.addWidget(self.status)
        self.body.addWidget(frame)

        after, after_inner = card(self.t("first_run.after.title"))
        after_inner.addWidget(label(self.t("first_run.after.body"), wrap=True))
        self.body.addWidget(after)

    def _remember(self, key: str) -> None:
        widget = {"config": self.config_path, "machine": self.machine, "codex": self.codex,
                  "workspace": self.workspace, "mirror": self.mirror}[key]
        self.model.values[key] = widget.currentText() if key == "machine" else widget.text()

    def _known_machines(self) -> tuple[str, ...]:
        names: list[str] = []
        for candidate in self.model.workspaces:
            names.extend(name for name in candidate.machines if name not in names)
        return tuple(names)

    def activated(self) -> None:
        """Look for a workspace once, in the background."""
        if self.model.searched or self.model.searching:
            return
        self.model.searching = True

        def apply(model: FirstRunModel, outcome: Outcome) -> None:
            model.searching = False
            model.searched = True
            model.workspaces = outcome.value if outcome.ok and outcome.value else ()

        self.read(find_workspaces, apply)

    def open_existing(self, path: Path | None = None) -> None:
        """Switch the window to a config that already exists on this machine."""
        if path is None:  # pragma: no cover - opens a native dialog
            chosen, _ = QFileDialog.getOpenFileName(
                self, self.t("first_run.open_existing"), str(Path.home()), "TOML (*.toml)"
            )
            if not chosen:
                return
            path = Path(chosen)
        self.host.open_config(Path(path))
        self.host.go_to("overview")

    def create(self) -> None:
        model = self.model
        if model.busy:
            return
        path = Path(self.config_path.text()).expanduser()
        model.busy = True
        model.result = None
        self.render()
        controller = self.host.controller
        machine = self.machine.currentText().strip()
        codex = self.codex.text()
        workspace = self.workspace.text()
        mirror = self.mirror.text()
        host = self.host

        def apply(model: FirstRunModel, outcome: Outcome) -> None:
            model.busy = False
            model.result = outcome
            if outcome.ok:
                model.values = {}

        def done_and_switch(model: FirstRunModel, outcome: Outcome) -> None:
            apply(model, outcome)
            if outcome.ok:
                host.config_changed(outcome.value.path)
                host.go_to("overview")

        self.run(lambda: controller.create_config(
            path, machine_id=machine, local_state_dir=codex,
            workspace_root_dir=workspace, cloud_root_dir=mirror,
        ), done_and_switch)

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        controller = self.host.controller
        self._render_workspaces()
        if controller.config_exists():
            self.banner.show_message(
                "ok", self.t("first_run.exists.title"),
                self.t("first_run.exists.detail", path=controller.config_path.resolve()), palette,
            )
        else:
            self.banner.show_message(
                "attention", self.t("first_run.missing.title"),
                self.t("first_run.missing.detail", path=controller.config_path.resolve()), palette,
            )
        self.create_button.setEnabled(not model.busy)
        text, tone = "", None
        if model.busy:
            text = self.t("first_run.creating")
        elif model.result is not None:
            if model.result.ok:
                text, tone = self.t("first_run.created", path=model.result.value.path), "ok"
            else:
                text, tone = self.failure_text(model.result), "danger"
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))

    def _render_workspaces(self) -> None:
        """Say what the search found -- including that it found nothing."""
        model = self.model
        palette = self.palette_
        if model.searching:
            text, tone = self.t("first_run.workspace.searching"), None
        elif not model.searched:
            text, tone = "", None
        elif model.workspaces:
            found = self.join([str(candidate.path) for candidate in model.workspaces])
            text, tone = self.t("first_run.workspace.found", paths=found), "ok"
            if not self.workspace.text() and not model.values.get("workspace"):
                self.workspace.setText(str(model.workspaces[0].path))
        else:
            text, tone = self.t("first_run.workspace.not_found"), None
        self.found.setText(text)
        set_tone(self.found, tone, palette)
        self.found.setVisible(bool(text))

        names = self._known_machines()
        # Adding the first item to an empty editable combo makes it the current
        # text. These names are offered, never chosen, so what was typed (or
        # suggested) is put back afterwards.
        typed = self.model.values.get("machine") or self.machine.currentText()
        blocked = self.machine.blockSignals(True)
        try:
            for name in names:
                if self.machine.findText(name) < 0:
                    self.machine.addItem(name)
            if self.machine.currentText() != typed:
                self.machine.setEditText(typed)
        finally:
            self.machine.blockSignals(blocked)
        warning = self.t("first_run.machine.known", names=self.join(list(names))) if names else ""
        self.machine_warning.setText(warning)
        set_tone(self.machine_warning, "attention" if warning else None, palette)
        self.machine_warning.setVisible(bool(warning))
