"""The state screen: what the environment looks like, and whether anything may run.

This is the only module in the package that imports Qt, and it holds no
knowledge of its own. It asks the controller a question, waits, and draws the
answer; every judgement in it -- what passed, what warned, whether Codex is
closed -- comes back from `run_preflight`, the same diagnostic `doctor` prints.

The indicator at the top is deliberately large and deliberately advisory. It
tells the user why the rest of the application will or will not let them do
anything, but nothing rests on it: a mutation is refused inside `app.py`
against a check taken at the moment of the write, so a stale green banner
cannot authorise anything.

There is no refresh timer. A reading that quietly replaced itself would hide
the one event the user most needs to notice -- the moment Codex opened or
closed underneath them -- behind an animation they did not ask for.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .controller import Controller, Failure, Outcome

#: Status colours chosen to stay legible on a light and a dark palette alike.
#: PASS takes the palette's own text colour rather than a green, so a healthy
#: report reads as ordinary rather than as a row demanding attention.
_STATUS_COLOURS = {
    "FAIL": QColor("#c0392b"),
    "WARN": QColor("#b8860b"),
}

_FAILURE_HEADLINES = {
    Failure.CONFIGURATION: "The configuration cannot be used",
    Failure.CODEX_NOT_STOPPED: "Codex is open, or its state could not be determined",
    Failure.NEEDS_A_DECISION: "This needs a decision only you can make",
    Failure.STOPPED_SAFELY: "The operation stopped without changing anything",
    Failure.UNEXPECTED: "Something went wrong that should not have",
}


class _JobSignals(QObject):
    finished = Signal(object)


class _Job(QRunnable):
    """One controller call, off the UI thread.

    The safety check is *not* moved here. It stays where `app.py` performs it,
    immediately before the commit, so a Codex that starts while this job is
    running still stops the write.
    """

    def __init__(self, call: Callable[[], Outcome]) -> None:
        super().__init__()
        self._call = call
        self.signals = _JobSignals()

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised only with a running pool
        self.signals.finished.emit(self._call())


class StateWindow(QMainWindow):
    """Everything the first screen shows."""

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self._controller = controller
        self._pool = QThreadPool.globalInstance()
        self._busy = False

        self.setWindowTitle("codexSync")
        self.resize(940, 560)

        self._banner = QLabel()
        banner_font = QFont()
        banner_font.setPointSize(15)
        banner_font.setBold(True)
        self._banner.setFont(banner_font)
        self._banner.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self._refresh = QPushButton("Refresh")
        self._refresh.clicked.connect(self.refresh)

        header = QHBoxLayout()
        header.addWidget(self._banner, stretch=1)
        header.addWidget(self._refresh, alignment=Qt.AlignRight | Qt.AlignVCenter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Check", "Status", "Details"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setWordWrap(False)
        headers = self._table.horizontalHeader()
        headers.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        headers.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        headers.setSectionResizeMode(2, QHeaderView.Stretch)

        body = QVBoxLayout()
        body.addLayout(header)
        body.addWidget(self._table, stretch=1)

        central = QWidget()
        central.setLayout(body)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(str(controller.config_path))

    # --- loading ---------------------------------------------------------

    def refresh(self) -> None:
        """Ask for a fresh reading, unless one is already on its way."""
        if self._busy:
            return
        self._busy = True
        self._refresh.setEnabled(False)
        self._banner.setText("Reading the environment…")
        job = _Job(self._controller.state)
        job.signals.finished.connect(self.show_outcome)
        self._pool.start(job)

    @Slot(object)
    def show_outcome(self, outcome: Outcome) -> None:
        self._busy = False
        self._refresh.setEnabled(True)
        if outcome.ok:
            self._show_state(outcome.value)
        else:
            self._show_failure(outcome)

    def _show_state(self, view) -> None:
        if view.codex_looks_stopped:
            self._banner.setText("Codex is closed — changes are allowed")
            self._banner.setStyleSheet("")
        else:
            # One sentence for two situations, because they permit the same
            # thing: nothing. An undetermined process is never read as a
            # stopped one.
            self._banner.setText("Codex is open or undetermined — changes are refused")
            self._banner.setStyleSheet(f"color: {_STATUS_COLOURS['WARN'].name()};")
        self._fill(view.checks)
        self.statusBar().showMessage(
            f"{self._controller.config_path}   —   "
            f"{len(view.checks)} checks, {view.warnings} warning(s), {view.failures} failure(s)"
        )

    def _show_failure(self, outcome: Outcome) -> None:
        self._banner.setText(_FAILURE_HEADLINES[outcome.failure])
        self._banner.setStyleSheet(f"color: {_STATUS_COLOURS['FAIL'].name()};")
        self._table.setRowCount(0)
        self.statusBar().showMessage(outcome.message)

    def _fill(self, checks) -> None:
        self._table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            colour = _STATUS_COLOURS.get(check.status)
            for column, text in enumerate((check.name, check.status, check.details)):
                item = QTableWidgetItem(text)
                item.setToolTip(check.details)
                if colour is not None:
                    item.setForeground(colour)
                self._table.setItem(row, column, item)


def launch(controller: Controller) -> int:
    """Show the window and run until it closes.

    Reuses an existing ``QApplication`` when there is one, so a host that
    already owns the event loop is not handed a second one.
    """
    app = QApplication.instance() or QApplication([])
    window = StateWindow(controller)
    window.show()
    window.refresh()
    return int(app.exec())
