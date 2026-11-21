"""The task list (CS-337): what runs now, from which page, and what ended lately.

Opened from the status bar's *Tasks…*. It draws `MainWindow.jobs()` and keeps
nothing of its own, so closing it loses nothing and two ways of looking at the
same work -- the status bar and this list -- cannot disagree.

Stop waiting drops a read's result. Cooperative Stop requests cancellation
and keeps the job visible until the worker reports its outcome.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QHeaderView,
    QHBoxLayout, QPushButton, QTableView, QTableWidget, QVBoxLayout, QWidget,
)

from ..widgets import Cell, fill_table, label, table
from ..task_items import task_rows
from ..theme import TABLE_CELL_BORDER, TABLE_CELL_HORIZONTAL_PADDING, TABLE_CELL_VERTICAL_PADDING
from ...app import JournalState
from ...cancellation import CancellationState
from ...progress_log import progress_record

if TYPE_CHECKING:  # pragma: no cover
    from ..window import MainWindow

#: Columns whose layout or text can change while the dialog is open.
PAGE_COLUMN = 1
STARTED_COLUMN = 2
ELAPSED_COLUMN = 3
PROGRESS_COLUMN = 4
ACTIONS_COLUMN = 5


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
        self.setMinimumWidth(640)
        self.resize(800, 580)
        self._layout = QVBoxLayout(self)
        self._resize_rows = QTimer(self)
        self._resize_rows.setSingleShot(True)
        self._resize_rows.timeout.connect(self._fit_tables)
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
        ], stretch=PROGRESS_COLUMN)
        self.running.horizontalHeader().setSectionResizeMode(ACTIONS_COLUMN, QHeaderView.Fixed)
        self._wrap_table(self.running)
        self._layout.addWidget(self.running, stretch=1)
        self.running_empty = label("", "muted", wrap=True)
        self._layout.addWidget(self.running_empty)
        self.write_note = label(self.t("tasks.write_note"), "muted", wrap=True)
        self._layout.addWidget(self.write_note)

        self._layout.addWidget(label(self.t("tasks.recent"), "cardTitle"))
        self.recent = table([
            self.t("tasks.column.what"),
            self.t("tasks.column.page"),
            self.t("tasks.column.started"),
            self.t("tasks.column.took"),
            self.t("tasks.column.result"),
        ], stretch=4)
        self._wrap_table(self.recent)
        self._layout.addWidget(self.recent, stretch=1)
        self.recent_empty = label("", "muted", wrap=True)
        self._layout.addWidget(self.recent_empty)

        self._layout.addWidget(label(self.t("tasks.items.title"), "cardTitle"))
        item_panel = QWidget()
        item_controls = QHBoxLayout(item_panel)
        item_controls.setContentsMargins(0, 0, 0, 0)
        self.load_items = QPushButton(self.t("tasks.items.load"))
        self.load_items.clicked.connect(self._load_items)
        item_controls.addWidget(self.load_items, stretch=1)
        self.items_filter = QComboBox()
        for value in ("all", "applied", "not_applied", "blocked", "failed"):
            self.items_filter.addItem(self.t(f"tasks.items.filter.{value}"), value)
        item_controls.addWidget(self.items_filter)
        self._layout.addWidget(item_panel)
        self.items_status = label("", "muted", wrap=True)
        self.items_status.hide()
        self._layout.addWidget(self.items_status)
        self.items_recovery = QPushButton(self.t("sync.link.recovery"))
        self.items_recovery.clicked.connect(self._open_recovery)
        self.items_recovery.setVisible(False)
        self._layout.addWidget(self.items_recovery)
        self.items_model = _ItemsModel(self)
        self.items_proxy = _ItemsFilter(self)
        self.items_proxy.setSourceModel(self.items_model)
        self.items_filter.currentIndexChanged.connect(self._filter_items)
        self.items_view = QTableView()
        self.items_view.setModel(self.items_proxy)
        self.items_view.horizontalHeader().setStretchLastSection(True)
        self._layout.addWidget(self.items_view, stretch=2)

        toggle_events = QPushButton(self.t("tasks.events.title"))
        toggle_events.setCheckable(True)
        self._layout.addWidget(toggle_events)
        event_panel = QWidget()
        event_layout = QVBoxLayout(event_panel)
        event_layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.events_filter = QComboBox()
        for value in ("all", "errors", "applied", "pending"):
            self.events_filter.addItem(self.t(f"tasks.events.filter.{value}"), value)
        controls.addWidget(self.events_filter)
        self.copy_events = QPushButton(self.t("tasks.events.copy"))
        self.copy_events.clicked.connect(self._copy_events)
        controls.addWidget(self.copy_events)
        open_events = QPushButton(self.t("tasks.events.open"))
        open_events.clicked.connect(self._open_events)
        controls.addWidget(open_events)
        event_layout.addLayout(controls)
        self.events_model = _EventsModel(self)
        self.events_proxy = _EventsFilter(self)
        self.events_proxy.setSourceModel(self.events_model)
        self.events_filter.currentIndexChanged.connect(self._filter_events)
        events_view = QTableView()
        events_view.setModel(self.events_proxy)
        events_view.horizontalHeader().setStretchLastSection(True)
        event_layout.addWidget(events_view)
        self.events_status = label("", "muted", wrap=True)
        event_layout.addWidget(self.events_status)
        self._layout.addWidget(event_panel, stretch=2)
        event_panel.setVisible(False)
        toggle_events.toggled.connect(event_panel.setVisible)
        self._event_revision = -1

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(self.t("common.close"))
        buttons.rejected.connect(self.reject)
        self._layout.addWidget(buttons)
        #: token -> the row's stop button, rebuilt with the rows.
        self.stop_buttons: dict[Any, QPushButton] = {}
        self.finish_buttons: dict[Any, QPushButton] = {}
        self.rollback_buttons: dict[Any, QPushButton] = {}
        self._controls: dict[Any, QWidget] = {}
        self._drawn_tokens: list[Any] | None = None
        self._drawn_recent = None
        self.render()

    def _wrap_table(self, widget: QTableWidget) -> None:
        widget.setWordWrap(True)
        widget.setTextElideMode(Qt.ElideNone)
        widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        header = widget.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.geometriesChanged.connect(self._resize_rows.start)
        header.sectionResized.connect(lambda *_: self._resize_rows.start())

    def _fit_tables(self) -> None:
        for widget in (self.running, self.recent):
            if not widget.rowCount():
                continue
            header = widget.horizontalHeader()
            if widget is self.running:
                self._fit_running_metadata()
            # Give the live progress/result the space left over after the
            # short columns. Long operation names wrap without widening it.
            title_width = min(widget.sizeHintForColumn(0), widget.width() // 5)
            widget.setColumnWidth(0, max(header.sectionSizeHint(0), title_width))
            widget.resizeRowsToContents()
            if widget is self.running:
                for row in range(widget.rowCount()):
                    cell = widget.cellWidget(row, ACTIONS_COLUMN)
                    if cell is not None:
                        height = cell.sizeHint().height() + 2 * TABLE_CELL_VERTICAL_PADDING + TABLE_CELL_BORDER
                        widget.setRowHeight(row, max(widget.rowHeight(row), height))
            # The details views share the available space. Keep the first task
            # readable even when the journal or transfer details are open.
            widget.setMinimumHeight(header.height() + widget.rowHeight(0) + 2 * widget.frameWidth())

    def _fit_running_metadata(self) -> None:
        # On a narrow window, put the page and start time under the title so
        # they do not compete with progress or the operation's actions.
        compact = self.running.width() < 720
        self.running.setColumnHidden(PAGE_COLUMN, compact)
        self.running.setColumnHidden(STARTED_COLUMN, compact)
        for row in range(self.running.rowCount()):
            title = self.running.item(row, 0).data(Qt.UserRole)
            if compact:
                title = self.t(
                    "tasks.job_details", job=title,
                    page=self.running.item(row, PAGE_COLUMN).text(),
                    started=self.running.item(row, STARTED_COLUMN).text(),
                )
            self._set(row, 0, title)

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
                self.items_status.hide()
                self.set_task_data(*outcome.value)
            else:
                self._task_data = None
                self.items_model.set_rows(())
                self.items_recovery.setVisible(False)
                self.items_status.setText(self.t("tasks.items.unavailable"))
                self.items_status.show()
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

    def _filter_events(self) -> None:
        self.events_proxy.category = self.events_filter.currentData()
        self.events_proxy.invalidate()

    def _copy_events(self) -> None:
        QApplication.clipboard().setText("".join(progress_record(event) + "\n" for event in self.events_model.rows))

    def _open_events(self) -> None:
        read = getattr(self.host.controller, "progress_logs", None)
        if read is None:
            return

        def done(outcome) -> None:
            path = outcome.value if outcome.ok else None
            if path is not None and path.is_dir() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self.events_status.clear()
            else:
                self.events_status.setText(self.t("tasks.events.unavailable"))

        self.host._jobs.start(read, done)

    def render(self) -> None:
        host = self.host
        book = host.jobs()
        if self._event_revision != book.events_revision:
            self.events_model.set_rows(tuple(book.events))
            self.copy_events.setEnabled(bool(book.events))
            self._event_revision = book.events_revision
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
            rows = []
            for _token, job in running:
                title = host.job_title(job.kind, job.page, dry_run=job.dry_run, cancellable=job.cancellable)
                rows.append([
                    Cell(title, data=title),
                    Cell(self.t(f"nav.{job.page}")),
                    Cell(_clock(job.started_at), muted=True),
                    Cell(""),
                    Cell(""),
                    Cell(""),
                ])
            # Drop the previous cell widgets too, since a remaining job may
            # move into a row that used to offer another job's stop action.
            self.running.setRowCount(0)
            fill_table(self.running, rows, palette)
            self.stop_buttons = {}
            self.finish_buttons = {}
            self.rollback_buttons = {}
            self._controls = {}
            for index, (token, job) in enumerate(running):
                if not job.cancellable and job.cancellation is None:
                    continue
                cell = QWidget()
                controls = QVBoxLayout(cell)
                controls.setContentsMargins(4, 4, 4, 4)
                controls.setSpacing(4)
                controls.setAlignment(Qt.AlignVCenter)
                stop = QPushButton(self.t("activity.cancel" if job.cancellable else "tasks.cancellation.stop"))
                stop.setObjectName("statusAction")
                stop.clicked.connect(lambda _=False, token=token: self.host.cancel_job(token))
                controls.addWidget(stop)
                self.running.setCellWidget(index, ACTIONS_COLUMN, cell)
                self.stop_buttons[token] = stop
                self._controls[token] = cell
                if job.cancellation is not None:
                    finish = QPushButton(self.t("tasks.cancellation.finish"))
                    finish.clicked.connect(self.accept)
                    controls.addWidget(finish)
                    self.finish_buttons[token] = finish
        width = 0
        for index, (token, job) in enumerate(running):
            cell = self._controls.get(token)
            if job.cancellation is not None:
                state = job.cancellation.state
                self.stop_buttons[token].setEnabled(state == CancellationState.PREPARING)
                self.stop_buttons[token].setVisible(state == CancellationState.PREPARING)
                self.finish_buttons[token].setVisible(state == CancellationState.COMMITTING)
                rollback = self.rollback_buttons.get(token)
                if job.cancellation.can_rollback and rollback is None:
                    rollback = QPushButton(self.t("tasks.cancellation.rollback"))
                    rollback.setObjectName("danger")
                    rollback.clicked.connect(lambda _=False, token=token: self._confirm_rollback(token))
                    cell.layout().addWidget(rollback)
                    self.rollback_buttons[token] = rollback
                if rollback is not None:
                    rollback.setVisible(job.cancellation.can_rollback)
            if cell is not None and not cell.layout().isEmpty():
                width = max(width, cell.sizeHint().width())
            self._set(index, ELAPSED_COLUMN, host.catalog.duration(book.seconds(job)))
            progress = host.job_progress_text(job, multiline=True) or self.t("tasks.progress.none")
            self._set(index, PROGRESS_COLUMN, progress)
        self.running.setColumnHidden(ACTIONS_COLUMN, width == 0)
        if width:
            self.running.setColumnWidth(ACTIONS_COLUMN, width + 2 * TABLE_CELL_HORIZONTAL_PADDING)
        self.running.setVisible(bool(running))
        self.running_empty.setText("" if running else self.t("tasks.running.none"))
        self.running_empty.setVisible(not running)
        self.write_note.setVisible(any(button.isVisibleTo(self.running) for button in self.stop_buttons.values()))

        recent = list(book.recent)
        if recent != self._drawn_recent:
            self._drawn_recent = recent
            fill_table(self.recent, [
                [
                    Cell(host.job_title(job.kind, job.page, dry_run=job.dry_run, cancellable=job.cancellable)),
                    Cell(self.t(f"nav.{job.page}")),
                    Cell(_clock(job.started_at), muted=True),
                    Cell(host.catalog.duration(int(job.seconds))),
                    Cell(*self._result(job)),
                ]
                for job in recent
            ], palette)
            self.recent.setVisible(bool(recent))
            self.recent_empty.setText("" if recent else self.t("tasks.recent.none"))
            self.recent_empty.setVisible(not recent)
        self._resize_rows.start()

    def _confirm_rollback(self, token: Any) -> None:
        if self.host.confirm(
            self.t("tasks.cancellation.rollback"),
            self.t("tasks.cancellation.confirm_rollback"),
            self.t("tasks.cancellation.rollback"),
        ):
            self.host.rollback_job(token)

    def _set(self, row: int, column: int, text: str) -> None:
        item = self.running.item(row, column)
        if item is not None and item.text() != text:
            item.setText(text)
            item.setToolTip(text)

    def _result(self, job) -> tuple[str, str]:
        if job.abandoned:
            return self.t("tasks.result.abandoned"), "attention"
        if job.failure is None:
            return self.t("tasks.result.done"), "ok"
        if job.code == "OPERATION_CANCELLED":
            return self.t("tasks.events.status.cancelled"), "attention"
        if job.code == "OPERATION_ROLLED_BACK":
            return self.t("tasks.events.status.rolled_back"), "attention"
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


class _EventsModel(QAbstractTableModel):
    _columns = ("time", "phase", "direction", "object", "bytes", "result")

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
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        row = self.rows[index.row()]
        return (
            _clock(row.at) if row.at is not None else "—",
            self.dialog.t(f"progress.phase.{row.phase}"),
            self.dialog.t(f"tasks.events.direction.{row.direction}") if row.direction else "—",
            row.item_id or row.relative_path or "—",
            str(row.item_bytes) if row.item_bytes is not None else "—",
            self.dialog.t(f"tasks.events.status.{row.status}"),
        )[index.column()]

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.dialog.t(f"tasks.events.column.{self._columns[section]}")
        return None


class _EventsFilter(QSortFilterProxyModel):
    category = "all"

    def filterAcceptsRow(self, row, parent) -> bool:
        status = self.sourceModel().rows[row].status
        return self.category == "all" or status == ("failed" if self.category == "errors" else self.category)
