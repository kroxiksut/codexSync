"""Recovery after an interruption: the evidence first, then only legal exits.

An unfinished mutation journal blocks every new change, and that block is the
protection: a half-applied state is never mutated further. The screen shows the
evidence each journal carries and offers exactly what `recover` allows for it --
resume (close the journal so the command can be re-run; it re-plans from what
is on disk) or roll back. Both start with a dry run. A rollback puts each file
back into the side its backup recorded (CS-293); naming a side only narrows it,
and is needed only for an older restore whose snapshot does not record one.
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, fill_table, label, row, selected_data, set_tone, table
from .base import Model, Screen
from .guardian import _when


class RecoveryModel(Model):
    def __init__(self) -> None:
        self.listing: Outcome | None = None
        self.busy = False
        self.target = ""
        #: (operation id, action, target) the last dry run was made for.
        self.checked: tuple[str, str, str] | None = None
        self.action_busy = False
        self.action_kind = ""
        self.result: Outcome | None = None
        #: The journal another page sent the person to (`reveal`).
        self.wanted: str | None = None


class RecoveryScreen(Screen):
    page = "recovery"

    def build(self) -> None:
        self.banner = Banner()
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.banner)

        self.summary = label()
        frame, inner = card(self.t("recovery.list.title"), self.summary)
        self.table = table([
            self.t("recovery.column.state"),
            self.t("recovery.column.machine"),
            self.t("recovery.column.family"),
            self.t("recovery.column.created"),
            self.t("recovery.column.actions"),
            self.t("recovery.column.backup"),
            self.t("recovery.column.id"),
        ], selectable=True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        inner.addWidget(self.table, stretch=1)
        self.body.addWidget(frame, stretch=1)

        actions, actions_inner = card(self.t("recovery.actions.title"))
        self.evidence = label("", wrap=True)
        actions_inner.addWidget(self.evidence)

        self.resume_dry = button(self.t("recovery.resume.dry"))
        self.resume_dry.clicked.connect(lambda: self.act("resume", dry_run=True))
        self.resume_apply = button(self.t("recovery.resume.apply"), primary=True)
        self.resume_apply.clicked.connect(lambda: self.act("resume", dry_run=False))
        actions_inner.addLayout(row(label(self.t("recovery.resume.label")), self.resume_dry, self.resume_apply))
        actions_inner.addWidget(label(self.t("recovery.resume.caption"), "muted", wrap=True))

        self.target = QComboBox()
        self.target.addItem(self.t("recovery.target.choose"), "")
        for target in ("local", "cloud"):
            self.target.addItem(self.t(f"backups.target.{target}"), target)
        self.target.setCurrentIndex(max(0, self.target.findData(self.model.target)))
        self.target.currentIndexChanged.connect(self._target_changed)
        self.rollback_dry = button(self.t("recovery.rollback.dry"))
        self.rollback_dry.clicked.connect(lambda: self.act("rollback", dry_run=True))
        self.rollback_apply = button(self.t("recovery.rollback.apply"), primary=True)
        self.rollback_apply.clicked.connect(lambda: self.act("rollback", dry_run=False))
        actions_inner.addLayout(row(
            label(self.t("recovery.rollback.label")), self.target, self.rollback_dry, self.rollback_apply
        ))
        actions_inner.addWidget(label(self.t("recovery.rollback.caption"), "muted", wrap=True))
        self.status = label("", wrap=True)
        actions_inner.addWidget(self.status)
        self.body.addWidget(actions)

    def activated(self) -> None:
        if not self.model.busy:
            self.refresh()

    def reveal(self, target: str) -> None:
        """Select the journal ``target`` names, now or once the listing arrives."""
        self.model.wanted = target
        self._select_wanted()

    def _select_wanted(self) -> None:
        wanted = self.model.wanted
        listing = self.model.listing
        if not wanted or listing is None or not listing.ok:
            return
        for index, journal in enumerate(listing.value):
            if journal.operation_id == wanted:
                self.table.selectRow(index)
                self.model.wanted = None
                return

    def refresh(self) -> None:
        if self.model.busy:
            return
        self.model.busy = True
        self.render()

        def apply(model: RecoveryModel, outcome: Outcome) -> None:
            model.busy = False
            model.listing = outcome

        self.read(self.host.controller.journals, apply)

    def _selected(self):
        items = selected_data(self.table)
        return items[0] if items else None

    def _selection_changed(self) -> None:
        self._render_actions()

    def _target_changed(self) -> None:
        self.model.target = self.target.currentData() or ""
        self._render_actions()

    def act(self, action: str, *, dry_run: bool) -> None:
        model = self.model
        journal = self._selected()
        if journal is None or model.action_busy:
            return
        # Resume takes no target; the combo belongs to rollback alone, and an
        # empty rollback target means "the side each file came from".
        target = model.target if action == "rollback" else ""
        if not dry_run:
            key = "recovery.confirm.resume" if action == "resume" else "recovery.confirm.rollback"
            if not self.host.confirm(
                self.t("recovery.confirm.title"),
                self.t(
                    key, operation_id=journal.operation_id,
                    target=self.t(f"backups.target.{target}") if target else self.t("recovery.target.choose"),
                ),
                self.t(f"recovery.{action}.apply"),
            ):
                return
        model.action_busy = True
        model.action_kind = f"{action}-{'dry' if dry_run else 'apply'}"
        model.result = None
        self.render()
        controller = self.host.controller
        operation = journal.operation_id

        def go() -> Outcome:
            if action == "resume":
                done = controller.resume_journal(operation, dry_run=dry_run)
            else:
                done = controller.rollback_journal(operation, target=target or None, dry_run=dry_run)
            if not done.ok or dry_run:
                return Outcome(value=(done, None))
            return Outcome(value=(done, controller.journals()))

        def apply(model: RecoveryModel, outcome: Outcome) -> None:
            model.action_busy = False
            if not outcome.ok:
                # Only when the job itself broke (`_Job.outcome`); a refusal
                # from the core arrives as ``done``.
                model.result = outcome
                return
            done, listing = outcome.value
            model.result = done
            if done.ok and dry_run:
                model.checked = (operation, action, _checked_target(action, target))
            if listing is not None:
                model.listing = listing
                model.checked = None

        self.run(go, apply)

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        outcome = model.listing
        if model.busy and outcome is None:
            self.banner.show_message("neutral", self.t("recovery.loading"), "", palette)
        elif outcome is None:
            self.banner.show_message("neutral", "", "", palette)
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
        else:
            open_ = [j for j in outcome.value if not j.terminal]
            blocking = [j for j in open_ if not j.closes_itself]
            if blocking:
                self.banner.show_message(
                    "danger", self.p("recovery.blocked.title", len(blocking)), self.t("recovery.blocked.detail"), palette
                )
            elif open_:
                self.banner.show_message(
                    "attention", self.p("recovery.self_closing.title", len(open_)),
                    self.t("recovery.self_closing.detail"), palette,
                )
            else:
                self.banner.show_message("ok", self.t("recovery.clear.title"), self.t("recovery.clear.detail"), palette)

        if outcome is None or not outcome.ok:
            self.table.setRowCount(0)
            self.summary.setText("")
        elif getattr(self, "_drawn", None) is not outcome:
            keep = self._selected()
            rows = []
            for journal in outcome.value:
                if not journal.readable:
                    state, tone = self.t("recovery.state.unreadable"), "danger"
                elif journal.terminal:
                    state, tone = self.t(f"journal.state.{journal.state}"), None
                else:
                    state, tone = self.t(f"journal.state.{journal.state}"), "danger"
                snapshot = journal.backup_snapshot or "—"
                if journal.backup_snapshot and journal.backup_snapshot_present is False:
                    snapshot = self.t("recovery.backup.missing", name=journal.backup_snapshot)
                if journal.own:
                    machine = self.t("recovery.machine.this", machine=journal.machine_id or self.host.machine_id() or "—")
                else:
                    machine = journal.machine_id or self.t("recovery.machine.unknown")
                rows.append([
                    Cell(state, tone=tone, data=journal),
                    Cell(machine, muted=journal.own),
                    Cell(self.t(f"journal.family.{journal.family}") if journal.family and self.host.catalog.has(f"journal.family.{journal.family}") else (journal.family or "—")),
                    Cell(_when(journal.created_at_utc) if journal.created_at_utc else "—"),
                    Cell(str(journal.action_count) if journal.action_count is not None else "—"),
                    Cell(snapshot, muted=True),
                    Cell(journal.operation_id, muted=True),
                ])
            self.table.blockSignals(True)
            fill_table(self.table, rows, palette)
            if keep is not None:
                for r, journal in enumerate(outcome.value):
                    if journal.operation_id == keep.operation_id:
                        self.table.selectRow(r)
            self.table.blockSignals(False)
            self._drawn = outcome
            self._select_wanted()
            self.summary.setText(self.p("recovery.count", len(outcome.value)))
        self._render_actions()

    def _render_actions(self) -> None:
        model = self.model
        palette = self.palette_
        journal = self._selected()
        busy = model.action_busy
        can_resume = journal is not None and journal.can_resume and not busy
        can_rollback = journal is not None and journal.can_rollback and not busy
        self.resume_dry.setEnabled(can_resume)
        self.resume_apply.setEnabled(can_resume and model.checked == (journal.operation_id, "resume", _checked_target("resume", model.target)))
        self.target.setEnabled(journal is not None and journal.can_rollback and not busy)
        self.rollback_dry.setEnabled(can_rollback)
        self.rollback_apply.setEnabled(can_rollback and model.checked == (journal.operation_id, "rollback", model.target))

        if journal is None:
            self.evidence.setText(self.t("recovery.select"))
        elif journal.terminal:
            self.evidence.setText(self.t("recovery.evidence.closed", state=self.t(f"journal.state.{journal.state}")))
        elif not journal.readable:
            self.evidence.setText(self.t("recovery.evidence.unreadable"))
        else:
            text = self.t(
                "recovery.evidence.open",
                family=journal.family or "—",
                state=self.t(f"journal.state.{journal.state}"),
                actions=journal.action_count if journal.action_count is not None else "—",
                backup=journal.backup_snapshot or self.t("recovery.backup.none"),
            )
            if journal.closes_itself:
                text += "\n" + self.t("recovery.evidence.closes_itself")
            elif not journal.own:
                text += "\n" + self.t(
                    "recovery.evidence.other_machine",
                    machine=journal.machine_id or self.t("recovery.machine.unknown"),
                )
            self.evidence.setText(text)

        text, tone = "", None
        if busy:
            text = self.t("recovery.working")
        elif model.result is not None:
            if model.result.ok:
                value = model.result.value
                text = self.t(f"recovery.action.{value.action.value}") + " " + value.detail
                tone = "ok"
            else:
                text, tone = self.failure_text(model.result), "danger"
        elif journal is not None and not journal.terminal and journal.readable:
            text = self.t("recovery.dry_first")
        self.status.setText(text)
        set_tone(self.status, tone, palette)
        self.status.setVisible(bool(text))


def _checked_target(action: str, target: str) -> str:
    """The target a dry run is recorded against: rollback's only.

    Resume has no target, so choosing one for rollback must not undo the dry
    run that unlocked resume.
    """
    return target if action == "rollback" else ""
