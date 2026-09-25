"""What a page shows while no config is open.

Every page but first run and about works on one config file. Before this
screen existed the window, finding no file, made one up -- a per-user path under
%APPDATA% -- and every page ran its reads against that path and reported its
absence in its own words (CS-268). A page cannot do anything useful without a
config, so it says so once, in the same words everywhere, and offers the two
ways out: open a file that exists wherever it is, or create one where the user
chooses.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog

from ..widgets import Banner, button, row
from .base import Model, Screen

if TYPE_CHECKING:  # pragma: no cover
    from ..window import MainWindow


class NoConfigScreen(Screen):
    """Stands in for any page; keeps that page's title so the sidebar still reads."""

    def __init__(self, host: "MainWindow", model: Model, page: str) -> None:
        # The heading is drawn from ``page`` inside ``Screen.__init__``.
        self.page = page
        super().__init__(host, model)

    def build(self) -> None:
        banner = Banner()
        banner.show_message(
            "attention", self.t("no_config.title"), self.t("no_config.detail"), self.palette_,
        )
        self.body.addWidget(banner)
        self.open_button = button(self.t("no_config.open"), primary=True)
        self.open_button.clicked.connect(lambda: self.open_existing())
        self.create_button = button(self.t("no_config.create"))
        self.create_button.clicked.connect(lambda: self.host.go_to("first_run"))
        self.body.addLayout(row(self.open_button, self.create_button))
        self.body.addStretch(1)

    def open_existing(self, path: Path | None = None) -> None:
        """Switch the window to a config that already exists, wherever it is."""
        if path is None:  # pragma: no cover - opens a native dialog
            chosen, _ = QFileDialog.getOpenFileName(
                self, self.t("no_config.open"), str(Path.home()), "TOML (*.toml)"
            )
            if not chosen:
                return
            path = Path(chosen)
        self.host.open_config(Path(path))
