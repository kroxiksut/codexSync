"""A chat changed on both machines is decided by the conflict rule (D-027).

The owner's ask: when one copy is clearly newer, keep it without a trip to the
Sessions page, in the window, the console and the sign-in task alike, and let
the rule live in the config. What stays fixed is that nothing is lost: the
copy not kept goes whole into the conflict bundle before anything is replaced,
and a rule that cannot tell -- two copies ending at the same moment -- still
asks.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import lzma
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest import mock
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.cli import main
from codexsync.exceptions import ConflictError, FailSafeError
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_transfer import (
    RESOLVED_BY_RULE,
    RULE_ASK,
    RULE_CANNOT_DECIDE,
    RULE_LOCAL,
    RULE_NEWER,
    RULE_REMOTE,
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    conflict_rule_for,
    load_transfer_plan,
    save_transfer_plan,
)
from codexsync.session_catalog import scan_sessions
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
SESSION = "s1"


def _history(*turns: tuple[str, str]) -> bytes:
    rows = [{"timestamp": "2026-10-01T09:00:00.000Z", "type": "session_meta",
             "payload": {"id": SESSION, "cwd": "D:\\Projects\\app"}}]
    for stamp, text in turns:
        rows.append({"timestamp": stamp, "type": "event_msg",
                     "payload": {"type": "user_message", "message": text}})
    return b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)


COMMON = ("2026-10-01T09:01:00.000Z", "shared question")
#: Continued here last, at 12:00.
LOCAL = _history(COMMON, ("2026-10-03T12:00:00.000Z", "continued on this machine"))
#: Continued on the other machine, at 10:00.
REMOTE = _history(COMMON, ("2026-10-03T10:00:00.000Z", "continued on the other machine"))


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"conflict-rules-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        for side in (self.local, self.cloud):
            (side / "sessions").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, side: Path, payload: bytes, *, xz: bool = False) -> Path:
        path = side / "sessions" / ("s1.jsonl" + (".xz" if xz else ""))
        path.write_bytes(lzma.compress(payload) if xz else payload)
        return path

    def plan(self, **kwargs):
        return build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.cloud),
            local_root=self.local, remote_root=self.cloud,
            source_machine="desktop", target_machine="laptop", **kwargs,
        )


class RuleFromSettingsTests(unittest.TestCase):
    def test_each_policy_names_its_rule(self) -> None:
        self.assertEqual(conflict_rule_for("manual_abort"), RULE_ASK)
        self.assertEqual(conflict_rule_for("prefer_newer_mtime"), RULE_NEWER)
        self.assertEqual(conflict_rule_for("prefer_local"), RULE_LOCAL)
        self.assertEqual(conflict_rule_for("prefer_cloud"), RULE_REMOTE)

    def test_a_one_way_direction_names_the_machine_that_wins(self) -> None:
        self.assertEqual(conflict_rule_for("manual_abort", "to_cloud"), RULE_LOCAL)
        self.assertEqual(conflict_rule_for("prefer_newer_mtime", "to_local"), RULE_REMOTE)

    def test_an_unknown_policy_is_refused(self) -> None:
        with self.assertRaises(FailSafeError):
            conflict_rule_for("newest")


class RulePlanTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.write(self.local, LOCAL)
        self.write(self.cloud, REMOTE)

    def test_asking_blocks_as_before(self) -> None:
        (item,) = self.plan().items
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertNotIn(RESOLVED_BY_RULE, item.codes)

    def test_newer_keeps_the_copy_whose_last_record_is_later(self) -> None:
        (item,) = self.plan(conflict_rule=RULE_NEWER).items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE, "this machine's copy goes to the mirror")
        self.assertIn(RESOLVED_BY_RULE, item.codes)
        self.assertIn("RULE_NEWER", item.codes)
        self.assertIsNotNone(item.conflict_id, "the bundle is still named by the conflict")

    def test_newer_follows_the_time_not_the_side(self) -> None:
        self.write(self.local, REMOTE)
        self.write(self.cloud, LOCAL)
        (item,) = self.plan(conflict_rule=RULE_NEWER).items
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT,
                         "the mirror's copy wins; writing .codex is still gated, never forced")
        self.assertIn(RESOLVED_BY_RULE, item.codes)

    def test_two_copies_ending_at_the_same_moment_still_ask(self) -> None:
        self.write(self.cloud, _history(COMMON, ("2026-10-03T12:00:00.000Z", "same time, other words")))
        (item,) = self.plan(conflict_rule=RULE_NEWER).items
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn(RULE_CANNOT_DECIDE, item.codes)

    def test_times_are_compared_as_moments_not_text(self) -> None:
        # Fewer fractional digits sort first as text and later as time.
        self.write(self.cloud, _history(COMMON, ("2026-10-03T12:00:01Z", "a second later")))
        (item,) = self.plan(conflict_rule=RULE_NEWER).items
        self.assertNotEqual(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertIn(RESOLVED_BY_RULE, item.codes)

    def test_this_machine_or_the_cloud_always_decide(self) -> None:
        (mine,) = self.plan(conflict_rule=RULE_LOCAL).items
        self.assertEqual(mine.action, TransferAction.FAST_FORWARD_REMOTE)
        (theirs,) = self.plan(conflict_rule=RULE_REMOTE).items
        self.assertIn(RESOLVED_BY_RULE, theirs.codes)
        self.assertNotEqual(theirs.action, TransferAction.FAST_FORWARD_REMOTE)

    def test_a_person_saying_later_outranks_the_rule(self) -> None:
        (item,) = self.plan().items
        resolution = BranchResolution(
            item.conflict_id, item.session_hash, item.local_sha256, item.remote_sha256, ResolutionChoice.DEFER,
        )
        (held,) = self.plan(conflict_rule=RULE_NEWER, resolutions={item.conflict_id: resolution}).items
        self.assertEqual(held.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn("DEFERRED", held.codes)

    def test_a_person_choosing_a_side_outranks_the_rule(self) -> None:
        (item,) = self.plan().items
        resolution = BranchResolution(
            item.conflict_id, item.session_hash, item.local_sha256, item.remote_sha256, ResolutionChoice.KEEP_LOCAL,
        )
        (kept,) = self.plan(conflict_rule=RULE_REMOTE, resolutions={item.conflict_id: resolution}).items
        self.assertEqual(kept.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertIn("RESOLVED_BY_USER", kept.codes)
        self.assertNotIn(RESOLVED_BY_RULE, kept.codes)


class DirectionTests(_Sandbox):
    def test_to_cloud_holds_back_writes_into_codex_and_blocks_nothing(self) -> None:
        self.write(self.local, _history(COMMON))
        self.write(self.cloud, _history(COMMON, ("2026-10-03T10:00:00.000Z", "more")))
        # The catalogue names the file, so the write would be an in-place one.
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {SESSION: "sessions/s1.jsonl"})
        (item,) = self.plan(direction="to_cloud", placements=placements).items
        self.assertEqual(item.action, TransferAction.HELD_BY_DIRECTION)
        self.assertIn("WOULD_BE_FAST_FORWARD_LOCAL", item.codes)
        self.assertFalse(item.action.is_blocked)
        self.assertFalse(item.action.writes)

    def test_to_local_holds_back_writes_into_the_mirror(self) -> None:
        self.write(self.local, _history(COMMON, ("2026-10-03T10:00:00.000Z", "more")))
        self.write(self.cloud, _history(COMMON))
        (item,) = self.plan(direction="to_local").items
        self.assertEqual(item.action, TransferAction.HELD_BY_DIRECTION)


class PlanFileTests(_Sandbox):
    def test_the_rule_and_direction_survive_the_plan_file_and_its_id(self) -> None:
        self.write(self.local, LOCAL)
        self.write(self.cloud, REMOTE)
        plan = self.plan(conflict_rule=RULE_NEWER, direction="to_cloud")
        path = save_transfer_plan(plan, self.root / "plan.json")
        loaded = load_transfer_plan(path)
        self.assertEqual((loaded.conflict_rule, loaded.direction), (RULE_NEWER, "to_cloud"))
        self.assertEqual(loaded.plan_id, plan.plan_id)

    def test_a_plan_at_the_defaults_hashes_as_before_and_names_neither(self) -> None:
        self.write(self.local, _history(COMMON))
        plan = self.plan()
        path = save_transfer_plan(plan, self.root / "plan.json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("conflict_rule", raw)
        self.assertNotIn("direction", raw)


class ApplyByRuleTests(_Sandbox):
    """Scan and apply under the default policy, with the loser kept whole."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config.toml"
        self.config.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "desktop"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            local_state_dir = "{self.local.as_posix()}"
            cloud_root_dir = "{self.cloud.as_posix()}"
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
        self.plan_path = self.root / "plan.json"
        self.write(self.local, LOCAL)
        self.write(self.cloud, REMOTE, xz=True)
        gate = mock.patch("codexsync.app._make_safety_gate", return_value=_StoppedGate())
        gate.start()
        self.addCleanup(gate.stop)

    def scan(self, **kwargs):
        plan = scan_session_transfer(self.config, source_machine="desktop", target_machine="laptop", **kwargs)
        save_transfer_plan(plan, self.plan_path)
        return plan

    def test_the_default_keeps_the_newer_copy_and_bundles_the_other(self) -> None:
        plan = self.scan()
        self.assertEqual(plan.conflict_rule, RULE_NEWER, "prefer_newer_mtime is the default")
        written = apply_session_transfer(self.config, plan_path=self.plan_path, confirm_plan=plan.plan_id)
        self.assertEqual(written, 1)
        mirror = self.cloud / "sessions" / "s1.jsonl.xz"
        self.assertEqual(lzma.decompress(mirror.read_bytes()), LOCAL)
        (item,) = plan.items
        bundle = self.root / "semantic" / "conflicts" / item.conflict_id
        kept = b"".join(path.read_bytes() for path in bundle.rglob("*") if path.is_file())
        self.assertIn(b"continued on the other machine", kept, "the copy not kept is saved whole")

    def test_a_one_run_policy_overrides_the_config(self) -> None:
        plan = self.scan(conflict_policy="manual_abort")
        self.assertEqual(plan.conflict_rule, RULE_ASK)
        with self.assertRaises(ConflictError):
            apply_session_transfer(self.config, plan_path=self.plan_path, confirm_plan=plan.plan_id)
        self.assertEqual(lzma.decompress((self.cloud / "sessions" / "s1.jsonl.xz").read_bytes()), REMOTE)

    def test_the_apply_decides_by_the_plans_rule_not_the_config_of_the_moment(self) -> None:
        plan = self.scan(conflict_policy="prefer_cloud")
        written = apply_session_transfer(self.config, plan_path=self.plan_path, confirm_plan=plan.plan_id)
        # The cloud's copy wins; writing .codex is gated, so nothing is written.
        self.assertEqual(written, 0)
        self.assertEqual((self.local / "sessions" / "s1.jsonl").read_bytes(), LOCAL)

    def test_the_cli_takes_a_one_run_policy(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("codexsync.cli.configure_logging"):
            code = main([
                "-c", str(self.config), "sessions", "scan", "--source-machine", "desktop",
                "--target-machine", "laptop", "--conflict-policy", "manual_abort",
            ])
        self.assertEqual(code, 2, "a conflict left open is exit 2, as before")
        self.assertIn('"BLOCKED_CONFLICT": 1', out.getvalue())


if __name__ == "__main__":
    unittest.main()
