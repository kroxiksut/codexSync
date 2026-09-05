"""Static guard against names used but never imported or defined.

The project runs no linter, so a missing import survives until the line that
needs it executes. That is fine for code every test path touches and dangerous
for code that no test reaches: moving helpers between modules once left
`repair-projects apply` raising `NameError` at runtime while the whole suite
stayed green, because that command had no test.

This walks every module in the package and fails on a loaded name that is
neither imported, defined, assigned, nor a builtin.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parent.parent / "src" / "codexsync"
_MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}
_ALLOWED = set(dir(builtins)) | _MODULE_GLOBALS


def _bound_names(tree: ast.AST) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    return bound


def _loaded_names(tree: ast.AST) -> set[str]:
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


class ModuleHygieneTests(unittest.TestCase):
    def test_every_module_resolves_the_names_it_uses(self) -> None:
        offenders: dict[str, list[str]] = {}
        # rglob, so the optional GUI package is walked too: nothing there is
        # reached by a test that renders a window, which is exactly the shape
        # of code this guard exists for.
        modules = sorted(PACKAGE.rglob("*.py"))
        self.assertTrue(modules, f"no modules found under {PACKAGE}")
        for module in modules:
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            missing = sorted(_loaded_names(tree) - _bound_names(tree) - _ALLOWED)
            if missing:
                offenders[module.name] = missing
        self.assertEqual(
            offenders, {},
            "names used without an import or definition (a NameError waiting for the right code path)",
        )


if __name__ == "__main__":
    unittest.main()
