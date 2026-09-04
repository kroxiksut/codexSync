"""Cold apply of a session transfer plan.

The acceptance for CS-224 is that no divergent record is ever lost and that no
synthetically interleaved history appears in the state directory. These tests
exercise the branch that writes: the plan must still describe reality, a whole
branch replaces its ancestor in one step, the ancestor is in a verifiable
backup, and both branches survive every refusal.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_transfer import PROVEN_LAYOUTS, TransferAction, save_transfer_plan


LAYOUT = "test-layout"


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


class SessionTransferApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"transfer-apply-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        (self.local_dir / "sessions").mkdir(parents=True)
        (self.cloud_dir / "sessions").mkdir(parents=True)
        self.config_path = self._write_config()
        self.plan_path = self.root / "plan.json"
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"

    def tearDown(self) -> None:
        PROVEN_LAYOUTS.clear()
        shutil.rmtree(self.root, ignore_errors=True)

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
                local_state_dir = "{self.local_dir.as_posix()}"
                cloud_root_dir = "{self.cloud_dir.as_posix()}"
                backup_dir = "{(self.root / 'backups').as_posix()}"
                temp_dir = "{(self.root / '.tmp').as_posix()}"

                [guardian]
                root_dir = "{(self.root / 'guardian').as_posix()}"

                [semantic]
                root_dir = "{(self.root / 'semantic').as_posix()}"

                [targets]
                include_roots = ["sessions"]

                [backup]
                backup_before_overwrite = true
                compression = "none"

                [state]
                manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _branch(self, root: Path, session_id: str, records: list[str]) -> Path:
        path = root / "sessions" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"type": "session_meta", "payload": {"id": session_id}}]
        payload += [{"type": "event", "record": value} for value in records]
        path.write_bytes(
            b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in payload)
        )
        return path

    def _plan(self, resolutions_path: Path | None = None):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            plan = scan_session_transfer(
                self.config_path,
                source_machine="desktop",
                target_machine="laptop",
                resolutions_path=resolutions_path,
            )
        # The scan does not know about the proven layout id, so rebuild with it.
        from codexsync.app import _rebuild_transfer_plan
        from codexsync.config import load_config
        from codexsync.semantic_transfer import TransferPlan

        cfg = load_config(self.config_path)
        fresh, _, _ = _rebuild_transfer_plan(
            cfg, self.local_dir, self.cloud_dir,
            TransferPlan(
                plan.version, plan.plan_id, plan.created_at_utc, "desktop", "laptop",
                LAYOUT, plan.canonical_version, False, plan.items, plan.codes,
            ),
            resolutions_path,
        )
        save_transfer_plan(fresh, self.plan_path)
        return fresh

    def _apply(self, plan, gate=None, **kwargs):
        with patch("codexsync.app._make_safety_gate", return_value=gate or _StoppedGate()):
            return apply_session_transfer(
                self.config_path,
                plan_path=self.plan_path,
                confirm_plan=kwargs.pop("confirm_plan", plan.plan_id),
                **kwargs,
            )

    # --- the happy path ---------------------------------------------------

    def test_a_fast_forward_replaces_the_ancestor_whole(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        remote = self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_LOCAL)

        written = self._apply(plan)
        self.assertEqual(written, 1)
        local = self.local_dir / "sessions" / "s1.jsonl"
        self.assertEqual(local.read_bytes(), remote.read_bytes(), "the whole branch is taken")
        self.assertEqual(len(local.read_bytes().splitlines()), 3)

    def test_the_ancestor_is_kept_in_a_verifiable_backup(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        before = local.read_bytes()
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())

        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertTrue(manifest["committed"])
        snapshot = self.root / "backups" / manifest["snapshot"]
        self.assertEqual((snapshot / "sessions" / "s1.jsonl").read_bytes(), before)

    def test_the_source_branch_is_never_touched(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        remote = self._branch(self.cloud_dir, "s1", ["one", "two"])
        before = remote.read_bytes()
        self._apply(self._plan())
        self.assertEqual(remote.read_bytes(), before)

    def test_dry_run_writes_nothing(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        before = local.read_bytes()
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        written = self._apply(self._plan(), dry_run=True)
        self.assertEqual(written, 1)
        self.assertEqual(local.read_bytes(), before)
        self.assertFalse(list((self.root / "backups").glob("*")))

    def test_the_journal_reaches_a_terminal_state(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())
        journals = JournalStore(self.root / ".tmp")
        self.assertEqual(journals.non_terminal(), [], "a finished transfer blocks nothing")

    # --- refusals ---------------------------------------------------------

    def test_a_divergence_blocks_the_apply_and_keeps_both_branches(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one", "left"])
        remote = self._branch(self.cloud_dir, "s1", ["one", "right"])
        local_before, remote_before = local.read_bytes(), remote.read_bytes()
        plan = self._plan()
        self.assertEqual(plan.items[0].action, TransferAction.BLOCKED_CONFLICT)

        with self.assertRaises(ConflictError):
            self._apply(plan)
        self.assertEqual(local.read_bytes(), local_before)
        self.assertEqual(remote.read_bytes(), remote_before)

    def test_state_changed_after_the_scan_is_refused(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        # The remote branch grows after the plan was frozen.
        self._branch(self.cloud_dir, "s1", ["one", "two", "three"])
        before = local.read_bytes()
        with self.assertRaises(FailSafeError):
            self._apply(plan)
        self.assertEqual(local.read_bytes(), before)

    def test_wrong_confirmation_id_is_refused(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        before = local.read_bytes()
        with self.assertRaises(ConfigError):
            self._apply(self._plan(), confirm_plan="not-the-plan-id")
        self.assertEqual(local.read_bytes(), before)

    def test_apply_is_blocked_while_codex_runs(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        before = local.read_bytes()
        with self.assertRaises(SafetyPreconditionError):
            self._apply(plan, gate=_RunningGate())
        self.assertEqual(local.read_bytes(), before)

    def test_dry_run_is_blocked_while_codex_runs(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        with self.assertRaises(SafetyPreconditionError):
            self._apply(plan, gate=_RunningGate(), dry_run=True)

    def test_an_unproven_layout_leaves_nothing_to_apply(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        PROVEN_LAYOUTS.clear()
        before = local.read_bytes()
        # Without a proven layout the rebuild classifies the item as blocked, so
        # the frozen plan no longer describes reality.
        with self.assertRaises(FailSafeError):
            self._apply(plan)
        self.assertEqual(local.read_bytes(), before)

    def test_the_losing_branch_survives_outside_backup_retention(self) -> None:
        """A resolved conflict must not depend on a snapshot that gets pruned."""
        from codexsync.app import record_branch_resolution

        local = self._branch(self.local_dir, "s1", ["one", "left"])
        losing_bytes = local.read_bytes()
        self._branch(self.cloud_dir, "s1", ["one", "right"])
        blocked = self._plan()
        conflict_id = blocked.items[0].conflict_id
        self.assertIsNotNone(conflict_id)

        resolutions = self.root / "resolutions.json"
        record_branch_resolution(
            self.plan_path, conflict_id=conflict_id, choice="KEEP_REMOTE", output_path=resolutions
        )
        plan = self._plan(resolutions)
        self.assertIn("RESOLVED_BY_USER", plan.items[0].codes)

        self._apply(plan, resolutions_path=resolutions)
        self.assertNotEqual(local.read_bytes(), losing_bytes, "the local branch was replaced")

        bundles = list((self.root / "semantic" / "conflicts").iterdir())
        self.assertEqual(len(bundles), 1, "the conflict must be bundled")
        preserved = (bundles[0] / "left.jsonl").read_bytes()
        self.assertEqual(preserved, losing_bytes, "the losing branch survives byte for byte")
        self.assertTrue((bundles[0] / "right.jsonl").is_file(), "both branches are kept")
        self.assertTrue((bundles[0] / "COMMITTED").is_file())

    # --- rebuilding the cloud mirror --------------------------------------

    def test_the_mirror_is_written_even_though_no_layout_is_proven(self) -> None:
        """The reason the gate is per-direction: nothing but codexSync reads the mirror."""
        PROVEN_LAYOUTS.clear()
        local = self._branch(self.local_dir, "s1", ["one"])
        plan = self._plan()
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_REMOTE)

        written = self._apply(plan)
        self.assertEqual(written, 1)
        mirrored = self.cloud_dir / "sessions" / "s1.jsonl"
        self.assertEqual(mirrored.read_bytes(), local.read_bytes())

    def test_a_session_blocked_on_the_layout_does_not_stop_the_mirror(self) -> None:
        PROVEN_LAYOUTS.clear()
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s2", ["one"])
        plan = self._plan()
        actions = {item.action for item in plan.items}
        self.assertEqual(
            actions,
            {TransferAction.FAST_FORWARD_REMOTE, TransferAction.BLOCKED_UNPROVEN_LAYOUT},
        )

        written = self._apply(plan)
        self.assertEqual(written, 1)
        self.assertEqual(
            (self.cloud_dir / "sessions" / "s1.jsonl").read_bytes(), local.read_bytes()
        )
        self.assertFalse(
            (self.local_dir / "sessions" / "s2.jsonl").exists(),
            "the blocked session is left exactly where it was",
        )

    def test_a_conflict_still_stops_the_whole_apply(self) -> None:
        """A partial apply is for standing limits, never for an open decision."""
        PROVEN_LAYOUTS.clear()
        self._branch(self.local_dir, "s1", ["one", "left"])
        self._branch(self.cloud_dir, "s1", ["one", "right"])
        self._branch(self.local_dir, "s2", ["one"])
        plan = self._plan()

        with self.assertRaises(ConflictError):
            self._apply(plan)
        self.assertFalse(
            (self.cloud_dir / "sessions" / "s2.jsonl").exists(),
            "nothing is written while a conflict is open",
        )

    # --- the common base the store records ---------------------------------

    def _store(self):
        from codexsync.semantic_store import SemanticStore

        return SemanticStore(self.root / "semantic", "machine-a")

    def test_a_completed_transfer_records_what_both_sides_now_hold(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())

        entries = list((self.root / "semantic" / "manifest" / "machine-a").glob("*.json"))
        self.assertEqual(len(entries), 1)
        recorded = json.loads(entries[0].read_text(encoding="utf-8"))
        self.assertEqual(recorded["format"], "codexsync-semantic-manifest-v1")
        self.assertEqual(recorded["record_count"], 3)
        self.assertEqual(recorded["state"], "ACTIVE")
        self.assertEqual(recorded["generation"], 1)
        self.assertEqual(recorded["base_sha256"], recorded["sha256"])
        self.assertEqual(self._store().confirmed_bases(), {recorded["session_hash"]})

    def test_the_manifest_holds_no_session_payload(self) -> None:
        """Copying every reconciled session would duplicate the whole directory."""
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())
        written = list((self.root / "semantic" / "manifest").rglob("*"))
        self.assertTrue(written)
        for path in written:
            self.assertNotEqual(path.suffix, ".jsonl", f"{path.name} carries a payload")
        total = sum(p.stat().st_size for p in written if p.is_file())
        self.assertLess(total, 4096, "an entry is metadata, measured in bytes not megabytes")

    def test_an_entry_is_self_verifying_and_tampering_drops_it(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())
        entry = next((self.root / "semantic" / "manifest" / "machine-a").glob("*.json"))
        raw = json.loads(entry.read_text(encoding="utf-8"))
        raw["record_count"] = 99
        entry.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(
            self._store().confirmed_bases(), set(),
            "a digest that no longer covers the contents is not an entry",
        )

    def test_recording_the_same_agreement_twice_does_not_grow_the_store(self) -> None:
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())
        entry = next((self.root / "semantic" / "manifest" / "machine-a").glob("*.json"))
        before = entry.read_bytes()

        store = self._store()
        raw = json.loads(before)
        store.record(
            "s1", state="ACTIVE", sha256=raw["sha256"], record_count=raw["record_count"],
            byte_count=raw["byte_count"], agreed=True,
        )
        self.assertEqual(entry.read_bytes(), before, "unchanged content is not a new generation")

    def test_a_recorded_base_is_what_makes_an_archive_transition_decidable(self) -> None:
        """Without it the same pair is refused, which is how it was before wiring."""
        self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        self._apply(self._plan())

        # The two sides now hold the same history; the cloud copy is archived.
        agreed = (self.local_dir / "sessions" / "s1.jsonl").read_bytes()
        archived = self.cloud_dir / "archived_sessions" / "s1.jsonl"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(agreed)
        (self.cloud_dir / "sessions" / "s1.jsonl").unlink()

        with_base = self._plan()
        self.assertEqual(with_base.items[0].action, TransferAction.ARCHIVE_TRANSITION)

        shutil.rmtree(self.root / "semantic" / "manifest")
        without_base = self._plan()
        self.assertEqual(without_base.items[0].action, TransferAction.BLOCKED_CONFLICT)

    def test_an_interrupted_apply_leaves_recovery_evidence(self) -> None:
        local = self._branch(self.local_dir, "s1", ["one"])
        self._branch(self.cloud_dir, "s1", ["one", "two"])
        plan = self._plan()
        before = local.read_bytes()
        with patch(
            "codexsync.sync_engine.SyncEngine._replace_staged",
            side_effect=OSError("simulated power cut"),
        ):
            with self.assertRaises(OSError):
                self._apply(plan)
        self.assertEqual(local.read_bytes(), before, "a failed transfer changes nothing")
        pending = JournalStore(self.root / ".tmp").non_terminal()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].family, "sessions")
        self.assertEqual(pending[0].state, JournalState.RECOVERY_REQUIRED)
        self.assertIsNotNone(pending[0].backup_snapshot)


if __name__ == "__main__":
    unittest.main()
