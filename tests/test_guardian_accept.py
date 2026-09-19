"""Accepting a suspicious shrink as Guardian's new baseline (`guardian accept`).

The case these tests are built around was observed on a real machine on
2026-09-13: the desktop build re-created every project under a new id, the
bindings that named the old ids were gone, and from then on every snapshot went
to quarantine as ``BINDING_COUNT_DROP`` because the baseline never moved.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from codexsync.cli import main
from codexsync.exceptions import ConflictError, FailSafeError, GuardianBusyError
from codexsync.guardian_accept import (
    NO_BASELINE,
    NOTHING_TO_ACCEPT,
    SHRINK_ACCEPTED,
    SOURCE_MISSING,
    STATE_REJECTED,
    GuardianAcceptPlan,
)
from codexsync.guardian_lock import GuardianRunnerLock
from codexsync.guardian_manifest import load_guardian_manifest
from codexsync.guardian_models import GuardianConfig, GuardianResultStatus, ValidationStatus
from codexsync.guardian_pointer import resolve_or_restore_latest_good
from codexsync.guardian_retention import prune_snapshots
from codexsync.guardian_runner import GuardianRunner
from codexsync.guardian_shrink import BINDING_COUNT_DROP
from codexsync.guardian_store import GuardianStore


HOST = "local:C:\\Users\\user\\.codex"


def _project(project_id: str, root: str) -> dict:
    return {"id": project_id, "name": project_id, "rootPaths": [root], "createdAt": 1, "updatedAt": 2}


def _state(projects: dict[str, str], bindings: dict[str, str], **extra) -> bytes:
    state = {
        "local-projects": {pid: _project(pid, root) for pid, root in projects.items()},
        "project-order": list(projects),
        "thread-project-assignments": {
            thread: {"projectKind": "local", "projectId": pid} for thread, pid in bindings.items()
        },
        "app-server-project-id-by-legacy-project-id-by-host": {HOST: {pid: f"as-{pid}" for pid in projects}},
        "selected-project": None,
    }
    state.update(extra)
    return json.dumps(state, ensure_ascii=False).encode("utf-8")


BEFORE = _state(
    {"p1": "C:/a", "p2": "C:/b", "p3": "C:/c"},
    {"t1": "p1", "t2": "p1", "t3": "p2", "t4": "p3"},
)
# Codex re-created every project under a new id; only one chat was bound again.
RECREATED = _state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t9": "n1"})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _tree(root: Path) -> dict[str, tuple[bool, int, int]]:
    if not root.exists():
        return {}
    result = {".": (True, 0, root.stat().st_mtime_ns)}
    for path in root.rglob("*"):
        stat_result = path.stat()
        result[path.relative_to(root).as_posix()] = (
            path.is_dir(), 0 if path.is_dir() else stat_result.st_size, stat_result.st_mtime_ns,
        )
    return result


class GuardianAcceptTests(unittest.TestCase):
    def setUp(self) -> None:
        # System temp like the other store tests: snapshot ids are long enough
        # to cross Windows' path limit under a deep checkout.
        self.root = Path(tempfile.mkdtemp(prefix="cs-gacc-"))
        self.source = self.root / ".codex-global-state.json"
        self.config = GuardianConfig(
            root_dir=self.root / "guardian",
            max_state_bytes=1024 * 1024,
            debounce_seconds=0.1,
            stable_read_interval_seconds=0.1,
            once_timeout_seconds=5,
        )
        self.store = GuardianStore(self.config.root_dir, "machine-a", producer_version="test")
        self.clock = _Clock()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _runner(self) -> GuardianRunner:
        return GuardianRunner(
            self.source, self.store, self.config, monotonic=self.clock.monotonic, sleep=self.clock.sleep
        )

    def _write(self, payload: bytes) -> None:
        self.source.write_bytes(payload)

    def _stuck_store(self):
        """A committed baseline, then a real drop that the watcher quarantines."""
        self._write(BEFORE)
        first = self._runner().once()
        self.assertEqual(first.status, GuardianResultStatus.COMMITTED)
        self._write(RECREATED)
        self.assertEqual(self._runner().once().status, GuardianResultStatus.QUARANTINED)
        assert first.result is not None and first.result.snapshot is not None
        return first.result.snapshot

    # -- the plan -----------------------------------------------------------------

    def test_a_recreated_project_set_is_explained_in_counts(self) -> None:
        baseline = self._stuck_store()

        plan = self._runner().preview_accept()

        self.assertEqual(plan.codes, ())
        self.assertEqual(plan.baseline_snapshot_id, baseline.snapshot_id)
        self.assertEqual(plan.shrink_codes, (BINDING_COUNT_DROP,))
        self.assertEqual((plan.projects_before, plan.projects_now), (3, 3))
        self.assertEqual((plan.bindings_before, plan.bindings_now), (4, 1))
        why = plan.explanation
        assert why is not None
        self.assertEqual((why.projects_replaced, why.projects_removed, why.projects_added), (3, 0, 0))
        self.assertEqual(
            (why.bindings_to_replaced_projects, why.bindings_to_removed_projects, why.bindings_dropped),
            (4, 0, 0),
        )

    def test_a_binding_lost_from_a_project_that_still_exists_is_called_dropped(self) -> None:
        self._write(BEFORE)
        self._runner().once()
        self._write(_state({"p1": "C:/a", "p2": "C:/b"}, {"t1": "p1"}))

        plan = self._runner().preview_accept()

        why = plan.explanation
        assert why is not None
        self.assertEqual((why.projects_replaced, why.projects_removed), (0, 1))
        self.assertEqual(
            (why.bindings_to_replaced_projects, why.bindings_to_removed_projects, why.bindings_dropped),
            (0, 1, 2),
        )

    def test_the_explanation_names_no_project_root_or_thread(self) -> None:
        self._stuck_store()
        plan = self._runner().preview_accept()
        rendered = repr(plan)
        for secret in ("C:/a", "t1", "p1", "n1"):
            self.assertNotIn(secret, rendered)

    def test_the_plan_id_ignores_bytes_that_do_not_change_the_drop(self) -> None:
        self._stuck_store()
        before = self._runner().preview_accept()
        self._write(_state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t9": "n1"}, **{"window-bounds": [1, 2]}))

        self.assertEqual(self._runner().preview_accept().plan_id, before.plan_id)

    def test_the_plan_id_follows_which_bindings_went(self) -> None:
        self._stuck_store()
        before = self._runner().preview_accept()
        # Same counts, but a different chat kept its binding.
        self._write(_state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t1": "n1"}))

        after = self._runner().preview_accept()

        self.assertEqual(after.bindings_now, before.bindings_now)
        self.assertNotEqual(after.plan_id, before.plan_id)

    def test_a_state_that_is_not_suspicious_needs_no_decision(self) -> None:
        self._write(BEFORE)
        self._runner().once()
        self.assertEqual(self._runner().preview_accept().codes, (NOTHING_TO_ACCEPT,))

    def test_without_a_baseline_there_is_nothing_to_accept(self) -> None:
        self._write(RECREATED)
        plan = self._runner().preview_accept()
        self.assertEqual(plan.codes, (NO_BASELINE,))
        self.assertFalse(self.config.root_dir.exists())

    def test_a_damaged_state_is_refused_whatever_is_confirmed(self) -> None:
        self._stuck_store()
        self._write(b'{"local-projects": ')

        plan = self._runner().preview_accept()

        self.assertEqual(plan.codes, (STATE_REJECTED,))
        with self.assertRaises(ConflictError):
            self._runner().accept(confirm_plan=plan.plan_id)

    def test_a_missing_state_is_refused(self) -> None:
        self._stuck_store()
        self.source.unlink()
        self.assertEqual(self._runner().preview_accept().codes, (SOURCE_MISSING,))

    def test_previewing_writes_nothing(self) -> None:
        self._stuck_store()
        pointer = self.config.root_dir / "latest-good" / "machine-a.json"
        pointer.unlink()
        before = _tree(self.root)

        plan = self._runner().preview_accept()

        self.assertEqual(_tree(self.root), before)
        self.assertFalse(pointer.exists())
        # The writer would rebuild that pointer; the preview says so instead.
        self.assertEqual(plan.codes, ("BASELINE_UNRESOLVED",))

    # -- accepting ----------------------------------------------------------------

    def test_accepting_moves_latest_good_and_unsticks_the_watcher(self) -> None:
        baseline = self._stuck_store()
        plan = self._runner().preview_accept()

        accepted_plan, snapshot = self._runner().accept(confirm_plan=plan.plan_id)

        self.assertEqual(accepted_plan.plan_id, plan.plan_id)
        latest = resolve_or_restore_latest_good(self.config.root_dir, "machine-a")
        self.assertEqual(latest, snapshot)
        manifest = load_guardian_manifest(snapshot.manifest_path)
        self.assertEqual(manifest.validation_status, ValidationStatus.PASS_WITH_WARNING)
        self.assertIn(BINDING_COUNT_DROP, manifest.validation_codes)
        self.assertEqual(manifest.validation_codes[-1], SHRINK_ACCEPTED)
        self.assertEqual(manifest.previous_good_snapshot_id, baseline.snapshot_id)
        self.assertEqual(snapshot.payload_path.read_bytes(), RECREATED)

        # The next ordinary change commits by itself again.
        self._write(_state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t9": "n1", "t8": "n2"}))
        self.assertEqual(self._runner().once().status, GuardianResultStatus.COMMITTED)

    def test_a_later_state_with_the_same_drop_is_what_gets_accepted(self) -> None:
        self._stuck_store()
        plan = self._runner().preview_accept()
        later = _state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t9": "n1"}, **{"window-bounds": [3]})
        self._write(later)

        _, snapshot = self._runner().accept(confirm_plan=plan.plan_id)

        self.assertEqual(snapshot.payload_path.read_bytes(), later)

    def test_a_different_drop_than_previewed_is_refused_and_nothing_is_committed(self) -> None:
        baseline = self._stuck_store()
        plan = self._runner().preview_accept()
        self._write(_state({"n1": "C:/a", "n2": "C:/b"}, {}))

        with self.assertRaises(FailSafeError):
            self._runner().accept(confirm_plan=plan.plan_id)

        self.assertEqual(resolve_or_restore_latest_good(self.config.root_dir, "machine-a"), baseline)

    def test_a_mistyped_plan_id_commits_nothing(self) -> None:
        baseline = self._stuck_store()
        with self.assertRaises(FailSafeError):
            self._runner().accept(confirm_plan="0" * 64)
        self.assertEqual(resolve_or_restore_latest_good(self.config.root_dir, "machine-a"), baseline)

    def test_accepting_waits_for_no_running_watcher(self) -> None:
        self._stuck_store()
        plan = self._runner().preview_accept()
        with GuardianRunnerLock(self.config.root_dir, "machine-a"):
            with self.assertRaises(GuardianBusyError):
                self._runner().accept(confirm_plan=plan.plan_id)

    def test_retention_keeps_the_decision_and_the_baseline_it_overrode(self) -> None:
        baseline = self._stuck_store()
        _, accepted = self._runner().accept(confirm_plan=self._runner().preview_accept().plan_id)
        for step in range(4):
            self._write(_state({"n1": "C:/a", "n2": "C:/b", "n3": "C:/c"}, {"t9": "n1"}, **{"window-bounds": [step]}))
            self.assertEqual(self._runner().once().status, GuardianResultStatus.COMMITTED)

        removed = prune_snapshots(
            self.config.root_dir, "machine-a", retention_days=1, max_snapshots=1,
            now=datetime.now(timezone.utc) + timedelta(days=400),
        )

        self.assertTrue(removed)
        self.assertNotIn(baseline.snapshot_id, removed)
        self.assertNotIn(accepted.snapshot_id, removed)
        self.assertTrue(baseline.directory.is_dir())
        self.assertTrue(accepted.directory.is_dir())


class GuardianAcceptCliTests(unittest.TestCase):
    def _run(self, argv: list[str], plan: GuardianAcceptPlan, accepted: str | None) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("codexsync.cli.accept_guardian_baseline", return_value=(plan, accepted)) as call, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["-c", "config.toml", "guardian", "accept", *argv])
        self.call = call
        return code, out.getvalue()

    def _plan(self, codes: tuple[str, ...] = ()) -> GuardianAcceptPlan:
        return GuardianAcceptPlan(
            version=1, plan_id="abc", machine_id="machine-a", baseline_snapshot_id="snap", baseline_generation=2,
            baseline_created_at_utc="2026-09-05T10:57:48.403582Z", schema_id="electron-v2",
            projects_before=16, bindings_before=6, projects_now=18, bindings_now=1,
            shrink_codes=(BINDING_COUNT_DROP,) if not codes else (), state_codes=(BINDING_COUNT_DROP,),
            explanation=None, codes=codes,
        )

    def test_a_preview_prints_the_confirmation_and_writes_nothing(self) -> None:
        code, out = self._run([], self._plan(), None)
        self.assertEqual(code, 0)
        self.assertIn("--confirm abc", out)
        self.assertEqual(self.call.call_args.kwargs["confirm_plan"], None)

    def test_a_refused_state_is_a_conflict_and_no_decision_needed_is_success(self) -> None:
        self.assertEqual(self._run([], self._plan((STATE_REJECTED,)), None)[0], 2)
        self.assertEqual(self._run([], self._plan((NOTHING_TO_ACCEPT,)), None)[0], 0)

    def test_confirming_passes_the_id_through(self) -> None:
        code, out = self._run(["--confirm", "abc"], self._plan(), "new-snap")
        self.assertEqual(code, 0)
        self.assertEqual(self.call.call_args.kwargs["confirm_plan"], "abc")
        self.assertIn("new-snap is now latest-good", out)


class GuardianSnapshotCliLabelTests(unittest.TestCase):
    def test_the_printed_status_is_the_bare_value(self) -> None:
        """Python 3.12 formats a str-mixin enum as `GuardianResultStatus.UNCHANGED`."""
        from codexsync.guardian_runner import GuardianRunOutcome, GuardianRunnerState

        runner = mock.Mock()
        runner.once.return_value = GuardianRunOutcome(GuardianResultStatus.UNCHANGED, GuardianRunnerState.IDLE)
        out = io.StringIO()
        with mock.patch("codexsync.cli.build_guardian_runner", return_value=runner),                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["-c", "config.toml", "guardian", "snapshot", "--once"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "Guardian snapshot: UNCHANGED")


if __name__ == "__main__":
    unittest.main()
