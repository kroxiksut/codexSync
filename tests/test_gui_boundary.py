"""The GUI is a second shell, and these tests are what keeps it one.

None of them needs PySide6, and that is the first thing they prove: the package
and its controller import on a machine that never installed the extra, so the
boundary can be inspected without the toolkit that the boundary is about.

The rest enforce two properties that cannot be recovered once they are lost.

**The boundary points one way.** Nothing in the core may import `codexsync.gui`,
or the optional extra stops being optional.

**The GUI cannot decide a mutation.** There is exactly one authority on whether
state may change (`safety_gate`) and exactly one envelope around a change (lock,
journal, verified backup, final check, atomic replace, `COMMITTED`). A second
shell that reached past either -- to keep a window responsive, or to show a
friendlier error -- would leave two different safety stories for one operation
with only one of them written down. So the GUI is allowed to call `app.py` and
nothing beneath it, and that is checked by reading the imports rather than by
trusting the author.
"""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from codexsync.exceptions import (
    ConfigError,
    ConflictError,
    FailSafeError,
    SafetyPreconditionError,
)
from codexsync.exit_codes import ExitCode
from codexsync.gui import MISSING_QT_MESSAGE
from codexsync.gui.controller import Controller, Failure, Outcome, run

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "codexsync"
GUI = PACKAGE / "gui"

#: Modules the GUI must never reach for. Each one is a piece of the mutation
#: envelope or the authority over it; calling any of them directly is how a
#: second shell would end up with its own idea of when a write is allowed.
FORBIDDEN = frozenset({
    "safety_gate",
    "sync_engine",
    "backup",
    "operation_lock",
    "mutation_journal",
    "recovery",
    "state_locator",
    "scanner",
    "planner",
    "guardian_store",
    "guardian_runner",
    "semantic_store",
    # These write files; the window reaches them only through app.py.
    "config_edit",
    "config_migrate",
    "system_scheduler",
})

#: The modules allowed to import Qt. Everything else in the package -- the
#: launcher, the controller, the catalogue, the theme -- must import without it.
QT_MODULES = ("window.py", "widgets.py", "screens")


def _imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, relative names included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(part for part in node.module.split("."))
            names.update(alias.name for alias in node.names)
    return names


class BoundaryTests(unittest.TestCase):
    def test_the_gui_package_imports_without_qt(self) -> None:
        """Proved by this module having imported it already, at file scope."""
        self.assertIn("PySide6", MISSING_QT_MESSAGE)
        self.assertTrue(callable(Controller))

    def test_nothing_in_the_core_imports_the_gui(self) -> None:
        offenders = [
            path.name
            for path in PACKAGE.glob("*.py")
            if "codexsync.gui" in path.read_text(encoding="utf-8")
            or "from .gui" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], "the optional extra must stay optional")

    def test_the_gui_reaches_no_further_than_the_app_layer(self) -> None:
        for path in sorted(GUI.rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(
                    _imported_modules(path) & FORBIDDEN,
                    set(),
                    "the GUI may call app.py and nothing beneath it",
                )

    def test_the_guard_itself_would_catch_a_planted_import(self) -> None:
        """A check that never fires proves nothing."""
        planted = GUI / "_planted_for_the_test.py"
        planted.write_text("from ..safety_gate import SafetyGate\n", encoding="utf-8")
        try:
            self.assertIn("safety_gate", _imported_modules(planted) & FORBIDDEN)
        finally:
            planted.unlink()

    def test_no_qt_import_survives_outside_the_window(self) -> None:
        """Importing the package must not pull the toolkit in by itself."""
        for path in sorted(GUI.rglob("*.py")):
            relative = path.relative_to(GUI).parts
            if relative[0] in QT_MODULES:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn("PySide6", _imported_modules(path))


class LauncherTests(unittest.TestCase):
    def test_a_missing_extra_is_bad_input_rather_than_a_traceback(self) -> None:
        import importlib.util

        from codexsync.gui.__main__ import main

        if importlib.util.find_spec("PySide6") is not None:
            self.skipTest("PySide6 is installed; the missing-extra path cannot run")
        self.assertEqual(main(["-c", "config.toml"]), int(ExitCode.BAD_INPUT))

    def test_the_launcher_takes_only_a_config_path(self) -> None:
        from codexsync.gui.__main__ import build_parser

        args = build_parser().parse_args(["-c", "somewhere/config.toml"])
        self.assertEqual(args.config, "somewhere/config.toml")
        self.assertEqual(sorted(vars(args)), ["config"])


class FailureTranslationTests(unittest.TestCase):
    """The window may not invent a fifth kind of no."""

    def test_each_core_refusal_keeps_the_meaning_the_cli_gives_it(self) -> None:
        cases = [
            (ConfigError("bad"), Failure.CONFIGURATION),
            (SafetyPreconditionError("open"), Failure.CODEX_NOT_STOPPED),
            (ConflictError("decide"), Failure.NEEDS_A_DECISION),
            (FailSafeError("stopped"), Failure.STOPPED_SAFELY),
            (RuntimeError("bug"), Failure.UNEXPECTED),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                def raise_it(exc=error):
                    raise exc

                outcome = run(raise_it)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.failure, expected)
                self.assertTrue(outcome.message)

    def test_a_guardian_failure_is_not_reported_as_a_plain_bug(self) -> None:
        """The FailSafeError subclasses must land in their family, not UNEXPECTED."""
        from codexsync.exceptions import GuardianBusyError, OperationBusyError

        for error in (GuardianBusyError("busy"), OperationBusyError("busy")):
            with self.subTest(error=type(error).__name__):
                def raise_it(exc=error):
                    raise exc

                self.assertEqual(run(raise_it).failure, Failure.STOPPED_SAFELY)

    def test_a_value_and_a_failure_are_never_both_present(self) -> None:
        self.assertTrue(Outcome(value=1).ok)
        self.assertFalse(Outcome(failure=Failure.CONFIGURATION, message="x").ok)
        self.assertIsNone(Outcome(failure=Failure.CONFIGURATION, message="x").value)


class QtStaysOptionalTests(unittest.TestCase):
    """Every command must work on a machine where importing Qt fails.

    Run in a subprocess, and that is not a detail. Blocking the import inside
    this interpreter means clearing `codexsync` out of `sys.modules` and
    importing it again, which leaves every later test patching a different
    module object than the one under test -- the same "patch where it is
    defined" trap the project already documents, except silent and global. A
    child process makes the guarantee testable on a machine that has the extra
    without touching the one running the suite.
    """

    SCRIPT = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "PySide6" or name.startswith("PySide6."):
                    raise ImportError("PySide6 is blocked for this test")
                return None

        sys.meta_path.insert(0, Blocker())
        import codexsync.cli
        import codexsync.app
        import codexsync.gui.controller
        from codexsync.gui.__main__ import main
        print(main(["-c", "no-such-config.toml"]))
        """
    )

    def test_the_cli_and_the_controller_import_with_qt_unavailable(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PySide6", result.stderr, "the launcher must name the missing extra")
        self.assertEqual(
            result.stdout.strip(), str(int(ExitCode.BAD_INPUT)),
            "a missing extra is bad input, not a crash",
        )


if __name__ == "__main__":
    unittest.main()
