"""First run: create `config.toml` for this machine.

It writes one file -- the packaged template with this machine's name, the local
`.codex` and the synced workspace filled in, every comment kept -- through the
same loader the command line uses. It does not create, touch or even look inside
the Codex state directory, and it does not create the workspace folders: those
appear when an operation that needs them runs.

Where the file goes is the user's to say. The field starts empty -- an earlier
version proposed a per-user path under %APPDATA% and the rest of the window
then worked against that path although nobody had created it (CS-268). A path
that names a file which already exists is a request to *use* that file, so the
button turns into "open this file" rather than refusing or overwriting it.

A machine name is permanent in practice. Backups, Guardian snapshots and
session plans are all filed under it, and `[[path_mappings]]` rules on the
other machine refer to it, so the screen says so instead of generating one
silently on every start. Names already filed in the workspace are shown
calmly with a button each -- most often they are this very machine's history
from an earlier version -- and never selected for the user: two machines
writing under one name mix their Guardian snapshots together.

The screen also answers the other half of "there is no config here": this
machine may well have one, and a workspace besides. "Open an existing
config.toml" switches the whole window to a file that already exists, and the
workspace search (`gui/locations.py`, read-only and shallow) fills the form
with a folder codexSync itself made.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QFormLayout, QHBoxLayout, QLineEdit

from ..controller import Outcome, find_workspaces, suggested_codex_dir, suggested_machine_id
from ..widgets import Banner, PathField, button, card, field, label, machine_combo, row, set_tone
from .base import Model, Screen


class FirstRunModel(Model):
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.busy = False
        self.result: Outcome | None = None
        #: The result is a prompt ("say where") rather than a create outcome.
        self.result_is_prompt = False
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

        self.config_path = PathField(
            browse, directory=False, any_file=True,
            dialog_title=self.t("first_run.config.dialog"), accept_text=self.t("common.choose"),
        )
        self.config_path.chosen = self._config_chosen
        # A path is only ever set without a file when it was named with `-c`,
        # which is a request to create it there; otherwise the field is empty.
        named = controller.config_path
        opened = str(named.resolve()) if named is not None else ""
        # An open file speaks for itself: the form shows what it says, never
        # this computer's host name or a guessed `.codex` beside it.
        loaded = self._loaded_values()
        self.config_path.setText(m.values.get("config", opened))
        self.config_path.edit.setPlaceholderText(self.t("first_run.config.placeholder"))
        self.machine = machine_combo(
            self._known_machines(),
            m.values.get("machine", loaded.get("machine", suggested_machine_id())),
        )
        self.codex = PathField(browse)
        self.codex.setText(m.values.get("codex", loaded.get("codex", str(suggested_codex_dir()))))
        self.workspace = PathField(browse)
        self.workspace.setText(m.values.get("workspace", loaded.get("workspace", "")))
        self.workspace.edit.setPlaceholderText(self.t("first_run.workspace.placeholder"))
        self.mirror = PathField(browse)
        self.mirror.setText(m.values.get("mirror", loaded.get("mirror", "")))
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
        #: One "this is <name>" button per machine the workspace holds.
        self.machine_actions = QHBoxLayout()
        self.machine_actions.setSpacing(8)
        inner.addLayout(self.machine_actions)
        self._machine_buttons: dict[str, object] = {}
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
        if key == "config":
            self._render_create_button()
        elif key == "machine":
            self._render_machine_hint()

    def _existing_target(self) -> Path | None:
        """The file the config field names, when it already exists."""
        text = self.config_path.text()
        if not text:
            return None
        path = Path(text).expanduser()
        return path if path.is_file() else None

    def _loaded_values(self) -> dict[str, str]:
        """The form's values as the open config file states them."""
        info = self.host.config_info()
        if info is None:
            return {}
        values: dict[str, str] = {}
        if info.machine_id:
            values["machine"] = info.machine_id
        if info.local_state_dir is not None:
            values["codex"] = str(info.local_state_dir)
        if info.workspace_root_dir is not None:
            values["workspace"] = str(info.workspace_root_dir)
        # The default mirror stays the placeholder rather than a spelled-out path.
        default_mirror = info.workspace_root_dir / "sync" if info.workspace_root_dir else None
        if info.cloud_root_dir != default_mirror:
            values["mirror"] = str(info.cloud_root_dir)
        return values

    def _config_chosen(self, _text: str) -> None:
        """Picking a file that exists is opening it; there is nothing to confirm."""
        existing = self._existing_target()
        if existing is not None:
            self.open_existing(existing)

    def _is_open(self, path: Path | None) -> bool:
        current = self.host.controller.config_path
        return path is not None and current is not None and path.resolve() == current.resolve()

    def _render_create_button(self) -> None:
        if not hasattr(self, "create_button"):
            return
        existing = self._existing_target()
        if self._is_open(existing):
            key = "first_run.is_open"
        elif existing is not None:
            key = "first_run.open_this"
        else:
            key = "first_run.create"
        self.create_button.setText(self.t(key))
        self.create_button.setEnabled(not self.model.busy and not self._is_open(existing))

    def use_machine(self, name: str) -> None:
        """Take a name the workspace already holds: this machine continues it."""
        self.machine.setEditText(name)
        self.model.values["machine"] = name
        self._render_machine_hint()

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
        # The window stays here: the form now shows what the file says, which
        # is the proof it was read; jumping away would hide exactly that.
        self.host.open_config(Path(path))

    def create(self) -> None:
        model = self.model
        if model.busy:
            return
        existing = self._existing_target()
        if existing is not None:
            # Naming a file that exists is asking to use it, never to replace it.
            if not self._is_open(existing):
                self.open_existing(existing)
            return
        if not self.config_path.text():
            model.result = Outcome(message=self.t("first_run.config.required"))
            model.result_is_prompt = True
            self.render()
            return
        path = Path(self.config_path.text()).expanduser()
        model.result_is_prompt = False
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
                # Another file starts every page afresh; the "created" line
                # belongs to the new one, and the window stays where it is.
                host.model("first_run").result = outcome
                host.screen("first_run").render()

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
                self.t("first_run.missing.detail"), palette,
            )
        self._render_create_button()
        text, tone = "", None
        if model.busy:
            text = self.t("first_run.creating")
        elif model.result is not None and model.result_is_prompt:
            text, tone = model.result.message, "attention"
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
        self._render_machine_buttons(names)
        self._render_machine_hint()

    def _render_machine_buttons(self, names: tuple[str, ...]) -> None:
        """A button per known name; rebuilt only when the names change.

        A click sets the name, which re-renders the hint -- rebuilding the
        buttons from inside that click would delete the very button whose
        signal is running, which is how a Qt window crashes.
        """
        if tuple(self._machine_buttons) == names:
            return
        while self.machine_actions.count():
            item = self.machine_actions.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._machine_buttons = {}
        for name in names:
            widget = button(self.t("first_run.machine.use", name=name))
            widget.clicked.connect(
                lambda _checked=False, name=name: QTimer.singleShot(0, lambda: self.use_machine(name))
            )
            self.machine_actions.addWidget(widget)
            self._machine_buttons[name] = widget
        if names:
            self.machine_actions.addStretch(1)

    def _render_machine_hint(self) -> None:
        """Say calmly what the workspace holds; most often it is this machine's past."""
        if not hasattr(self, "machine_warning"):
            return
        names = self._known_machines()
        typed = self.machine.currentText().strip()
        if not names:
            text, tone = "", None
        elif typed in names:
            text, tone = self.t("first_run.machine.continues", name=typed), "ok"
        else:
            text, tone = self.t("first_run.machine.known", names=self.join(list(names))), None
        self.machine_warning.setText(text)
        set_tone(self.machine_warning, tone, self.palette_)
        self.machine_warning.setVisible(bool(text))
        for name, widget in self._machine_buttons.items():
            widget.setVisible(name != typed)
