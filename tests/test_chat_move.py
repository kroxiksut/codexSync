"""Putting a chat under a project — the one command that writes a binding.

It writes a single line of JSON per chat, so the tests are about everything
guarding that line: the plan describes reality when it is applied, nothing is
written without the exact id from the preview, the shape matches the schema, the
previous state is in a verified backup, and a running Codex stops all of it.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import move_chats
from codexsync.chat_directory import Association
from codexsync.chat_move import ChatMoveKind
from codexsync.exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from codexsync.guardian_models import ValidationReport, ValidationStatus
from codexsync.mutation_journal import JournalStore
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision


ALPHA = "D:\\Projects\\alpha"
BETA = "D:\\Projects\\beta"


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class _RunningGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.RUNNING, False, "Codex is running")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        raise SafetyPreconditionError("Codex is running")


class ChatMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"chat-move-{uuid.uuid4().hex}"
        self.state_dir = self.root / "local-state"
        (self.state_dir / "sessions").mkdir(parents=True)
        self.state_file = self.state_dir / ".codex-global-state.json"
        self._write_state(self._state())
        self.config_path = self._write_config()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------

    def _chat(self, session_id: str, *, cwd: str = ALPHA, parent: str | None = None,
              thread_source: str = "user") -> None:
        payload: dict = {
            "id": session_id, "cwd": cwd, "thread_source": thread_source,
            "timestamp": "2026-08-16T10:00:00.000Z",
        }
        if parent:
            payload["parent_thread_id"] = parent
        rows = [
            {"type": "session_meta", "payload": payload},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "do the thing"}},
        ]
        path = self.state_dir / "sessions" / f"{session_id}.jsonl"
        path.write_bytes(b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in rows))

    def _state(self, assignments: dict | None = None) -> dict:
        return {
            "local-projects": {
                "p-alpha": {"id": "p-alpha", "name": "alpha", "rootPaths": [ALPHA],
                            "createdAt": 1, "updatedAt": 2},
                "p-beta": {"id": "p-beta", "name": "beta", "rootPaths": [BETA],
                           "createdAt": 3, "updatedAt": 4},
            },
            "project-order": ["p-alpha", "p-beta"],
            "thread-project-assignments": assignments or {},
            "app-server-project-id-by-legacy-project-id-by-host": {},
        }

    def _write_state(self, payload: dict) -> None:
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_config(self) -> Path:
        path = self.root / "config.toml"
        path.write_text(
            textwrap.dedent(
                f"""
                [identity]
                machine_id = "machine-a"

                [sync]
                mode = "cold"
                session_mode = "all"

                [paths]
                local_state_dir = "{self.state_dir.as_posix()}"
                cloud_root_dir = "{(self.root / 'cloud').as_posix()}"
                backup_dir = "{(self.root / 'backups').as_posix()}"
                temp_dir = "{(self.root / '.tmp').as_posix()}"

                [guardian]
                root_dir = "{(self.root / 'guardian').as_posix()}"

                [semantic]
                root_dir = "{(self.root / 'semantic').as_posix()}"

                [targets]
                include_roots = ["sessions"]

                [state]
                manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _move(self, refs, to="beta", gate=None, **kwargs):
        with patch("codexsync.app._make_safety_gate", return_value=gate or _StoppedGate()):
            return move_chats(self.config_path, chat_refs=refs, to_project=to, **kwargs)

    # --- what a move means ------------------------------------------------

    def test_a_chat_that_belongs_nowhere_is_bound(self) -> None:
        self._chat("11111111-0000-0000-0000-000000000001", cwd="D:\\unrelated")
        plan, written = self._move(["11111111"])
        self.assertEqual(written, 0, "a preview writes nothing")
        self.assertEqual(plan.actions[0].kind, ChatMoveKind.BIND)
        self.assertEqual(plan.actions[0].from_association, Association.NONE)

    def test_a_chat_bound_elsewhere_is_repointed(self) -> None:
        self._chat("22222222-0000-0000-0000-000000000002")
        self._write_state(self._state({
            "22222222-0000-0000-0000-000000000002": {"projectKind": "local", "projectId": "p-alpha"}
        }))
        plan, _ = self._move(["22222222"])
        self.assertEqual(plan.actions[0].kind, ChatMoveKind.REBIND)
        self.assertEqual(plan.actions[0].from_project_id, "p-alpha")

    def test_a_chat_already_there_writes_nothing(self) -> None:
        self._chat("33333333-0000-0000-0000-000000000003")
        self._write_state(self._state({
            "33333333-0000-0000-0000-000000000003": {"projectKind": "local", "projectId": "p-beta"}
        }))
        plan, _ = self._move(["33333333"])
        self.assertEqual(plan.actions[0].kind, ChatMoveKind.ALREADY_THERE)
        self.assertEqual(plan.writing_actions, ())
        self.assertIn("NOTHING_TO_DO", plan.codes)

    def test_a_chat_that_only_reaches_its_project_by_path_records_that(self) -> None:
        """Moving it is exactly what makes it stop following the directory."""
        self._chat("44444444-0000-0000-0000-000000000004", cwd=ALPHA)
        plan, _ = self._move(["44444444"])
        self.assertEqual(plan.actions[0].from_association, Association.DERIVED)
        self.assertEqual(plan.actions[0].from_project_id, "p-alpha")

    # --- writing ----------------------------------------------------------

    def test_confirming_the_plan_writes_the_binding_and_nothing_else(self) -> None:
        self._chat("55555555-0000-0000-0000-000000000005")
        before = json.loads(self.state_file.read_text(encoding="utf-8"))
        plan, _ = self._move(["55555555"])

        _, written = self._move(["55555555"], confirm_plan=plan.plan_id)
        self.assertEqual(written, 1)
        after = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            after["thread-project-assignments"],
            {"55555555-0000-0000-0000-000000000005": {"projectKind": "local", "projectId": "p-beta"}},
            "written in the shape the detected schema uses",
        )
        self.assertEqual(after["local-projects"], before["local-projects"])
        self.assertEqual(after["project-order"], before["project-order"])

    def test_several_chats_move_together(self) -> None:
        self._chat("66666666-0000-0000-0000-000000000006")
        self._chat("77777777-0000-0000-0000-000000000007")
        plan, _ = self._move(["66666666", "77777777"])
        _, written = self._move(["66666666", "77777777"], confirm_plan=plan.plan_id)
        self.assertEqual(written, 2)
        assignments = json.loads(self.state_file.read_text(encoding="utf-8"))["thread-project-assignments"]
        self.assertEqual(len(assignments), 2)

    def test_the_previous_state_is_in_a_verifiable_backup(self) -> None:
        self._chat("88888888-0000-0000-0000-000000000008")
        before = self.state_file.read_bytes()
        plan, _ = self._move(["88888888"])
        self._move(["88888888"], confirm_plan=plan.plan_id)

        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertTrue(manifest["committed"])
        snapshot = self.root / "backups" / manifest["snapshot"]
        self.assertEqual((snapshot / ".codex-global-state.json").read_bytes(), before)

    def test_the_journal_reaches_a_terminal_state(self) -> None:
        self._chat("99999999-0000-0000-0000-000000000009")
        plan, _ = self._move(["99999999"])
        self._move(["99999999"], confirm_plan=plan.plan_id)
        self.assertEqual(JournalStore(self.root / ".tmp").non_terminal(), [])

    def test_dry_run_checks_everything_and_writes_nothing(self) -> None:
        self._chat("aaaaaaa0-0000-0000-0000-00000000000a")
        before = self.state_file.read_bytes()
        plan, _ = self._move(["aaaaaaa0"])
        _, written = self._move(["aaaaaaa0"], confirm_plan=plan.plan_id, dry_run=True)
        self.assertEqual(written, 1, "it reports what it would write")
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertFalse(list((self.root / "backups").glob("*")))

    # --- refusals ---------------------------------------------------------

    def test_nothing_is_written_without_the_confirmation(self) -> None:
        self._chat("bbbbbbb0-0000-0000-0000-00000000000b")
        before = self.state_file.read_bytes()
        self._move(["bbbbbbb0"])
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_stale_confirmation_is_refused(self) -> None:
        """The id covers the state bytes, so a state that moved refuses itself."""
        self._chat("ccccccc0-0000-0000-0000-00000000000c")
        plan, _ = self._move(["ccccccc0"])
        self._write_state(self._state({"someone-else": {"projectKind": "local", "projectId": "p-alpha"}}))
        before = self.state_file.read_bytes()
        with self.assertRaises(ConfigError):
            self._move(["ccccccc0"], confirm_plan=plan.plan_id)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_wrong_confirmation_is_refused(self) -> None:
        self._chat("ddddddd0-0000-0000-0000-00000000000d")
        before = self.state_file.read_bytes()
        with self.assertRaises(ConfigError):
            self._move(["ddddddd0"], confirm_plan="not-the-plan-id")
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_move_is_blocked_while_codex_runs(self) -> None:
        self._chat("eeeeeee0-0000-0000-0000-00000000000e")
        plan, _ = self._move(["eeeeeee0"])
        before = self.state_file.read_bytes()
        with self.assertRaises(SafetyPreconditionError):
            self._move(["eeeeeee0"], confirm_plan=plan.plan_id, gate=_RunningGate())
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_preview_still_works_while_codex_runs(self) -> None:
        """Reading is allowed; the result simply cannot be confirmed as it is."""
        self._chat("fffffff0-0000-0000-0000-00000000000f")
        plan, _ = self._move(["fffffff0"], gate=_RunningGate())
        self.assertEqual(len(plan.actions), 1)

    def test_an_ambiguous_chat_reference_is_refused(self) -> None:
        self._chat("abcd0000-0000-0000-0000-000000000010")
        self._chat("abcd1111-0000-0000-0000-000000000011")
        with self.assertRaises(ConfigError):
            self._move(["abcd"])

    def test_an_unknown_chat_or_project_is_refused(self) -> None:
        self._chat("11110000-0000-0000-0000-000000000012")
        with self.assertRaises(ConfigError):
            self._move(["nope"])
        with self.assertRaises(ConfigError):
            self._move(["11110000"], to="no-such-project")

    def test_a_spawned_thread_cannot_be_named_by_accident(self) -> None:
        self._chat("22220000-0000-0000-0000-000000000013")
        self._chat(
            "33330000-0000-0000-0000-000000000014",
            parent="22220000-0000-0000-0000-000000000013", thread_source="subagent",
        )
        with self.assertRaises(ConfigError):
            self._move(["33330000"])

    def test_naming_a_spawned_thread_on_purpose_is_still_refused_at_apply(self) -> None:
        """It is not shown under a project, so pinning it would state a fiction."""
        self._chat("44440000-0000-0000-0000-000000000015")
        self._chat(
            "55550000-0000-0000-0000-000000000016",
            parent="44440000-0000-0000-0000-000000000015", thread_source="subagent",
        )
        plan, _ = self._move(["55550000"], include_sub_threads=True)
        self.assertIn("NOT_A_CHAT", plan.codes)
        before = self.state_file.read_bytes()
        with self.assertRaises(ConflictError):
            self._move(["55550000"], confirm_plan=plan.plan_id, include_sub_threads=True)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_write_that_goes_wrong_after_the_replace_is_rolled_back(self) -> None:
        """The state is already replaced here, so only the backup can undo it."""
        self._chat("66660000-0000-0000-0000-000000000017")
        plan, _ = self._move(["66660000"])
        before = self.state_file.read_bytes()

        reports = [
            ValidationReport(ValidationStatus.PASS, ()),          # the candidate is fine
            ValidationReport(ValidationStatus.INVALID, ("BROKEN",)),  # re-read after replace
        ]
        with patch(
            "codexsync.app.validate_global_state_references", side_effect=reports
        ):
            with self.assertRaises(FailSafeError):
                self._move(["66660000"], confirm_plan=plan.plan_id)

        self.assertEqual(
            self.state_file.read_bytes(), before,
            "the verified backup is restored byte for byte",
        )
        journals = JournalStore(self.root / ".tmp")
        self.assertEqual(journals.non_terminal(), [], "the rollback closed the journal")
        written = sorted(JournalStore(self.root / ".tmp").root.glob("*.json"))
        self.assertTrue(written, "the attempt still left durable evidence")
        record = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(record["family"], "chats")
        self.assertEqual(record["state"], "FAILED")


if __name__ == "__main__":
    unittest.main()
