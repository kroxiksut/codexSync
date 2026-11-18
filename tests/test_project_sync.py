"""Carrying the project list between machines (CS-333).

0.1 copied `.codex-global-state.json` whole, so a laptop showed the desktop's
projects after a sync. 0.2 stopped copying it (it holds per-machine values) and
for a while carried nothing at all: the laptop kept its own sidebar. These
tests pin the replacement -- a merge, never an overwrite, never a removal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from codexsync.app import (
    PlaceStatus,
    ProjectsNotCarried,
    path_rules,
    place_project,
    project_places,
    run_handoff,
    sync_projects,
)
from codexsync.config import load_config
from codexsync.path_mapping import apply_path_mapping
from codexsync.exceptions import ConfigError, SafetyPreconditionError
from codexsync.guardian_models import ValidationStatus
from codexsync.guardian_schema import ELECTRON_V2_SCHEMA, validate_global_state_references
from codexsync.project_sync import (
    FOLDER_MISSING_HERE,
    NO_PLACE_HERE,
    ROOT_DIFFERS,
    ROOT_PLACED,
    ROOTS_COLLAPSE,
    SEVERAL_LOCAL_CANDIDATES,
    SKIPPED_HERE,
    MergeKind,
    Publication,
    RootMappingAmbiguous,
    RootNotPlaced,
    project_root_rules,
    build_project_merge,
    publication_from_state,
    publication_path,
    read_board,
    root_key,
    write_publication,
)
from codexsync.safety_gate import ProcessState

try:
    from tests.test_handoff import _Workspace
except ImportError:  # collected with tests/ itself on sys.path
    from test_handoff import _Workspace


def _project(project_id: str, name: str, root: str) -> dict:
    return {"createdAt": 1, "id": project_id, "name": name, "rootPaths": [root], "updatedAt": 2}


def _state(projects: list[dict], *, order=None, pinned=(), bindings=None, extra=None) -> dict:
    state = {
        "electron-main-window-bounds": {"x": 1},
        "local-projects": {item["id"]: item for item in projects},
        "project-order": list(order if order is not None else [item["id"] for item in projects]),
        "pinned-project-ids": list(pinned),
        "thread-project-assignments": {
            thread: {"projectKind": "local", "projectId": project}
            for thread, project in (bindings or {}).items()
        },
    }
    state.update(extra or {})
    return state


def _bytes(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")


def _same(_peer: str, root: str) -> str:
    return root


def _merge(local: dict, *peers: Publication, folders=lambda root: True, map_root=_same):
    return build_project_merge(
        _bytes(local), peers, machine="laptop", map_root=map_root, folder_exists=folders,
    )


class PublicationTests(unittest.TestCase):
    def test_carries_projects_order_pins_and_bindings_but_nothing_per_machine(self) -> None:
        state = _state(
            [_project("a", "Alpha", "D:\\P\\alpha"), _project("b", "Beta", "D:\\P\\beta")],
            order=["b", "a"], pinned=["a"], bindings={"t1": "a"},
        )
        publication = publication_from_state(state, "desktop")
        self.assertEqual(publication.order, ("b", "a"))
        self.assertEqual(publication.pinned, ("a",))
        self.assertEqual(publication.bindings, {"t1": "a"})
        self.assertNotIn("electron-main-window-bounds", json.dumps(publication.to_json()))

    def test_an_app_server_binding_is_carried_as_the_shared_legacy_id(self) -> None:
        state = _state([_project("a", "Alpha", "D:\\P\\alpha")], extra={
            "app-server-project-id-by-legacy-project-id-by-host": {"local:C:\\x": {"a": "srv-1"}},
        })
        state["thread-project-assignments"] = {"t1": {"projectKind": "app-server", "projectId": "srv-1"}}
        self.assertEqual(publication_from_state(state, "desktop").bindings, {"t1": "a"})

    def test_content_id_ignores_when_it_was_written(self) -> None:
        state = _state([_project("a", "Alpha", "D:\\P\\alpha")])
        first = publication_from_state(state, "desktop", now=lambda: "2026-10-01T00:00:00Z")
        second = publication_from_state(state, "desktop", now=lambda: "2026-10-02T00:00:00Z")
        self.assertEqual(first.publication_id, second.publication_id)

    def test_a_touched_updated_at_keeps_the_content_id(self) -> None:
        """CS-445: only what the merge reads of a project is in its id."""
        first = publication_from_state(_state([_project("a", "Alpha", "D:\\P\\alpha")]), "desk")
        touched = _project("a", "Alpha", "D:\\P\\alpha")
        touched["updatedAt"] = 99
        second = publication_from_state(_state([touched]), "desk")
        self.assertEqual(first.publication_id, second.publication_id)
        self.assertEqual(second.projects["a"]["updatedAt"], 99, "the record itself is published whole")
        renamed = publication_from_state(_state([_project("a", "Beta", "D:\\P\\alpha")]), "desk")
        self.assertNotEqual(first.publication_id, renamed.publication_id)

    def test_projectless_chats_are_carried_beside_the_list_not_in_its_id(self) -> None:
        plain = _state([_project("a", "Alpha", "D:\\P\\alpha")])
        apart = _state([_project("a", "Alpha", "D:\\P\\alpha")], extra={"projectless-thread-ids": ["t2", "t1", 3]})
        publication = publication_from_state(apart, "desktop")
        self.assertEqual(publication.projectless, ("t1", "t2"))
        # Taking a chat out of a project there must not make that machine's
        # order and pins win here again.
        self.assertEqual(publication.publication_id, publication_from_state(plain, "desktop").publication_id)
        root = Path(__file__).resolve().parents[1] / "test-sandbox" / "project-board-projectless"
        self.addCleanup(lambda: [p.unlink() for p in root.glob("*.json")])
        write_publication(root, publication)
        self.assertEqual(read_board(root).publications["desktop"].projectless, ("t1", "t2"))

    def test_a_list_written_before_projectless_was_published_still_reads(self) -> None:
        root = Path(__file__).resolve().parents[1] / "test-sandbox" / "project-board-old"
        self.addCleanup(lambda: [p.unlink() for p in root.glob("*.json")])
        path = write_publication(root, publication_from_state(_state([_project("a", "A", "D:\\a")]), "desktop"))
        raw =json.loads(path.read_text(encoding="utf-8"))
        del raw["projectless"], raw["entry_digest"]
        from codexsync.project_sync import _digest

        raw["entry_digest"] = _digest(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(read_board(root).publications["desktop"].projectless, ())

    def test_a_damaged_or_misplaced_file_is_not_believed(self) -> None:
        root = Path(__file__).resolve().parents[1] / "test-sandbox" / "project-board"
        self.addCleanup(lambda: [p.unlink() for p in root.glob("*.json")])
        write_publication(root, publication_from_state(_state([_project("a", "A", "D:\\a")]), "desktop"))
        good = publication_path(root, "desktop")
        (root / "laptop.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
        board = read_board(root)
        self.assertIn("desktop", board.publications)
        self.assertIn("laptop.json", board.unreadable)
        good.write_text(good.read_text(encoding="utf-8").replace('"A"', '"B"'), encoding="utf-8")
        self.assertIn("desktop.json", read_board(root).unreadable)


class MergeTests(unittest.TestCase):
    def test_a_project_only_the_peer_has_is_added_as_its_own_entry(self) -> None:
        local = _state([_project("l1", "Mine", "D:\\P\\mine")])
        peer = publication_from_state(_state([_project("d1", "Theirs", "D:\\P\\theirs")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual([item.kind for item in plan.items], [MergeKind.ADD])
        self.assertEqual(state["local-projects"]["d1"], _project("d1", "Theirs", "D:\\P\\theirs"))
        self.assertIn("l1", state["local-projects"])  # nothing removed
        self.assertEqual(state["electron-main-window-bounds"], {"x": 1})  # untouched
        self.assertIn(
            validate_global_state_references(_bytes(state)).status,
            {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING},
        )

    def test_projects_reach_a_codex_that_never_had_one(self) -> None:
        """The state a fresh Linux Codex wrote (2026-10-09): no project, no order key."""
        local = {
            "local-projects": {},
            "projectless-thread-ids": ["t0"],
            "selected-project": None,
            "app-server-projects-migration-by-host": {
                "local:/home/someone/.codex": {"version": 1, "projectsMigrated": True},
            },
        }
        peer = publication_from_state(
            _state([_project("d1", "One", "D:\\P\\one"), _project("d2", "Two", "D:\\P\\two")],
                   order=["d2", "d1"], bindings={"t1": "d1"}),
            "desktop",
        )
        plan, state = _merge(local, peer)
        self.assertEqual([item.kind for item in plan.items], [MergeKind.ADD, MergeKind.ADD])
        self.assertEqual(state["project-order"], ["d2", "d1"])
        self.assertEqual(state["thread-project-assignments"], {"t1": {"projectKind": "local", "projectId": "d1"}})
        report = validate_global_state_references(_bytes(state))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, ELECTRON_V2_SCHEMA)

    def test_a_merge_that_adds_nothing_does_not_invent_an_order(self) -> None:
        local = {"local-projects": {}, "selected-project": None}
        plan, state = _merge(local, publication_from_state(_state([]), "desktop"))
        self.assertFalse(plan.order_changed)
        self.assertNotIn("project-order", state)

    def test_the_same_folder_under_another_id_is_matched_not_duplicated(self) -> None:
        local = _state([_project("l1", "Proj", "d:/p/proj/")], bindings={})
        peer = publication_from_state(
            _state([_project("d1", "Proj", "D:\\P\\Proj")], pinned=["d1"], bindings={"t9": "d1"}), "desktop",
        )
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].kind, MergeKind.MATCHED)
        self.assertEqual(plan.items[0].local_project_id, "l1")
        self.assertEqual(set(state["local-projects"]), {"l1"})
        self.assertEqual(state["pinned-project-ids"], ["l1"])
        self.assertEqual(state["thread-project-assignments"]["t9"], {"projectKind": "local", "projectId": "l1"})

    def test_peer_pins_and_order_win_for_shared_projects_and_local_ones_keep_theirs(self) -> None:
        local = _state(
            [_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b"), _project("own", "Own", "D:\\own")],
            order=["own", "a", "b"], pinned=["b", "own"],
        )
        peer = publication_from_state(
            _state([_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b")], order=["b", "a"], pinned=["a"]),
            "desktop",
        )
        plan, state = _merge(local, peer)
        self.assertEqual(state["project-order"], ["b", "a", "own"])
        self.assertEqual(state["pinned-project-ids"], ["a", "own"])
        self.assertTrue(plan.pins_changed and plan.order_changed)

    def test_a_missing_folder_is_added_and_said(self) -> None:
        peer = publication_from_state(_state([_project("d1", "Gone", "D:\\nowhere")]), "desktop")
        plan, state = _merge(_state([]), peer, folders=lambda root: False)
        self.assertEqual(plan.items[0].kind, MergeKind.ADD)
        self.assertEqual(plan.items[0].codes, (FOLDER_MISSING_HERE,))
        self.assertIn("d1", state["local-projects"])

    def test_a_mapped_root_is_written_as_it_reads_here(self) -> None:
        peer = publication_from_state(_state([_project("d1", "P", "D:\\Work\\p")]), "desktop")
        plan, state = _merge(_state([]), peer, map_root=lambda peer, root: root.replace("D:\\Work", "E:\\W"))
        self.assertEqual(state["local-projects"]["d1"]["rootPaths"], ["E:\\W\\p"])

    def test_two_local_candidates_or_an_ambiguous_mapping_leave_the_project_alone(self) -> None:
        local = _state([_project("x", "X", "D:\\p"), _project("y", "Y", "D:\\p")])
        peer = publication_from_state(_state([_project("d1", "P", "D:\\p")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].codes, (SEVERAL_LOCAL_CANDIDATES,))
        self.assertEqual(set(state["local-projects"]), {"x", "y"})

        def ambiguous(peer, root):
            raise RootMappingAmbiguous("AMBIGUOUS_MAPPING")

        plan, _ = _merge(_state([]), peer, map_root=ambiguous)
        self.assertEqual(plan.items[0].kind, MergeKind.AMBIGUOUS)
        self.assertFalse(plan.writes)

    def test_same_id_with_another_root_keeps_this_machines_root(self) -> None:
        local = _state([_project("a", "A", "D:\\here")])
        peer = publication_from_state(_state([_project("a", "A", "D:\\there")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].codes, (ROOT_DIFFERS,))
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["D:\\here"])

    def test_merging_what_is_already_here_changes_nothing(self) -> None:
        state = _state([_project("a", "A", "D:\\a")], pinned=["a"], bindings={"t": "a"})
        plan, _ = _merge(state, publication_from_state(state, "desktop"))
        self.assertFalse(plan.writes)

    def test_the_plan_id_follows_the_state_bytes(self) -> None:
        peer = publication_from_state(_state([_project("d1", "P", "D:\\p")]), "desktop")
        first, _ = _merge(_state([]), peer)
        second, _ = _merge(_state([_project("z", "Z", "D:\\z")]), peer)
        self.assertNotEqual(first.plan_id, second.plan_id)

    def test_root_key_compares_windows_paths_loosely_and_posix_paths_exactly(self) -> None:
        self.assertEqual(root_key("D:/P/x/"), root_key("d:\\p\\X"))
        self.assertEqual(root_key("\\\\?\\D:\\p"), root_key("D:\\p"))
        self.assertNotEqual(root_key("/home/a"), root_key("/home/A"))



def _windows_to_linux(places: dict[str, str]):
    """A mapper as a Linux machine has it: a Windows path is placed or refused."""
    def map_root(_peer: str, root: str) -> str:
        for there, here in places.items():
            if root_key(root) == root_key(there) or root_key(root).startswith(root_key(there) + "\\"):
                return here + root[len(there):].replace("\\", "/")
        if ":" in root:
            raise RootNotPlaced(root)
        return root
    return map_root


class CrossSystemMergeTests(unittest.TestCase):
    """D-033 / CS-402 / CS-403: Windows lists arriving on Linux."""

    def _peer(self, *projects, bindings=None):
        return publication_from_state(_state(list(projects), bindings=bindings), "desk")

    def test_a_windows_root_without_a_place_is_carried_as_it_is(self) -> None:
        """D-034: Codex on Linux shows such a project and opens its chats (2026-10-10)."""
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"), bindings={"t1": "a"})
        plan, state = _merge(_state([]), peer, map_root=_windows_to_linux({}))
        self.assertEqual([(item.kind, item.codes) for item in plan.items], [(MergeKind.ADD, (NO_PLACE_HERE,))])
        self.assertEqual(len(plan.unplaced), 1)
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["D:\\P\\alpha"])
        self.assertEqual(state["thread-project-assignments"], {"t1": {"projectKind": "local", "projectId": "a"}})
        self.assertEqual(plan.taken, {"desk": peer.publication_id})

    def test_one_folder_under_two_ids_of_two_windows_machines_is_not_carried_twice(self) -> None:
        """Seen on Linux 2026-10-10: seven projects would have arrived a second time."""
        placed = publication_from_state(_state([_project("k", "codexSync", "D:\\Projects\\codexSync")]), "krox")
        other = self._peer(_project("m", "codexSync", "D:\\Projects\\codexSync"))
        local = _state([_project("k", "codexSync", "/home/u/Projects/codexSync")])
        plan, state = build_project_merge(
            _bytes(local), [other], machine="lin", map_root=_windows_to_linux({}),
            folder_exists=lambda root: True, taken_before=[placed],
        )
        self.assertEqual([(item.kind, item.local_project_id) for item in plan.items], [(MergeKind.MATCHED, "k")])
        self.assertEqual(set(state["local-projects"]), {"k"})

    def test_a_project_carried_as_it_is_is_matched_by_that_root_next_time(self) -> None:
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"))
        _, state = _merge(_state([]), peer, map_root=_windows_to_linux({}))
        again, _ = _merge(state, peer, map_root=_windows_to_linux({}))
        self.assertEqual([(item.kind, item.codes) for item in again.items], [(MergeKind.MATCHED, ())])
        self.assertFalse(again.writes)

    def test_a_chat_under_a_root_carried_as_it_is_is_bound_not_left_to_codex(self) -> None:
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"))
        plan, state = build_project_merge(
            _bytes(_state([])), [peer], machine="lin", map_root=_windows_to_linux({}),
            folder_exists=lambda root: False, chats=[("t1", "D:\\P\\alpha\\src")],
            native=lambda root: ":" not in root,
        )
        self.assertEqual(plan.bound_by_folder, 1)
        self.assertEqual(state["thread-project-assignments"], {"t1": {"projectKind": "local", "projectId": "a"}})

    def test_two_folders_mapped_onto_one_are_both_left_alone(self) -> None:
        peer = self._peer(
            _project("a", "Rake", "D:\\Projects\\Rake"), _project("b", "Rake", "D:\\Cloud\\Rake"),
            bindings={"t1": "a", "t2": "b"},
        )
        mapper = _windows_to_linux({"D:\\Projects": "/home/u/p", "D:\\Cloud": "/home/u/p"})
        plan, state = _merge(_state([]), peer, map_root=mapper)
        self.assertEqual({item.codes for item in plan.items}, {(ROOTS_COLLAPSE,)})
        self.assertEqual(state["local-projects"], {})
        self.assertEqual(state["thread-project-assignments"], {})

    def test_the_same_folder_under_two_ids_is_still_one_folder(self) -> None:
        peer = self._peer(_project("a", "Rake", "D:\\Projects\\Rake"), _project("b", "Rake", "d:/projects/rake"))
        plan, _ = _merge(_state([]), peer, map_root=_windows_to_linux({"D:\\Projects": "/home/u/p"}))
        self.assertNotIn((ROOTS_COLLAPSE,), {item.codes for item in plan.items})

    def test_a_skipped_project_is_not_carried(self) -> None:
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"))
        plan, state = build_project_merge(
            _bytes(_state([])), [peer], machine="lin", map_root=_windows_to_linux({}),
            folder_exists=lambda root: True, skipped=frozenset({"a"}),
        )
        self.assertEqual([(item.kind, item.codes) for item in plan.items], [(MergeKind.SKIPPED, (SKIPPED_HERE,))])
        self.assertFalse(plan.writes)

    def test_a_place_given_after_the_list_was_taken_still_adds_the_project_and_its_chats(self) -> None:
        peer = self._peer(
            _project("a", "Alpha", "D:\\P\\alpha"), _project("b", "Beta", "D:\\P\\beta"),
            bindings={"t1": "a", "t2": "b"},
        )
        local = _state([_project("b", "Beta", "/home/u/beta")], bindings={"t2": "b"})
        plan, state = build_project_merge(
            _bytes(local), [], machine="lin", map_root=_windows_to_linux({"D:\\P\\alpha": "/home/u/alpha"}),
            folder_exists=lambda root: True, taken_before=[peer],
        )
        self.assertEqual([item.kind for item in plan.items], [MergeKind.ADD])
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["/home/u/alpha"])
        self.assertEqual(state["project-order"], ["b", "a"])
        self.assertEqual(state["thread-project-assignments"]["t1"], {"projectKind": "local", "projectId": "a"})
        self.assertEqual(plan.taken, {})  # the list itself was taken before

    def test_a_project_carried_with_a_root_naming_nothing_follows_its_place(self) -> None:
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"))
        local = _state([_project("a", "Alpha", "D:\\P\\alpha")])
        exists = {"/home/u/alpha"}
        plan, state = build_project_merge(
            _bytes(local), [], machine="lin", map_root=_windows_to_linux({"D:\\P": "/home/u"}),
            folder_exists=lambda root: root in exists, taken_before=[peer],
        )
        self.assertEqual([(item.kind, item.codes) for item in plan.items], [(MergeKind.PLACED, (ROOT_PLACED,))])
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["/home/u/alpha"])
        self.assertEqual(state["local-projects"]["a"]["name"], "Alpha")

    def test_a_root_chosen_here_is_never_moved(self) -> None:
        peer = self._peer(_project("a", "Alpha", "D:\\P\\alpha"))
        local = _state([_project("a", "Alpha", "/srv/alpha")])
        plan, state = build_project_merge(
            _bytes(local), [peer], machine="lin", map_root=_windows_to_linux({"D:\\P": "/home/u"}),
            folder_exists=lambda root: True,
        )
        self.assertEqual([item.kind for item in plan.items], [MergeKind.MATCHED])
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["/srv/alpha"])


class BoundByFolderTests(unittest.TestCase):
    """Chats Codex placed by their folder on the other machine arrive under that project."""

    def _plan(self, local, peer, chats, places):
        return build_project_merge(
            _bytes(local), [peer], machine="lin", map_root=_windows_to_linux(places),
            folder_exists=lambda root: True, chats=chats,
        )

    def test_a_chat_without_binding_follows_the_project_its_folder_was_under_there(self) -> None:
        peer = publication_from_state(_state([_project("a", "Alpha", "D:\\P\\alpha")]), "desk")
        plan, state = self._plan(
            _state([]), peer,
            [("t1", "D:\\P\\alpha\\src"), ("t2", "D:\\Other"), ("t3", "/home/u/alpha"), ("t4", None)],
            {"D:\\P": "/home/u"},
        )
        self.assertEqual(plan.bound_by_folder, 1)
        self.assertEqual(state["thread-project-assignments"], {"t1": {"projectKind": "local", "projectId": "a"}})

    def test_a_binding_of_a_kind_not_carried_is_still_a_binding(self) -> None:
        # CS-526: a chat bound to a remote project is not "unbound".
        peer = publication_from_state(_state([_project("a", "Alpha", "D:\\P\\alpha")]), "desk")
        remote = {"projectKind": "remote", "projectId": "r-1"}
        local = _state([], extra={"thread-project-assignments": {"t1": remote}})
        plan, state = self._plan(local, peer, [("t1", "D:\\P\\alpha\\src")], {"D:\\P": "/home/u"})
        self.assertEqual(plan.bound_by_folder, 0)
        self.assertEqual(state["thread-project-assignments"], {"t1": remote})

    def test_a_binding_or_a_folder_codex_places_by_itself_is_left_alone(self) -> None:
        peer = publication_from_state(_state([_project("a", "Alpha", "D:\\P\\alpha")]), "desk")
        local = _state([_project("z", "Zed", "D:\\P\\alpha")], bindings={"t1": "z"})
        plan, _ = self._plan(local, peer, [("t1", "D:\\P\\alpha"), ("t2", "D:\\P\\alpha\\x")], {})
        self.assertEqual(plan.bound_by_folder, 0)

    def test_a_chat_codex_shows_without_a_project_there_stays_without_one(self) -> None:
        """CS-406: its folder is the project's folder, and still it is "just a chat"."""
        peer = publication_from_state(
            _state([_project("a", "Alpha", "D:\\P\\alpha")], extra={"projectless-thread-ids": ["t1"]}), "desk",
        )
        plan, state = self._plan(
            _state([]), peer, [("t1", "D:\\P\\alpha"), ("t2", "D:\\P\\alpha\\src")], {"D:\\P":"/home/u"},
        )
        self.assertEqual(plan.bound_by_folder, 1)
        self.assertEqual(state["thread-project-assignments"], {"t2": {"projectKind": "local", "projectId": "a"}})

    def test_a_chat_made_projectless_here_is_not_bound_by_its_folder(self) -> None:
        peer = publication_from_state(_state([_project("a", "Alpha", "D:\\P\\alpha")]), "desk")
        plan, state = self._plan(
            _state([], extra={"projectless-thread-ids": ["t1"]}), peer,
            [("t1", "D:\\P\\alpha\\src")], {"D:\\P":"/home/u"},
        )
        self.assertEqual(plan.bound_by_folder, 0)
        self.assertEqual(state["thread-project-assignments"], {})

    def test_a_peer_binding_does_not_rebind_a_chat_made_projectless_here(self) -> None:
        """CS-444: an explicit peer binding honours "no project" too."""
        peer = publication_from_state(
            _state([_project("a", "Alpha", "D:\\P\\alpha")], bindings={"t1": "a", "t2": "a"}), "desk",
        )
        plan, state = self._plan(
            _state([], extra={"projectless-thread-ids": ["t1"]}), peer, [], {"D:\\P": "/home/u"},
        )
        self.assertEqual(state["thread-project-assignments"], {"t2": {"projectKind": "local", "projectId": "a"}})

    def test_a_peer_binding_for_a_chat_projectless_there_is_skipped(self) -> None:
        peer = publication_from_state(
            _state(
                [_project("a", "Alpha", "D:\\P\\alpha")], bindings={"t1": "a"},
                extra={"projectless-thread-ids": ["t1"]},
            ), "desk",
        )
        _, state = self._plan(_state([]), peer, [], {"D:\\P": "/home/u"})
        self.assertEqual(state["thread-project-assignments"], {})

    def test_a_collapsed_project_passes_on_no_chat(self) -> None:
        peer = publication_from_state(_state([
            _project("a", "Rake", "D:\\Projects\\Rake"), _project("b", "Rake", "D:\\Cloud\\Rake"),
        ]), "desk")
        local = _state([_project("a", "Rake", "/home/u/p/Rake")])
        _, state = self._plan(
            local, peer, [("t1", "D:\\Projects\\Rake"), ("t2", "D:\\Cloud\\Rake\\x")],
            {"D:\\Projects": "/home/u/p", "D:\\Cloud": "/home/u/p"},
        )
        self.assertEqual(state["thread-project-assignments"], {"t1": {"projectKind": "local", "projectId": "a"}})


