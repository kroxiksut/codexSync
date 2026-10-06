"""The console does what the window does (CS-367..373).

The CLI is the automation surface: whatever a person can do in the window, a
script must be able to do too. The first test is the guard -- every core
function the window's controller calls is used by the CLI as well, or named
here with the reason it is not -- so the next window feature cannot quietly
leave the console behind. The rest exercise the commands that closed the gap.
"""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tomllib
import unittest
from unittest import mock

from codexsync.app import write_working_set
from codexsync.cli import _print_summary, main
from codexsync.config_edit import NOT_EDITABLE
from codexsync.home_stats import CopiesStats, HomeSummary, StateStats
from codexsync.session_scope import SessionScope

try:
    from tests.test_handoff import _Workspace
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "codexsync"

#: Core names the window calls that the console reaches another way.
WINDOW_ONLY = {
    "SessionScope": "a value the window builds; the console passes --project/--chat",
    "config_diff": "the console edits through change_config_value/change_path_mappings",
    "read_config_document": "the console edits through change_config_value/change_path_mappings",
    "remove_key": "the console edits through remove_config_value",
    "replace_array_of_tables": "the console edits through change_path_mappings",
    "save_config_text": "the console edits through change_config_value/change_path_mappings",
    "set_value": "the console edits through change_config_value",
    "validate_config_text": "the console edits through change_config_value; `validate` checks the file",
    "known_machines": "the console names both machines; `handoff status` lists them",
    "session_pair_name": "names the window's plan files for a pair; the console takes --plan",
    "preview_path": "shows where a folder typed into a form would resolve; the console reads the file",
    "remember_state_stats": "the window keeps a chat scan's counts for Home; `summary --recount` does it here",
}


class ConsoleReachesEverythingTests(unittest.TestCase):
    def _window_calls(self) -> set[str]:
        tree = ast.parse((PACKAGE / "gui" / "controller.py").read_text(encoding="utf-8"))
        imported = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "app"
            for alias in node.names
        }
        called = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        return imported & called

    def test_every_core_call_of_the_window_is_reachable_from_the_console(self) -> None:
        cli = (PACKAGE / "cli.py").read_text(encoding="utf-8")
        missing = sorted(
            name for name in self._window_calls() - set(WINDOW_ONLY)
            if not re.search(rf"\b{re.escape(name)}\b", cli)
        )
        self.assertEqual(missing, [], "add a command for these, or say in WINDOW_ONLY why there is none")

    def test_the_guard_sees_the_window(self) -> None:
        # A guard that found nothing would pass for ever.
        calls = self._window_calls()
        self.assertIn("run_handoff", calls)
        self.assertIn("list_backup_snapshots", calls)

    def test_the_exceptions_are_still_called_by_the_window(self) -> None:
        self.assertEqual(sorted(set(WINDOW_ONLY) - self._window_calls()), [])


