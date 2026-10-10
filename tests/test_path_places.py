"""Remembered folder places between Windows, Linux and macOS (D-033).

One answer -- "machine A's `D:\\Projects\\atlas` is `/opt/atlas` here" -- has to
serve every direction and every pair of machines, or each handoff asks again.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.path_mapping import apply_path_mapping
from codexsync.path_places import (
    SCOPE_FOLDER,
    SCOPE_PROJECT,
    MachinePlaces,
    PlacesBoard,
    forget,
    learn_place,
    learn_skip,
    learned_rules,
    read_places,
    suggest_here,
    write_places,
)

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox" / "path-places"


def _map(rules, path: str, source: str, target: str) -> str:
    return apply_path_mapping(path, source_machine=source, target_machine=target, rules=rules).target_path


def _board(*machines: MachinePlaces) -> PlacesBoard:
    return PlacesBoard({item.machine: item for item in machines})


class LearningTests(unittest.TestCase):
    def test_a_project_answer_also_learns_the_folder_above_it(self) -> None:
        learned = learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\Projects\\atlas", here="/opt/atlas")
        self.assertEqual(
            [(item.scope, item.there, item.here) for item in learned.places],
            [(SCOPE_PROJECT, "D:\\Projects\\atlas", "/opt/atlas"), (SCOPE_FOLDER, "D:\\Projects", "/opt")],
        )

    def test_different_names_or_a_drive_root_learn_only_the_project(self) -> None:
        renamed = learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\P\\atlas", here="/opt/globe")
        self.assertEqual([item.scope for item in renamed.places], [SCOPE_PROJECT])
        at_root = learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\atlas", here="/atlas")
        self.assertEqual([item.scope for item in at_root.places], [SCOPE_PROJECT])
        only = learn_place(
            MachinePlaces("lin", "linux"), peer="win", there="D:\\P\\atlas", here="/opt/atlas", whole_folder=False,
        )
        self.assertEqual([item.scope for item in only.places], [SCOPE_PROJECT])

    def test_a_new_answer_replaces_the_old_one_and_a_skip_is_undone_by_a_place(self) -> None:
        current = learn_skip(MachinePlaces("lin", "linux"), project_id="p1", name="atlas")
        current = learn_place(current, peer="win", there="D:\\P\\atlas", here="/opt/atlas", project_id="p1")
        current = learn_place(current, peer="win", there="d:/p/ATLAS", here="/srv/atlas", project_id="p1")
        self.assertEqual(current.skipped, ())
        self.assertEqual([item.here for item in current.places if item.scope == SCOPE_PROJECT], ["/srv/atlas"])
        self.assertEqual([item.scope for item in forget(current, project_id="p1").places], [SCOPE_FOLDER])

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\P\\atlas", here="opt/atlas")


class RuleTests(unittest.TestCase):
    def test_one_answer_maps_both_directions(self) -> None:
        board = _board(
            learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\Projects\\atlas", here="/opt/atlas"),
            MachinePlaces("win", "windows"),
        )
        on_linux = learned_rules(board, "lin")
        self.assertEqual(_map(on_linux, "D:\\Projects\\atlas\\src", "win", "lin"), "/opt/atlas/src")
        self.assertEqual(_map(on_linux, "d:/projects/globe", "win", "lin"), "/opt/globe")  # the folder above
        on_windows = learned_rules(board, "win")
        self.assertEqual(_map(on_windows, "/opt/atlas/src", "lin", "win"), "D:\\Projects\\atlas\\src")

    def test_two_machines_placed_from_a_third_map_onto_each_other(self) -> None:
        board = _board(
            learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\Projects\\atlas", here="/opt/atlas"),
            learn_place(
                MachinePlaces("mac", "macos"), peer="win", there="D:\\Projects\\atlas", here="/Users/u/code/atlas",
            ),
            MachinePlaces("win", "windows"),
        )
        self.assertEqual(_map(learned_rules(board, "mac"), "/opt/atlas/x", "lin", "mac"), "/Users/u/code/atlas/x")
        self.assertEqual(_map(learned_rules(board, "lin"), "/users/U/code/atlas", "mac", "lin"), "/opt/atlas")

    def test_case_follows_the_machine_not_the_look_of_the_path(self) -> None:
        board = _board(
            learn_place(MachinePlaces("win", "windows"), peer="lin", there="/home/u/atlas", here="D:\\atlas2"),
            learn_place(MachinePlaces("win2", "windows"), peer="mac", there="/Users/u/atlas", here="D:\\atlas3"),
            MachinePlaces("lin", "linux"),
            MachinePlaces("mac", "macos"),
        )
        with self.assertRaises(Exception):
            _map(learned_rules(board, "win"), "/home/U/atlas", "lin", "win")  # Linux tells case apart
        self.assertEqual(_map(learned_rules(board, "win2"), "/users/u/ATLAS", "mac", "win2"), "D:\\atlas3")

    def test_nothing_is_learned_into_the_machine_about_itself(self) -> None:
        board = _board(learn_place(MachinePlaces("lin", "linux"), peer="lin", there="/a/x", here="/b/x"))
        self.assertEqual(learned_rules(board, "lin"), [])


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        SANDBOX.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [item.unlink() for item in SANDBOX.glob("*.json")])

    def test_written_and_read_back_and_a_tampered_file_is_not_believed(self) -> None:
        places = learn_skip(
            learn_place(MachinePlaces("lin", "linux"), peer="win", there="D:\\P\\atlas", here="/opt/atlas"),
            project_id="p9", name="old",
        )
        path = write_places(SANDBOX, places)
        read = read_places(SANDBOX)
        self.assertEqual(read.own("lin").places, places.places)
        self.assertEqual(read.skipped_here("lin"), frozenset({"p9"}))
        self.assertEqual(read.system_of("lin"), "linux")
        path.write_text(path.read_text(encoding="utf-8").replace("/opt/atlas", "/tmp/atlas"), encoding="utf-8")
        self.assertIn("lin.json", read_places(SANDBOX).unreadable)


class SuggestionTests(unittest.TestCase):
    def test_a_same_named_folder_beside_a_placed_one_is_offered(self) -> None:
        existing = {"/opt/globe"}
        found = suggest_here("D:\\Projects\\globe", ["/opt/atlas", "/srv/atlas"], exists=lambda path: path in existing)
        self.assertEqual(found, ("/opt/globe",))
        self.assertEqual(suggest_here("D:\\Projects\\none", ["/opt/atlas"], exists=lambda path: False), ())