class ProjectRootRuleTests(unittest.TestCase):
    def test_one_project_under_two_roots_is_a_rule_and_equal_roots_are_none(self) -> None:
        peer = publication_from_state(
            _state([_project("a", "A", "D:\\P\\a"), _project("b", "B", "D:\\P\\b")]), "desk",
        )
        local = _state([_project("a", "A", "/opt/a"), _project("b", "B", "D:\\P\\b")])
        rules = project_root_rules(local, [peer], "lin", case_sensitive=lambda machine: False)
        self.assertEqual([(rule.source_prefix, rule.target_prefix, rule.case_sensitive) for rule in rules],
                         [("D:\\P\\a", "/opt/a", False)])


class TwoMachineTests(_Workspace):
    """The scenario from 2026-10-01: desktop's projects must show on the laptop."""

    def codex_state(self, local: Path, state: dict) -> Path:
        path = local / ".codex-global-state.json"
        path.write_bytes(_bytes(state))
        return path

    def read_state(self, local: Path) -> dict:
        return json.loads((local / ".codex-global-state.json").read_text(encoding="utf-8"))

    def test_desktop_projects_reach_the_laptop_and_the_laptops_go_back(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state(
            [_project("lab", "LabTakt", str(self.root / "LabTakt")),
             _project("chl", "project-chloya", str(self.root / "chloya"))],
            pinned=["lab", "chl"], bindings={"chat-1": "lab"},
        ))
        self.codex_state(laptop_codex, _state([_project("own", "NetRuleRouter", str(self.root / "nrr"))]))

        run_handoff(desktop)
        run_handoff(laptop)
        on_laptop = self.read_state(laptop_codex)
        self.assertEqual(set(on_laptop["local-projects"]), {"lab", "chl", "own"})
        self.assertEqual(on_laptop["pinned-project-ids"], ["lab", "chl"])
        self.assertEqual(on_laptop["project-order"][:2], ["lab", "chl"])
        self.assertEqual(
            on_laptop["thread-project-assignments"]["chat-1"], {"projectKind": "local", "projectId": "lab"},
        )
        self.assertEqual(on_laptop["electron-main-window-bounds"], {"x": 1})

        run_handoff(desktop)
        self.assertIn("own", self.read_state(desktop_codex)["local-projects"])

        # Settled: another round changes nothing on either side.
        self.assertFalse(sync_projects(laptop).plan.writes)
        self.assertFalse(sync_projects(desktop).plan.writes)

        # The history says what the run did, not only that it ran.
        from codexsync.recovery import list_history

        (run,) = list_history(laptop, family="project-sync")[-1:]
        self.assertEqual(run.counts["projects_added"], 2)
        self.assertEqual(run.origin, "handoff")

    def test_a_list_already_taken_is_not_reapplied_over_a_local_change(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state([_project("a", "A", "D:\\a")], pinned=["a"]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        run_handoff(laptop)
        state = self.read_state(laptop_codex)
        state["pinned-project-ids"] = []  # unpinned on the laptop afterwards
        self.codex_state(laptop_codex, state)
        self.assertEqual(sync_projects(laptop).pending, ())
        self.assertFalse(sync_projects(laptop).plan.writes)

    def test_a_chat_taken_out_of_a_project_is_published_again(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        self.codex_state(desktop_codex, _state([_project("a", "A", str(self.root / "a"))]))
        run_handoff(desktop)
        state = self.read_state(desktop_codex)
        state["projectless-thread-ids"] = ["t9"]
        self.codex_state(desktop_codex, state)
        plan = sync_projects(desktop).plan
        self.assertTrue(sync_projects(desktop, confirm_plan=plan.plan_id).published)
        from codexsync.app import projects_root

        published = read_board(projects_root(load_config(desktop))).publications["desktop"]
        self.assertEqual(published.projectless, ("t9",))

    def test_apply_needs_codex_closed_and_the_previewed_id(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        # A path of the system the suite runs on: another system's would wait for its place.
        self.codex_state(desktop_codex, _state([_project("a", "A", str(self.root / "a"))]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        preview = sync_projects(laptop)
        self.assertEqual(len(preview.plan.added), 1)
        with self.assertRaises(ConfigError):
            sync_projects(laptop, confirm_plan="not-the-id")
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        self.assertEqual(self.read_state(laptop_codex)["local-projects"], {})
        self.gate.state = ProcessState.STOPPED
        done = sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        self.assertEqual(done.written, 2)  # the project, and its place in the order
        backups = list((self.workspace / "backups").rglob(".codex-global-state.json"))
        self.assertTrue(backups, "the replaced state must be backed up first")

    def test_a_project_from_another_system_comes_without_a_folder_then_follows_one(self) -> None:
        """D-033/D-034 end to end: carried as it is, placed once, read backwards by the other machine."""
        windows_here = os.name == "nt"
        # A root written by the *other* kind of system, wherever the suite runs.
        foreign = "/srv/work/atlas" if windows_here else "D:\\Work\\atlas"
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state([_project("atl", "atlas", foreign)]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        chat = laptop_codex / "sessions" / "2026" / "10" / "09" / "rollout-chat-1.jsonl"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "chat-1", "cwd": foreign + ("/src" if windows_here else "\\src"),
        }}) + "\n", encoding="utf-8")

        report = project_places(laptop)
        self.assertEqual(report.waiting, ())
        self.assertEqual([item.status for item in report.without_folder], [PlaceStatus.UNPLACED])
        first = sync_projects(laptop)
        self.assertEqual([(item.kind, item.codes) for item in first.plan.items], [(MergeKind.ADD, (NO_PLACE_HERE,))])
        # Codex is not relied on to place a chat under another system's root.
        self.assertEqual(first.plan.bound_by_folder, 1)
        sync_projects(laptop, confirm_plan=first.plan.plan_id)
        state = self.read_state(laptop_codex)
        self.assertEqual(state["local-projects"]["atl"]["rootPaths"], [foreign])
        self.assertEqual([item.status for item in project_places(laptop).without_folder], [PlaceStatus.MISSING_HERE])

        here = self.root / "laptop-work" / "atlas"
        here.mkdir(parents=True)
        place_project(laptop, "atlas", path=str(here))
        preview = sync_projects(laptop)
        self.assertEqual([item.kind for item in preview.plan.items], [MergeKind.PLACED])
        sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        state = self.read_state(laptop_codex)
        self.assertEqual(state["local-projects"]["atl"]["rootPaths"], [str(here)])
        self.assertEqual(
            state["thread-project-assignments"]["chat-1"], {"projectKind": "local", "projectId": "atl"},
        )
        self.assertEqual(project_places(laptop).without_folder, ())

        # The desktop reads the laptop's answer backwards: no question there.
        back = apply_path_mapping(
            str(here / "docs"), source_machine="laptop", target_machine="desktop",
            rules=path_rules(load_config(desktop)),
        ).target_path
        self.assertEqual(back.replace("\\", "/"), (foreign + "/docs").replace("\\", "/"))

    def test_an_answer_over_an_unreadable_own_file_is_refused(self) -> None:
        # CS-527: read as empty, the new answer replaced every earlier one.
        from codexsync.app import places_root
        from codexsync.exceptions import FailSafeError

        foreign = "/srv/work/atlas" if os.name == "nt" else "D:\\Work\\atlas"
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state([_project("atl", "atlas", foreign)]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        place_project(laptop, "atlas", skip=True)
        own = places_root(load_config(laptop)) / "laptop.json"
        damaged = own.read_text(encoding="utf-8")[:-10]
        own.write_text(damaged, encoding="utf-8")
        with self.assertRaises(FailSafeError):
            place_project(laptop, "atlas", forget=True)
        self.assertEqual(own.read_text(encoding="utf-8"), damaged)

    def test_a_machine_without_a_global_state_still_hands_off(self) -> None:
        laptop, _ = self.machine("laptop")
        with self.assertRaises(ProjectsNotCarried):
            sync_projects(laptop)
        self.assertEqual(run_handoff(laptop).projects_added, 0)


class RemovedProjectTests(_Workspace):
    """D-039: a project removed in Codex on one machine (CS-497).

    Before it, the next sync took the project back from the other machine,
    every time, so a carried project could be removed nowhere.
    """

    def codex_state(self, local: Path, state: dict) -> None:
        (local / ".codex-global-state.json").write_bytes(_bytes(state))

    def read_state(self, local: Path) -> dict:
        return json.loads((local / ".codex-global-state.json").read_text(encoding="utf-8"))

    def removed_in_codex(self, local: Path, project_id: str) -> None:
        state = self.read_state(local)
        del state["local-projects"][project_id]
        state["project-order"] = [item for item in state["project-order"] if item != project_id]
        state["pinned-project-ids"] = [item for item in state["pinned-project-ids"] if item != project_id]
        # A binding to a project that is gone would leave the state invalid.
        state["thread-project-assignments"] = {
            thread: value for thread, value in state["thread-project-assignments"].items()
            if value.get("projectId") != project_id
        }
        self.codex_state(local, state)

    def policy(self, config: Path, value: str) -> None:
        text = config.read_text(encoding="utf-8")
        config.write_text(text.replace('session_mode = "all"', f'session_mode = "all"\nremoved_projects = "{value}"', 1),
                          encoding="utf-8")

    def two_machines(self, *, desktop_policy: str | None = None, laptop_policy: str | None = None):
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        for config, value in ((desktop, desktop_policy), (laptop, laptop_policy)):
            if value:
                self.policy(config, value)
        self.codex_state(desktop_codex, _state(
            [_project("a", "Alpha", str(self.root / "alpha")), _project("b", "Beta", str(self.root / "beta"))],
            pinned=["b"], bindings={"chat-b": "b", "chat-a": "a"},
        ))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        run_handoff(laptop)
        self.assertEqual(set(self.read_state(laptop_codex)["local-projects"]), {"a", "b"})
        return desktop, desktop_codex, laptop, laptop_codex

    def test_by_default_a_project_removed_here_stays_removed_and_waits_for_an_answer(self) -> None:
        from codexsync.app import project_removals

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        self.removed_in_codex(laptop_codex, "b")

        result = run_handoff(laptop)
        self.assertNotIn("b", self.read_state(laptop_codex)["local-projects"])
        report = project_removals(laptop)
        self.assertEqual([(item.project_id, item.confirmed, item.here) for item in report.items], [("b", False, True)])
        self.assertEqual(report.policy, "ask")
        self.assertEqual(result.projects_waiting, 1)
        # Not on purpose yet: the other machine keeps it.
        run_handoff(desktop)
        self.assertIn("b", self.read_state(desktop_codex)["local-projects"])
        # And asking again changes nothing: the question stands, the project stays out.
        run_handoff(laptop)
        self.assertNotIn("b", self.read_state(laptop_codex)["local-projects"])
        self.assertEqual(len(project_removals(laptop).waiting), 1)

    def test_on_purpose_the_other_machine_removes_it_too_once(self) -> None:
        from codexsync.app import decide_project_removal, project_removals

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        decide_project_removal(laptop, "Beta", everywhere=True)
        self.assertEqual(project_removals(laptop).waiting, ())

        result = run_handoff(desktop)
        state = self.read_state(desktop_codex)
        self.assertEqual(set(state["local-projects"]), {"a"})
        self.assertNotIn("b", state["pinned-project-ids"])
        self.assertNotIn("b", state["project-order"])
        self.assertNotIn("chat-b", state["thread-project-assignments"])
        self.assertIn("chat-a", state["thread-project-assignments"])
        self.assertEqual(result.projects_removed, 1)
        report = validate_global_state_references(_bytes(state))
        self.assertIn(report.status, {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING})
        self.assertFalse(sync_projects(desktop).plan.writes)
        self.assertFalse(sync_projects(laptop).plan.writes)

        # Made again on the desktop later, under its own new id: kept there and carried back.
        state["local-projects"]["b2"] = _project("b2", "Beta", str(self.root / "beta"))
        state["project-order"].append("b2")
        self.codex_state(desktop_codex, state)
        run_handoff(desktop)
        self.assertIn("b2", self.read_state(desktop_codex)["local-projects"])
        run_handoff(laptop)
        self.assertIn("b2", self.read_state(laptop_codex)["local-projects"])
        self.assertEqual(project_removals(laptop).items, (), "the removal ends once the folder is a project again")

    def test_by_mistake_the_next_sync_takes_it_back(self) -> None:
        from codexsync.app import decide_project_removal, project_removals

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        decide_project_removal(laptop, "b", everywhere=False)
        run_handoff(laptop)
        self.assertIn("b", self.read_state(laptop_codex)["local-projects"])
        self.assertEqual(project_removals(laptop).items, ())
        run_handoff(desktop)
        self.assertIn("b", self.read_state(desktop_codex)["local-projects"])

    def test_remove_means_on_purpose_without_a_question(self) -> None:
        desktop, desktop_codex, laptop, laptop_codex = self.two_machines(laptop_policy="remove")
        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        run_handoff(desktop)
        self.assertEqual(set(self.read_state(desktop_codex)["local-projects"]), {"a"})

    def test_bring_back_takes_it_back_at_once(self) -> None:
        desktop, desktop_codex, laptop, laptop_codex = self.two_machines(laptop_policy="bring_back")
        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        self.assertIn("b", self.read_state(laptop_codex)["local-projects"])

    def test_a_third_machine_that_still_lists_it_does_not_bring_it_back(self) -> None:
        desktop, desktop_codex, laptop, laptop_codex = self.two_machines(laptop_policy="remove")
        office, office_codex = self.machine("office")
        self.codex_state(office_codex, _state([]))
        run_handoff(office)
        self.assertIn("b", self.read_state(office_codex)["local-projects"])

        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        run_handoff(desktop)
        # The office has not synced yet and still publishes it.
        run_handoff(laptop)
        run_handoff(desktop)
        self.assertNotIn("b", self.read_state(laptop_codex)["local-projects"])
        self.assertNotIn("b", self.read_state(desktop_codex)["local-projects"])
        run_handoff(office)
        self.assertNotIn("b", self.read_state(office_codex)["local-projects"])

    def test_the_same_folder_under_a_new_id_is_not_a_removal(self) -> None:
        # Codex re-created every project under a new id once (2026-09-13).
        from codexsync.app import project_removals

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        state = self.read_state(laptop_codex)
        state["local-projects"]["b-new"] = {**state["local-projects"].pop("b"), "id": "b-new"}
        state["project-order"] = ["b-new" if item == "b" else item for item in state["project-order"]]
        state["pinned-project-ids"] = []
        self.codex_state(laptop_codex, state)
        run_handoff(laptop)
        self.assertEqual(project_removals(laptop).items, ())

    def test_the_preview_and_its_apply_have_one_plan_id(self) -> None:
        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        self.removed_in_codex(laptop_codex, "b")
        preview = sync_projects(laptop)
        self.assertEqual([(item.peer_project_id, item.kind) for item in preview.plan.held], [("b", MergeKind.HELD)])
        self.assertEqual(sync_projects(laptop).plan.plan_id, preview.plan.plan_id)
        done = sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        self.assertTrue(done.published)

    def test_an_unreadable_own_list_refuses_instead_of_forgetting_removals(self) -> None:
        from codexsync.app import projects_root
        from codexsync.exceptions import FailSafeError

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        own = publication_path(projects_root(load_config(laptop)), "laptop")
        own.write_text(own.read_text(encoding="utf-8")[:-20], encoding="utf-8")
        with self.assertRaises(FailSafeError):
            sync_projects(laptop)

    def test_the_console_lists_and_answers(self) -> None:
        import contextlib
        import io

        from codexsync.app import project_removals
        from codexsync.cli import main

        desktop, desktop_codex, laptop, laptop_codex = self.two_machines()
        self.removed_in_codex(laptop_codex, "b")
        run_handoff(laptop)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["-c", str(laptop), "projects", "removed", "--json"]), 0)
        self.assertEqual([item["name"] for item in json.loads(out.getvalue())["removed"]], ["Beta"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["-c", str(laptop), "projects", "removed", "--everywhere", "Beta"]), 0)
            self.assertEqual(main(["-c", str(laptop), "projects", "removed", "--bring-back", "nope"]), 4)
        self.assertTrue(project_removals(laptop).items[0].confirmed)

    def test_an_unknown_policy_is_refused(self) -> None:
        laptop, _ = self.machine("laptop")
        self.policy(laptop, "forget")
        with self.assertRaises(ConfigError):
            load_config(laptop)


class RemovalMergeTests(unittest.TestCase):
    def test_a_removal_elsewhere_takes_the_project_out_of_sections_and_the_sidebar_order(self) -> None:
        from codexsync.project_sync import REMOVED_ELSEWHERE, Removal

        account = {
            "sections": [{"id": "s1", "name": "Work", "itemKeys": ["codex:project:b", "codex:project:a"]}],
            "sectionOrder": ["custom:s1"],
        }
        local = _state(
            [_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b")],
            extra={"electron-persisted-atom-state": {
                "sidebar-custom-sections-v3": {"acct": account},
                "unified-sidebar-project-order-v1": ["codex:project:b", "codex:project:a"],
            }},
        )
        peer = Publication(
            "desktop", ELECTRON_V2_SCHEMA, {"a": _project("a", "A", "D:\\a")}, ("a",), (), {},
            removed=(Removal("r1", "b", "B", ("D:\\b",), "2026-10-26T00:00:00Z", True),),
        )
        plan, state = _merge(local, peer)
        self.assertEqual([(item.kind, item.codes) for item in plan.removed], [(MergeKind.REMOVE, (REMOVED_ELSEWHERE,))])
        atoms = state["electron-persisted-atom-state"]
        self.assertEqual(atoms["unified-sidebar-project-order-v1"], ["codex:project:a"])
        self.assertEqual(atoms["sidebar-custom-sections-v3"]["acct"]["sections"][0]["itemKeys"], ["codex:project:a"])
        self.assertEqual(plan.followed, ("r1",))

    def test_a_removal_not_confirmed_elsewhere_is_ignored(self) -> None:
        from codexsync.project_sync import Removal

        local = _state([_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b")])
        peer = Publication(
            "desktop", ELECTRON_V2_SCHEMA, {"a": _project("a", "A", "D:\\a")}, ("a",), (), {},
            removed=(Removal("r1", "b", "B", ("D:\\b",), "", False),),
        )
        plan, state = _merge(local, peer)
        self.assertEqual(plan.removed, ())
        self.assertIn("b", state["local-projects"])

    def test_a_removal_older_readers_never_saw_keeps_the_file_readable(self) -> None:
        from codexsync.project_sync import Removal

        publication = Publication(
            "desktop", ELECTRON_V2_SCHEMA, {}, (), (), {},
            removed=(Removal("r1", "b", "B", ("D:\\b",), "t", True),), followed=("x",),
        )
        plain = Publication("desktop", ELECTRON_V2_SCHEMA, {}, (), (), {})
        self.assertEqual(publication.publication_id, plain.publication_id)
        self.assertNotIn("removed", plain.to_json())


if __name__ == "__main__":
    unittest.main()
