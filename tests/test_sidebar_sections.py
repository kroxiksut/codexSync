"""Sidebar sections between machines (CS-408).

Shape observed on Linux 2026-10-10 (Codex 26.1002): a section holds projects
and single chats; a section written into the JSON alone is shown by Codex.
"""
from __future__ import annotations

import copy
import json
import unittest

from codexsync.project_sync import build_project_merge, publication_from_state
from codexsync.sidebar_sections import merge_sections, published_sections

ACCOUNT = "9a238ece-d3bc-4b57-a789-85347e045032"


def _state(sections=(), *, order=None, account=ACCOUNT, projects=("p1", "p2")) -> dict:
    store = {
        "sections": [copy.deepcopy(item) for item in sections],
        "collapsedSectionIds": [],
        "sectionOrder": list(order if order is not None else [f"custom:{item['id']}" for item in sections]),
        "appServerLegacySectionIds": [],
        "appServerMigratedHostIds": ["local"],
    }
    return {
        "local-projects": {
            pid: {"id": pid, "name": pid, "rootPaths": [f"D:\\P\\{pid}"], "createdAt": 1, "updatedAt": 1}
            for pid in projects
        },
        "project-order": list(projects),
        "pinned-project-ids": [],
        "thread-project-assignments": {},
        "electron-persisted-atom-state": {"sidebar-custom-sections-v3": {account: store}, "other-atom": 1},
    }


def _section(section_id, name, *items, host="row-here"):
    return {"id": section_id, "name": name, "hostSectionIds": {"local": host}, "itemKeys": list(items),
            "appearance": None}


def _store(state):
    return state["electron-persisted-atom-state"]["sidebar-custom-sections-v3"][ACCOUNT]


SAME = {"p1": "p1", "p2": "p2"}


class PublishTests(unittest.TestCase):
    def test_a_section_is_published_without_its_row_id_in_sidebar_order(self) -> None:
        state = _state([_section("b", "Second", "codex:project:p2"), _section("a", "First", "codex:project:p1")],
                       order=["custom:a", "custom:b"])
        published = published_sections(state)
        self.assertEqual(published["account"], ACCOUNT)
        self.assertEqual([item["id"] for item in published["sections"]], ["a", "b"])
        self.assertNotIn("hostSectionIds", json.dumps(published))

    def test_nothing_is_published_without_sections_or_with_two_accounts(self) -> None:
        self.assertIsNone(published_sections(_state()))
        state = _state([_section("a", "A")])
        state["electron-persisted-atom-state"]["sidebar-custom-sections-v3"]["other"] = _store(state)
        self.assertIsNone(published_sections(state))

    def test_a_list_without_sections_keeps_its_content_id(self) -> None:
        plain = _state()
        del plain["electron-persisted-atom-state"]
        self.assertEqual(
            publication_from_state(plain, "m").publication_id, publication_from_state(_state(), "m").publication_id,
        )


class MergeTests(unittest.TestCase):
    def test_a_new_section_arrives_with_its_project_and_chat_and_no_row_id(self) -> None:
        peer = published_sections(_state([_section("s", "Work", "codex:thread:local:t1", "codex:project:p1")]))
        state = _state()
        changes = merge_sections(state, peer, projects=SAME, fresh=True, chats=["t1"])
        self.assertGreater(changes, 0)
        store = _store(state)
        self.assertEqual(store["sections"], [{
            "id": "s", "name": "Work", "hostSectionIds": {}, "itemKeys": ["codex:thread:local:t1", "codex:project:p1"],
            "appearance": None,
        }])
        self.assertEqual(store["sectionOrder"], ["custom:s"])
        self.assertEqual(store["threadHostIds"], {"t1": "local"})
        self.assertEqual(state["electron-persisted-atom-state"]["other-atom"], 1)

    def test_a_project_follows_the_merges_translation_and_unknown_items_are_dropped(self) -> None:
        peer = published_sections(_state([_section("s", "W", "codex:project:theirs", "codex:project:gone",
                                                   "codex:thread:local:not-here")]))
        state = _state()
        merge_sections(state, peer, projects={"theirs": "p2"}, fresh=True, chats=["t1"])
        self.assertEqual(_store(state)["sections"][0]["itemKeys"], ["codex:project:p2"])

    def test_a_fresh_list_wins_name_items_and_order_and_moves_an_item_out_of_another_section(self) -> None:
        here = _state([_section("a", "Old", "codex:project:p1"), _section("b", "B", "codex:project:p2")],
                      order=["custom:a", "custom:b"])
        peer = published_sections(_state([_section("b", "B", "codex:project:p1"), _section("a", "New")],
                                         order=["custom:b", "custom:a"]))
        merge_sections(here, peer, projects=SAME, fresh=True)
        store = _store(here)
        by_id = {item["id"]: item for item in store["sections"]}
        self.assertEqual(by_id["a"]["name"], "New")
        self.assertEqual(by_id["a"]["itemKeys"], [])
        self.assertEqual(by_id["b"]["itemKeys"], ["codex:project:p1"])
        self.assertEqual(by_id["a"]["hostSectionIds"], {"local": "row-here"}, "this machine's row id stays")
        self.assertEqual(store["sectionOrder"], ["custom:b", "custom:a"])

    def test_a_list_already_taken_only_adds_what_is_new_and_steals_nothing(self) -> None:
        here = _state([_section("a", "Renamed here", "codex:project:p1")])
        peer = published_sections(_state([_section("a", "Old name"), _section("n", "New", "codex:project:p1",
                                                                           "codex:project:p2")]))
        merge_sections(here, peer, projects=SAME, fresh=False)
        by_id = {item["id"]: item for item in _store(here)["sections"]}
        self.assertEqual(by_id["a"]["name"], "Renamed here")
        self.assertEqual(by_id["a"]["itemKeys"], ["codex:project:p1"])
        self.assertEqual(by_id["n"]["itemKeys"], ["codex:project:p2"])

    def test_a_section_is_never_removed_and_another_account_is_not_touched(self) -> None:
        here = _state([_section("mine", "Only here")])
        before = copy.deepcopy(here)
        merge_sections(here, published_sections(_state([_section("x", "X")], account="someone-else")),
                       projects=SAME, fresh=True)
        self.assertEqual(here, before)
        merge_sections(here, published_sections(_state([_section("x", "X")])), projects=SAME, fresh=True)
        self.assertEqual({item["id"] for item in _store(here)["sections"]}, {"mine", "x"})

    def test_the_second_merge_changes_nothing(self) -> None:
        peer = published_sections(_state([_section("s", "Work", "codex:project:p1")]))
        state = _state()
        merge_sections(state, peer, projects=SAME, fresh=True)
        self.assertEqual(merge_sections(state, peer, projects=SAME, fresh=True), 0)


class ProjectMergeCarriesSectionsTests(unittest.TestCase):
    def test_sections_travel_with_the_project_list(self) -> None:
        desk = _state([_section("s", "Work", "codex:project:p1")])
        lap = _state()
        peer = publication_from_state(desk, "desk")
        plan, state = build_project_merge(
            json.dumps(lap).encode("utf-8"), [peer], machine="lap",
            map_root=lambda _peer, root: root, folder_exists=lambda root: True,
        )
        self.assertGreater(plan.sections_changed, 0)
        self.assertTrue(plan.writes)
        self.assertEqual(_store(state)["sections"][0]["itemKeys"], ["codex:project:p1"])


if __name__ == "__main__":
    unittest.main()
