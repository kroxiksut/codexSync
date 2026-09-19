"""Listing what may be synchronised: read-only, and blind to secrets.

The listing exists so that a settings screen never opens `.codex` itself. That
makes two properties load-bearing and both are asserted here against the write
spy the Guardian tests use: nothing is created, and a token file is not in the
answer at all, so no window can offer one by mistake.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.config import load_config
from codexsync.sync_candidates import SECRET_NAMES, list_sync_candidates
from test_guardian_state_isolation import forbid_writes_under

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"

CONFIG = """
[identity]
machine_id = "laptop"

[paths]
workspace_root_dir = "{workspace}"
local_state_dir = "{codex}"
cloud_root_dir = "{cloud}"
backup_dir = "${{workspace_root}}/backups"
temp_dir = "${{workspace_root}}/.tmp"

[targets]
include_roots = ["skills"]

[filters]
exclude_globs = ["**/tmp/**", "**/*.log"]

[state]
manifest_file = "${{workspace_root}}/state/manifest.json"
"""


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"candidates-{uuid.uuid4().hex[:8]}"
        self.codex = self.root / "codex"
        self.cloud = self.root / "cloud"
        for relative in (
            "codex/sessions/2026", "codex/archived_sessions", "codex/skills/pack",
            "codex/plugins", "codex/tmp/arg0", "codex/sqlite", "codex/.sandbox-secrets",
            "cloud/skills", "cloud/only-in-the-cloud",
        ):
            (self.root / relative).mkdir(parents=True)
        for relative in (
            "codex/auth.json", "codex/cap_sid", "codex/session_index.jsonl",
            "codex/state_5.sqlite", "codex/AGENTS.md", "codex/tmp/arg0/lock",
        ):
            (self.root / relative).write_text("", encoding="utf-8")
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            CONFIG.format(workspace=self._q(self.root / "workspace"), codex=self._q(self.codex),
                          cloud=self._q(self.cloud)),
            encoding="utf-8",
        )
        self.cfg = load_config(self.config_path)
        self.addCleanup(shutil.rmtree, self.root, True)

    @staticmethod
    def _q(path: Path) -> str:
        return str(path).replace("\\", "\\\\")

    def _names(self, relative: str = "") -> list[str]:
        return [item.name for item in list_sync_candidates(self.cfg, relative)]

    def test_no_secret_is_ever_in_the_answer(self) -> None:
        """Not filtered by the window: never handed to it."""
        listed = set(self._names())
        self.assertTrue(listed.isdisjoint(SECRET_NAMES))
        self.assertIn("auth.json", {p.name for p in self.codex.iterdir()}, "it is really there")

    def test_the_listing_creates_nothing_inside_the_state_directory(self) -> None:
        with forbid_writes_under(self.codex):
            list_sync_candidates(self.cfg)
            list_sync_candidates(self.cfg, "skills")

    def test_both_sides_are_merged_and_each_side_is_named(self) -> None:
        items = {item.name: item for item in list_sync_candidates(self.cfg)}
        self.assertTrue(items["skills"].local and items["skills"].cloud)
        self.assertTrue(items["only-in-the-cloud"].cloud)
        self.assertFalse(items["only-in-the-cloud"].local)
        self.assertTrue(items["plugins"].local)
        self.assertFalse(items["plugins"].cloud)

    def test_semantic_owned_entries_are_reported_rather_than_hidden(self) -> None:
        items = {item.name: item for item in list_sync_candidates(self.cfg)}
        for name in ("sessions", "archived_sessions", "sqlite", "session_index.jsonl", "state_5.sqlite"):
            self.assertIn(name, items, f"{name} is shown, with its reason")
            self.assertTrue(items[name].semantic_owned, name)
        self.assertFalse(items["skills"].semantic_owned)

    def test_a_lock_folder_under_tmp_is_already_covered_by_the_default_glob(self) -> None:
        """`~/.codex/tmp/arg0/*` on Unix: claimed to be covered, so it is checked."""
        inside = {item.name: item for item in list_sync_candidates(self.cfg, "tmp/arg0")}
        self.assertTrue(inside["lock"].excluded_by_glob)

    def test_a_child_node_is_listed_with_its_full_relative_path(self) -> None:
        items = list_sync_candidates(self.cfg, "skills")
        self.assertEqual([item.relative for item in items], ["skills/pack"])

    def test_a_node_that_is_not_there_lists_nothing_and_does_not_raise(self) -> None:
        self.assertEqual(list_sync_candidates(self.cfg, "no/such/node"), ())


if __name__ == "__main__":
    unittest.main()
