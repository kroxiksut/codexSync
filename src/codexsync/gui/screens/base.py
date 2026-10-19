"""What every screen has: a heading, a body, the catalogue, and a way to run a job."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from .. import theme
from ..controller import Failure, Outcome
from ..widgets import label

if TYPE_CHECKING:  # pragma: no cover
    from ..window import MainWindow


class Model:
    """State a screen keeps across rebuilds."""

    def reset_plans(self) -> None:
        """Forget every plan and scan that was built against the old config.

        Called after `config.toml` is saved: a plan computed under the previous
        rules would otherwise stay on screen looking applicable.
        """

    def receive(self, kind: str, outcome: Outcome) -> None:
        """Take the outcome of shared work another page started (`jobs.SHARED_KINDS`).

        The page that started it applies its own outcome; every other page
        offering the same work gets it here, so both show the one result.
        """


class Screen(QWidget):
    page = ""
    #: Long forms scroll; screens whose table should take the remaining height
    #: do not, because a table inside a scroll area never gets a height to fill.
    scrollable = False
    #: A scrolling screen normally ends in a stretch so short content sits at
    #: the top. One whose last big widget should take the spare height instead
    #: (settings: the tabs) turns it off.
    scroll_tail_stretch = True

    def __init__(self, host: "MainWindow", model: Model) -> None:
        super().__init__()
        self.host = host
        self.model = model

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        content.setObjectName("content")
        column = QVBoxLayout(content)
        pad = theme.CONTENT_PADDING
        column.setContentsMargins(pad, pad - 8, pad, pad - 8)
        column.setSpacing(theme.GRID_GAP - 4)

        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(label(self.t(f"nav.{self.page}"), "pageTitle"))
        heading.addWidget(label(self.t(f"page.{self.page}.subtitle"), "pageSubtitle", wrap=True))
        top = QHBoxLayout()
        top.setSpacing(12)
        top.addLayout(heading, stretch=1)
        #: A screen may put one small control beside its title -- the language
        #: chooser does, because it belongs to no tab in particular.
        self.heading_actions = QHBoxLayout()
        self.heading_actions.setSpacing(8)
        top.addLayout(self.heading_actions)
        column.addLayout(top)
        self.body = column
        self.build()

        if self.scrollable:
            if self.scroll_tail_stretch:
                column.addStretch(1)
            scroll = QScrollArea()
            scroll.setObjectName("content")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(content)
            outer.addWidget(scroll)
        else:
            outer.addWidget(content)
        self.render()

    # --- to override ---------------------------------------------------------

    def build(self) -> None:
        raise NotImplementedError

    def render(self) -> None:
        """Draw the model. Must be safe to call at any time, any number of times."""

    def activated(self) -> None:
        """The screen was just shown."""

    def reveal(self, target: str) -> None:
        """Bring ``target`` -- a tab or a ``section.key`` field -- into view.

        Another page's message pointed here (`MainWindow.go_to`). A screen
        that has nothing addressable ignores it.
        """

    # --- helpers --------------------------------------------------------------

    def t(self, key: str, /, **params: object) -> str:
        return self.host.catalog.text(key, **params)

    def p(self, key: str, count: int, /, **params: object) -> str:
        return self.host.catalog.plural(key, count, **params)

    def show_widget(self, widget: QWidget) -> None:
        """Scroll every area holding ``widget`` to it and give it the focus."""
        def go() -> None:
            child = widget
            parent = widget.parentWidget()
            while parent is not None:
                if isinstance(parent, QScrollArea):
                    parent.ensureWidgetVisible(child, 40, 40)
                    child = parent
                parent = parent.parentWidget()
            widget.setFocus()
        go()
        # Once more after the page switch has laid itself out: a page that was
        # hidden had no geometry to scroll to yet.
        QTimer.singleShot(0, go)

    @property
    def palette_(self) -> theme.Palette:
        return self.host.palette()

    def run(
        self,
        call: Callable[..., Outcome],
        apply: Callable[[Any, Outcome], None],
        *,
        kind: str | None = None,
        dry_run: bool = False,
        progress: bool = False,
    ) -> None:
        """Run a job that may write; it cannot be abandoned.

        ``kind`` names the work for the task list (`jobs.JOB_KINDS`), and
        ``dry_run`` says this run of it writes nothing.
        """
        self.host.run(self.page, call, apply, kind=kind, dry_run=dry_run, progress=progress)

    def read(
        self,
        call: Callable[..., Outcome],
        apply: Callable[[Any, Outcome], None],
        *,
        progress: bool = False,
        kind: str | None = None,
    ) -> None:
        """Run a read-only job the person may stop waiting for.

        ``progress`` is for the reads that take seconds -- the session scan
        hashes every file -- and means ``call`` accepts a ``progress=``
        callback. ``progress_text`` then has something to draw.
        """
        self.host.run(self.page, call, apply, cancellable=True, progress=progress, kind=kind)

    def shared_busy(self, *kinds: str) -> bool:
        """Is any of this work running, from whichever page started it (CS-336)?"""
        return any(self.host.busy(kind) for kind in kinds)

    def progress_text(self, kind: str | None = None) -> str:
        """How far this page's running job -- or the job of ``kind`` -- has got, or ''."""
        return self.host.progress_text(self.page, kind)

    def headline(self, failure: Failure) -> str:
        return self.t(f"failure.{failure.value}")

    def failure_text(self, outcome: Outcome) -> str:
        if outcome.failure is None:
            return ""
        body = self.refusal_text(outcome, sentence_start=False)
        return f"{self.headline(outcome.failure)}: {body}" if body else self.headline(outcome.failure)

    def refusal_text(self, outcome: Outcome, *, sentence_start: bool = True) -> str:
        """Why ``outcome`` produced nothing, in the window's language when core named the kind.

        A refusal core names by code is said from ``failure.code.<CODE>`` with
        the values it attached (CS-332); the English message is only for kinds
        the catalogue has no sentence for. The sentences are written to follow
        a headline and a colon, so a banner body, which stands alone, starts
        with a capital (``sentence_start``) -- unless it starts with a value:
        ``config.toml`` stays as it is written.
        """
        if outcome.failure is None:
            return ""
        known = f"failure.code.{outcome.code}" if outcome.code else ""
        catalog = self.host.catalog
        if not (known and catalog.has(known)):
            return outcome.message
        params = outcome.details if isinstance(outcome.details, dict) else {}
        try:
            if catalog.is_plural(known):
                text = catalog.plural(known, int(params.get("count", 0)), **{
                    name: value for name, value in params.items() if name != "count"
                })
            else:
                # Formatted here even without values, so a placeholder left
                # unfilled is noticed instead of drawn as ``{name}``.
                text = catalog.text(known).format(**params)
        except (KeyError, IndexError, ValueError, TypeError):
            # A sentence naming a value the refusal did not carry: the English
            # message still says it, rather than a half-filled template.
            return outcome.message
        # A plural form starts with its count or a word; a plain sentence may
        # start with a value such as a file name, which keeps its own case.
        starts_with_value = not catalog.is_plural(known) and catalog.text(known).startswith("{")
        if not sentence_start or starts_with_value:
            return text
        return text[:1].upper() + text[1:]

    def join(self, parts: list[str]) -> str:
        return self.t("common.separator").join(part for part in parts if part)
