"""CS-330a: a chat continued on the other machine is written over its own file.

No layout is proven, so a session this machine has never held still cannot be
placed into `.codex`. A session it does hold can: the runtime's catalogue names
the file, the write goes to exactly that file, and nothing about where
sessions live is guessed. Every other case stays blocked as before, and these
tests pin each one, because a refusal that quietly stopped firing would put a
branch where the runtime never looks.
"""
from __future__ import annotations

import gzip
import json
import lzma
from pathlib import Path
import shutil
import sqlite3
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.jsonl_codec import ZSTD_READABLE
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_merge import BranchRelation
from codexsync.semantic_transfer import (
    IN_PLACE,
    IN_PLACE_ARCHIVE_FLAG_DIFFERS,
    IN_PLACE_CATALOG_ABSENT,
    IN_PLACE_CONTAINER,
    IN_PLACE_DECOMPRESSED,
    IN_PLACE_STATE_CHANGES,
    MOVES_BRANCH,
    PROVEN_LAYOUTS,
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    conflict_id_for,
    save_transfer_plan,
)
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements


def _catalogue(by_session, archived=()):
    return ThreadPlacements(PlacementStatus.AVAILABLE, dict(by_session), frozenset(archived))


class InPlacePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(PROVEN_LAYOUTS, {}, "these tests are about the unproven case")
        self.root = Path.cwd() / "test-sandbox" / f"in-place-{uuid.uuid4().hex}"
        self.local_root = self.root / "local"
        self.remote_root = self.root / "remote"
        self.local_root.mkdir(parents=True)
        self.remote_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _branch(self, root: Path, relative: str, lines: list[str]) -> None:
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"".join(json.dumps({"r": line}).encode("utf-8") + b"\n" for line in lines)
        if relative.endswith(".gz"):
            payload = gzip.compress(payload)
        elif relative.endswith(".xz"):
            payload = lzma.compress(payload)
        path.write_bytes(payload)

    def _descriptor(self, relative: str, lines: int, *, state=SessionState.ACTIVE, session_id="s1"):
        return SessionDescriptor(session_id, state, relative, "0" * 64, 0, lines)

    def _plan(self, local, remote, **kwargs):
        return build_transfer_plan(
            SessionCatalog(list(local), {}), SessionCatalog(list(remote), {}),
            local_root=self.local_root, remote_root=self.remote_root,
            source_machine="desktop", target_machine="laptop", **kwargs,
        )

    def _continued(self, local_path="sessions/2026/09/27/a.jsonl", remote_path=None, *,
                   state=SessionState.ACTIVE, **kwargs):
        """A chat both machines hold, one record further on the other machine."""
        remote_path = remote_path or local_path
        self._branch(self.local_root, local_path, ["1"])
        self._branch(self.remote_root, remote_path, ["1", "2"])
        plan = self._plan(
            [self._descriptor(local_path, 1, state=state)],
            [self._descriptor(remote_path, 2, state=state)],
            **kwargs,
        )
        return plan.items[0]

    # --- allowed ----------------------------------------------------------

    def test_a_continued_chat_is_written_over_its_own_file(self) -> None:
        item = self._continued(placements=_catalogue({"s1": "sessions/2026/09/27/a.jsonl"}))
        self.assertEqual(item.relation, BranchRelation.FAST_FORWARD_LOCAL)
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "sessions/2026/09/27/a.jsonl")
        self.assertIn(IN_PLACE, item.codes)

    def test_the_target_is_this_machines_path_not_the_mirrors(self) -> None:
        # The mirror may keep the branch under another date folder; the write
        # still goes where this machine keeps it, which the catalogue names.
        item = self._continued(
            "sessions/2026/09/27/a.jsonl", "sessions/2026/09/26/a.jsonl.xz",
            placements=_catalogue({"s1": "sessions/2026/09/27/a.jsonl"}),
        )
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "sessions/2026/09/27/a.jsonl")

    def test_an_archived_chat_the_catalogue_marks_archived_is_allowed(self) -> None:
        item = self._continued(
            "archived_sessions/a.jsonl", state=SessionState.ARCHIVED,
            placements=_catalogue({"s1": "archived_sessions/a.jsonl"}, archived={"s1"}),
        )
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "archived_sessions/a.jsonl")

    def test_a_resolved_conflict_is_written_in_place_too(self) -> None:
        path = "sessions/a.jsonl"
        self._branch(self.local_root, path, ["1", "left"])
        self._branch(self.remote_root, path, ["1", "right"])
        local, remote = [self._descriptor(path, 2)], [self._descriptor(path, 2)]
        placements = _catalogue({"s1": path})
        blocked = self._plan(local, remote, placements=placements).items[0]
        self.assertEqual(blocked.action, TransferAction.BLOCKED_CONFLICT)
        resolution = BranchResolution(
            blocked.conflict_id, blocked.session_hash, blocked.local_sha256, blocked.remote_sha256,
            ResolutionChoice.KEEP_REMOTE,
        )
        self.assertEqual(
            blocked.conflict_id,
            conflict_id_for(blocked.session_hash, blocked.local_sha256, blocked.remote_sha256),
        )
        item = self._plan(
            local, remote, placements=placements, resolutions={blocked.conflict_id: resolution}
        ).items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertIn(IN_PLACE, item.codes)
        self.assertIn("RESOLVED_BY_USER", item.codes)

    def _resolved(self, placements):
        path = "sessions/a.jsonl"
        self._branch(self.local_root, path, ["1", "left"])
        self._branch(self.remote_root, path, ["1", "right"])
        local, remote = [self._descriptor(path, 2)], [self._descriptor(path, 2)]
        blocked = self._plan(local, remote, placements=placements).items[0]
        resolution = BranchResolution(
            blocked.conflict_id, blocked.session_hash, blocked.local_sha256, blocked.remote_sha256,
            ResolutionChoice.KEEP_REMOTE,
        )
        return self._plan(local, remote, placements=placements, resolutions={blocked.conflict_id: resolution}).items[0]

    def test_a_resolution_never_replaces_a_chat_codex_has_projected(self) -> None:
        # CS-426, 2026-10-19: Codex kept showing the old turns from its
        # thread_history projection, whose offset then pointed past the file.
        projected = ThreadPlacements(
            PlacementStatus.AVAILABLE, {"s1": "sessions/a.jsonl"}, history=PlacementStatus.AVAILABLE,
            projected=frozenset({"s1"}),
        )
        item = self._resolved(projected)
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn("IN_PLACE_PROJECTED_REWRITE", item.codes)

    def test_a_history_store_that_cannot_be_read_refuses_a_replacement(self) -> None:
        unknown = ThreadPlacements(
            PlacementStatus.AVAILABLE, {"s1": "sessions/a.jsonl"}, history=PlacementStatus.INDETERMINATE,
        )
        self.assertIn("IN_PLACE_HISTORY_UNKNOWN", self._resolved(unknown).codes)

    def test_a_continuation_of_a_projected_chat_is_still_written(self) -> None:
        # An append keeps the projection's offset valid; Codex then showed the
        # new turn and moved the offset to the new end (observed).
        projected = ThreadPlacements(
            PlacementStatus.AVAILABLE, {"s1": "sessions/2026/09/27/a.jsonl"}, history=PlacementStatus.AVAILABLE,
            projected=frozenset({"s1"}),
        )
        item = self._continued(placements=projected)
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertIn(IN_PLACE, item.codes)

    def test_a_resolution_for_a_chat_without_a_projection_is_written(self) -> None:
        other = ThreadPlacements(
            PlacementStatus.AVAILABLE, {"s1": "sessions/a.jsonl"}, history=PlacementStatus.AVAILABLE,
            projected=frozenset({"someone-else"}),
        )
        self.assertEqual(self._resolved(other).action, TransferAction.FAST_FORWARD_LOCAL)

    def test_in_place_is_part_of_the_plan_id(self) -> None:
        allowed = self._plan_id(placements=_catalogue({"s1": "sessions/a.jsonl"}))
        refused = self._plan_id(placements=_catalogue({"s1": "sessions/b.jsonl"}))
        self.assertNotEqual(allowed, refused)

    def _plan_id(self, **kwargs) -> str:
        self._branch(self.local_root, "sessions/a.jsonl", ["1"])
        self._branch(self.remote_root, "sessions/a.jsonl", ["1", "2"])
        return self._plan(
            [self._descriptor("sessions/a.jsonl", 1)], [self._descriptor("sessions/a.jsonl", 2)], **kwargs
        ).plan_id

    # --- still refused ----------------------------------------------------

    def _assert_refused(self, item, code: str) -> None:
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn(code, item.codes)
        self.assertNotIn(IN_PLACE, item.codes)

    def test_without_a_catalogue_nothing_is_written(self) -> None:
        self._assert_refused(self._continued(), IN_PLACE_CATALOG_ABSENT)

    def test_an_absent_catalogue_refuses_rather_than_allows(self) -> None:
        item = self._continued(placements=ThreadPlacements(PlacementStatus.ABSENT, {}))
        self._assert_refused(item, IN_PLACE_CATALOG_ABSENT)

    def test_an_unreadable_catalogue_refuses(self) -> None:
        item = self._continued(
            placements=ThreadPlacements(PlacementStatus.INDETERMINATE, {}, codes=("CATALOG_UNAVAILABLE",))
        )
        self._assert_refused(item, "CATALOG_UNREADABLE")

    def test_a_thread_the_catalogue_does_not_know_is_refused(self) -> None:
        item = self._continued(placements=_catalogue({"other": "sessions/2026/09/27/a.jsonl"}))
        self._assert_refused(item, "SESSION_NOT_IN_CATALOG")

    def test_a_catalogue_naming_another_file_is_refused(self) -> None:
        item = self._continued(placements=_catalogue({"s1": "sessions/2026/09/26/a.jsonl"}))
        self._assert_refused(item, "CATALOG_PLACES_ELSEWHERE")

    def test_a_catalogue_row_without_a_path_is_refused(self) -> None:
        item = self._continued(placements=_catalogue({"s1": None}))
        self._assert_refused(item, "CATALOG_PLACEMENT_UNKNOWN")

    def test_an_archive_flag_that_disagrees_with_the_folder_is_refused(self) -> None:
        item = self._continued(
            placements=_catalogue({"s1": "sessions/2026/09/27/a.jsonl"}, archived={"s1"})
        )
        self._assert_refused(item, IN_PLACE_ARCHIVE_FLAG_DIFFERS)

    def test_a_resolution_moves_a_branch_archived_on_one_side(self) -> None:
        path = "sessions/a.jsonl"
        self._branch(self.local_root, path, ["1"])
        self._branch(self.remote_root, "archived_sessions/a.jsonl", ["1", "2"])
        # A resolution is the only way such a pair reaches a fast-forward.
        local = [self._descriptor(path, 1)]
        remote = [self._descriptor("archived_sessions/a.jsonl", 2, state=SessionState.ARCHIVED)]
        placements = _catalogue({"s1": path})
        blocked = self._plan(local, remote, placements=placements).items[0]
        self.assertEqual(blocked.action, TransferAction.BLOCKED_CONFLICT)
        resolution = BranchResolution(
            blocked.conflict_id, blocked.session_hash, blocked.local_sha256, blocked.remote_sha256,
            ResolutionChoice.KEEP_REMOTE,
        )
        item = self._plan(
            local, remote, placements=placements, resolutions={blocked.conflict_id: resolution}
        ).items[0]
        # The kept branch is archived, so this machine's copy moves into the
        # archive with it (D-023), and only because the catalogue names it.
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(item.target_relative_path, "archived_sessions/a.jsonl")
        self.assertIn(MOVES_BRANCH, item.codes)
        self.assertNotIn(IN_PLACE_STATE_CHANGES, item.codes)

        absent = ThreadPlacements(PlacementStatus.ABSENT, {}, frozenset())
        refused = self._plan(
            local, remote, placements=absent, resolutions={blocked.conflict_id: resolution}
        ).items[0]
        self._assert_refused(refused, IN_PLACE_CATALOG_ABSENT)

    def test_a_local_branch_in_a_container_is_refused(self) -> None:
        item = self._continued(
            "sessions/a.jsonl.gz", "sessions/a.jsonl",
            placements=_catalogue({"s1": "sessions/a.jsonl.gz"}),
        )
        self._assert_refused(item, IN_PLACE_CONTAINER)

    def test_a_chat_this_machine_never_held_still_needs_a_layout(self) -> None:
        self._branch(self.remote_root, "sessions/a.jsonl", ["1", "2"])
        item = self._plan(
            [], [self._descriptor("sessions/a.jsonl", 2)],
            placements=_catalogue({"s1": "sessions/a.jsonl"}),
        ).items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertNotIn(IN_PLACE, item.codes)
        self.assertIsNone(item.target_relative_path)


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class InPlaceApplyTests(unittest.TestCase):
    """The whole envelope, with a real thread catalogue and no proven layout."""

    def setUp(self) -> None:
        self.assertEqual(PROVEN_LAYOUTS, {})
        self.root = Path.cwd() / "test-sandbox" / f"in-place-apply-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        self.local_dir.mkdir(parents=True)
        self.cloud_dir.mkdir(parents=True)
        self.plan_path = self.root / "plan.json"
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(textwrap.dedent(f"""
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
            """).strip() + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _branch(self, root: Path, relative: str, session_id: str, records: list[str]) -> Path:
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session_meta", "payload": {"id": session_id}}]
        rows += [{"type": "event", "record": value} for value in records]
        path.write_bytes(b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows))
        return path

    def _thread_catalogue(self, rows: dict[str, Path]) -> None:
        connection = sqlite3.connect(self.local_dir / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, 0)",
                # The runtime stores absolute paths, a good share of them with
                # the extended-length prefix.
                [(thread, "\\\\?\\" + str(path)) for thread, path in rows.items()],
            )
            connection.commit()
        finally:
            connection.close()

    def _scan(self):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            plan = scan_session_transfer(self.config_path, source_machine="desktop", target_machine="laptop")
        save_transfer_plan(plan, self.plan_path)
        return plan

    def _apply(self, plan) -> int:
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return apply_session_transfer(self.config_path, plan_path=self.plan_path, confirm_plan=plan.plan_id)

    def test_the_continuation_reaches_codex_and_the_old_file_is_backed_up(self) -> None:
        relative = "sessions/2026/09/27/rollout-s1.jsonl"
        local = self._branch(self.local_dir, relative, "s1", ["one"])
        before = local.read_bytes()
        remote = self._branch(self.cloud_dir, relative, "s1", ["one", "two"])
        self._thread_catalogue({"s1": local})

        plan = self._scan()
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)
        self.assertIn(IN_PLACE, item.codes)

        self.assertEqual(self._apply(plan), 1)
        self.assertEqual(local.read_bytes(), remote.read_bytes())
        siblings = sorted(path.name for path in local.parent.iterdir())
        self.assertEqual(siblings, ["rollout-s1.jsonl"], "no second file for one session")

        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        snapshot = self.root / "backups" / json.loads(manifests[0].read_text(encoding="utf-8"))["snapshot"]
        self.assertEqual((snapshot / Path(*relative.split("/"))).read_bytes(), before)

    @unittest.skipUnless(ZSTD_READABLE, "this Python has no compression.zstd (added in 3.14)")
    def test_a_compressed_chat_continued_elsewhere_becomes_plain_and_the_zst_is_backed_up(self) -> None:
        # CS-430: Codex keeps `x.jsonl.zst` while its row names `x.jsonl`.
        from compression import zstd

        relative = "sessions/2026/09/27/rollout-s1.jsonl"
        plain = self._branch(self.local_dir, relative, "s1", ["one"])
        packed = plain.with_name(plain.name + ".zst")
        before = zstd.compress(plain.read_bytes())
        packed.write_bytes(before)
        plain.unlink()
        remote = self._branch(self.cloud_dir, relative, "s1", ["one", "two"])
        self._thread_catalogue({"s1": plain})

        plan = self._scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL, item.codes)
        self.assertIn(IN_PLACE_DECOMPRESSED, item.codes)

        self.assertEqual(self._apply(plan), 2, "one copy, one removal")
        self.assertEqual(plain.read_bytes(), remote.read_bytes())
        siblings = sorted(path.name for path in plain.parent.iterdir())
        self.assertEqual(siblings, ["rollout-s1.jsonl"], "the .zst is gone, no second file for one chat")

        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        snapshot = self.root / "backups" / json.loads(manifests[0].read_text(encoding="utf-8"))["snapshot"]
        self.assertEqual((snapshot / Path(*(relative + ".zst").split("/"))).read_bytes(), before)

    def test_a_new_chat_stays_in_the_cloud_copy_when_asked_to(self) -> None:
        # `keep_in_cloud`; the default carries it (test_new_chats_same_path).
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "[semantic]\n", '[semantic]\nnew_chats = "keep_in_cloud"\n', 1
            ),
            encoding="utf-8",
        )
        self._branch(self.cloud_dir, "sessions/2026/09/27/rollout-s2.jsonl", "s2", ["one"])
        self._thread_catalogue({})
        plan = self._scan()
        self.assertEqual(plan.items[0].action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertEqual(self._apply(plan), 0)
        self.assertFalse((self.local_dir / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
