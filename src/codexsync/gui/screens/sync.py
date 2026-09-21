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
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QLineEdit

from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, row, set_tone, table
from .base import Model, Screen

#: Filter values for the direction box, in the order they are offered.
DIRECTIONS = ("all", "to_local", "to_cloud", "conflict")


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

    def reset_plans(self) -> None:
        self.preview = None
        self.result = None


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
        frame, inner = card(self.t("sync.plan.title"), self.summary)
        inner.addLayout(self._build_filters())
        self.table = table([self.t("sync.column.direction"), self.t("sync.column.path")])
        inner.addWidget(self.table, stretch=1)
        self.empty = label("", "muted", wrap=True)
        inner.addWidget(self.empty)
        self.whole_plan = label(self.t("sync.filter.whole_plan"), "muted", wrap=True)
        inner.addWidget(self.whole_plan)
        self.body.addWidget(frame, stretch=1)

        self.result = label("", wrap=True)
        self.body.addWidget(self.result)

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
        """

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

        self.run(lambda: controller.sync(dry_run=dry_run), apply)

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
        if model.run_busy:
            self.result.setText(self.t("sync.running"))
            set_tone(self.result, None, palette)
        elif run is None:
            self.result.setText("")
        elif run.ok:
            value = run.value
            key = "sync.done.apply" if value.applied else "sync.done.dry"
            self.result.setText(self.t(key) + " " + self.join([
                self.p("sync.count.files", value.actions),
                self.p("sync.count.conflicts", value.conflicts),
            ]))
            set_tone(self.result, "ok", palette)
        else:
            self.result.setText(self.failure_text(run))
            set_tone(self.result, "danger", palette)
        self.result.setVisible(bool(self.result.text()))


def _apply_preview(model: SyncModel, outcome: Outcome) -> None:
    model.preview_busy = False
    model.preview = outcome
