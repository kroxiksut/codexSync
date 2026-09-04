"""The read-only view a person uses before moving a chat anywhere.

Two things here are load-bearing and neither is cosmetic. A chat is told apart
from a thread an agent spawned by structure, never by what its records say. And
the reason a chat sits under a project is reported, not just the project: bound,
derived from the directory, or only reachable through a path mapping this
machine's Codex knows nothing about.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.chat_directory import (
    Association,
    ChatKind,
    build_chat_directory,
    search_chats,
)
from codexsync.path_mapping import PathMappingRule
from codexsync.session_catalog import SessionState


DESKTOP_ROOT = "D:\\Projects\\alpha"
LAPTOP_ROOT = "C:\\Work\\alpha"


class ChatDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"chats-{uuid.uuid4().hex}"
        (self.root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------

    def _session(
        self,
        session_id: str,
        *,
        cwd: str | None = DESKTOP_ROOT,
        parent: str | None = None,
        thread_source: str | None = "user",
        opening: str | None = "how do I start",
        injected: str | None = None,
        archived: bool = False,
    ) -> Path:
        payload: dict = {"id": session_id, "timestamp": f"2026-08-16T10:00:00.000Z"}
        if cwd:
            payload["cwd"] = cwd
        if parent:
            payload["parent_thread_id"] = parent
        if thread_source:
            payload["thread_source"] = thread_source
        rows: list[dict] = [{"type": "session_meta", "payload": payload}]
        if injected:
            rows.append({
                "type": "response_item",
                "payload": {"role": "user", "content": [{"type": "input_text", "text": injected}]},
            })
        if opening:
            rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": opening}})
        folder = "archived_sessions" if archived else "sessions"
        path = self.root / folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"".join(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n" for row in rows)
        )
        return path

    def _state(self, *, roots=(DESKTOP_ROOT,), assignments=None) -> bytes:
        return json.dumps({
            "local-projects": {
                "p1": {"id": "p1", "name": "alpha", "rootPaths": list(roots),
                       "createdAt": 1, "updatedAt": 2}
            },
            "project-order": ["p1"],
            "thread-project-assignments": assignments or {},
            "app-server-project-id-by-legacy-project-id-by-host": {"host-a": {"p1": "as-p1"}},
        }).encode("utf-8")

    def _directory(self, state=None, **kwargs):
        return build_chat_directory(self.root, state or self._state(), **kwargs)

    # --- what counts as a chat --------------------------------------------

    def test_a_thread_an_agent_spawned_is_not_a_chat(self) -> None:
        self._session("aaaaaaaa-0000-0000-0000-000000000001")
        self._session(
            "bbbbbbbb-0000-0000-0000-000000000002",
            parent="aaaaaaaa-0000-0000-0000-000000000001",
            thread_source="subagent",
        )
        directory = self._directory()
        kinds = {chat.session_id[:8]: chat.kind for chat in directory.chats}
        self.assertEqual(kinds["aaaaaaaa"], ChatKind.TOP_LEVEL)
        self.assertEqual(kinds["bbbbbbbb"], ChatKind.SUB_THREAD)
        self.assertEqual(len(search_chats(directory)), 1, "the default list is chats")
        self.assertEqual(len(search_chats(directory, include_sub_threads=True)), 2)

    def test_a_thread_source_alone_marks_a_sub_thread(self) -> None:
        """Either signal is enough; neither is inferred from the records."""
        self._session("cccccccc-0000-0000-0000-000000000003", thread_source="guardian_review")
        directory = self._directory()
        self.assertEqual(directory.chats[0].kind, ChatKind.SUB_THREAD)

    def test_a_session_older_than_the_field_is_still_a_chat(self) -> None:
        self._session("dddddddd-0000-0000-0000-000000000004", thread_source=None)
        self.assertEqual(self._directory().chats[0].kind, ChatKind.TOP_LEVEL)

    def test_a_chat_is_never_reclassified_by_what_it_says(self) -> None:
        self._session(
            "eeeeeeee-0000-0000-0000-000000000005",
            opening="the following is the Codex agent history whose request action you are assessing",
        )
        self.assertEqual(self._directory().chats[0].kind, ChatKind.TOP_LEVEL)

    # --- why a chat is where it is ----------------------------------------

    def test_a_chat_reaches_its_project_through_its_directory(self) -> None:
        self._session("11111111-0000-0000-0000-000000000001")
        chat = self._directory().chats[0]
        self.assertEqual(chat.association, Association.DERIVED)
        self.assertEqual(chat.project_id, "p1")

    def test_an_explicit_binding_wins_over_the_directory(self) -> None:
        self._session("22222222-0000-0000-0000-000000000002", cwd="D:\\elsewhere")
        state = self._state(assignments={
            "22222222-0000-0000-0000-000000000002": {"projectKind": "local", "projectId": "p1"}
        })
        chat = self._directory(state).chats[0]
        self.assertEqual(chat.association, Association.BOUND)
        self.assertEqual(chat.project_id, "p1")

    def test_a_binding_to_a_project_that_is_gone_is_reported_not_hidden(self) -> None:
        self._session("33333333-0000-0000-0000-000000000003")
        state = self._state(assignments={
            "33333333-0000-0000-0000-000000000003": {"projectKind": "local", "projectId": "vanished"}
        })
        chat = self._directory(state).chats[0]
        self.assertEqual(chat.association, Association.BOUND)
        self.assertIn("BINDING_TO_MISSING_PROJECT", chat.codes)

    def test_a_chat_under_no_root_belongs_nowhere(self) -> None:
        self._session("44444444-0000-0000-0000-000000000004", cwd="D:\\somewhere-else")
        chat = self._directory().chats[0]
        self.assertEqual(chat.association, Association.NONE)
        self.assertIsNone(chat.project_id)

    def test_the_longest_matching_root_wins_and_a_tie_is_refused(self) -> None:
        self._session("55555555-0000-0000-0000-000000000005", cwd=DESKTOP_ROOT + "\\inner")
        state = json.loads(self._state().decode("utf-8"))
        state["local-projects"]["p2"] = {
            "id": "p2", "name": "inner", "rootPaths": [DESKTOP_ROOT + "\\inner"],
            "createdAt": 1, "updatedAt": 2,
        }
        state["project-order"].append("p2")
        chat = self._directory(json.dumps(state).encode("utf-8")).chats[0]
        self.assertEqual(chat.project_id, "p2", "the more specific project owns the chat")

        state["local-projects"]["p2"]["rootPaths"] = [DESKTOP_ROOT]
        tie = self._directory(json.dumps(state).encode("utf-8")).chats[0]
        self.assertEqual(tie.association, Association.NONE)
        self.assertIn("AMBIGUOUS_PROJECT_FOR_CWD", tie.codes)

    # --- the case that appears after a handoff -----------------------------

    def test_a_chat_from_the_other_machine_is_marked_as_reachable_only_by_rule(self) -> None:
        """On the laptop the project is on C: while the chat still says D:."""
        self._session("66666666-0000-0000-0000-000000000006", cwd=DESKTOP_ROOT)
        state = self._state(roots=(LAPTOP_ROOT,))

        without_rules = self._directory(state).chats[0]
        self.assertEqual(
            without_rules.association, Association.NONE,
            "this is what Codex itself shows: the chat is under no project",
        )

        rule = PathMappingRule("moved", "desktop", "laptop", DESKTOP_ROOT, LAPTOP_ROOT)
        with_rules = self._directory(
            state, rules=[rule], source_machine="desktop", target_machine="laptop"
        ).chats[0]
        self.assertEqual(with_rules.association, Association.DERIVED_VIA_MAPPING)
        self.assertEqual(with_rules.project_id, "p1")

    def test_a_mapping_never_upgrades_itself_into_a_plain_derivation(self) -> None:
        """The distinction is the warning: Codex does not read these rules."""
        self._session("77777777-0000-0000-0000-000000000007", cwd=DESKTOP_ROOT)
        rule = PathMappingRule("moved", "desktop", "laptop", DESKTOP_ROOT, LAPTOP_ROOT)
        directory = self._directory(
            self._state(roots=(LAPTOP_ROOT,)), rules=[rule],
            source_machine="desktop", target_machine="laptop",
        )
        self.assertNotEqual(directory.chats[0].association, Association.DERIVED)

    def test_a_binding_is_preferred_over_a_mapping(self) -> None:
        self._session("88888888-0000-0000-0000-000000000008", cwd=DESKTOP_ROOT)
        state = self._state(roots=(LAPTOP_ROOT,), assignments={
            "88888888-0000-0000-0000-000000000008": {"projectKind": "local", "projectId": "p1"}
        })
        rule = PathMappingRule("moved", "desktop", "laptop", DESKTOP_ROOT, LAPTOP_ROOT)
        chat = self._directory(
            state, rules=[rule], source_machine="desktop", target_machine="laptop"
        ).chats[0]
        self.assertEqual(chat.association, Association.BOUND)

    # --- titles ------------------------------------------------------------

    def test_the_title_is_what_the_person_typed_not_what_was_injected(self) -> None:
        self._session(
            "99999999-0000-0000-0000-000000000009",
            injected="<environment_context><cwd>D:\\Projects\\alpha</cwd></environment_context>",
            opening="rename the module please",
        )
        self.assertEqual(self._directory().chats[0].title, "rename the module please")

    def test_a_chat_with_nothing_typed_yet_has_no_title(self) -> None:
        self._session("aaaaaaa0-0000-0000-0000-00000000000a", opening=None)
        self.assertIsNone(self._directory().chats[0].title)

    def test_an_older_chat_falls_back_to_its_first_untagged_message(self) -> None:
        self._session(
            "aaaaaaa1-0000-0000-0000-00000000000b",
            injected="please do the thing", opening=None,
        )
        self.assertEqual(self._directory().chats[0].title, "please do the thing")

    # --- searching ---------------------------------------------------------

    def test_search_matches_the_opening_message_and_the_directory(self) -> None:
        self._session("bbbbbbb0-0000-0000-0000-00000000000c", opening="fix the parser")
        self._session("bbbbbbb1-0000-0000-0000-00000000000d", opening="write the docs")
        directory = self._directory()
        self.assertEqual(len(search_chats(directory, text="parser")), 1)
        self.assertEqual(len(search_chats(directory, text="Projects")), 2, "the cwd matches too")
        self.assertEqual(len(search_chats(directory, text="nothing here")), 0)

    def test_search_can_ask_for_the_chats_that_belong_nowhere(self) -> None:
        self._session("ccccccc0-0000-0000-0000-00000000000e")
        self._session("ccccccc1-0000-0000-0000-00000000000f", cwd="D:\\unrelated")
        directory = self._directory()
        self.assertEqual(len(search_chats(directory, project="none")), 1)
        self.assertEqual(len(search_chats(directory, project="alpha")), 1)

    def test_search_can_isolate_the_chats_a_handoff_left_behind(self) -> None:
        self._session("ddddddd0-0000-0000-0000-000000000010", cwd=DESKTOP_ROOT)
        rule = PathMappingRule("moved", "desktop", "laptop", DESKTOP_ROOT, LAPTOP_ROOT)
        directory = self._directory(
            self._state(roots=(LAPTOP_ROOT,)), rules=[rule],
            source_machine="desktop", target_machine="laptop",
        )
        stranded = search_chats(directory, association=Association.DERIVED_VIA_MAPPING)
        self.assertEqual(len(stranded), 1)

    def test_a_rejected_session_is_kept_out_of_the_list_unless_asked_for(self) -> None:
        broken = self.root / "sessions" / "broken.jsonl"
        broken.write_bytes(b'{"type":"session_meta","payload":{"id":"eeeeeee0-0000-0000-0000-000000000011"}}\n{oops\n')
        directory = self._directory()
        self.assertEqual(directory.chats[0].state, SessionState.INVALID)
        self.assertEqual(len(search_chats(directory)), 0)
        self.assertEqual(len(search_chats(directory, include_invalid=True)), 1)

    # --- resolving what a person typed ------------------------------------

    def test_a_chat_is_found_by_an_id_prefix(self) -> None:
        self._session("fffffff0-0000-0000-0000-000000000012")
        directory = self._directory()
        self.assertEqual(len(directory.find("fffffff0")), 1)
        self.assertEqual(len(directory.find("FFFFFFF0")), 1, "case is not an identity")
        self.assertEqual(len(directory.find("nope")), 0)

    def test_a_project_is_found_by_name_or_id(self) -> None:
        self._session("fffffff1-0000-0000-0000-000000000013")
        directory = self._directory()
        self.assertEqual(len(directory.project_named("alpha")), 1)
        self.assertEqual(len(directory.project_named("p1")), 1)
        self.assertEqual(len(directory.project_named("zzz")), 0)

    def test_an_unrecognised_state_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_chat_directory(self.root, b'{"local-projects": 5}')


if __name__ == "__main__":
    unittest.main()
