"""Snapshot guardian: verified snapshots of `.codex-global-state.json`.

A snapshot may be taken while Codex is open: Guardian reads the state file until
it is stable, validates it, and writes only into its own root outside `.codex`,
the mirror, backups and temp. A state that fails validation goes to quarantine
and can never become latest-good -- so the quarantine table is not an error log
to hide, it is the list of states Guardian refused to trust, with the reason.

Putting a snapshot back is a mutation of the global state, so it is the usual
three steps: a preview that says what changes (projects and bindings now
against the snapshot), a dry run through the gate, and a restore that quotes
the preview's plan id and goes through the same envelope as every other state
write -- the replaced file is backed up first.

Accepting a new baseline is the way out when the quarantine keeps filling for a
drop that is real: the preview says, in counts, why projects and bindings fell,
and the confirmation quotes the plan id. It writes only into the Guardian root,
so it is allowed while Codex is open, and it cannot accept a damaged state.
"""
from __future__ import annotations

from ..controller import Outcome
from ..widgets import Banner, Cell, button, card, command, fill_table, label, row, selected_data, set_tone, table
from .base import Model, Screen

#: Plan codes that mean "Guardian will manage by itself", shown without alarm.
_NO_DECISION_NEEDED = frozenset({"NO_BASELINE", "NOTHING_TO_ACCEPT"})
_RESULT_TONES = {"COMMITTED": "ok", "UNCHANGED": "ok", "BUSY": "attention", "QUARANTINED": "attention", "FAILED": "danger"}


class GuardianModel(Model):
    def __init__(self) -> None:
        self.inventory: Outcome | None = None
        self.busy = False
        self.snapshot_busy = False
        self.snapshot: Outcome | None = None
        #: The snapshot the restore card is about, and what was learned about it.
        self.selected: str | None = None
        self.preview: Outcome | None = None
        self.checked: str | None = None
        self.restore_busy = False
        self.restore_kind = ""
        self.restore_result: Outcome | None = None
        #: Accepting the current state as the new baseline.
        self.accept_preview: Outcome | None = None
        self.accept_busy = False
        self.accept_result: Outcome | None = None

    def reset_plans(self) -> None:
        self.preview = None
        self.checked = None
        self.restore_result = None


