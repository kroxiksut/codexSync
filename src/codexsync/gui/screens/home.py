"""Home: this machine's CodexSync at a glance, one tile per thing it keeps (CS-275).

Beside Overview, not instead of it. Overview is a diagnostic -- every check and
whether anything may run -- while Home answers "how are things": when the last
sync ran and how it ended, how much went each way this month, how many copies
and snapshots exist and how old the newest is, which tasks are on, and how many
chats and projects there are. Every tile links to the page that owns it, which
is also how the sync history stops being hard to find (CS-274).

The tiles come from one read (`read_home_summary`) that opens no session file,
so the page may read it when it is shown. The chat and project counts need the
full chat scan; they come from a cache every chat scan refreshes, are labelled
with the time they were taken, and are recounted only on request.

Like every indicator in the window, the "Codex is open" line is advisory: a
write takes its own reading at the moment it happens.
"""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QWidget

from ..controller import Outcome
from ..widgets import Banner, button, card, human_size, label, row, set_tone
from .base import Model, Screen
from .sync import local_time, run_result

#: Tiles in reading order; the id names the catalogue keys `home.<id>.*`.
TILES = ("sync", "state", "copies", "backups", "guardian", "automation")
COLUMNS = 2


class HomeModel(Model):
    def __init__(self) -> None:
        self.summary: Outcome | None = None
        self.busy = False
        self.recount_busy = False
        self.recount: Outcome | None = None

    def reset_plans(self) -> None:
        self.summary = None
        self.recount = None


