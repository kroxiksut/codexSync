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

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHeaderView, QPushButton, QVBoxLayout

from ..widgets import Cell, fill_table, label, table

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

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText(self.t("common.close"))
        buttons.rejected.connect(self.reject)
        self._layout.addWidget(buttons)
        #: token -> the row's stop button, rebuilt with the rows.
        self.stop_buttons: dict[Any, QPushButton] = {}
        self._drawn_tokens: list[Any] | None = None
        self.render()

    def render(self) -> None:
        host = self.host
        book = host.jobs()
        palette = host.palette()
        running = list(book.running.items())
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
            self._set(index, PROGRESS_COLUMN, host.phase_text(job.progress) or self.t("tasks.progress.none"))
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
        return self.t(f"failure.{job.failure.value}"), "danger"


def _clock(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%H:%M:%S")