class GuardianScreen(Screen):
    page = "guardian"
    scrollable = True

    def build(self) -> None:
        self.banner = Banner()
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.banner)

        self.snapshot_button = button(self.t("guardian.take"), primary=True)
        self.snapshot_button.clicked.connect(self.take_snapshot)
        self.result = label("", wrap=True)
        self.body.addLayout(row(self.snapshot_button, self.result))
        self.body.addWidget(label(self.t("guardian.caption"), "muted", wrap=True))

        self.summary = label()
        frame, inner = card(self.t("guardian.snapshots.title"), self.summary)
        self.snapshots = table([
            self.t("guardian.column.created"),
            self.t("guardian.column.generation"),
            self.t("guardian.column.status"),
            self.t("guardian.column.projects"),
            self.t("guardian.column.bindings"),
            self.t("guardian.column.schema"),
            self.t("guardian.column.size"),
            self.t("guardian.column.id"),
        ], selectable=True)
        self.snapshots.itemSelectionChanged.connect(self._selection_changed)
        self.snapshots.setMinimumHeight(220)
        inner.addWidget(self.snapshots, stretch=1)
        self.body.addWidget(frame, stretch=3)

        restore, restore_inner = card(self.t("guardian.restore.title"))
        restore_inner.addWidget(label(self.t("guardian.restore.caption"), "muted", wrap=True))
        self.restore_selected = label("", wrap=True)
        restore_inner.addWidget(self.restore_selected)
        self.restore_preview = button(self.t("guardian.restore.preview"))
        self.restore_preview.clicked.connect(self.preview_restore)
        self.restore_dry = button(self.t("action.dry_run"))
        self.restore_dry.clicked.connect(lambda: self.restore(dry_run=True))
        self.restore_apply = button(self.t("guardian.restore.apply"), primary=True)
        self.restore_apply.clicked.connect(lambda: self.restore(dry_run=False))
        restore_inner.addLayout(row(self.restore_preview, self.restore_dry, self.restore_apply))
        self.restore_plan = command("")
        restore_inner.addWidget(self.restore_plan)
        self.restore_status = label("", wrap=True)
        restore_inner.addWidget(self.restore_status)
        self.body.addWidget(restore)

        self.quarantine_summary = label()
        frame, inner = card(self.t("guardian.quarantine.title"), self.quarantine_summary)
        inner.addWidget(label(self.t("guardian.quarantine.caption"), "muted", wrap=True))
        self.quarantine = table([
            self.t("guardian.column.created"),
            self.t("guardian.column.reasons"),
            self.t("guardian.column.size"),
            self.t("guardian.column.id"),
        ], stretch=1)
        self.quarantine.setMinimumHeight(140)
        inner.addWidget(self.quarantine, stretch=1)
        self.body.addWidget(frame, stretch=2)

        accept, accept_inner = card(self.t("guardian.accept.title"))
        accept_inner.addWidget(label(self.t("guardian.accept.caption"), "muted", wrap=True))
        self.accept_counts = label("", wrap=True)
        accept_inner.addWidget(self.accept_counts)
        self.accept_preview = button(self.t("guardian.accept.preview"))
        self.accept_preview.clicked.connect(self.preview_accept)
        self.accept_apply = button(self.t("guardian.accept.apply"), primary=True)
        self.accept_apply.clicked.connect(self.accept)
        accept_inner.addLayout(row(self.accept_preview, self.accept_apply))
        self.accept_plan = command("")
        accept_inner.addWidget(self.accept_plan)
        self.accept_status = label("", wrap=True)
        accept_inner.addWidget(self.accept_status)
        self.body.addWidget(accept)

    def activated(self) -> None:
        if self.model.inventory is None and not self.model.busy:
            self.refresh()

    def refresh(self) -> None:
        if self.model.busy:
            return
        self.model.busy = True
        self.render()

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.busy = False
            model.inventory = outcome

        self.read(self.host.controller.guardian_inventory, apply)

    def take_snapshot(self) -> None:
        if self.model.snapshot_busy:
            return
        self.model.snapshot_busy = True
        self.model.snapshot = None
        self.render()
        controller = self.host.controller

        def go() -> Outcome:
            taken = controller.guardian_snapshot()
            if not taken.ok:
                return taken
            listing = controller.guardian_inventory()
            return Outcome(value=(taken.value, listing))

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.snapshot_busy = False
            if outcome.ok:
                taken, listing = outcome.value
                model.snapshot = Outcome(value=taken)
                model.inventory = listing
            else:
                model.snapshot = outcome

        self.run(go, apply)

    # --- restoring ---------------------------------------------------------------

    def _selection_changed(self) -> None:
        items = selected_data(self.snapshots)
        snapshot_id = items[0].snapshot_id if items else None
        if snapshot_id != self.model.selected:
            self.model.selected = snapshot_id
            self.model.preview = None
            self.model.checked = None
            self.model.restore_result = None
        self._render_restore()

    def preview_restore(self) -> None:
        model = self.model
        if model.selected is None or model.restore_busy:
            return
        model.restore_busy = True
        model.restore_kind = "preview"
        model.restore_result = None
        model.checked = None
        self._render_restore()
        controller = self.host.controller
        snapshot_id = model.selected

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.restore_busy = False
            if model.selected == snapshot_id:
                model.preview = outcome

        self.read(lambda: controller.preview_guardian_restore(snapshot_id), apply)

    def restore(self, *, dry_run: bool) -> None:
        model = self.model
        preview = model.preview
        if preview is None or not preview.ok or model.restore_busy:
            return
        plan = preview.value
        if not dry_run and not self.host.confirm(
            self.t("guardian.restore.confirm.title"),
            self.t(
                "guardian.restore.confirm.body",
                created=_when(plan.snapshot_created_at_utc),
                projects_now=plan.projects_now if plan.projects_now is not None else "?",
                projects=plan.projects_in_snapshot,
                plan_id=plan.plan_id,
            ),
            self.t("guardian.restore.apply"),
        ):
            return
        model.restore_busy = True
        model.restore_kind = "dry" if dry_run else "apply"
        model.restore_result = None
        self._render_restore()
        controller = self.host.controller
        snapshot_id = plan.snapshot_id
        plan_id = plan.plan_id

        def go() -> Outcome:
            done = controller.apply_guardian_restore(snapshot_id, confirm_plan=plan_id, dry_run=dry_run)
            if not done.ok or dry_run:
                return Outcome(value=(done, None))
            return Outcome(value=(done, controller.guardian_inventory()))

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.restore_busy = False
            done, listing = outcome.value
            model.restore_result = done
            if done.ok and dry_run:
                model.checked = plan_id
            if done.ok and not dry_run:
                # The live state is now the snapshot: the preview described the past.
                model.preview = None
                model.checked = None
                if listing is not None:
                    model.inventory = listing

        self.run(go, apply)

    # --- accepting a new baseline ------------------------------------------------

    def preview_accept(self) -> None:
        model = self.model
        if model.accept_busy:
            return
        model.accept_busy = True
        model.accept_result = None
        self._render_accept()
        controller = self.host.controller

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.accept_busy = False
            model.accept_preview = outcome

        self.read(controller.preview_guardian_accept, apply)

    def accept(self) -> None:
        model = self.model
        preview = model.accept_preview
        if preview is None or not preview.ok or preview.value.codes or model.accept_busy:
            return
        plan = preview.value
        if not self.host.confirm(
            self.t("guardian.accept.confirm.title"),
            self.t(
                "guardian.accept.confirm.body",
                bindings_before=_count(plan.bindings_before),
                bindings_now=_count(plan.bindings_now),
                plan_id=plan.plan_id,
            ),
            self.t("guardian.accept.apply"),
        ):
            return
        model.accept_busy = True
        model.accept_result = None
        self._render_accept()
        controller = self.host.controller
        plan_id = plan.plan_id

        def go() -> Outcome:
            done = controller.apply_guardian_accept(confirm_plan=plan_id)
            return Outcome(value=(done, controller.guardian_inventory() if done.ok else None))

        def apply(model: GuardianModel, outcome: Outcome) -> None:
            model.accept_busy = False
            done, listing = outcome.value
            model.accept_result = done
            if done.ok:
                # The baseline moved: the preview describes a decision already taken.
                model.accept_preview = None
                if listing is not None:
                    model.inventory = listing

        self.run(go, apply)

    def _render_accept(self) -> None:
        model = self.model
        palette = self.palette_
        preview = model.accept_preview
        plan = preview.value if preview is not None and preview.ok else None
        busy = model.accept_busy
        self.accept_preview.setEnabled(not busy)
        self.accept_apply.setEnabled(plan is not None and not plan.codes and not busy)

        lines: list[str] = []
        if plan is not None and plan.baseline_snapshot_id:
            lines.append(self.t(
                "guardian.accept.counts",
                created=_when(plan.baseline_created_at_utc),
                projects_before=_count(plan.projects_before), projects_now=_count(plan.projects_now),
                bindings_before=_count(plan.bindings_before), bindings_now=_count(plan.bindings_now),
            ))
        why = plan.explanation if plan is not None else None
        if why is not None:
            lines.append(self.t(
                "guardian.accept.why.projects",
                replaced=why.projects_replaced, removed=why.projects_removed, added=why.projects_added,
            ))
            lines.append(self.t(
                "guardian.accept.why.bindings",
                replaced=why.bindings_to_replaced_projects,
                removed=why.bindings_to_removed_projects,
                dropped=why.bindings_dropped,
            ))
        self.accept_counts.setText("\n".join(lines))
        self.accept_counts.setVisible(bool(lines))
        if plan is not None:
            self.accept_plan.setText(self.t("common.plan_id", plan_id=plan.plan_id))
        self.accept_plan.setVisible(plan is not None and not plan.codes)

        text, tone = "", None
        if busy:
            text = self.t("guardian.accept.working")
        elif model.accept_result is not None:
            if model.accept_result.ok:
                text, tone = self.t("guardian.accept.done"), "ok"
            else:
                text, tone = self.failure_text(model.accept_result), "danger"
        elif preview is not None and not preview.ok:
            text, tone = self.failure_text(preview), "danger"
        elif plan is not None and plan.codes:
            text = ", ".join(self._reason(code) for code in plan.codes)
            tone = None if set(plan.codes) <= _NO_DECISION_NEEDED else "danger"
        self.accept_status.setText(text)
        set_tone(self.accept_status, tone, palette)
        self.accept_status.setVisible(bool(text))

    def _render_restore(self) -> None:
        model = self.model
        palette = self.palette_
        preview = model.preview
        plan = preview.value if preview is not None and preview.ok else None
        busy = model.restore_busy
        self.restore_preview.setEnabled(model.selected is not None and not busy)
        ready = plan is not None and not plan.codes
        self.restore_dry.setEnabled(ready and not busy)
        # The restore unlocks only after a dry run of this very plan passed.
        self.restore_apply.setEnabled(ready and not busy and model.checked == plan.plan_id)

        if model.selected is None:
            self.restore_selected.setText(self.t("guardian.restore.select"))
        elif plan is None:
            self.restore_selected.setText(self.t("guardian.restore.selected", snapshot_id=model.selected))
        else:
            now = plan.projects_now if plan.projects_now is not None else "?"
            bindings_now = plan.bindings_now if plan.bindings_now is not None else "?"
            self.restore_selected.setText(self.t(
                "guardian.restore.changes",
                created=_when(plan.snapshot_created_at_utc),
                projects_now=now, projects=plan.projects_in_snapshot,
                bindings_now=bindings_now, bindings=plan.bindings_in_snapshot,
            ))
        if plan is not None:
            self.restore_plan.setText(self.t("common.plan_id", plan_id=plan.plan_id))
        self.restore_plan.setVisible(plan is not None)

        text, tone = "", None
        if busy:
            text = self.t("guardian.restore.working")
        elif model.restore_result is not None:
            result = model.restore_result
            if not result.ok:
                text, tone = self.failure_text(result), "danger"
            elif model.restore_kind == "dry":
                text, tone = self.t("guardian.restore.done.dry"), "ok"
            else:
                text, tone = self.t("guardian.restore.done.apply"), "ok"
        elif preview is not None and not preview.ok:
            text, tone = self.failure_text(preview), "danger"
        elif plan is not None:
            if plan.identical:
                text = self.t("guardian.restore.identical")
            elif plan.codes:
                text, tone = self.t("common.codes", codes=", ".join(self._reason(code) for code in plan.codes)), "danger"
            elif model.checked != plan.plan_id:
                text = self.t("guardian.restore.dry_first")
        self.restore_status.setText(text)
        set_tone(self.restore_status, tone, palette)
        self.restore_status.setVisible(bool(text))

    def render(self) -> None:
        model = self.model
        palette = self.palette_
        self.refresh_button.setEnabled(not model.busy)
        self.snapshot_button.setEnabled(not model.snapshot_busy)

        outcome = model.inventory
        if model.busy and outcome is None:
            self.banner.show_message("neutral", self.t("guardian.loading"), "", palette)
        elif outcome is None:
            self.banner.show_message("neutral", "", "", palette)
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), outcome.message, palette)
        else:
            inventory = outcome.value
            latest = next((s for s in inventory.snapshots if s.latest_good), None)
            detail = self.join([self.t("guardian.root", path=inventory.root_dir), *inventory.problems])
            if latest is not None:
                self.banner.show_message(
                    "ok",
                    self.t("guardian.latest.title", created=_when(latest.created_at_utc)),
                    detail, palette,
                )
            else:
                self.banner.show_message("attention", self.t("guardian.latest.none"), detail, palette)

        if outcome is None or not outcome.ok:
            self.snapshots.setRowCount(0)
            self.quarantine.setRowCount(0)
            self.summary.setText("")
            self.quarantine_summary.setText("")
        else:
            inventory = outcome.value
            rows = []
            for snap in inventory.snapshots:
                if snap.latest_good:
                    status, tone = self.t("guardian.status.latest"), "ok"
                elif snap.committed and snap.verified:
                    status, tone = self.t("guardian.status.verified"), None
                elif not snap.verified:
                    status, tone = self.t("guardian.status.damaged"), "danger"
                else:
                    status, tone = self.t("guardian.status.uncommitted"), "attention"
                rows.append([
                    Cell(_when(snap.created_at_utc), data=snap),
                    Cell(str(snap.generation)),
                    Cell(status, tone=tone, tooltip=", ".join(snap.validation_codes) or snap.validation_status),
                    Cell(str(snap.project_count)),
                    Cell(str(snap.binding_count)),
                    Cell(snap.schema_id or "—", muted=True),
                    Cell(_size(snap.size)),
                    Cell(snap.snapshot_id, muted=True),
                ])
            self.snapshots.blockSignals(True)
            fill_table(self.snapshots, rows, palette)
            for index, snap in enumerate(inventory.snapshots):
                if snap.snapshot_id == model.selected:
                    self.snapshots.selectRow(index)
            self.snapshots.blockSignals(False)
            self.summary.setText(self.p("guardian.count", len(inventory.snapshots)))
            rows = [
                [
                    Cell(_when(item.created_at_utc) if item.created_at_utc else "—"),
                    Cell(", ".join(self._reason(code) for code in item.reason_codes), tone="attention", tooltip=", ".join(item.reason_codes)),
                    Cell(_size(item.size) if item.size is not None else "—"),
                    Cell(item.event_id, muted=True),
                ]
                for item in inventory.quarantine
            ]
            fill_table(self.quarantine, rows, palette)
            self.quarantine_summary.setText(self.p("guardian.quarantine.count", len(inventory.quarantine)))

        text, tone = "", None
        if model.snapshot_busy:
            text = self.t("guardian.taking")
        elif model.snapshot is not None:
            if model.snapshot.ok:
                status = model.snapshot.value.status.value
                text = self.t(f"guardian.result.{status}")
                if model.snapshot.value.detail and status in {"FAILED", "QUARANTINED"}:
                    text = f"{text} {model.snapshot.value.detail}"
                tone = _RESULT_TONES.get(status)
            else:
                text, tone = self.failure_text(model.snapshot), "danger"
        self.result.setText(text)
        set_tone(self.result, tone, palette)
        self._render_restore()
        self._render_accept()

    def _reason(self, code: str) -> str:
        key = f"guardian.reason.{code}"
        return self.t(key) if self.host.catalog.has(key) else code


def _when(iso: str) -> str:
    """`2026-09-05T10:57:48.403582Z` -> `2026-09-05 10:57 UTC`."""
    if not iso:
        return ""
    return iso[:16].replace("T", " ") + " UTC"


def _count(value: int | None) -> str:
    return "?" if value is None else str(value)


def _size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)
