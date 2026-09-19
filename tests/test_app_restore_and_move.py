"""The app-level envelopes around a Guardian restore and a project move.

The planning modules are tested on their own; these tests are about what the
wrappers add and what would silently go wrong without them: the preview writes
nothing, an apply needs the exact id and a stopped Codex, the state write goes
through `commit_global_state` (backup, journal), and a project move never
touches the old folder.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import (
    apply_project_move_plan,
    restore_global_state,
    save_project_move_plan,
    scan_project_move,
)
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.guardian_models import SourceObservation, ValidationStatus
from codexsync.guardian_schema import validate_global_state_references
from codexsync.guardian_store import GuardianStore
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision


class _StoppedGate:
    def __init__(self) -> None:
        self.required: list[OperationKind] = []

    def check(self, operation, *, final=False):
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation, *, final=False):
        self.required.append(operation)
        return self.check(operation, final=final)


class _RunningGate:
    def check(self, operation, *, final=False):
        return SafetyDecision(operation, ProcessState.RUNNING, False, "Codex is running")

    def require(self, operation, *, final=False):
        raise SafetyPreconditionError("Codex is running")


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1] / "test-sandbox" / f"app-rm-{uuid.uuid4().hex[:8]}"
        self.state_dir = self.root / "codex"
        (self.state_dir / "sessions").mkdir(parents=True)
        self.state_file = self.state_dir / ".codex-global-state.json"
        self.projects = self.root / "projects"
        (self.projects / "alpha" / "src").mkdir(parents=True)
        (self.projects / "alpha" / "src" / "main.py").write_text("print('alpha')\n", encoding="utf-8")
        (self.projects / "alpha" / "README.md").write_text("# alpha\n", encoding="utf-8")
        self.alpha = str(self.projects / "alpha")
        self._write_state(self._state())
        self.config_path = self._write_config()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _state(self, **overrides) -> dict:
        state = {
            "local-projects": {
                "p-alpha": {"id": "p-alpha", "name": "alpha", "rootPaths": [self.alpha], "createdAt": 1, "updatedAt": 2},
                "p-beta": {"id": "p-beta", "name": "beta", "rootPaths": [str(self.projects / "beta")], "createdAt": 3, "updatedAt": 4},
            },
            "project-order": ["p-alpha", "p-beta"],
            "thread-project-assignments": {},
            "app-server-project-id-by-legacy-project-id-by-host": {},
        }
        state.update(overrides)
        return state

    def _write_state(self, state: dict) -> bytes:
        payload = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        self.state_file.write_bytes(payload)
        return payload

    def _chat(self, session_id: str, cwd: str) -> None:
        rows = [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd, "thread_source": "user", "timestamp": "2026-08-16T10:00:00.000Z"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
        ]
        (self.state_dir / "sessions" / f"{session_id}.jsonl").write_bytes(
            b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)
        )

    def _write_config(self) -> Path:
        path = self.root / "config.toml"
        path.write_text(textwrap.dedent(f"""
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
        """).strip() + "\n", encoding="utf-8")
        return path

    def _gate(self, gate):
        return patch("codexsync.app._make_safety_gate", return_value=gate)


class GuardianRestoreTests(_Fixture):
    def _snapshot(self, payload: bytes) -> str:
        report = validate_global_state_references(payload)
        self.assertIn(report.status, {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING})
        observation = SourceObservation(payload, ".codex-global-state.json", len(payload), len(payload), 1, 1, "id", "id", True)
        store = GuardianStore(self.root / "guardian", "machine-a", producer_version="test")
        return store.commit(observation, report).snapshot.snapshot_id

    def test_preview_writes_nothing_and_apply_restores_through_the_envelope(self) -> None:
        good = self.state_file.read_bytes()
        snapshot_id = self._snapshot(good)
        damaged = self._write_state(self._state(**{"project-order": ["p-alpha"], "local-projects": {
            "p-alpha": self._state()["local-projects"]["p-alpha"]}}))

        before = _tree(self.state_dir)
        with self._gate(_RunningGate()):
            plan, written = restore_global_state(self.config_path, snapshot_id=snapshot_id)
        self.assertEqual((written, plan.codes), (0, ()), "a preview works while Codex runs")
        self.assertEqual((plan.projects_now, plan.projects_in_snapshot), (1, 2))
        self.assertEqual(_tree(self.state_dir), before)

        gate = _StoppedGate()
        with self._gate(gate):
            _, written = restore_global_state(self.config_path, snapshot_id=snapshot_id, confirm_plan=plan.plan_id)
        self.assertEqual(written, 1)
        self.assertEqual(self.state_file.read_bytes(), good)
        self.assertIn(OperationKind.GUARDIAN_RESTORE, gate.required)
        backups = [p for p in (self.root / "backups").rglob(".codex-global-state.json")]
        self.assertEqual([p.read_bytes() for p in backups], [damaged], "the replaced state is backed up first")
        journals = list((self.root / ".tmp" / "journals").glob("*.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(JournalStore(self.root / ".tmp").load(journals[0].stem).state, JournalState.COMMITTED)

    def test_apply_refuses_while_codex_runs_and_writes_nothing(self) -> None:
        snapshot_id = self._snapshot(self.state_file.read_bytes())
        self._write_state(self._state(**{"thread-project-assignments": {"t": {"projectKind": "local", "projectId": "p-alpha"}}}))
        with self._gate(_StoppedGate()):
            plan, _ = restore_global_state(self.config_path, snapshot_id=snapshot_id)
        before = self.state_file.read_bytes()
        with self._gate(_RunningGate()), self.assertRaises(SafetyPreconditionError):
            restore_global_state(self.config_path, snapshot_id=snapshot_id, confirm_plan=plan.plan_id)
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_a_state_that_moved_after_the_preview_is_refused(self) -> None:
        snapshot_id = self._snapshot(self.state_file.read_bytes())
        self._write_state(self._state(**{"project-order": ["p-beta", "p-alpha"]}))
        with self._gate(_StoppedGate()):
            plan, _ = restore_global_state(self.config_path, snapshot_id=snapshot_id)
            moved = self._write_state(self._state(**{"project-order": ["p-alpha"]}))
            with self.assertRaises(ConfigError):
                restore_global_state(self.config_path, snapshot_id=snapshot_id, confirm_plan=plan.plan_id)
        self.assertEqual(self.state_file.read_bytes(), moved)

    def test_a_dry_run_verifies_and_writes_nothing(self) -> None:
        snapshot_id = self._snapshot(self.state_file.read_bytes())
        self._write_state(self._state(**{"project-order": ["p-beta", "p-alpha"]}))
        with self._gate(_StoppedGate()):
            plan, _ = restore_global_state(self.config_path, snapshot_id=snapshot_id)
            before = _tree(self.root)
            _, written = restore_global_state(self.config_path, snapshot_id=snapshot_id, confirm_plan=plan.plan_id, dry_run=True)
        self.assertEqual(written, 1)
        self.assertEqual(_tree(self.root), before)

    def test_a_missing_state_file_is_not_recreated(self) -> None:
        snapshot_id = self._snapshot(self.state_file.read_bytes())
        self._write_state(self._state(**{"project-order": ["p-beta", "p-alpha"]}))
        with self._gate(_StoppedGate()):
            plan, _ = restore_global_state(self.config_path, snapshot_id=snapshot_id)
            self.state_file.unlink()
            with self.assertRaises(FailSafeError):
                restore_global_state(self.config_path, snapshot_id=snapshot_id, confirm_plan=plan.plan_id)
        self.assertFalse(self.state_file.exists())


class ProjectMoveTests(_Fixture):
    def test_preview_then_apply_copies_remaps_pins_and_keeps_the_old_folder(self) -> None:
        self._chat("11111111-0000-0000-0000-000000000001", cwd=str(Path(self.alpha) / "src"))
        target = self.projects / "alpha-moved"
        old_tree = _tree(self.projects / "alpha")

        with self._gate(_StoppedGate()):
            plan = scan_project_move(self.config_path, project_id="p-alpha", new_root=target)
        self.assertEqual(plan.codes, ())
        self.assertEqual(plan.file_count, 2)
        self.assertEqual(plan.bindings, ("11111111-0000-0000-0000-000000000001",))
        self.assertFalse(target.exists(), "a preview creates nothing")

        plan_path = save_project_move_plan(plan, self.root / "plans" / "move.json")
        gate = _StoppedGate()
        with self._gate(gate):
            result = apply_project_move_plan(self.config_path, plan_path=plan_path, confirm_plan=plan.plan_id)

        self.assertEqual(result.copied_files, 2)
        self.assertEqual((target / "src" / "main.py").read_text(encoding="utf-8"), "print('alpha')\n")
        self.assertEqual(_tree(self.projects / "alpha"), old_tree, "the old folder is untouched")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"]["p-alpha"]["rootPaths"], [str(target)])
        self.assertEqual(state["local-projects"]["p-beta"], self._state()["local-projects"]["p-beta"])
        self.assertEqual(
            state["thread-project-assignments"]["11111111-0000-0000-0000-000000000001"],
            {"projectKind": "local", "projectId": "p-alpha"},
        )
        self.assertIn(OperationKind.PROJECT_MOVE, gate.required)
        self.assertTrue(list((self.root / "backups").rglob(".codex-global-state.json")), "state backed up first")

    def test_a_running_codex_blocks_the_apply_before_any_copy(self) -> None:
        target = self.projects / "alpha-moved"
        with self._gate(_StoppedGate()):
            plan = scan_project_move(self.config_path, project_id="p-alpha", new_root=target)
        plan_path = save_project_move_plan(plan, self.root / "plans" / "move.json")
        with self._gate(_RunningGate()), self.assertRaises(SafetyPreconditionError):
            apply_project_move_plan(self.config_path, plan_path=plan_path, confirm_plan=plan.plan_id)
        self.assertFalse(target.exists())

    def test_a_plan_built_while_codex_runs_is_volatile_and_refused(self) -> None:
        target = self.projects / "alpha-moved"
        with self._gate(_RunningGate()):
            plan = scan_project_move(self.config_path, project_id="p-alpha", new_root=target)
        self.assertTrue(plan.volatile)
        plan_path = save_project_move_plan(plan, self.root / "plans" / "move.json")
        with self._gate(_StoppedGate()), self.assertRaises(FailSafeError):
            apply_project_move_plan(self.config_path, plan_path=plan_path, confirm_plan=plan.plan_id)
        self.assertFalse(target.exists())

    def test_a_target_inside_codex_is_refused(self) -> None:
        with self._gate(_StoppedGate()):
            plan = scan_project_move(self.config_path, project_id="p-alpha", new_root=self.state_dir / "alpha")
        self.assertIn("PROTECTED_TARGET", plan.codes)

    def test_an_unreadable_plan_file_is_bad_input(self) -> None:
        broken = self.root / "plans" / "broken.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{}", encoding="utf-8")
        with self._gate(_StoppedGate()), self.assertRaises(ConfigError):
            apply_project_move_plan(self.config_path, plan_path=broken, confirm_plan="x")


if __name__ == "__main__":
    unittest.main()
