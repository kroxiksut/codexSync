"""Qt building blocks every screen shares.

Nothing here knows what codexSync does. It is the vocabulary screens are drawn
in -- a card, a banner, a table, a confirmation -- plus the one piece of
plumbing that must be exactly right everywhere: moving a controller call off
the UI thread and bringing its result back onto it.

That plumbing is centralised for a reason. A PySide signal connected to a plain
Python callable runs the callable in the thread that emitted it, so a result
delivered that way would touch widgets from a worker thread -- a crash that
only shows up under load. `JobRunner` is a QObject living on the UI thread, and
results are delivered to its slot, which Qt queues onto that thread.
"""
from __future__ import annotations

from dataclasses import dataclass
import itertools
import time
from typing import Any, Callable, Iterable, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .controller import Outcome

#: A long read reports once per file; the window is redrawn far less often.
PROGRESS_INTERVAL_MS = 100


# --- jobs --------------------------------------------------------------------


class _JobSignals(QObject):
    finished = Signal(object)
    progress = Signal(object)


class _Job(QRunnable):
    """One controller call, off the UI thread.

    The safety check is *not* moved here. It stays where `app.py` performs it,
    immediately before the commit, so a Codex that starts while this job is
    running still stops the write.

    A call that accepts one reports how far it has got. The report crosses to
    the UI thread as a signal like the result does, and is thinned to one every
    `PROGRESS_INTERVAL_MS`: a session scan calls back once per file, hundreds of
    times a second, and queueing all of that would make the window slower than
    the work it is describing.
    """

    def __init__(self, token: int, call: Callable[..., Outcome], *, with_progress: bool = False) -> None:
        super().__init__()
        self._token = token
        self._call = call
        self._with_progress = with_progress
        self._last_report = 0.0
        self.signals = _JobSignals()

    def _report(self, phase: str, done: int, total: int) -> None:  # pragma: no cover - worker thread
        now = time.monotonic() * 1000
        finished = total and done >= total
        if not finished and now - self._last_report < PROGRESS_INTERVAL_MS:
            return
        self._last_report = now
        try:
            self.signals.progress.emit((self._token, phase, done, total))
        except RuntimeError:
            pass

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised only with a running pool
        outcome = self._call(progress=self._report) if self._with_progress else self._call()
        try:
            self.signals.finished.emit((self._token, outcome))
        except RuntimeError:
            # The window closed while this ran; there is nobody left to tell.
            # The core finished or refused on its own terms either way.
            pass


class JobRunner(QObject):
    """Starts jobs and delivers each result on the UI thread, exactly once."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._tokens = itertools.count(1)
        self._pending: dict[int, tuple[_Job, Callable[[Outcome], None]]] = {}
        self._abandoned: dict[int, _Job] = {}
        self._watchers: dict[int, Callable[[str, int, int], None]] = {}

    def start(
        self,
        call: Callable[..., Outcome],
        done: Callable[[Outcome], None],
        progress: Callable[[str, int, int], None] | None = None,
    ) -> int:
        token = next(self._tokens)
        job = _Job(token, call, with_progress=progress is not None)
        job.setAutoDelete(False)
        job.signals.finished.connect(self._deliver)
        if progress is not None:
            job.signals.progress.connect(self._report)
            self._watchers[token] = progress
        # Held until delivery: a QRunnable collected mid-run takes its signals
        # object with it and the result never arrives.
        self._pending[token] = (job, done)
        self._pool.start(job)
        return token

    @Slot(object)
    def _report(self, payload: object) -> None:
        """A progress report, now on the UI thread."""
        token, phase, done, total = payload  # type: ignore[misc]
        watcher = self._watchers.get(token)
        if watcher is not None and token in self._pending:
            watcher(phase, done, total)

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def abandon(self, token: int) -> bool:
        """Stop waiting for a job. The job itself runs to its end; its result is dropped.

        There is no way to interrupt a thread safely, and none is needed for
        the jobs this is offered for: a read-only scan finishing unobserved
        changes nothing. The job object stays referenced until it finishes, or
        Qt would delete it under the running thread.
        """
        entry = self._pending.pop(token, None)
        if entry is None:
            return False
        self._abandoned[token] = entry[0]
        self._watchers.pop(token, None)
        return True

    @Slot(object)
    def _deliver(self, payload: object) -> None:
        token, outcome = payload  # type: ignore[misc]
        self._abandoned.pop(token, None)
        self._watchers.pop(token, None)
        entry = self._pending.pop(token, None)
        if entry is not None:
            entry[1](outcome)


# --- text ----------------------------------------------------------------------


def label(text: str = "", name: str | None = None, *, wrap: bool = False, selectable: bool = True) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def command(text: str) -> QLabel:
    """A command or an id the user may copy: monospace, selectable."""
    widget = label(text, "command", wrap=True)
    return widget


def set_tone(widget: QLabel, tone: str | None, palette: theme.Palette) -> None:
    """Colour a line of text by tone, or give it back the palette's own colour."""
    if tone is None:
        widget.setStyleSheet("")
    else:
        widget.setStyleSheet(f"color: {theme.tone_colour(tone, palette)};")


