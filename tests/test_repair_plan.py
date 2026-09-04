from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.path_mapping import PathMappingRule
from codexsync.repair_plan import RepairActionKind, build_repair_plan, load_repair_plan, save_repair_plan
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState


def _electron_state(roots: list[str], *, assigned: str | None = "p1") -> bytes:
    """The shape the Electron desktop build writes: roots live in a list."""
    return json.dumps({
        "local-projects": {
            "p1": {"id": "p1", "name": "repo", "rootPaths": roots, "createdAt": 1, "updatedAt": 2}
        },
        "project-order": ["p1"],
        "thread-project-assignments": (
            {"thread-a": {"projectKind": "local", "projectId": assigned}} if assigned else {}
        ),
        "app-server-project-id-by-legacy-project-id-by-host": {},
    }).encode("utf-8")


class RepairPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"repair-plan-{uuid.uuid4().hex}"
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scan_allocates_deterministic_project_and_binding(self) -> None:
        cwd = str(self.repo / "subdir")
        Path(cwd).mkdir()
        descriptor = SessionDescriptor("thread-a", SessionState.ACTIVE, "sessions/a.jsonl", "a" * 64, 1, 1, cwd=cwd)
        catalog = SessionCatalog([descriptor], {})
        source_prefix = str(self.root)
        rule = PathMappingRule("identity", "source", "target", source_prefix, source_prefix)
        state = b'{"local-projects":{},"project-order":[],"thread-project-assignments":{}}'
        first = build_repair_plan(catalog, state, source_machine="source", target_machine="target", rules=[rule], volatile=False)
        second = build_repair_plan(catalog, state, source_machine="source", target_machine="target", rules=[rule], volatile=False)
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertIn(RepairActionKind.ADD_PROJECT, {action.kind for action in first.actions})
        self.assertIn(RepairActionKind.ADD_BINDING, {action.kind for action in first.actions})

        path = self.root / "plan.json"
        save_repair_plan(first, path)
        self.assertEqual(load_repair_plan(path).plan_id, first.plan_id)

    # --- a project that moved keeps its id --------------------------------

    def _moved_case(self):
        """A project recorded under an old path that now lives somewhere else."""
        old_root = str(self.root / "old" / "repo")
        new_root = self.root / "new" / "repo"
        (new_root / ".git").mkdir(parents=True)
        rule = PathMappingRule(
            "moved", "source", "target", str(self.root / "old"), str(self.root / "new")
        )
        descriptor = SessionDescriptor(
            "thread-a", SessionState.ACTIVE, "sessions/a.jsonl", "a" * 64, 1, 1, cwd=old_root
        )
        return old_root, new_root, rule, SessionCatalog([descriptor], {})

    def test_a_moved_project_is_remapped_instead_of_duplicated(self) -> None:
        old_root, new_root, rule, catalog = self._moved_case()
        state = json.dumps({
            "local-projects": {"p1": {"root": old_root}},
            "project-order": ["p1"],
            "thread-project-assignments": {"thread-a": "p1"},
        }).encode("utf-8")

        plan = build_repair_plan(
            catalog, state, source_machine="source", target_machine="target",
            rules=[rule], volatile=False,
        )
        kinds = {action.kind for action in plan.actions}
        self.assertIn(RepairActionKind.REMAP_ROOT, kinds)
        self.assertNotIn(RepairActionKind.ADD_PROJECT, kinds)

        remap = next(a for a in plan.actions if a.kind is RepairActionKind.REMAP_ROOT)
        self.assertEqual(remap.project_id, "p1", "the project keeps its id")
        self.assertEqual(remap.source_root, old_root)
        self.assertEqual(remap.target_root, str(new_root.resolve()))

    def test_a_remapped_project_keeps_the_bindings_it_already_has(self) -> None:
        """The whole point: one corrected root migrates every chat."""
        old_root, _, rule, catalog = self._moved_case()
        state = json.dumps({
            "local-projects": {"p1": {"root": old_root}},
            "project-order": ["p1"],
            "thread-project-assignments": {"thread-a": "p1"},
        }).encode("utf-8")
        plan = build_repair_plan(
            catalog, state, source_machine="source", target_machine="target",
            rules=[rule], volatile=False,
        )
        kinds = {action.kind for action in plan.actions}
        self.assertIn(RepairActionKind.KEEP_BINDING, kinds)
        self.assertNotIn(RepairActionKind.ADD_BINDING, kinds)

    def test_a_moved_project_is_recognised_in_the_electron_shape(self) -> None:
        old_root, new_root, rule, catalog = self._moved_case()
        plan = build_repair_plan(
            catalog, _electron_state([old_root]), source_machine="source",
            target_machine="target", rules=[rule], volatile=False,
        )
        self.assertEqual(plan.schema_id, "electron-v2")
        remap = next(a for a in plan.actions if a.kind is RepairActionKind.REMAP_ROOT)
        self.assertEqual(remap.project_id, "p1")
        self.assertEqual(remap.source_root, old_root)
        self.assertIn(
            RepairActionKind.KEEP_BINDING, {action.kind for action in plan.actions},
            "an Electron binding is an object; comparing it to a bare id would rewrite it",
        )

    def test_a_project_already_at_the_new_root_is_kept_not_remapped(self) -> None:
        _, new_root, rule, catalog = self._moved_case()
        plan = build_repair_plan(
            catalog, _electron_state([str(new_root.resolve())]), source_machine="source",
            target_machine="target", rules=[rule], volatile=False,
        )
        kinds = {action.kind for action in plan.actions}
        self.assertIn(RepairActionKind.KEEP_PROJECT, kinds)
        self.assertNotIn(RepairActionKind.REMAP_ROOT, kinds)

    def test_two_projects_claiming_one_new_root_are_ambiguous(self) -> None:
        old_root, _, rule, catalog = self._moved_case()
        state = json.dumps({
            "local-projects": {"p1": {"root": old_root}, "p2": {"root": old_root}},
            "project-order": ["p1", "p2"],
            "thread-project-assignments": {},
        }).encode("utf-8")
        plan = build_repair_plan(
            catalog, state, source_machine="source", target_machine="target",
            rules=[rule], volatile=False,
        )
        self.assertIn("AMBIGUOUS_PROJECT", plan.codes)
        self.assertNotIn(RepairActionKind.REMAP_ROOT, {action.kind for action in plan.actions})

    def _older_generation(self, old_root):
        """A chat from before the move whose directory no longer exists.

        It reaches the project only through the root the remap is about to
        replace, and its own path maps nowhere, so nothing else in the plan
        would mention it.
        """
        return SessionDescriptor(
            "thread-old", SessionState.ACTIVE, "sessions/b.jsonl", "b" * 64, 1, 1,
            cwd=old_root + os.sep + "gone",
        )

    def _state_with_project_at(self, root):
        return json.dumps({
            "local-projects": {"p1": {"root": root}},
            "project-order": ["p1"],
            "thread-project-assignments": {},
        }).encode("utf-8")

    def test_a_remap_binds_the_older_chats_it_would_otherwise_detach(self) -> None:
        """Replacing the root alone would silently detach every older chat."""
        old_root, _, rule, catalog = self._moved_case()
        catalog = SessionCatalog(
            list(catalog.descriptors) + [self._older_generation(old_root)], {}
        )
        plan = build_repair_plan(
            catalog, self._state_with_project_at(old_root), source_machine="source",
            target_machine="target", rules=[rule], volatile=False,
        )
        older = [a for a in plan.actions if a.session_id == "thread-old"]
        kinds = {a.kind for a in older}
        self.assertIn(
            RepairActionKind.SKIP_UNMAPPED, kinds,
            "its own path maps nowhere, so only the remap can account for it",
        )
        binding = [a for a in older if a.kind is RepairActionKind.ADD_BINDING]
        self.assertEqual(len(binding), 1, "the older chat must be pinned to the project")
        self.assertEqual(binding[0].project_id, "p1")
        self.assertNotIn("REMAP_ORPHANS_SESSIONS", plan.codes)

    def test_the_orphan_guard_fires_when_the_cover_is_removed(self) -> None:
        """A guard that never fires proves nothing."""
        old_root, _, rule, catalog = self._moved_case()
        catalog = SessionCatalog(
            list(catalog.descriptors) + [self._older_generation(old_root)], {}
        )
        with patch(
            "codexsync.repair_plan._bind_sessions_left_behind_by_a_remap", return_value=[]
        ):
            plan = build_repair_plan(
                catalog, self._state_with_project_at(old_root), source_machine="source",
                target_machine="target", rules=[rule], volatile=False,
            )
        self.assertIn("REMAP_ORPHANS_SESSIONS", plan.codes)

    def test_an_unrecognised_state_is_refused_rather_than_read(self) -> None:
        _, _, rule, catalog = self._moved_case()
        state = b'{"local-projects":{},"project-order":[],"thread-project-assignments":{"t":42}}'
        with self.assertRaises(ValueError):
            build_repair_plan(
                catalog, state, source_machine="source", target_machine="target",
                rules=[rule], volatile=False,
            )

    def test_a_tampered_source_root_is_rejected(self) -> None:
        old_root, _, rule, catalog = self._moved_case()
        state = json.dumps({
            "local-projects": {"p1": {"root": old_root}},
            "project-order": ["p1"],
            "thread-project-assignments": {},
        }).encode("utf-8")
        plan = build_repair_plan(
            catalog, state, source_machine="source", target_machine="target",
            rules=[rule], volatile=False,
        )
        path = self.root / "remap-plan.json"
        save_repair_plan(plan, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        target = next(a for a in raw["actions"] if a["kind"] == "REMAP_ROOT")
        target["source_root"] = str(self.root / "elsewhere")
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_repair_plan(path)

    def test_tampered_target_path_is_rejected(self) -> None:
        descriptor = SessionDescriptor("thread-a", SessionState.ACTIVE, "sessions/a", "a" * 64, 1, 1, cwd=str(self.repo))
        rule = PathMappingRule("identity", "a", "b", str(self.root), str(self.root))
        plan = build_repair_plan(SessionCatalog([descriptor], {}), b'{"local-projects":{},"project-order":[]}', source_machine="a", target_machine="b", rules=[rule], volatile=False)
        path = self.root / "plan.json"
        save_repair_plan(plan, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["actions"][0]["target_root"] = str(self.root / "elsewhere")
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_repair_plan(path)


if __name__ == "__main__":
    unittest.main()
