"""Backups: the snapshots backup-first left behind, and restoring one.

Only a snapshot whose committed manifest matches it is offered for restore; a
legacy or damaged one is listed with the reason and cannot be picked here (the
command line still accepts an explicit legacy id, on purpose, and only there).

A restore starts with a dry run, which verifies every file's hash against the
manifest and plans the copy without writing. The real restore needs Codex
closed, takes its own backup of whatever it is about to replace, and runs the
same journal-and-final-check envelope as a sync.
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, row, selected_data, set_tone, table
from .base import Model, Screen
from .guardian import _size, _when


class BackupsModel(Model):
    def __init__(self) -> None:
        self.listing: Outcome | None = None
        self.busy = False
        self.target = "local"
        #: (snapshot name, target) the last dry run verified.
        self.checked: tuple[str, str] | None = None
        self.action_busy = False
        self.action_kind = ""
        self.result: Outcome | None = None

    def reset_plans(self) -> None:
        self.checked = None
        self.result = None


class BackupsScreen(Screen):
    page = "backups"

    def build(self) -> None:
        self.banner = Banner()
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(self.t("backups.list.title"), self.summary)
        self.table = table([
            self.t("backups.column.created"),
            self.t("backups.column.machine"),
            self.t("backups.column.status"),
            self.t("backups.column.files"),
            self.t("backups.column.size"),
            self.t("backups.column.format"),
            self.t("backups.column.name"),
        ], selectable=True)
        self.table.itemSelectionChanged.connect(self.render)
        inner.addWidget(self.table, stretch=1)
        self.body.addWidget(frame, stretch=1)

        restore, restore_inner = card(self.t("backups.restore.title"))
        restore_inner.addWidget(label(self.t("backups.restore.caption"), "muted", wrap=True))
        self.target = QComboBox()
        for target in ("local", "cloud"):
            self.target.addItem(self.t(f"backups.target.{target}"), target)
        self.target.setCurrentIndex(max(0, self.target.findData(self.model.target)))
        self.target.currentIndexChanged.connect(self._target_changed)
        self.selected = label("", "muted")
        restore_inner.addLayout(row(label(self.t("backups.restore.target")), self.target, self.selected))
        self.dry_button = button(self.t("action.dry_run"))
        self.dry_button.clicked.connect(lambda: self.restore(dry_run=True))
        self.apply_button = button(self.t("backups.restore.apply"), primary=True)
        self.apply_button.clicked.connect(lambda: self.restore(dry_run=False))
        restore_inner.addLayout(row(self.dry_button, self.apply_button))
        self.status = label("", wrap=True)
        restore_inner.addWidget(self.status)
        self.body.addWidget(restore)

    def activated(self) -> None:
        if self.model.listing is None and not self.model.busy:
            self.refresh()

    def refresh(self) -> None:
        if self.model.busy:
            return
        self.model.busy = True
        self.render()

        def apply(model: BackupsModel, outcome: Outcome) -> None:
            model.busy = False
            model.listing = outcome

        self.read(self.host.controller.backups, apply)

    def _target_changed(self) -> None:
        self.model.target = self.target.currentData()
        self.render()

    def _selected(self):
        items = selected_data(self.table)
        return items[0] if items else None

    def restore(self, *, dry_run: bool) -> None:
        model = self.model
        snapshot = self._selected()
        if snapshot is None or model.action_busy or not snapshot.committed:
            return
        target = model.target
        if not dry_run and not self.host.confirm(
            self.t("backups.confirm.title"),
            self.t("backups.confirm.body", name=snapshot.name, target=self.t(f"backups.target.{target}")),
            self.t("backups.restore.apply"),
        ):
            return
        model.action_busy = True
        model.action_kind = "dry" if dry_run else "apply"
        model.result = None
        self.render()
        controller = self.host.controller
        name = snapshot.name

        def apply(model: BackupsModel, outcome: Outcome) -> None:
            model.action_busy = False
            model.result = outcome
            if dry_run and outcome.ok:
                model.checked = (name, target)
            if not dry_run and outcome.ok:
                model.checked = None
                model.listing = None

        self.run(lambda: controller.restore(snapshot_name=name, target=target, dry_run=dry_run), apply)

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        outcome = model.listing
        if model.busy:
            self.banner.show_message("neutral", self.t("backups.loading"), "", palette)
        elif outcome is not None and not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
        else:
            info = self.host.config_info()
            self.banner.show_message(
                "neutral", self.t("backups.dir", path=info.backup_dir) if info else "", "", palette
            )

        keep = self._selected()
        if outcome is None or not outcome.ok:
            if self.table.rowCount():
                self.table.setRowCount(0)
            self.summary.setText("")
        elif not self.table.rowCount() or getattr(self, "_drawn", None) is not outcome:
            rows = []
            for snap in outcome.value:
                if snap.committed:
                    status, tone = self.t("backups.status.committed"), "ok"
                elif snap.legacy:
                    status, tone = self.t("backups.status.legacy"), "attention"
                else:
                    status, tone = self.t("backups.status.damaged"), "danger"
                rows.append([
                    Cell(_when(snap.created_utc or snap.modified_utc), data=snap),
                    Cell(snap.machine or "—"),
                    Cell(status, tone=tone, tooltip=snap.problem),
                    Cell(str(snap.entries) if snap.entries is not None else "—"),
                    Cell(_size(snap.total_bytes) if snap.total_bytes is not None else "—"),
                    Cell("zip" if snap.compressed else self.t("backups.format.folder"), muted=True),
                    Cell(snap.name, muted=True),
                ])
            self.table.blockSignals(True)
            fill_table(self.table, rows, palette)
            if keep is not None:
                for r, snap in enumerate(outcome.value):
                    if snap.name == keep.name:
                        self.table.selectRow(r)
            self.table.blockSignals(False)
            self._drawn = outcome
            committed = sum(1 for snap in outcome.value if snap.committed)
            self.summary.setText(self.join([
                self.p("backups.count", len(outcome.value)),
                self.p("backups.count.committed", committed),
            ]))

        snapshot = self._selected()
        can = snapshot is not None and snapshot.committed and not model.action_busy
        self.dry_button.setEnabled(can)
        # The real restore is offered only after a dry run of this exact choice.
        self.apply_button.setEnabled(can and model.checked == (snapshot.name, model.target))
        if snapshot is None:
            self.selected.setText(self.t("backups.select"))
        elif not snapshot.committed:
            self.selected.setText(self.t("backups.not_restorable"))
        else:
            self.selected.setText(snapshot.name)

        text, tone = "", None
        if model.action_busy:
            text = self.t("backups.working")
        elif model.result is not None:
            if model.result.ok:
                value = model.result.value
                key = "backups.done.dry" if model.action_kind == "dry" else "backups.done.apply"
                text, tone = self.p(key, value.restored_files, name=value.snapshot_name), "ok"
            else:
                text, tone = self.failure_text(model.result), "danger"
        elif snapshot is not None and snapshot.committed and model.checked != (snapshot.name, model.target):
            text = self.t("backups.dry_first")
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))
