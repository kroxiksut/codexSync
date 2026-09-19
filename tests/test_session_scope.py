"""The working set: carry one project, mirror everything.

The scenario is a laptop that needs `project-chloya` and none of the other 864
MiB. Two properties make that safe rather than merely smaller, and both are
easy to lose:

* the cloud mirror is never narrowed, so the backup can never become partial;
* the set is part of the plan id, so an apply cannot be confirmed against a
  plan built for a different set.

The third is a matter of completeness rather than safety: a project means all
of its chats, including the sub-threads they spawned -- about half the session
files on a real machine -- and including chats that appeared on the other
machine after the set was chosen.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.chat_directory import (
    Association,
    ChatDirectory,
    ChatEntry,
    ChatKind,
    ProjectView,
)
from codexsync.semantic_transfer import TransferAction, build_transfer_plan
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState
from codexsync.session_scope import (
    build_session_scope,
    load_session_scope,
    save_session_scope,
    scope_path_for,
    session_hash,
)

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


def chat(session_id: str, project: str | None, *, parent: str | None = None, kind=ChatKind.TOP_LEVEL) -> ChatEntry:
    return ChatEntry(
        session_id, f"sessions/{session_id}.jsonl", SessionState.ACTIVE, kind,
        Association.BOUND if project else Association.NONE, project,
        "2026-09-16T10:00:00Z", "C:/work", 10, parent,
    )


def directory(*chats: ChatEntry, projects: dict[str, str] | None = None) -> ChatDirectory:
    views = {
        project_id: ProjectView(project_id, name, (f"C:/work/{name}",))
        for project_id, name in (projects or {}).items()
    }
    return ChatDirectory(views, tuple(chats), "electron-v2")


class ScopeExpansionTests(unittest.TestCase):
    def test_a_project_brings_every_chat_in_it(self) -> None:
        scope = build_session_scope(
            directory(
                chat("a", "p1"), chat("b", "p1"), chat("c", "p2"),
                projects={"p1": "chloya", "p2": "other"},
            ),
            projects=("p1",),
        )
        self.assertEqual(scope.chat_count, 2)
        self.assertEqual(
            set(scope.session_hashes), {session_hash("a"), session_hash("b")},
        )

    def test_a_project_can_be_named_by_its_name(self) -> None:
        """A person reads names; the plan stores ids."""
        scope = build_session_scope(
            directory(chat("a", "p1"), projects={"p1": "project-chloya"}),
            projects=("project-chloya",),
        )
        self.assertEqual(scope.projects, ("p1",))
        self.assertEqual(scope.chat_count, 1)

    def test_a_name_that_matches_nothing_simply_adds_nothing(self) -> None:
        scope = build_session_scope(
            directory(chat("a", "p1"), projects={"p1": "chloya"}), projects=("typo",),
        )
        self.assertEqual(scope.chat_count, 0)

    def test_a_chat_can_be_added_on_its_own(self) -> None:
        scope = build_session_scope(
            directory(chat("a", "p1"), chat("loose", None), projects={"p1": "chloya"}),
            chats=("loose",),
        )
        self.assertEqual(set(scope.session_hashes), {session_hash("loose")})

    def test_a_sub_thread_follows_its_parent(self) -> None:
        """Half the session files on a real machine are sub-threads."""
        scope = build_session_scope(
            directory(
                chat("parent", "p1"),
                chat("child", None, parent="parent", kind=ChatKind.SUB_THREAD),
                chat("grandchild", None, parent="child", kind=ChatKind.SUB_THREAD),
                chat("stranger", None),
                projects={"p1": "chloya"},
            ),
            projects=("p1",),
        )
        self.assertEqual(
            set(scope.session_hashes),
            {session_hash("parent"), session_hash("child"), session_hash("grandchild")},
        )

    def test_a_chat_reached_only_through_a_mapping_rule_is_included(self) -> None:
        """`DERIVED_VIA_MAPPING` is how a chat from the other machine arrives."""
        mapped = ChatEntry(
            "m", "sessions/m.jsonl", SessionState.ACTIVE, ChatKind.TOP_LEVEL,
            Association.DERIVED_VIA_MAPPING, "p1", None, "D:/work/chloya", 3, None,
        )
        scope = build_session_scope(
            directory(mapped, projects={"p1": "chloya"}), projects=("p1",),
        )
        self.assertEqual(scope.chat_count, 1)

    def test_the_set_adds_up_the_sizes_of_the_chats_it_covers(self) -> None:
        """The counter on screen is real bytes, not a placeholder zero."""
        import dataclasses

        big = dataclasses.replace(chat("a", "p1"), byte_count=4096)
        small = dataclasses.replace(chat("b", "p1"), byte_count=1024)
        scope = build_session_scope(
            directory(big, small, projects={"p1": "chloya"}), projects=("p1",),
        )
        self.assertEqual(scope.total_bytes, 5120)

    def test_a_chat_this_machine_cannot_place_is_named(self) -> None:
        """Mirrored either way, but it will not appear in Codex here."""
        from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements

        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {"a": "sessions/a.jsonl"})
        scope = build_session_scope(
            directory(chat("a", "p1"), chat("b", "p1"), projects={"p1": "chloya"}),
            projects=("p1",), placements=placements,
        )
        self.assertEqual(scope.not_in_catalog, ("b",))

    def test_without_a_catalogue_nothing_is_claimed_either_way(self) -> None:
        """"Unknown" must not be reported as "every chat will arrive"."""
        from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements

        for status in (PlacementStatus.ABSENT, PlacementStatus.INDETERMINATE):
            with self.subTest(status=status):
                scope = build_session_scope(
                    directory(chat("a", "p1"), projects={"p1": "chloya"}),
                    projects=("p1",), placements=ThreadPlacements(status, {}),
                )
                self.assertEqual(scope.not_in_catalog, ())

    def test_an_empty_set_means_everything(self) -> None:
        scope = build_session_scope(directory(chat("a", "p1")))
        self.assertTrue(scope.is_empty)
        self.assertEqual(scope.session_hashes, ())


class ScopeFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"scope-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_file_is_named_for_the_pair_of_machines(self) -> None:
        path = scope_path_for(self.root, "desktop", "laptop")
        self.assertEqual(path.name, "sessions-scope-desktop-laptop.json")

    def test_only_the_choice_is_stored_never_the_expansion(self) -> None:
        """A frozen list of ids would stop covering tomorrow's chat."""
        scope = build_session_scope(
            directory(chat("a", "p1"), projects={"p1": "chloya"}), projects=("p1",),
        )
        path = save_session_scope(scope, self.root / "set.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["projects"], ["p1"])
        self.assertNotIn("session_hashes", payload)

    def test_a_missing_file_is_an_empty_set_and_not_an_error(self) -> None:
        self.assertTrue(load_session_scope(self.root / "absent.json").is_empty)

    def test_an_unknown_format_is_refused(self) -> None:
        path = self.root / "set.json"
        path.write_text(json.dumps({"format": "something-else"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_session_scope(path)

    def test_what_is_written_reads_back(self) -> None:
        scope = build_session_scope(
            directory(chat("a", "p1"), chat("b", None), projects={"p1": "chloya"}),
            projects=("p1",), chats=("b",),
        )
        path = save_session_scope(scope, self.root / "set.json")
        again = load_session_scope(path)
        self.assertEqual(again.projects, ("p1",))
        self.assertEqual(again.chats, ("b",))


class ScopedPlanTests(unittest.TestCase):
    """The plan itself: what a working set does and does not narrow."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"scoped-plan-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.remote = self.root / "remote"
        (self.local / "sessions").mkdir(parents=True)
        (self.remote / "sessions").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _branch(self, root: Path, name: str, lines: list[str]) -> None:
        path = root / "sessions" / name
        path.write_bytes(b"".join(json.dumps({"r": line}).encode() + b"\n" for line in lines))

    def _descriptor(self, session_id: str, name: str, lines: int) -> SessionDescriptor:
        return SessionDescriptor(session_id, SessionState.ACTIVE, f"sessions/{name}", "0" * 64, 0, lines)

    def _plan(self, *, scope=None):
        return build_transfer_plan(
            SessionCatalog([self._descriptor("kept", "kept.jsonl", 2),
                            self._descriptor("other", "other.jsonl", 2)], {}),
            SessionCatalog([self._descriptor("kept", "kept.jsonl", 1),
                            self._descriptor("other", "other.jsonl", 1)], {}),
            local_root=self.local, remote_root=self.remote,
            source_machine="desktop", target_machine="laptop",
            scope=scope,
        )

    def setUpBranches(self) -> None:
        # Local is ahead of the mirror on both sessions: both would be copied
        # towards the cloud, and neither towards `.codex`.
        self._branch(self.local, "kept.jsonl", ["one", "two"])
        self._branch(self.remote, "kept.jsonl", ["one"])
        self._branch(self.local, "other.jsonl", ["one", "two"])
        self._branch(self.remote, "other.jsonl", ["one"])

    def test_the_mirror_is_written_in_full_whatever_the_set_says(self) -> None:
        """A partial backup is the one thing a working set must never produce."""
        self.setUpBranches()
        plan = self._plan(scope=[session_hash("kept")])
        actions = {item.session_hash: item.action for item in plan.items}
        self.assertEqual(actions[session_hash("kept")], TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(actions[session_hash("other")], TransferAction.FAST_FORWARD_REMOTE)

    def test_a_write_into_codex_outside_the_set_is_held_back(self) -> None:
        self._branch(self.local, "kept.jsonl", ["one"])
        self._branch(self.remote, "kept.jsonl", ["one", "two"])
        self._branch(self.local, "other.jsonl", ["one"])
        self._branch(self.remote, "other.jsonl", ["one", "two"])
        plan = self._plan(scope=[session_hash("kept")])
        actions = {item.session_hash: item.action for item in plan.items}
        self.assertEqual(actions[session_hash("other")], TransferAction.OUT_OF_SCOPE)
        self.assertNotEqual(actions[session_hash("kept")], TransferAction.OUT_OF_SCOPE)

    def test_what_it_would_have_been_is_recorded(self) -> None:
        """So a summary can say what is being held back, not just omit it.

        With `PROVEN_LAYOUTS` empty a write into `.codex` is blocked on the
        layout before the working set ever looks at it, so that is what the
        code names here. The point is that the original decision survives.
        """
        self._branch(self.local, "kept.jsonl", ["one"])
        self._branch(self.remote, "kept.jsonl", ["one", "two"])
        self._branch(self.local, "other.jsonl", ["one"])
        self._branch(self.remote, "other.jsonl", ["one", "two"])
        plan = self._plan(scope=[session_hash("kept")])
        held = next(item for item in plan.items if item.session_hash == session_hash("other"))
        self.assertEqual(held.action, TransferAction.OUT_OF_SCOPE)
        self.assertTrue(
            [code for code in held.codes if code.startswith("WOULD_BE_")],
            f"the original decision is kept: {held.codes}",
        )

    def test_a_conflict_outside_the_set_does_not_block_the_apply(self) -> None:
        self._branch(self.local, "kept.jsonl", ["one", "mine"])
        self._branch(self.remote, "kept.jsonl", ["one", "theirs"])
        self._branch(self.local, "other.jsonl", ["one", "mine"])
        self._branch(self.remote, "other.jsonl", ["one", "theirs"])
        plan = self._plan(scope=[session_hash("kept")])
        actions = {item.session_hash: item.action for item in plan.items}
        self.assertEqual(actions[session_hash("kept")], TransferAction.BLOCKED_CONFLICT)
        self.assertEqual(actions[session_hash("other")], TransferAction.OUT_OF_SCOPE)
        self.assertEqual(
            [item.session_hash for item in plan.blocked_items], [session_hash("kept")],
        )

    def test_the_set_is_part_of_the_plan_id(self) -> None:
        self.setUpBranches()
        one = self._plan(scope=[session_hash("kept")])
        two = self._plan(scope=[session_hash("other")])
        self.assertNotEqual(one.plan_id, two.plan_id)

    def test_rebuilding_with_the_same_set_gives_the_same_id(self) -> None:
        self.setUpBranches()
        first = self._plan(scope=[session_hash("kept")])
        again = self._plan(scope=[session_hash("kept")])
        self.assertEqual(first.plan_id, again.plan_id)

    def test_a_plan_without_a_set_keeps_the_id_it_always_had(self) -> None:
        """An empty scope is left out of the hashed material entirely."""
        self.setUpBranches()
        without = self._plan()
        empty = self._plan(scope=[])
        self.assertEqual(without.plan_id, empty.plan_id)
        self.assertEqual(without.scope, ())

    def test_the_set_survives_saving_and_loading_the_plan(self) -> None:
        from codexsync.semantic_transfer import load_transfer_plan, save_transfer_plan

        self.setUpBranches()
        plan = self._plan(scope=[session_hash("kept")])
        path = save_transfer_plan(plan, self.root / "plan.json")
        again = load_transfer_plan(path)
        self.assertEqual(again.scope, plan.scope)
        self.assertEqual(again.plan_id, plan.plan_id)


if __name__ == "__main__":
    unittest.main()
