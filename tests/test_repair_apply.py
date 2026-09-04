"""End-to-end cover for the one command that rewrites Codex state in place.

`repair-projects apply` edits `.codex-global-state.json`. The P2 acceptance
criteria require it to refuse a stale plan, an ambiguous or unsupported action,
a running or unknown Codex and a backup it cannot verify, to change only the
approved fields, and to leave a full verifiable backup behind.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_repair_projects
from codexsync.config import load_config
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.repair_plan import RepairActionKind, build_repair_plan, save_repair_plan
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState


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


class _UnknownGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.UNKNOWN, False, "cannot enumerate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        raise FailSafeError("Cannot verify Codex process state")


_EMPTY_STATE = {
    "local-projects": {},
    "project-order": [],
    "thread-project-assignments": {},
}


class RepairApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"repair-apply-{uuid.uuid4().hex}"
        self.state_dir = self.root / "local-state"
        (self.state_dir / "sessions").mkdir(parents=True)
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.state_file = self.state_dir / ".codex-global-state.json"
        self._write_state(_EMPTY_STATE)
        self.config_path = self._write_config()
        self.plan_path = self.root / "plan.json"
        self.plan = self._build_plan()
        save_repair_plan(self.plan, self.plan_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_state(self, payload: dict) -> None:
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_config(self) -> Path:
        prefix = str(self.root).replace("\\", "/")
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

                [[path_mappings]]
                rule_id = "identity"
                source_machine = "source"
                target_machine = "target"
                from = "{prefix}"
                to = "{prefix}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _build_plan(self):
        descriptor = SessionDescriptor(
            "thread-a", SessionState.ACTIVE, "sessions/a.jsonl", "a" * 64, 1, 1,
            cwd=str(self.repo),
        )
        # Use the rules exactly as the config parser produces them, so the plan's
        # mapping digest matches what apply recomputes from the same config.
        rules = load_config(self.config_path).path_mappings
        return build_repair_plan(
            SessionCatalog([descriptor], {}),
            self.state_file.read_bytes(),
            source_machine="source",
            target_machine="target",
            rules=rules,
            volatile=False,
        )

    def _apply(self, gate=None, **kwargs):
        with patch("codexsync.app._make_safety_gate", return_value=gate or _StoppedGate()):
            return apply_repair_projects(
                self.config_path,
                plan_path=self.plan_path,
                confirm_plan=kwargs.pop("confirm_plan", self.plan.plan_id),
                **kwargs,
            )

    def test_apply_adds_only_the_approved_fields(self) -> None:
        approved = self._apply()
        self.assertGreater(approved, 0)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(set(state), set(_EMPTY_STATE), "no new top-level keys may appear")
        self.assertEqual(len(state["local-projects"]), 1)
        self.assertEqual(state["project-order"], list(state["local-projects"]))
        self.assertEqual(list(state["thread-project-assignments"]), ["thread-a"])
        project_id = next(iter(state["local-projects"]))
        self.assertEqual(state["thread-project-assignments"]["thread-a"], project_id)
        self.assertEqual(state["local-projects"][project_id]["root"], str(self.repo))

    def test_apply_leaves_a_verifiable_backup_of_the_previous_state(self) -> None:
        before = self.state_file.read_bytes()
        self._apply()
        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1, "exactly one committed backup manifest")
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "codexsync-backup-v1")
        self.assertTrue(manifest["committed"])
        snapshot = self.root / "backups" / manifest["snapshot"]
        backed_up = snapshot / ".codex-global-state.json"
        self.assertTrue(backed_up.is_file())
        self.assertEqual(backed_up.read_bytes(), before, "the backup must hold the pre-apply bytes")

    def test_dry_run_changes_nothing(self) -> None:
        before = self.state_file.read_bytes()
        approved = self._apply(dry_run=True)
        self.assertGreater(approved, 0)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertFalse(
            list((self.root / "backups").glob("*")),
            "a preview must not create backups",
        )

    def test_dry_run_is_blocked_while_codex_runs(self) -> None:
        with self.assertRaises(SafetyPreconditionError):
            self._apply(gate=_RunningGate(), dry_run=True)
        with self.assertRaises(FailSafeError):
            self._apply(gate=_UnknownGate(), dry_run=True)

    def test_apply_is_blocked_while_codex_runs(self) -> None:
        before = self.state_file.read_bytes()
        with self.assertRaises(SafetyPreconditionError):
            self._apply(gate=_RunningGate())
        with self.assertRaises(FailSafeError):
            self._apply(gate=_UnknownGate())
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_wrong_confirmation_id_is_refused(self) -> None:
        before = self.state_file.read_bytes()
        with self.assertRaises(ConfigError):
            self._apply(confirm_plan="not-the-plan-id")
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_state_changed_after_the_scan_is_refused(self) -> None:
        # The plan pins the state hash; editing state after the scan makes it stale.
        self._write_state({**_EMPTY_STATE, "project-order": []} | {"local-projects": {"x": {"root": "C:/x"}}})
        with self.assertRaises(FailSafeError):
            self._apply()

    def test_unreadable_plan_file_is_reported_as_bad_input(self) -> None:
        """A malformed --plan must exit 4, not surface as an internal error."""
        broken = self.root / "broken-plan.json"
        broken.write_text("{not json", encoding="utf-8")
        self.plan_path = broken
        with self.assertRaises(ConfigError):
            self._apply()
        missing = self.root / "absent-plan.json"
        self.plan_path = missing
        with self.assertRaises(ConfigError):
            self._apply()

    def test_a_binding_is_written_in_the_shape_the_schema_uses(self) -> None:
        """A v1-shaped binding in an Electron state would be unreadable to Codex."""
        from codexsync.guardian_schema import build_binding_value, supports_project_creation

        self.assertEqual(build_binding_value("legacy-v1", "p1"), "p1")
        self.assertEqual(
            build_binding_value("electron-v2", "p1"),
            {"projectKind": "local", "projectId": "p1"},
        )
        with self.assertRaises(ValueError):
            build_binding_value("something-new", "p1")
        self.assertTrue(supports_project_creation("legacy-v1"))
        self.assertFalse(
            supports_project_creation("electron-v2"),
            "an entry with unconfirmed fields must not be invented",
        )

    def test_apply_is_idempotent_for_the_same_plan(self) -> None:
        self._apply()
        after_first = self.state_file.read_bytes()
        # The plan is now stale against the rewritten state, which is itself the
        # protection against applying the same edit twice.
        with self.assertRaises(FailSafeError):
            self._apply()
        self.assertEqual(self.state_file.read_bytes(), after_first)


class RepairRemapApplyTests(unittest.TestCase):
    """A project that moved to a new path keeps its id, and its chats with it.

    This is the migration codexSync exists for, and the reason it is done here
    rather than by editing session files: a record's raw bytes are its identity,
    so rewriting a ``cwd`` inside a session would make the same history on two
    machines permanently divergent. Correcting the root in the global state
    moves every thread bound to the project and touches no session at all.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"repair-remap-{uuid.uuid4().hex}"
        self.state_dir = self.root / "local-state"
        (self.state_dir / "sessions").mkdir(parents=True)
        self.old_root = (self.root / "old" / "repo").as_posix()
        self.new_root = self.root / "new" / "repo"
        (self.new_root / ".git").mkdir(parents=True)
        # A real session recorded under the old path. Its bytes must survive.
        self.session = self.state_dir / "sessions" / "a.jsonl"
        self.session.write_bytes(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "thread-a", "cwd": self.old_root}},
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        self.state_file = self.state_dir / ".codex-global-state.json"
        self._write_state(self._electron_state())
        self.config_path = self._write_config()
        self.plan_path = self.root / "plan.json"
        self.plan = self._build_plan()
        save_repair_plan(self.plan, self.plan_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_state(self, payload: dict) -> None:
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _electron_state(self) -> dict:
        return {
            "local-projects": {
                "p1": {
                    "id": "p1",
                    "name": "repo",
                    "rootPaths": [self.old_root],
                    "createdAt": 1700000000,
                    "updatedAt": 1700000001,
                }
            },
            "project-order": ["p1"],
            "thread-project-assignments": {
                "thread-a": {"projectKind": "local", "projectId": "p1"}
            },
            "app-server-project-id-by-legacy-project-id-by-host": {},
        }

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

                [[path_mappings]]
                rule_id = "moved"
                source_machine = "source"
                target_machine = "target"
                from = "{(self.root / 'old').as_posix()}"
                to = "{(self.root / 'new').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _build_plan(self, extra=()):
        descriptor = SessionDescriptor(
            "thread-a", SessionState.ACTIVE, "sessions/a.jsonl", "a" * 64, 1, 1,
            cwd=self.old_root,
        )
        return build_repair_plan(
            SessionCatalog([descriptor, *extra], {}),
            self.state_file.read_bytes(),
            source_machine="source",
            target_machine="target",
            rules=load_config(self.config_path).path_mappings,
            volatile=False,
        )

    def _apply(self, gate=None, **kwargs):
        with patch("codexsync.app._make_safety_gate", return_value=gate or _StoppedGate()):
            return apply_repair_projects(
                self.config_path,
                plan_path=self.plan_path,
                confirm_plan=kwargs.pop("confirm_plan", self.plan.plan_id),
                **kwargs,
            )

    def test_the_plan_remaps_rather_than_creating_a_second_project(self) -> None:
        kinds = {action.kind for action in self.plan.actions}
        self.assertEqual(self.plan.schema_id, "electron-v2")
        self.assertIn(RepairActionKind.REMAP_ROOT, kinds)
        self.assertNotIn(RepairActionKind.ADD_PROJECT, kinds)

    def test_apply_rewrites_only_the_root_value(self) -> None:
        self._apply()
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        entry = state["local-projects"]["p1"]
        self.assertEqual(entry["rootPaths"], [str(self.new_root.resolve())])
        self.assertEqual(entry["name"], "repo", "a remap invents nothing around the root")
        self.assertEqual(entry["id"], "p1")
        self.assertEqual(entry["createdAt"], 1700000000)
        self.assertEqual(entry["updatedAt"], 1700000001)
        self.assertEqual(state["project-order"], ["p1"], "no second project appears")
        self.assertEqual(len(state["local-projects"]), 1)

    def test_the_chats_follow_the_project_without_being_rebound(self) -> None:
        before = json.loads(self.state_file.read_text(encoding="utf-8"))
        self._apply()
        after = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            after["thread-project-assignments"],
            before["thread-project-assignments"],
            "the binding already points at p1; correcting p1's root is the whole migration",
        )

    def test_no_session_byte_is_touched(self) -> None:
        before = self.session.read_bytes()
        self._apply()
        self.assertEqual(self.session.read_bytes(), before)

    def test_dry_run_changes_nothing(self) -> None:
        before = self.state_file.read_bytes()
        self.assertGreater(self._apply(dry_run=True), 0)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_the_old_root_survives_in_a_verifiable_backup(self) -> None:
        before = self.state_file.read_bytes()
        self._apply()
        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertTrue(manifest["committed"])
        snapshot = self.root / "backups" / manifest["snapshot"]
        self.assertEqual((snapshot / ".codex-global-state.json").read_bytes(), before)

    def test_an_older_chat_is_pinned_to_the_project_before_its_root_moves(self) -> None:
        """The remap points the project away from where the old chats live."""
        older = SessionDescriptor(
            "thread-old", SessionState.ACTIVE, "sessions/b.jsonl", "b" * 64, 1, 1,
            cwd=self.old_root + "/gone",
        )
        self.plan = self._build_plan(extra=[older])
        save_repair_plan(self.plan, self.plan_path)
        self.assertNotIn("REMAP_ORPHANS_SESSIONS", self.plan.codes)

        self._apply()
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(
            state["thread-project-assignments"]["thread-old"],
            {"projectKind": "local", "projectId": "p1"},
            "written in the shape this schema uses, not a bare id",
        )
        self.assertEqual(
            state["local-projects"]["p1"]["rootPaths"], [str(self.new_root.resolve())]
        )

    def test_a_plan_that_would_orphan_a_chat_is_refused(self) -> None:
        older = SessionDescriptor(
            "thread-old", SessionState.ACTIVE, "sessions/b.jsonl", "b" * 64, 1, 1,
            cwd=self.old_root + "/gone",
        )
        with patch(
            "codexsync.repair_plan._bind_sessions_left_behind_by_a_remap", return_value=[]
        ):
            self.plan = self._build_plan(extra=[older])
        save_repair_plan(self.plan, self.plan_path)
        before = self.state_file.read_bytes()
        with self.assertRaises(FailSafeError):
            self._apply()
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_remap_of_a_root_the_state_no_longer_has_is_refused(self) -> None:
        # The plan is built, then the project moves again behind its back.
        state = self._electron_state()
        state["local-projects"]["p1"]["rootPaths"] = [(self.root / "somewhere").as_posix()]
        self._write_state(state)
        with self.assertRaises(FailSafeError):
            self._apply()



if __name__ == "__main__":
    unittest.main()