class _Console(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        self.config, self.codex = self.machine("desktop")
        logging_patch = mock.patch("codexsync.cli.configure_logging")
        logging_patch.start()
        self.addCleanup(logging_patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["-c", str(self.config), *argv])
        return code, out.getvalue()

    def settings(self) -> dict:
        return tomllib.loads(self.config.read_text(encoding="utf-8"))


class ConfigSetTests(_Console):
    def test_a_value_is_set_comments_kept_and_the_old_file_kept(self) -> None:
        self.config.write_text(
            self.config.read_text(encoding="utf-8").replace("[conflict]", "[conflict]\n# chosen by hand"),
            encoding="utf-8",
        )
        code, out = self.run_cli("config", "set", "conflict.policy", "prefer_local")
        self.assertEqual(code, 0)
        self.assertEqual(self.settings()["conflict"]["policy"], "prefer_local")
        self.assertIn("# chosen by hand", self.config.read_text(encoding="utf-8"))
        self.assertIn('+policy = "prefer_local"', out)
        code, out = self.run_cli("config", "history", "--json")
        self.assertEqual(len(json.loads(out)), 1, "the replaced file is in config-history/")

    def test_a_dry_run_writes_nothing(self) -> None:
        before = self.config.read_bytes()
        code, out = self.run_cli("config", "set", "scheduler.interval_seconds", "900", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.config.read_bytes(), before)

    def test_values_take_the_templates_type(self) -> None:
        self.run_cli("config", "set", "targets.include_roots", '["sessions", "skills", "rules"]')
        self.run_cli("config", "set", "handoff.notify", "false")
        settings = self.settings()
        self.assertEqual(settings["targets"]["include_roots"], ["sessions", "skills", "rules"])
        self.assertIs(settings["handoff"]["notify"], False)

    def test_what_the_window_does_not_edit_is_refused(self) -> None:
        before = self.config.read_bytes()
        for name, value in (
            ("safety.fail_on_unknown", "false"),
            ("process_detection.allow_terminate_if_running", "true"),
            ("handoff.root_dir", "elsewhere"),
            ("conflict.nonsense", "1"),
            ("scheduler.enabled", "maybe"),
            ("conflict.policy", "newest"),
        ):
            code, _ = self.run_cli("config", "set", name, value)
            self.assertEqual(code, 4, name)
        self.assertEqual(self.config.read_bytes(), before)

    def test_the_refused_list_covers_safety_and_the_windows_exceptions(self) -> None:
        self.assertIn("safety.require_codex_stopped", NOT_EDITABLE)
        self.assertIn("safety.fail_on_unknown", NOT_EDITABLE)

    def test_unset_returns_to_the_default(self) -> None:
        self.run_cli("config", "set", "scheduler.interval_seconds", "900")
        code, _ = self.run_cli("config", "unset", "scheduler.interval_seconds")
        self.assertEqual(code, 0)
        self.assertNotIn("interval_seconds", self.settings().get("scheduler", {}))
        code, out = self.run_cli("config", "unset", "scheduler.interval_seconds")
        self.assertIn("nothing to change", out)


class MappingTests(_Console):
    def test_a_rule_is_added_listed_and_removed(self) -> None:
        code, _ = self.run_cli(
            "config", "mapping", "add", "--id", "laptop-projects", "--source-machine", "laptop",
            "--target-machine", "desktop", "--from", "C:/Users/me/Projects", "--to", "D:/Projects",
            "--case-sensitive", "false",
        )
        self.assertEqual(code, 0)
        code, out = self.run_cli("config", "mapping", "list", "--json")
        rules = json.loads(out)
        self.assertEqual(rules[0]["rule_id"], "laptop-projects")
        self.assertIs(rules[0]["case_sensitive"], False)
        code, _ = self.run_cli(
            "config", "mapping", "add", "--id", "laptop-projects", "--source-machine", "a",
            "--target-machine", "b", "--from", "x", "--to", "y",
        )
        self.assertEqual(code, 4, "ids are unique")
        code, _ = self.run_cli("config", "mapping", "remove", "--id", "laptop-projects")
        self.assertEqual(code, 0)
        self.assertEqual(self.settings().get("path_mappings", []), [])
        code, _ = self.run_cli("config", "mapping", "remove", "--id", "laptop-projects")
        self.assertEqual(code, 4)


class RootsTests(_Console):
    def test_one_level_of_both_sides_with_reasons(self) -> None:
        self.write(self.codex / "skills" / "tool.md", "x")
        self.write(self.codex / "auth.json", "{}")
        self.session(self.codex, "11111111-1111-1111-1111-111111111111")
        code, out = self.run_cli("config", "roots", "--json")
        self.assertEqual(code, 0)
        items = {item["path"]: item for item in json.loads(out)}
        self.assertTrue(items["skills"]["included"])
        self.assertTrue(items["sessions"]["semantic_owned"])
        self.assertNotIn("auth.json", items, "a credential file is never offered")


class BackupsTests(_Console):
    def test_an_empty_folder_and_a_snapshot_after_a_sync(self) -> None:
        code, out = self.run_cli("backups", "list")
        self.assertEqual((code, out.strip()), (0, "No backups yet."))
        self.write(self.cloud / "skills" / "tool.md", "from the cloud")
        self.write(self.codex / "skills" / "tool.md", "older here")
        os.utime(self.codex / "skills" / "tool.md", (1_000_000, 1_000_000))
        code, _ = self.run_cli("sync", "--apply")
        self.assertEqual(code, 0)
        code, out = self.run_cli("backups", "list", "--json")
        snapshots = json.loads(out)
        self.assertEqual(len(snapshots), 1, "the overwrite was backed up first")
        self.assertTrue(snapshots[0]["committed"])
        self.assertEqual(snapshots[0]["machine"], "desktop")


class ScopeTests(_Console):
    def test_the_stored_working_set_is_shown(self) -> None:
        code, out = self.run_cli("sessions", "scope", "--source-machine", "laptop", "--target-machine", "desktop")
        self.assertIn("No working set", out)
        write_working_set(
            self.config, SessionScope(projects=("p-alpha",), chats=("0199",)),
            source_machine="laptop", target_machine="desktop",
        )
        code, out = self.run_cli(
            "sessions", "scope", "--source-machine", "laptop", "--target-machine", "desktop", "--json",
        )
        self.assertEqual(json.loads(out), {"projects": ["p-alpha"], "chats": ["0199"], "empty": False})


class SummaryTests(_Console):
    def test_the_home_page_in_lines(self) -> None:
        summary = HomeSummary(
            codex="stopped", sync=None, open_journals=2,
            backups=CopiesStats(3, 300, "2026-10-06T01:00:00Z"), copies=None, copies_configured=False,
            guardian=None, automation=None,
            state=StateStats("2026-10-06T02:00:00Z", 10, 4, 1, 2, 3, 1, 1000),
            errors={"guardian": "no guardian root"},
        )
        out = io.StringIO()
        with redirect_stdout(out):
            _print_summary(summary, as_json=False)
        text = out.getvalue()
        self.assertIn("Open journals: 2", text)
        self.assertIn("Copies of .codex: off", text)
        self.assertIn("Chats: 10", text)
        self.assertIn("Not read: guardian: no guardian root", text)
        out = io.StringIO()
        with redirect_stdout(out):
            _print_summary(summary, as_json=True)
        self.assertEqual(json.loads(out.getvalue())["state"]["chats"], 10)

    def test_recount_counts_before_reading(self) -> None:
        # The real summary asks the OS scheduler; no test may run schtasks.
        order = []
        with mock.patch("codexsync.cli.recount_state", side_effect=lambda path: order.append("recount")), \
                mock.patch("codexsync.cli.read_home_summary", side_effect=lambda path: order.append("read")), \
                mock.patch("codexsync.cli._print_summary", return_value=0):
            code, _ = self.run_cli("summary", "--recount")
        self.assertEqual((code, order), (0, ["recount", "read"]))


if __name__ == "__main__":
    unittest.main()
