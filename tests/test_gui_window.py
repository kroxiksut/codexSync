"""The state screen, rendered offscreen.

Skipped wholesale when PySide6 is absent, which is what CI sees: the GUI is an
optional extra and the test matrix does not install it. That makes these tests
the kind this project has been bitten by twice -- code no green suite reaches --
so they are written to run without a display, and they assert the two things a
window can get wrong on its own.

An undetermined process must read as "refused", never as "allowed", because the
whole safety spine treats UNKNOWN as not-stopped. And every refusal the core can
raise must produce a headline, or the window shows a blank banner for the one
event the user needed explained.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest

_HAS_QT = importlib.util.find_spec("PySide6") is not None
if _HAS_QT:
    # Must be set before the first QApplication: it lets the widgets be built
    # and painted with no display attached.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from codexsync.gui.controller import CheckRow, Controller, Failure, Outcome, StateView


def _view(*, stopped: bool, checks=()) -> StateView:
    rows = checks or (
        CheckRow("config", "PASS", "Loaded config"),
        CheckRow("codex_process", "PASS" if stopped else "WARN", "…"),
        CheckRow("session_catalog", "WARN", "sessions=252"),
    )
    return StateView(
        checks=rows,
        codex_looks_stopped=stopped,
        failures=sum(1 for row in rows if row.status == "FAIL"),
        warnings=sum(1 for row in rows if row.status == "WARN"),
    )


@unittest.skipUnless(_HAS_QT, "PySide6 is an optional extra and is not installed")
class StateWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from codexsync.gui.window import StateWindow

        return StateWindow(Controller(Path("config.toml")))

    def test_a_stopped_codex_reads_as_allowed(self) -> None:
        window = self._window()
        window.show_outcome(Outcome(value=_view(stopped=True)))
        self.assertIn("closed", window._banner.text())
        self.assertIn("allowed", window._banner.text())

    def test_an_undetermined_codex_reads_as_refused_not_as_allowed(self) -> None:
        """UNKNOWN is not optimistically a stopped process, here as everywhere."""
        window = self._window()
        window.show_outcome(Outcome(value=_view(stopped=False)))
        self.assertIn("refused", window._banner.text())
        self.assertNotIn("allowed", window._banner.text())

    def test_every_check_reaches_the_table(self) -> None:
        window = self._window()
        view = _view(stopped=True)
        window.show_outcome(Outcome(value=view))
        self.assertEqual(window._table.rowCount(), len(view.checks))
        self.assertEqual(window._table.item(0, 0).text(), "config")
        self.assertEqual(window._table.item(1, 1).text(), "PASS")
        self.assertEqual(window._table.item(2, 2).text(), "sessions=252")

    def test_the_counts_reach_the_status_bar(self) -> None:
        window = self._window()
        window.show_outcome(Outcome(value=_view(stopped=True)))
        message = window.statusBar().currentMessage()
        self.assertIn("3 checks", message)
        self.assertIn("1 warning(s)", message)
        self.assertIn("0 failure(s)", message)

    def test_every_refusal_has_a_headline(self) -> None:
        window = self._window()
        for failure in Failure:
            with self.subTest(failure=failure.value):
                window.show_outcome(Outcome(failure=failure, message="because"))
                self.assertTrue(window._banner.text())
                self.assertEqual(window._table.rowCount(), 0)
                self.assertEqual(window.statusBar().currentMessage(), "because")

    def test_a_second_refresh_is_ignored_while_one_is_running(self) -> None:
        """Two readings at once would race to draw over each other."""
        window = self._window()
        window._busy = True
        window._refresh.setEnabled(True)
        window.refresh()
        self.assertTrue(window._refresh.isEnabled(), "no second job may start")

    def test_a_finished_reading_re_enables_the_button(self) -> None:
        window = self._window()
        window._busy = True
        window._refresh.setEnabled(False)
        window.show_outcome(Outcome(value=_view(stopped=True)))
        self.assertTrue(window._refresh.isEnabled())
        self.assertFalse(window._busy)


if __name__ == "__main__":
    unittest.main()