class HomeScreen(Screen):
    page = "home"
    scrollable = True

    def build(self) -> None:
        self.banner = Banner()
        self.refresh_button = button(self.t("action.refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.banner.actions.addWidget(self.refresh_button)
        self.body.addWidget(self.banner)

        self.recovery = Banner()
        self.open_recovery = button(self.t("overview.open_recovery"))
        self.open_recovery.clicked.connect(lambda: self.host.go_to("recovery"))
        self.recovery.actions.addWidget(self.open_recovery)
        self.body.addWidget(self.recovery)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        #: tile id -> (value line, detail lines, action buttons)
        self.tiles: dict[str, tuple] = {}
        for index, tile in enumerate(TILES):
            frame, layout = card(self.t(f"home.{tile}.title"))
            value = label("", "tileValue", wrap=True)
            layout.addWidget(value)
            details = label("", "muted", wrap=True)
            layout.addWidget(details)
            layout.addStretch(1)
            actions = self._tile_actions(tile)
            layout.addLayout(row(*actions))
            self.tiles[tile] = (value, details, actions)
            grid.addWidget(frame, index // COLUMNS, index % COLUMNS)
        holder = QWidget()
        holder.setObjectName("content")
        holder.setLayout(grid)
        self.body.addWidget(holder)

    def _tile_actions(self, tile: str) -> list:
        go = {
            "sync": self._open_history,
            "state": lambda: self.host.go_to("chats"),
            "copies": lambda: self.host.go_to("automation", "state_backup.root_dir"),
            "backups": lambda: self.host.go_to("backups"),
            "guardian": lambda: self.host.go_to("guardian"),
            "automation": lambda: self.host.go_to("automation"),
        }[tile]
        opener = button(self.t(f"home.{tile}.open"))
        opener.clicked.connect(go)
        if tile != "state":
            return [opener]
        self.recount_button = button(self.t("home.state.recount"))
        self.recount_button.clicked.connect(self.recount)
        return [self.recount_button, opener]

    def _open_history(self) -> None:
        self.host.go_to("sync")
        self.host.screen("sync").show_history()

    # --- data flow ---------------------------------------------------------------

    def activated(self) -> None:
        if self.model.summary is None and not self.model.busy:
            self.refresh()

    def refresh(self) -> None:
        if self.model.busy or not self.host.controller.config_exists():
            return
        self.model.busy = True
        self.render()

        def apply(model: HomeModel, outcome: Outcome) -> None:
            model.busy = False
            model.summary = outcome

        self.read(self.host.controller.home, apply, kind="home")

    def recount(self) -> None:
        """Scan the chats now; the tile shows the new counts when it is done."""
        if self.model.recount_busy:
            return
        self.model.recount_busy = True
        self.model.recount = None
        self.render()
        controller = self.host.controller

        def apply(model: HomeModel, outcome: Outcome) -> None:
            model.recount_busy = False
            model.recount = outcome

        self.read(lambda progress=None: controller.recount_state(progress=progress), apply, progress=True, kind="recount")

    # --- drawing -----------------------------------------------------------------

    def render(self) -> None:
        palette = self.palette_
        self.refresh_button.setEnabled(not self.model.busy)
        outcome = self.model.summary
        summary = outcome.value if outcome is not None and outcome.ok else None
        if outcome is None:
            self.banner.show_message("neutral", self.t("banner.reading") if self.model.busy else "", "", palette)
        elif not outcome.ok:
            self.banner.show_message("danger", self.headline(outcome.failure), self.refusal_text(outcome), palette)
        else:
            tone = {"stopped": "ok", "running": "attention"}.get(summary.codex, "attention")
            self.banner.show_message(
                tone, self.t(f"home.codex.{summary.codex}"), self.t("home.codex.advisory"), palette
            )
        open_journals = summary.open_journals if summary is not None else None
        if open_journals:
            self.recovery.show_message(
                "danger", self.p("recovery.blocked.title", open_journals), self.t("recovery.blocked.detail"), palette
            )
        else:
            self.recovery.setVisible(False)
        for tile in TILES:
            value, details, _actions = self.tiles[tile]
            text, lines, tone = ("", [], None) if summary is None else getattr(self, f"_tile_{tile}")(summary)
            error = summary.errors.get(self._error_part(tile)) if summary is not None else None
            if error:
                text, lines, tone = self.t("home.unreadable"), [error], "danger"
            value.setText(text)
            set_tone(value, tone, palette)
            details.setText("\n".join(line for line in lines if line))
            details.setVisible(bool(details.text()))
        self.recount_button.setEnabled(not self.model.recount_busy)

    @staticmethod
    def _error_part(tile: str) -> str:
        return {"sync": "journals", "state": "state"}.get(tile, tile)

    def _tile_sync(self, summary):
        sync = summary.sync
        if sync is None:
            return "", [], None
        if sync.last is None:
            text, tone = self.t("home.sync.never"), None
        else:
            text = self.t("home.sync.last", when=local_time(sync.last.created_at_utc), result=run_result(self, sync.last))
            tone = None if sync.last.readable and sync.last.state == "COMMITTED" else "attention"
        lines = [
            self.join([self.p("home.sync.runs", sync.runs), self.p("home.sync.failed", sync.failed)]),
            self.t("home.sync.files", to_cloud=sync.to_cloud, to_local=sync.to_local),
            self.t("home.sync.chats", to_cloud=sync.chats_to_cloud, to_local=sync.chats_to_local),
        ]
        return text, lines, tone

    def _tile_state(self, summary):
        recount = self.model.recount
        # Whichever count is newer: a chat scan elsewhere in the window writes
        # the cache after a recount here, and the page must not keep the older.
        candidates = [item for item in (
            recount.value if recount is not None and recount.ok else None, summary.state,
        ) if item is not None]
        stats = max(candidates, key=lambda item: item.computed_at_utc, default=None)
        lines = []
        if self.model.recount_busy:
            lines.append(self.progress_text() or self.t("home.state.counting"))
        elif recount is not None and not recount.ok:
            lines.append(self.failure_text(recount))
        if stats is None:
            return self.t("home.state.unknown"), lines + [self.t("home.state.how")], None
        text = self.join([self.p("home.state.chats", stats.chats), self.p("home.state.projects", stats.projects)])
        lines += [
            self.join([
                self.p("home.state.sub_threads", stats.sub_threads),
                self.p("home.state.archived", stats.archived),
            ]),
            self.t("home.state.size", size=human_size(stats.session_bytes)),
        ]
        if stats.chats_without_project:
            lines.append(self.p("home.state.without_project", stats.chats_without_project))
        if stats.chats_via_mapping:
            lines.append(self.p("home.state.via_mapping", stats.chats_via_mapping))
        lines.append(self.t("home.state.as_of", when=local_time(stats.computed_at_utc)))
        return text, lines, None

    def _tile_copies(self, summary):
        if not summary.copies_configured:
            return self.t("home.copies.no_folder"), [self.t("home.copies.how")], None
        copies = summary.copies
        if copies is None:
            return "", [], None
        if copies.count == 0:
            return self.t("home.copies.none"), [], "attention"
        return (
            self.join([self.p("home.copies.count", copies.count), human_size(copies.bytes)]),
            [self.t("home.copies.newest", when=local_time(copies.newest_utc))],
            None,
        )

    def _tile_backups(self, summary):
        backups = summary.backups
        if backups is None:
            return "", [], None
        if backups.count == 0:
            return self.t("home.backups.none"), [self.t("home.backups.how")], None
        return (
            self.join([self.p("home.backups.count", backups.count), human_size(backups.bytes)]),
            [self.t("home.backups.newest", when=local_time(backups.newest_utc))],
            None,
        )

    def _tile_guardian(self, summary):
        guardian = summary.guardian
        if guardian is None:
            return "", [], None
        if guardian.latest_good_utc is None:
            text, tone = self.t("home.guardian.no_latest"), "attention" if guardian.snapshots else None
        else:
            text, tone = self.t("home.guardian.latest", when=local_time(guardian.latest_good_utc)), None
        lines = [self.p("home.guardian.snapshots", guardian.snapshots)]
        if guardian.quarantined:
            lines.append(self.p("home.guardian.quarantined", guardian.quarantined))
        if guardian.problems:
            lines.append(self.p("home.guardian.problems", guardian.problems))
            tone = "attention"
        return text, lines, tone

    def _tile_automation(self, summary):
        view = summary.automation
        if view is None:
            return "", [], None
        on = []
        stale = False
        tasks = (
            (view.enabled, view.status, "home.automation.periodic"),
            (view.sync_at_login, view.login_status, "home.automation.sync_at_login"),
            (view.backup_scheduled, view.backup_status, "home.automation.copies"),
        )
        lines = []
        for wanted, status, key in tasks:
            installed = status is not None and status.installed
            if wanted:
                on.append(self.t(key))
            if wanted != installed or (installed and status.definition_matches is False):
                stale = True
        if not on:
            text = self.t("home.automation.none")
        else:
            text = self.p("home.automation.on", len(on))
            lines.append(self.join(on))
        if stale:
            lines.append(self.t("home.automation.not_applied"))
        return text, lines, "attention" if stale else None
