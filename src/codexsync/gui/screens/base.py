"""What every screen has: a heading, a body, the catalogue, and a way to run a job."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

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


class Screen(QWidget):
    page = ""
    #: Long forms scroll; screens whose table should take the remaining height
    #: do not, because a table inside a scroll area never gets a height to fill.
    scrollable = False

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

    # --- helpers --------------------------------------------------------------

    def t(self, key: str, /, **params: object) -> str:
        return self.host.catalog.text(key, **params)

    def p(self, key: str, count: int, /, **params: object) -> str:
        return self.host.catalog.plural(key, count, **params)

    @property
    def palette_(self) -> theme.Palette:
        return self.host.palette()

    def run(self, call: Callable[[], Outcome], apply: Callable[[Any, Outcome], None]) -> None:
        """Run a job that may write; it cannot be abandoned."""
        self.host.run(self.page, call, apply)

    def read(
        self,
        call: Callable[..., Outcome],
        apply: Callable[[Any, Outcome], None],
        *,
        progress: bool = False,
    ) -> None:
        """Run a read-only job the person may stop waiting for.

        ``progress`` is for the reads that take seconds -- the session scan
        hashes every file -- and means ``call`` accepts a ``progress=``
        callback. ``progress_text`` then has something to draw.
        """
        self.host.run(self.page, call, apply, cancellable=True, progress=progress)

    def progress_text(self) -> str:
        """How far this page's running read has got, as a sentence or ''."""
        return self.host.progress_text(self.page)

    def headline(self, failure: Failure) -> str:
        return self.t(f"failure.{failure.value}")

    def failure_text(self, outcome: Outcome) -> str:
        if outcome.failure is None:
            return ""
        return f"{self.headline(outcome.failure)}: {outcome.message}" if outcome.message else self.headline(outcome.failure)

    def join(self, parts: list[str]) -> str:
        return self.t("common.separator").join(part for part in parts if part)