# --- containers -----------------------------------------------------------------


def card(title: str | None = None, summary: QLabel | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 16, 22, 18)
    layout.setSpacing(10)
    if title is not None or summary is not None:
        header = QHBoxLayout()
        header.addWidget(label(title or "", "cardTitle"), stretch=1)
        if summary is not None:
            summary.setObjectName("cardSummary")
            header.addWidget(summary)
        layout.addLayout(header)
    return frame, layout


def row(*widgets: QWidget, stretch_last: bool = True, spacing: int = 10) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch_last:
        layout.addStretch(1)
    return layout


def button(text: str, *, primary: bool = False, danger: bool = False) -> QPushButton:
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    elif danger:
        widget.setObjectName("danger")
    return widget


class Banner(QFrame):
    """A tinted strip with a dot, a headline and a sentence underneath."""

    def __init__(self, *, actions_below: bool = False) -> None:
        super().__init__()
        self.setObjectName("banner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 14, 12)
        layout.setSpacing(12)
        self.dot = label("●", "bannerDot", selectable=False)
        layout.addWidget(self.dot, alignment=Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.title = label("", "bannerTitle", wrap=True)
        self.detail = label("", wrap=True)
        texts.addWidget(self.title)
        texts.addWidget(self.detail)
        layout.addLayout(texts, stretch=1)
        self.actions = QHBoxLayout()
        if actions_below:
            # A long headline (a file path) keeps the full width and the
            # buttons never get squeezed beside it in a narrow window.
            self.actions.setContentsMargins(0, 6, 0, 0)
            self.actions.addStretch(1)
            texts.addLayout(self.actions)
        else:
            layout.addLayout(self.actions)

    def show_message(self, tone: str, title: str, detail: str, palette: theme.Palette) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.setStyleSheet(theme.banner_style(tone, palette))
        # A neutral strip is a caption, not a status: a dot would claim one.
        self.dot.setVisible(tone != "neutral" and bool(title))
        # A banner that carries a button stays, or the button would vanish with it.
        self.setVisible(bool(title or detail) or any(
            self.actions.itemAt(index).widget() is not None for index in range(self.actions.count())
        ))


# --- tables ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cell:
    """One table cell: text, an optional tone, and what the row carries."""

    text: str
    tone: str | None = None
    tooltip: str | None = None
    muted: bool = False
    data: Any = None


def table(headers: Sequence[str], *, stretch: int | None = None, selectable: bool = False, multi: bool = False) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(list(headers))
    widget.verticalHeader().setVisible(False)
    widget.setShowGrid(False)
    widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
    widget.setWordWrap(False)
    widget.setAlternatingRowColors(False)
    if selectable:
        widget.setSelectionBehavior(QAbstractItemView.SelectRows)
        widget.setSelectionMode(
            QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection
        )
    else:
        widget.setSelectionMode(QAbstractItemView.NoSelection)
        widget.setFocusPolicy(Qt.NoFocus)
    headers_view = widget.horizontalHeader()
    headers_view.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    headers_view.setHighlightSections(False)
    last = len(headers) - 1 if stretch is None else stretch
    for column in range(len(headers)):
        mode = QHeaderView.Stretch if column == last else QHeaderView.ResizeToContents
        headers_view.setSectionResizeMode(column, mode)
    return widget


def fill_table(widget: QTableWidget, rows: Iterable[Sequence[Cell | str]], palette: theme.Palette) -> None:
    rows = list(rows)
    widget.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, cell in enumerate(cells):
            if isinstance(cell, str):
                cell = Cell(cell)
            item = QTableWidgetItem(cell.text)
            if cell.tooltip or cell.text:
                item.setToolTip(cell.tooltip or cell.text)
            if cell.tone is not None:
                item.setForeground(QColor(theme.tone_colour(cell.tone, palette)))
            elif cell.muted:
                item.setForeground(QColor(palette.muted_text))
            if cell.data is not None:
                item.setData(Qt.UserRole, cell.data)
            widget.setItem(r, c, item)


def selected_data(widget: QTableWidget, column: int = 0) -> list[Any]:
    """What the selected rows carry, in table order."""
    rows = sorted({index.row() for index in widget.selectionModel().selectedRows()})
    values = []
    for r in rows:
        item = widget.item(r, column)
        if item is not None:
            values.append(item.data(Qt.UserRole))
    return values


# --- inputs ------------------------------------------------------------------------


class PathField(QWidget):
    """A path the user can type or pick. Picking never creates anything."""

    def __init__(
        self, browse_text: str, *, directory: bool = True, any_file: bool = False,
        dialog_title: str = "", accept_text: str = "",
    ) -> None:
        super().__init__()
        self._directory = directory
        #: The file may exist or not yet exist. This is never a "save" dialog:
        #: that one is titled and buttoned "Save" and asks to replace a file
        #: that exists, when picking an existing file here means opening it.
        self._any_file = any_file
        self._dialog_title = dialog_title
        self._accept_text = accept_text
        #: Turns what the dialog returned into what the field holds, e.g. a
        #: parent folder into the not-yet-existing folder inside it.
        self.picked = None
        #: Called with the field's new text after the user picked something.
        self.chosen = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = QLineEdit()
        layout.addWidget(self.edit, stretch=1)
        self.browse = QPushButton(browse_text)
        self.browse.clicked.connect(self._pick)
        layout.addWidget(self.browse)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:  # noqa: N802 - mirrors QLineEdit
        self.edit.setText(value)

    def _pick(self) -> None:  # pragma: no cover - opens a native dialog
        start = self.text()
        if self._directory:
            chosen = QFileDialog.getExistingDirectory(self, self._dialog_title, start)
        elif self._any_file:
            dialog = QFileDialog(self, self._dialog_title, start, "TOML (*.toml)")
            # AcceptSave is the only mode that lets a name that does not exist
            # yet through; the overwrite question and the "Save" caption are
            # what made picking an existing config look like replacing it.
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
            dialog.setDefaultSuffix("toml")
            if self._accept_text:
                dialog.setLabelText(QFileDialog.DialogLabel.Accept, self._accept_text)
            chosen = dialog.selectedFiles()[0] if dialog.exec() and dialog.selectedFiles() else ""
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, self._dialog_title, start, "TOML (*.toml)")
        if chosen:
            self.take(chosen)

    def take(self, chosen: str) -> None:
        """Put what a dialog returned into the field, as if the user picked it."""
        self.edit.setText(self.picked(chosen) if self.picked is not None else chosen)
        if self.chosen is not None:
            self.chosen(self.text())


class PathTreeDialog(QDialog):
    """Pick `include_roots` from the two state directories, one level at a time.

    The listing comes from the core (`app.list_sync_candidates`) -- the window
    never opens `.codex` itself -- and arrives already stripped of anything
    holding a token, so there is no checkbox to tick by mistake. What the core
    marks as semantic-owned is shown and disabled with its reason beside it:
    such a path is not copied by `sync` at all, and hiding it would leave
    someone hunting for `sessions` in a list where it cannot appear.

    Every sentence is passed in. A widget module that knew one would be a
    second place strings live.
    """

    #: Marks a node whose children have not been listed yet.
    UNREAD = "codexsync.unread"

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        list_children: Callable[[str], Iterable[Any]],
        selected: Iterable[str],
        texts: dict,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 520)
        self._list_children = list_children
        self._texts = texts
        self._selected: set[str] = {str(item).strip() for item in selected if str(item).strip()}

        layout = QVBoxLayout(self)
        layout.addWidget(label(texts["hint"], wrap=True))
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([texts["column.path"], texts["column.note"]])
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemChanged.connect(self._toggled)
        layout.addWidget(self.tree, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(texts["ok"])
        buttons.button(QDialogButtonBox.Cancel).setText(texts["cancel"])
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill(self.tree.invisibleRootItem(), "")

    # --- building ----------------------------------------------------------

    def _fill(self, parent: Any, relative: str) -> None:
        blocked = self.tree.blockSignals(True)
        try:
            for candidate in self._list_children(relative):
                item = QTreeWidgetItem(parent)
                item.setText(0, candidate.name)
                item.setData(0, Qt.UserRole, candidate.relative)
                item.setText(1, self._note(candidate))
                if candidate.semantic_owned:
                    item.setFlags(Qt.ItemIsEnabled)
                else:
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(0, self._state(candidate.relative))
                if candidate.is_dir:
                    QTreeWidgetItem(item).setData(0, Qt.UserRole, self.UNREAD)
        finally:
            self.tree.blockSignals(blocked)

    def _note(self, candidate: Any) -> str:
        parts = []
        if candidate.semantic_owned:
            parts.append(self._texts["semantic"])
        if candidate.excluded_by_glob:
            parts.append(self._texts["excluded"])
        if not candidate.local:
            parts.append(self._texts["cloud_only"])
        elif not candidate.cloud:
            parts.append(self._texts["local_only"])
        return self._texts["separator"].join(parts)

    def _state(self, relative: str) -> Qt.CheckState:
        if relative in self._selected:
            return Qt.Checked
        prefix = relative + "/"
        if any(chosen.startswith(prefix) for chosen in self._selected):
            return Qt.PartiallyChecked
        return Qt.Unchecked

    def _expand(self, item: QTreeWidgetItem) -> None:
        first = item.child(0)
        if first is None or first.data(0, Qt.UserRole) != self.UNREAD:
            return
        item.removeChild(first)
        self._fill(item, item.data(0, Qt.UserRole))

    def _toggled(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        relative = item.data(0, Qt.UserRole)
        if not relative or relative == self.UNREAD:
            return
        if item.checkState(0) == Qt.Checked:
            # Choosing a folder covers everything under it; keeping a child
            # beside its parent would write the same root twice.
            prefix = relative + "/"
            self._selected = {
                chosen for chosen in self._selected if not chosen.startswith(prefix)
            }
            self._selected.add(relative)
        else:
            self._selected.discard(relative)
        self._refresh_states()

    def _refresh_states(self) -> None:
        blocked = self.tree.blockSignals(True)
        try:
            stack = [self.tree.invisibleRootItem().child(i)
                     for i in range(self.tree.invisibleRootItem().childCount())]
            while stack:
                item = stack.pop()
                relative = item.data(0, Qt.UserRole)
                if relative and relative != self.UNREAD and item.flags() & Qt.ItemIsUserCheckable:
                    item.setCheckState(0, self._state(relative))
                stack.extend(item.child(i) for i in range(item.childCount()))
        finally:
            self.tree.blockSignals(blocked)

    # --- result ------------------------------------------------------------

    def chosen(self) -> list[str]:
        """The picked roots, in the order the text box would hold them."""
        return sorted(self._selected)


def machine_combo(names: Iterable[str], current: str, *, placeholder: str = "") -> QComboBox:
    """An editable choice of machine name: the config's names, or anything typed."""
    widget = QComboBox()
    widget.setEditable(True)
    widget.setMinimumWidth(180)
    for name in names:
        widget.addItem(name)
    widget.setEditText(current)
    if placeholder:
        widget.lineEdit().setPlaceholderText(placeholder)
    return widget


def field(widget: QWidget, hint: str | None = None) -> QWidget:
    """An input with its explanation directly underneath, as one form cell.

    A wrapped label given its own form row makes QFormLayout reserve height for
    the widest imaginable wrap, which spreads a form over the whole page.
    """
    cell = QWidget()
    layout = QVBoxLayout(cell)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    layout.addWidget(widget)
    if hint:
        note = label(hint, "muted", wrap=True)
        layout.addWidget(note)
    return cell


def vbox(*items: QWidget | QHBoxLayout | QVBoxLayout, spacing: int = 8) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(spacing)
    for item in items:
        if isinstance(item, QWidget):
            layout.addWidget(item)
        else:
            layout.addLayout(item)
    return layout


__all__ = [
    "Banner",
    "Cell",
    "JobRunner",
    "PathField",
    "button",
    "card",
    "command",
    "field",
    "fill_table",
    "label",
    "machine_combo",
    "row",
    "selected_data",
    "set_tone",
    "table",
    "vbox",
]
