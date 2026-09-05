from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.exceptions import FailSafeError
from codexsync.semantic_merge import BranchRelation
from codexsync.semantic_transfer import (
    MIRROR_LAYOUT_ID,
    PROVEN_LAYOUTS,
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    conflict_id_for,
    load_transfer_plan,
    save_transfer_plan,
    target_relative_path,
)
from codexsync.semantic_transfer import _catalogue_objection
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements


PROVEN = "{state}/{file_name}"


def _catalogue(by_session):
    return ThreadPlacements(PlacementStatus.AVAILABLE, dict(by_session))


class TransferPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"transfer-{uuid.uuid4().hex}"
        self.local_root = self.root / "local"
        self.remote_root = self.root / "remote"
        (self.local_root / "sessions").mkdir(parents=True)
        (self.remote_root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        PROVEN_LAYOUTS.clear()

    def _branch(self, root: Path, name: str, lines: list[str], *, state=SessionState.ACTIVE):
        folder = "archived_sessions" if state is SessionState.ARCHIVED else "sessions"
        (root / folder).mkdir(parents=True, exist_ok=True)
        path = root / folder / name
        payload = b"".join(json.dumps({"r": line}).encode("utf-8") + b"\n" for line in lines)
        path.write_bytes(payload)
        return path

    def _descriptor(self, session_id: str, name: str, lines: int, *, state=SessionState.ACTIVE):
        folder = "archived_sessions" if state is SessionState.ARCHIVED else "sessions"
        return SessionDescriptor(session_id, state, f"{folder}/{name}", "0" * 64, 0, lines)

    def _catalog(self, descriptors):
        return SessionCatalog(list(descriptors), {})

    def _plan(self, local_desc, remote_desc, **kwargs):
        return build_transfer_plan(
            self._catalog(local_desc),
            self._catalog(remote_desc),
            local_root=self.local_root,
            remote_root=self.remote_root,
            source_machine="desktop",
            target_machine="laptop",
            **kwargs,
        )

    # --- classification into actions --------------------------------------

    def test_identical_branches_are_a_noop_and_never_blocked(self) -> None:
        self._branch(self.local_root, "a.jsonl", ["1", "2"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)], [self._descriptor("s1", "a.jsonl", 2)]
        )
        self.assertEqual(plan.items[0].action, TransferAction.NOOP)
        self.assertEqual(plan.items[0].relation, BranchRelation.IDENTICAL)

    def test_fast_forward_is_blocked_while_the_layout_is_unproven(self) -> None:
        self.assertEqual(PROVEN_LAYOUTS, {}, "no layout may be assumed proven")
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 1)], [self._descriptor("s1", "a.jsonl", 2)]
        )
        item = plan.items[0]
        self.assertEqual(item.relation, BranchRelation.FAST_FORWARD_LOCAL)
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn("PLAN_HAS_BLOCKED_ITEMS", plan.codes)
        self.assertEqual(plan.writable_items, ())

    def test_fast_forward_becomes_writable_once_a_layout_is_proven(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 1)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "sessions/a.jsonl")
        self.assertEqual(len(plan.writable_items), 1)

    def test_a_session_only_one_side_has_is_reported_not_dropped(self) -> None:
        self._branch(self.local_root, "a.jsonl", ["1"])
        plan = self._plan([self._descriptor("s1", "a.jsonl", 1)], [])
        self.assertIn("SESSION_ON_ONE_SIDE_ONLY", plan.items[0].codes)

    # --- divergence and resolution ---------------------------------------

    def test_divergence_blocks_and_names_a_conflict(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1", "left"])
        self._branch(self.remote_root, "a.jsonl", ["1", "right"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIsNotNone(item.conflict_id)
        self.assertEqual(plan.writable_items, ())

    def test_a_recorded_resolution_unblocks_exactly_one_side(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1", "left"])
        self._branch(self.remote_root, "a.jsonl", ["1", "right"])
        blocked = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
        ).items[0]

        resolution = BranchResolution(
            blocked.conflict_id, blocked.session_hash,
            blocked.local_sha256, blocked.remote_sha256, ResolutionChoice.KEEP_REMOTE,
        )
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
            resolutions={resolution.conflict_id: resolution},
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertIn("RESOLVED_BY_USER", item.codes)

    def test_a_resolution_goes_stale_when_a_branch_changes(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1", "left"])
        self._branch(self.remote_root, "a.jsonl", ["1", "right"])
        first = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
        ).items[0]
        resolution = BranchResolution(
            first.conflict_id, first.session_hash,
            first.local_sha256, first.remote_sha256, ResolutionChoice.KEEP_REMOTE,
        )
        # The user's choice was about a history that has since moved on.
        self._branch(self.remote_root, "a.jsonl", ["1", "right", "and more"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 3)],
            layout_id="test-layout",
            resolutions={resolution.conflict_id: resolution},
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn("STALE_RESOLUTION", item.codes)

    def test_defer_keeps_the_conflict_blocked(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1", "left"])
        self._branch(self.remote_root, "a.jsonl", ["1", "right"])
        blocked = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
        ).items[0]
        resolution = BranchResolution(
            blocked.conflict_id, blocked.session_hash,
            blocked.local_sha256, blocked.remote_sha256, ResolutionChoice.DEFER,
        )
        item = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
            resolutions={resolution.conflict_id: resolution},
        ).items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn("DEFERRED", item.codes)

    def test_confirmation_hash_binds_the_choice_to_both_branch_hashes(self) -> None:
        conflict = conflict_id_for("s" * 64, "a" * 64, "b" * 64)
        keep_local = BranchResolution(conflict, "s" * 64, "a" * 64, "b" * 64, ResolutionChoice.KEEP_LOCAL)
        keep_remote = BranchResolution(conflict, "s" * 64, "a" * 64, "b" * 64, ResolutionChoice.KEEP_REMOTE)
        other_bytes = BranchResolution(conflict, "s" * 64, "c" * 64, "b" * 64, ResolutionChoice.KEEP_LOCAL)
        self.assertNotEqual(keep_local.confirmation, keep_remote.confirmation)
        self.assertNotEqual(keep_local.confirmation, other_bytes.confirmation)

    # --- other blocks ------------------------------------------------------

    # --- the runtime catalogue decides what it will ever see ---------------

    def _towards_local(self, **kwargs):
        """A fast-forward whose destination is the Codex state directory."""
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        return self._plan(
            [self._descriptor("s1", "a.jsonl", 1)],
            [self._descriptor("s1", "a.jsonl", 2)],
            layout_id="test-layout",
            **kwargs,
        ).items[0]

    def test_a_branch_the_catalogue_already_places_there_may_be_overwritten(self) -> None:
        item = self._towards_local(placements=_catalogue({"s1": "sessions/a.jsonl"}))
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)

    def test_a_session_the_catalogue_never_heard_of_cannot_be_written(self) -> None:
        """Making the runtime see it would need a row codexSync will not write."""
        item = self._towards_local(placements=_catalogue({"other": "sessions/x.jsonl"}))
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND)
        self.assertIn("SESSION_NOT_IN_CATALOG", item.codes)

    def test_a_catalogue_pointing_somewhere_else_blocks_the_write(self) -> None:
        item = self._towards_local(placements=_catalogue({"s1": "sessions/2026/other.jsonl"}))
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND)
        self.assertIn("CATALOG_PLACES_ELSEWHERE", item.codes)

    def test_a_catalogue_that_cannot_be_read_blocks_rather_than_is_ignored(self) -> None:
        """Unreadable is not the same as absent; the runtime may still rule here."""
        item = self._towards_local(
            placements=ThreadPlacements(PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",))
        )
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND)
        self.assertIn("CATALOG_UNREADABLE", item.codes)

    def test_no_catalogue_at_all_constrains_nothing(self) -> None:
        item = self._towards_local(placements=ThreadPlacements(PlacementStatus.ABSENT, {}))
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)

    def test_the_mirror_is_written_whatever_the_catalogue_says(self) -> None:
        """Nothing has to find that copy afterwards, so the catalogue is moot."""
        self._branch(self.local_root, "a.jsonl", ["1", "2"])
        self._branch(self.remote_root, "a.jsonl", ["1"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)],
            [self._descriptor("s1", "a.jsonl", 1)],
            placements=ThreadPlacements(PlacementStatus.INDETERMINATE, {}),
        )
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_REMOTE)

    def test_two_sessions_wanting_one_destination_block_the_second(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        for root in (self.local_root, self.remote_root):
            self._branch(root, "same.jsonl", ["1"])
        self._branch(self.remote_root, "same.jsonl", ["1", "2"])
        local = [self._descriptor("s1", "same.jsonl", 1), self._descriptor("s2", "same.jsonl", 1)]
        remote = [self._descriptor("s1", "same.jsonl", 2), self._descriptor("s2", "same.jsonl", 2)]
        plan = self._plan(local, remote, layout_id="test-layout")
        actions = [item.action for item in plan.items]
        self.assertIn(TransferAction.BLOCKED_TARGET_COLLISION, actions)

    def test_target_path_is_refused_while_the_layout_is_unproven(self) -> None:
        with self.assertRaises(FailSafeError):
            target_relative_path("unproven", self._descriptor("s1", "a.jsonl", 1))

    def test_archive_transition_requires_a_confirmed_base(self) -> None:
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1"], state=SessionState.ARCHIVED)
        local = [self._descriptor("s1", "a.jsonl", 1)]
        remote = [self._descriptor("s1", "a.jsonl", 1, state=SessionState.ARCHIVED)]

        without_base = self._plan(local, remote, layout_id="test-layout")
        self.assertEqual(without_base.items[0].relation, BranchRelation.MISSING_BASE)
        self.assertEqual(without_base.items[0].action, TransferAction.BLOCKED_CONFLICT)

        session_hash = without_base.items[0].session_hash
        with_base = self._plan(
            local, remote, layout_id="test-layout", confirmed_bases={session_hash}
        )
        self.assertEqual(with_base.items[0].action, TransferAction.ARCHIVE_TRANSITION)

    # --- the cloud mirror is a different destination -----------------------

    def test_a_write_towards_the_mirror_needs_no_proven_layout(self) -> None:
        """The mirror is codexSync's own copy, so its layout is not a guess."""
        self.assertEqual(PROVEN_LAYOUTS, {}, "no layout may be assumed proven")
        self._branch(self.local_root, "a.jsonl", ["1", "2"])
        self._branch(self.remote_root, "a.jsonl", ["1"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2)], [self._descriptor("s1", "a.jsonl", 1)]
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(item.target_relative_path, "sessions/a.jsonl")
        self.assertIn("MIRROR_DESTINATION", item.codes)
        self.assertEqual(plan.mirror_layout_id, MIRROR_LAYOUT_ID)

    def test_the_way_back_into_codex_state_is_still_gated(self) -> None:
        self.assertEqual(PROVEN_LAYOUTS, {}, "no layout may be assumed proven")
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 1)], [self._descriptor("s1", "a.jsonl", 2)]
        )
        self.assertEqual(plan.items[0].action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)

    def test_a_local_only_session_can_rebuild_the_mirror(self) -> None:
        """The case the mirror exists for: the cloud copy is missing sessions."""
        self._branch(self.local_root, "a.jsonl", ["1"])
        plan = self._plan([self._descriptor("s1", "a.jsonl", 1)], [])
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertIn("SESSION_ON_ONE_SIDE_ONLY", item.codes)
        self.assertEqual(item.target_relative_path, "sessions/a.jsonl")
        self.assertEqual(len(plan.writable_items), 1)

    def test_a_mirror_only_session_stays_blocked_until_the_layout_is_proven(self) -> None:
        self._branch(self.remote_root, "a.jsonl", ["1"])
        plan = self._plan([], [self._descriptor("s1", "a.jsonl", 1)])
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn("SESSION_ON_ONE_SIDE_ONLY", item.codes)
        self.assertEqual(plan.writable_items, ())

    def test_the_mirror_keeps_the_path_the_branch_already_has(self) -> None:
        (self.local_root / "archived_sessions").mkdir(parents=True, exist_ok=True)
        path = self.local_root / "archived_sessions" / "2026" / "a.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"r": "1"}).encode("utf-8") + b"\n")
        descriptor = SessionDescriptor(
            "s1", SessionState.ARCHIVED, "archived_sessions/2026/a.jsonl", "0" * 64, 0, 1
        )
        plan = self._plan([descriptor], [])
        self.assertEqual(plan.items[0].target_relative_path, "archived_sessions/2026/a.jsonl")

    def test_the_two_destinations_are_separate_and_do_not_collide(self) -> None:
        """One relative path under two roots is two files, not a collision."""
        PROVEN_LAYOUTS["test-layout"] = PROVEN
        self._branch(self.local_root, "a.jsonl", ["1", "2"])
        self._branch(self.remote_root, "a.jsonl", ["1"])
        self._branch(self.local_root, "b.jsonl", ["1"])
        self._branch(self.remote_root, "b.jsonl", ["1", "2"])
        # s1 goes local -> mirror, s2 goes mirror -> local; both are named
        # a.jsonl in their own destination layout.
        plan = build_transfer_plan(
            self._catalog([
                self._descriptor("s1", "a.jsonl", 2), self._descriptor("s2", "b.jsonl", 1),
            ]),
            self._catalog([
                self._descriptor("s1", "a.jsonl", 1), self._descriptor("s2", "b.jsonl", 2),
            ]),
            local_root=self.local_root,
            remote_root=self.remote_root,
            source_machine="desktop",
            target_machine="laptop",
            layout_id="test-layout",
        )
        actions = {item.action for item in plan.items}
        self.assertNotIn(TransferAction.BLOCKED_TARGET_COLLISION, actions)
        self.assertEqual(len(plan.writable_items), 2)

    # --- the plan is frozen ------------------------------------------------

    def test_plan_id_is_deterministic_and_round_trips(self) -> None:
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        local = [self._descriptor("s1", "a.jsonl", 1)]
        remote = [self._descriptor("s1", "a.jsonl", 2)]
        first = self._plan(local, remote)
        path = self.root / "plan.json"
        save_transfer_plan(first, path)
        loaded = load_transfer_plan(path)
        self.assertEqual(loaded.plan_id, first.plan_id)
        self.assertEqual(loaded.items[0].action, first.items[0].action)

    def test_a_tampered_plan_is_rejected(self) -> None:
        self._branch(self.local_root, "a.jsonl", ["1"])
        self._branch(self.remote_root, "a.jsonl", ["1", "2"])
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 1)], [self._descriptor("s1", "a.jsonl", 2)]
        )
        path = self.root / "plan.json"
        save_transfer_plan(plan, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["items"][0]["action"] = TransferAction.FAST_FORWARD_LOCAL.value
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_transfer_plan(path)


class LayoutTemplateTests(unittest.TestCase):
    """What a proven layout template has to be able to say.

    Measured on the state this was written against: 228 active branches live at
    ``sessions/<year>/<month>/<day>/<file>`` and 23 archived ones lie flat at
    ``archived_sessions/<file>``. A template that can name only the state folder
    and the file describes the second shape and silently puts every branch of
    the first in a directory the thread catalogue does not name -- a session the
    runtime never shows, with no error. These tests run the branch that
    ``PROVEN_LAYOUTS`` keeps shut, because nothing else ever will.
    """

    PARTITIONED = "{state}/{source_dir}/{file_name}"

    def tearDown(self) -> None:
        PROVEN_LAYOUTS.clear()

    def _descriptor(self, relative_path: str, *, state=SessionState.ACTIVE, session_id="s1"):
        return SessionDescriptor(session_id, state, relative_path, "0" * 64, 0, 1)

    def _render(self, template: str, relative_path: str, **kwargs) -> str:
        PROVEN_LAYOUTS["probe"] = template
        return target_relative_path("probe", self._descriptor(relative_path, **kwargs))

    def test_a_date_partitioned_branch_keeps_its_date_folders(self) -> None:
        self.assertEqual(
            self._render(self.PARTITIONED, "sessions/2026/03/12/rollout-a.jsonl"),
            "sessions/2026/03/12/rollout-a.jsonl",
        )

    def test_the_same_template_renders_a_flat_archived_branch(self) -> None:
        """One template covers both shapes: the empty segment disappears."""
        self.assertEqual(
            self._render(
                self.PARTITIONED,
                "archived_sessions/rollout-a.jsonl",
                state=SessionState.ARCHIVED,
            ),
            "archived_sessions/rollout-a.jsonl",
        )

    def test_the_state_folder_is_the_template_choice_not_the_source_one(self) -> None:
        """A branch active there and archived here lands under the local folder."""
        self.assertEqual(
            self._render(
                self.PARTITIONED, "sessions/2026/03/12/a.jsonl", state=SessionState.ARCHIVED
            ),
            "archived_sessions/2026/03/12/a.jsonl",
        )

    def test_a_container_never_survives_into_the_destination_name(self) -> None:
        self.assertEqual(
            self._render(self.PARTITIONED, "sessions/2026/03/12/a.jsonl.xz"),
            "sessions/2026/03/12/a.jsonl",
        )

    def test_a_flat_template_is_what_the_catalogue_refuses(self) -> None:
        """The regression itself, stated as the catalogue sees it."""
        descriptor = self._descriptor("sessions/2026/03/12/a.jsonl")
        placements = _catalogue({"s1": "sessions/2026/03/12/a.jsonl"})

        PROVEN_LAYOUTS["probe"] = PROVEN
        flat = target_relative_path("probe", descriptor)
        self.assertEqual(flat, "sessions/a.jsonl")
        self.assertEqual(
            _catalogue_objection(placements, "s1", flat), ("CATALOG_PLACES_ELSEWHERE",)
        )

        PROVEN_LAYOUTS["probe"] = self.PARTITIONED
        kept = target_relative_path("probe", descriptor)
        self.assertIsNone(_catalogue_objection(placements, "s1", kept))

    def test_the_session_id_is_available_to_a_template(self) -> None:
        self.assertEqual(
            self._render("{state}/{session_id}.jsonl", "sessions/2026/03/12/a.jsonl"),
            "sessions/s1.jsonl",
        )

    def test_a_template_naming_an_id_the_branch_lacks_is_refused(self) -> None:
        PROVEN_LAYOUTS["probe"] = "{state}/{session_id}.jsonl"
        descriptor = SessionDescriptor(
            None, SessionState.ACTIVE, "sessions/a.jsonl", "0" * 64, 0, 1
        )
        with self.assertRaises(FailSafeError):
            target_relative_path("probe", descriptor)

    def test_an_unknown_placeholder_is_refused_rather_than_raising_keyerror(self) -> None:
        with self.assertRaises(FailSafeError):
            self._render("{state}/{machine}/{file_name}", "sessions/a.jsonl")

    def test_a_template_that_escapes_the_state_root_is_refused(self) -> None:
        with self.assertRaises(FailSafeError):
            self._render("{state}/../{file_name}", "sessions/a.jsonl")

    def test_a_malformed_template_is_refused_rather_than_raising_valueerror(self) -> None:
        with self.assertRaises(FailSafeError):
            self._render("{state}/{file_name", "sessions/a.jsonl")


class PlanCompatibilityTests(unittest.TestCase):
    """A plan from an older build is named as such, not as a corrupt one."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"plan-compat-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_plan_without_a_mirror_layout_id_says_what_it_is(self) -> None:
        path = self.root / "plan.json"
        raw = {
            "format": "codexsync-session-transfer-v1",
            "version": 1, "plan_id": "x", "created_at_utc": "2026-01-01T00:00:00Z",
            "source_machine": "a", "target_machine": "b", "layout_id": "unproven",
            "canonical_version": "canonical-json-v1", "volatile": False,
            "items": [], "codes": [],
        }
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_transfer_plan(path)
        self.assertIn("predates the mirror layout id", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
