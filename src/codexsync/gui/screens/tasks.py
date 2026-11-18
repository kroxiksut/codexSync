"""The task list (CS-337): what runs now, from which page, and what ended lately.

Opened from the status bar's *Tasks…*. It draws `MainWindow.jobs()` and keeps
nothing of its own, so closing it loses nothing and two ways of looking at the
same work -- the status bar and this list -- cannot disagree.

A read can be stopped here, one at a time, exactly as the status bar's *Stop
waiting* stops all of them: the window drops its result, and the read itself,
which changes nothing, runs to its end unobserved. A job that may write has no
such button. A write that is merely no longer watched has not stopped, and a
button saying otherwise would be the one lie this window must not tell.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QHeaderView, QPushButton, QTableView, QVBoxLayout

from ..widgets import Cell, fill_table, label, table
from ..task_items import task_rows
from ...app import JournalState

if TYPE_CHECKING:  # pragma: no cover
    from ..window import MainWindow

#: Columns of the running list that change while a job runs, and the stop button's.
ELAPSED_COLUMN = 3
PROGRESS_COLUMN = 4
STOP_COLUMN = 5


class TasksDialog(QDialog):
    """A window beside the main one; it follows the job book as it changes."""

    def __init__(self, host: "MainWindow") -> None:
        super().__init__(host)
        self.host = host
        self._task_data = None
        self._last_item_progress = None
        self._was_transfer_running = False
        self._reload_after_load = False
        self.setModal(False)
        self.resize(800, 580)
        self._layout = QVBoxLayout(self)
        self.rebuild()

    def t(self, key: str, /, **params: object) -> str:
        return self.host.catalog.text(key, **params)

    def rebuild(self) -> None:
        """Build the widgets again, in the window's current language."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.setWindowTitle(self.t("tasks.title"))
        self._layout.addWidget(label(self.t("tasks.running"), "cardTitle"))
        self.running = table([
            self.t("tasks.column.what"),
            self.t("tasks.column.page"),
            self.t("tasks.column.started"),
            self.t("tasks.column.elapsed"),
            self.t("tasks.column.progress"),
            "",
        ], stretch=0)
        self.running.horizontalHeader().setSectionResizeMode(STOP_COLUMN, QHeaderView.Fixed)
        self._layout.addWidget(self.running, stretch=1)
        self.running_empty = label("", "muted", wrap=True)
        self._layout.addWidget(self.running_empty)
        self._layout.addWidget(label(self.t("tasks.write_note"), "muted", wrap=True))

        self._layout.addWidget(label(self.t("tasks.recent"), "cardTitle"))
        self.recent = table([
            self.t("tasks.column.what"),
            self.t("tasks.column.page"),
            self.t("tasks.column.started"),
            self.t("tasks.column.took"),
            self.t("tasks.column.result"),
        ], stretch=4)
        self._layout.addWidget(self.recent, stretch=1)
        self.recent_empty = label("", "muted", wrap=True)
        self._layout.addWidget(self.recent_empty)

        self._layout.addWidget(label(self.t("tasks.items.title"), "cardTitle"))
        self.load_items = QPushButton(self.t("tasks.items.load"))
        self.load_items.clicked.connect(self._load_items)
        self._layout.addWidget(self.load_items)
        self.items_status = label("", "muted", wrap=True)
        self._layout.addWidget(self.items_status)
        self.items_recovery = QPushButton(self.t("sync.link.recovery"))
        self.items_recovery.clicked.connect(self._open_recovery)
        self.items_recovery.setVisible(False)
        self._layout.addWidget(self.items_recovery)
        self.items_filter = QComboBox()
        for value in ("all", "applied", "not_applied", "blocked", "failed"):
            self.items_filter.addItem(self.t(f"tasks.items.filter.{value}"), value)
        self._layout.addWidget(self.items_filter)
        self.items_model = _ItemsModel(self)
        self.items_proxy = _ItemsFilter(self)
        self.items_proxy.setSourceModel(self.items_model)
        self.items_filter.currentIndexChanged.connect(self._filter_items)
        self.items_view = QTableView()
        self.items_view.setModel(self.items_proxy)
        self.items_view.horizontalHeader().setStretchLastSection(True)
        self._layout.addWidget(self.items_view, stretch=2)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(self.t("common.close"))
        buttons.rejected.connect(self.reject)
        self._layout.addWidget(buttons)
        #: token -> the row's stop button, rebuilt with the rows.
        self.stop_buttons: dict[Any, QPushButton] = {}
        self._drawn_tokens: list[Any] | None = None
        self.render()

    def _load_items(self) -> None:
        read = getattr(self.host.controller, "task_items", None)
        if read is None:
            return
        if not self.load_items.isEnabled():
            self._reload_after_load = True
            return
        self.load_items.setEnabled(False)

        def done(outcome) -> None:
            self.load_items.setEnabled(True)
            if outcome.ok and outcome.value is not None:
                self.items_status.setText("")
                self.set_task_data(*outcome.value)
            else:
                self._task_data = None
                self.items_model.set_rows(())
                self.items_recovery.setVisible(False)
                self.items_status.setText(self.t("tasks.items.unavailable"))
            if self._reload_after_load:
                self._reload_after_load = False
                self._load_items()

        self.host._jobs.start(read, done)

    def set_task_data(self, plan, journal) -> None:
        self._task_data = (plan, journal)
        self._last_item_progress = None
        state = journal.state.value if isinstance(journal.state, JournalState) else journal.state
        self.items_model.set_rows(task_rows(plan, journal.failure_params, completed=state == "COMMITTED"))
        running = any(job.kind == "full_sync" for job in self.host.jobs().running.values())
        recoverable = state == "RECOVERY_REQUIRED" or (state == "COMMITTING" and not running)
        self.items_recovery.setVisible(recoverable and bool(getattr(journal, "operation_id", None)))

    def _open_recovery(self) -> None:
        if self._task_data is not None:
            self.host.go_to("recovery", self._task_data[1].operation_id)

    def update_task_progress(self, progress) -> None:
        if self._task_data is None or progress == self._last_item_progress:
            return
        plan, journal = self._task_data
        state = journal.state.value if isinstance(journal.state, JournalState) else journal.state
        if state not in {"PREPARED", "BACKED_UP", "COMMITTING"}:
            return
        phase, done, total = progress
        if phase == "stage_chats":
            params = {"staged": done, "applied": 0}
        elif phase == "apply_chats":
            params = {"staged": total, "applied": done}
        elif phase == "delete_chat_files":
            count = len(plan.writable_items)
            params = {"staged": count, "applied": count}
        else:
            return
        self._last_item_progress = progress
        self.items_model.set_rows(task_rows(plan, params))

    def _filter_items(self) -> None:
        self.items_proxy.category = self.items_filter.currentData()
        self.items_proxy.invalidateFilter()

    def render(self) -> None:
        host = self.host
        book = host.jobs()
        palette = host.palette()
        running = list(book.running.items())
        transfer_running = any(job.kind == "full_sync" for _token, job in running)
        if self._was_transfer_running and not transfer_running and self._task_data is not None:
            self._load_items()
        self._was_transfer_running = transfer_running
        if self._task_data is not None:
            _plan, journal = self._task_data
            if getattr(journal, "machine_id", None) == host.machine_id():
                for _token, job in running:
                    if job.kind == "full_sync" and job.progress is not None:
                        self.update_task_progress(job.progress)
                        break
        tokens = [token for token, _job in running]
        if tokens != self._drawn_tokens:
            # Rebuilt only when the set of jobs changes: the list is redrawn
            # every second while anything runs, and a stop button replaced
            # under the pointer would swallow the click meant for it.
            self._drawn_tokens = tokens
            fill_table(self.running, [
                [
                    Cell(host.job_title(job.kind, job.page, dry_run=job.dry_run, cancellable=job.cancellable)),
                    Cell(self.t(f"nav.{job.page}")),
                    Cell(_clock(job.started_at), muted=True),
                    Cell(""),
                    Cell(""),
                    Cell(""),
                ]
                for _token, job in running
            ], palette)
            self.stop_buttons = {}
            width = 0
            for index, (token, job) in enumerate(running):
                if not job.cancellable:
                    continue
                stop = QPushButton(self.t("activity.cancel"))
                stop.setObjectName("statusAction")
                stop.clicked.connect(lambda _=False, token=token: self.host.cancel_job(token))
                self.running.setCellWidget(index, STOP_COLUMN, stop)
                self.running.setRowHeight(index, max(self.running.rowHeight(index), stop.sizeHint().height() + 4))
                self.stop_buttons[token] = stop
                width = max(width, stop.sizeHint().width() + 12)
            self.running.setColumnWidth(STOP_COLUMN, width)
        for index, (_token, job) in enumerate(running):
            self._set(index, ELAPSED_COLUMN, self.duration(book.seconds(job)))
            self._set(index, PROGRESS_COLUMN, host.job_progress_text(job) or self.t("tasks.progress.none"))
        self.running_empty.setText("" if running else self.t("tasks.running.none"))
        self.running_empty.setVisible(not running)

        recent = list(book.recent)
        fill_table(self.recent, [
            [
                Cell(host.job_title(job.kind, job.page, dry_run=job.dry_run, cancellable=job.cancellable)),
                Cell(self.t(f"nav.{job.page}")),
                Cell(_clock(job.started_at), muted=True),
                Cell(self.duration(int(job.seconds))),
                Cell(*self._result(job)),
            ]
            for job in recent
        ], palette)
        self.recent_empty.setText("" if recent else self.t("tasks.recent.none"))
        self.recent_empty.setVisible(not recent)

    def _set(self, row: int, column: int, text: str) -> None:
        item = self.running.item(row, column)
        if item is not None and item.text() != text:
            item.setText(text)
            item.setToolTip(text)

    def duration(self, seconds: int) -> str:
        if seconds < 60:
            return self.host.catalog.plural("common.seconds", seconds)
        return self.t("tasks.duration", minutes=seconds // 60, seconds=seconds % 60)

    def _result(self, job) -> tuple[str, str]:
        if job.abandoned:
            return self.t("tasks.result.abandoned"), "attention"
        if job.failure is None:
            return self.t("tasks.result.done"), "ok"
        text = self.t(f"failure.{job.failure.value}")
        if job.progress:
            text += " — " + self.host.phase_text(job.progress)
        return text, "danger"


def _clock(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%H:%M:%S")


class _ItemsModel(QAbstractTableModel):
    def __init__(self, dialog: TasksDialog) -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self.rows = ()

    def set_rows(self, rows) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else 5

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        item = self.rows[index.row()]
        state = self.dialog.t(f"tasks.items.state.{item.state}")
        direction = self.dialog.t(f"tasks.items.direction.{item.direction}") if item.direction else "—"
        return (item.title or "—", item.session_id or "—", direction, item.path or "—", state)[index.column()]

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.dialog.t(f"tasks.items.column.{('title', 'session', 'direction', 'path', 'state')[section]}")
        return None


class _ItemsFilter(QSortFilterProxyModel):
    category = "all"

    def filterAcceptsRow(self, row, parent) -> bool:
        state = self.sourceModel().rows[row].state
        if self.category == "all":
            return True
        if self.category == "not_applied":
            return state != "applied"
        return state == self.category
