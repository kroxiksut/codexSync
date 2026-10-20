"""D-023: a chat archived (or unarchived) on one machine follows on the other.

The laptop run of 2026-10-02 stopped for good on 11 chats the desktop had
archived: the sync said "decide on the Sessions page", and that page had nothing
to decide. Now the move is applied -- backup first, then the copy at the new
path, then the old file removed -- and which side moved is read from this
machine's own semantic manifest entry, never from clocks.

CS-348 lives here too: one session id in two files is settled by the runtime's
own catalogue naming exactly one of them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_merge import BranchRelation
from codexsync.semantic_store import SemanticStore
from codexsync.semantic_transfer import (
    ARCHIVE_FOLLOWS_LOCAL,
    ARCHIVE_FOLLOWS_REMOTE,
    ARCHIVED_AND_CONTINUED,
    IN_PLACE_CATALOG_ABSENT,
    MOVES_BRANCH,
    PROVEN_LAYOUTS,
    STALE_DUPLICATE_BY_CATALOG,
    TransferAction,
    build_transfer_plan,
    prefer_catalogued_copies,
    save_transfer_plan,
    transfer_direction,
)
from codexsync.jsonl_codec import ZSTD_READABLE
from codexsync.session_catalog import SessionState, scan_sessions
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements

SID = "019d5e22-369a-7cf3-8818-051cdd79ebf5"
ACTIVE_PATH = f"sessions/2026/04/05/rollout-2026-04-05T22-53-13-{SID}.jsonl"
ARCHIVED_PATH = f"archived_sessions/rollout-2026-04-05T22-53-13-{SID}.jsonl"


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class _Machine(unittest.TestCase):
    """This machine (`machine-a`), its `.codex`, the cloud mirror and a real catalogue."""

    def setUp(self) -> None:
        self.assertEqual(PROVEN_LAYOUTS, {})
        self.root = Path.cwd() / "test-sandbox" / f"archive-moves-{uuid.uuid4().hex}"
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
            mirror_compression = "none"

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

    def branch(self, root: Path, relative: str, records: list[str], session_id: str = SID) -> Path:
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session_meta", "payload": {"id": session_id}}]
        rows += [{"type": "event", "record": value} for value in records]
        path.write_bytes(b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows))
        return path

    def catalogue(self, rows: dict[str, Path], archived: tuple[str, ...] = ()) -> None:
        connection = sqlite3.connect(self.local_dir / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                [(thread, str(path), 1 if thread in archived else 0) for thread, path in rows.items()],
            )
            connection.commit()
        finally:
            connection.close()

    def agreed(self, machine: str, state: str, records: list[str]) -> None:
        """A semantic manifest entry: `machine` and the mirror agreed on this history."""
        probe = self.root / "probe.jsonl"
        self.branch(self.root, "probe.jsonl", records)
        digest = hashlib.sha256()
        count = 0
        for line in probe.read_bytes().splitlines(keepends=True):
            digest.update(line)
            count += 1
        SemanticStore(self.root / "semantic", machine).record(
            SID, state=state, sha256=digest.hexdigest(), record_count=count,
            byte_count=probe.stat().st_size, agreed=True,
        )
        probe.unlink()

    def scan(self):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            plan = scan_session_transfer(self.config_path, source_machine="desktop", target_machine="machine-a")
        save_transfer_plan(plan, self.plan_path)
        return plan

    def apply(self, plan) -> int:
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return apply_session_transfer(self.config_path, plan_path=self.plan_path, confirm_plan=plan.plan_id)


class ArchiveMoveApplyTests(_Machine):
    @unittest.skipUnless(ZSTD_READABLE, "this Python has no compression.zstd (added in 3.14)")
    def test_a_compressed_chat_archived_elsewhere_follows_into_the_archive(self) -> None:
        # Codex keeps `x.jsonl.zst` while its row names `x.jsonl`; read as two
        # paths, the move was refused as `CATALOG_PLACES_ELSEWHERE` (CS-430).
        from compression import zstd

        plain = self.branch(self.local_dir, ACTIVE_PATH, ["one", "two"])
        packed = plain.with_name(plain.name + ".zst")
        packed.write_bytes(zstd.compress(plain.read_bytes()))
        plain.unlink()
        remote = self.branch(self.cloud_dir, ARCHIVED_PATH, ["one", "two"])
        self.catalogue({SID: plain})
        self.agreed("desktop", "ARCHIVED", ["one", "two"])

        plan = self.scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.ARCHIVE_TRANSITION, item.codes)
        self.assertIn(MOVES_BRANCH, item.codes)
        self.assertEqual(self.apply(plan), 2, "one copy and one removal")
        self.assertEqual((self.local_dir / Path(*ARCHIVED_PATH.split("/"))).read_bytes(), remote.read_bytes())
        self.assertFalse(packed.exists())

    def test_a_chat_archived_on_the_other_machine_is_archived_here(self) -> None:
        # The laptop case: the desktop archived the chat, the laptop never
        # recorded an agreement of its own, the contents are the same.
        here = self.branch(self.local_dir, ACTIVE_PATH, ["one", "two"])
        before = here.read_bytes()
        self.branch(self.cloud_dir, ARCHIVED_PATH, ["one", "two"])
        self.catalogue({SID: here})
        self.agreed("desktop", "ARCHIVED", ["one", "two"])

        plan = self.scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.ARCHIVE_TRANSITION, item.codes)
        self.assertIn(ARCHIVE_FOLLOWS_REMOTE, item.codes)
        self.assertIn(MOVES_BRANCH, item.codes)
        self.assertEqual(item.target_relative_path, ARCHIVED_PATH)
        self.assertEqual(transfer_direction(item), "local")

        self.assertEqual(self.apply(plan), 2, "one copy and one removal")
        moved = self.local_dir / Path(*ARCHIVED_PATH.split("/"))
        self.assertEqual(moved.read_bytes(), before)
        self.assertFalse(here.exists(), "the active copy is gone: one file for one chat")

        manifests = list((self.root / "backups").glob("*.manifest.json"))
        self.assertEqual(len(manifests), 1)
        snapshot = self.root / "backups" / json.loads(manifests[0].read_text(encoding="utf-8"))["snapshot"]
        self.assertEqual((snapshot / Path(*ACTIVE_PATH.split("/"))).read_bytes(), before, "backed up first")

        store = SemanticStore(self.root / "semantic", "machine-a")
        self.assertEqual(set(store.own_states().values()), {"ARCHIVED"}, "the agreement is recorded")
        again = self.scan()
        self.assertEqual([entry.action for entry in again.items], [TransferAction.NOOP])

    def test_a_chat_archived_here_is_archived_in_the_mirror(self) -> None:
        # This machine agreed on the active chat, then archived it: the mirror
        # still holds the agreed state, so the mirror is what moves.
        self.branch(self.local_dir, ARCHIVED_PATH, ["one"])
        mirror = self.branch(self.cloud_dir, ACTIVE_PATH, ["one"])
        self.catalogue({})
        self.agreed("machine-a", "ACTIVE", ["one"])

        plan = self.scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.ARCHIVE_TRANSITION, item.codes)
        self.assertIn(ARCHIVE_FOLLOWS_LOCAL, item.codes)
        self.assertEqual(transfer_direction(item), "mirror")
        self.assertEqual(item.target_relative_path, ARCHIVED_PATH)

        self.assertEqual(self.apply(plan), 2)
        self.assertFalse(mirror.exists())
        self.assertTrue((self.cloud_dir / Path(*ARCHIVED_PATH.split("/"))).is_file())
        self.assertTrue((self.local_dir / Path(*ARCHIVED_PATH.split("/"))).is_file(), "this side untouched")

    def test_a_move_without_a_catalogue_is_refused_and_writes_nothing(self) -> None:
        here = self.branch(self.local_dir, ACTIVE_PATH, ["one"])
        self.branch(self.cloud_dir, ARCHIVED_PATH, ["one"])
        self.agreed("desktop", "ARCHIVED", ["one"])
        plan = self.scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn(IN_PLACE_CATALOG_ABSENT, item.codes)
        self.assertEqual(self.apply(plan), 0)
        self.assertTrue(here.is_file())

    def test_archived_on_one_machine_and_continued_on_the_other_is_a_decision(self) -> None:
        # The mirror archived the chat at one record; this machine continued it.
        here = self.branch(self.local_dir, ACTIVE_PATH, ["one", "two"])
        self.branch(self.cloud_dir, ARCHIVED_PATH, ["one"])
        self.catalogue({SID: here})
        self.agreed("desktop", "ARCHIVED", ["one"])
        (item,) = self.scan().items
        self.assertEqual(item.relation, BranchRelation.ARCHIVE_TRANSITION)
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn(ARCHIVED_AND_CONTINUED, item.codes)
        self.assertIsNotNone(item.conflict_id)


class ArchiveMoveScopeTests(unittest.TestCase):
    def test_a_move_the_mirror_follows_is_never_narrowed_by_a_working_set(self) -> None:
        from codexsync.semantic_transfer import TransferItem, _apply_scope

        mirror = TransferItem(
            "h", BranchRelation.ARCHIVE_TRANSITION, TransferAction.ARCHIVE_TRANSITION,
            "a", "a", 1, 1, ARCHIVED_PATH, None, (ARCHIVE_FOLLOWS_LOCAL, MOVES_BRANCH),
        )
        here = TransferItem(
            "h", BranchRelation.ARCHIVE_TRANSITION, TransferAction.ARCHIVE_TRANSITION,
            "a", "a", 1, 1, ARCHIVED_PATH, None, (ARCHIVE_FOLLOWS_REMOTE, MOVES_BRANCH),
        )
        self.assertIs(_apply_scope(mirror, set()), mirror)
        self.assertEqual(_apply_scope(here, set()).action, TransferAction.OUT_OF_SCOPE)


class CatalogueSettlesDuplicateTests(_Machine):
    """CS-348: Codex carried a thread on in a new file and left the old one."""

    OLD = f"sessions/2026/08/31/rollout-2026-08-31T11-06-11-{SID}.jsonl"
    NEW = f"sessions/2026/08/31/rollout-2026-08-31T16-26-54-{SID}_01a056ed-b595-7740-8d3c-b99906130f7e.jsonl"

    def test_the_copy_the_catalogue_names_is_the_branch(self) -> None:
        old = self.branch(self.local_dir, self.OLD, ["old"])
        new = self.branch(self.local_dir, self.NEW, ["new", "more"])
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {SID: self.NEW}, frozenset())
        catalog = prefer_catalogued_copies(scan_sessions(self.local_dir), placements)
        by_path = {item.relative_path: item for item in catalog.descriptors}
        self.assertIs(by_path[self.NEW].state, SessionState.ACTIVE)
        self.assertIn(STALE_DUPLICATE_BY_CATALOG, by_path[self.OLD].codes)
        self.assertEqual(catalog.branches, {})

        plan = build_transfer_plan(
            catalog, scan_sessions(self.cloud_dir),
            local_root=self.local_dir, remote_root=self.cloud_dir,
            source_machine="desktop", target_machine="machine-a", placements=placements,
        )
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE, "the live copy reaches the mirror")
        self.assertEqual(item.target_relative_path, self.NEW)
        self.assertIn(STALE_DUPLICATE_BY_CATALOG, item.codes)
        self.assertIn(STALE_DUPLICATE_BY_CATALOG, plan.codes)
        self.assertTrue(old.is_file() and new.is_file(), "reading moves nothing")

    def test_without_a_catalogue_naming_one_copy_the_duplicate_stays_blocked(self) -> None:
        self.branch(self.local_dir, self.OLD, ["old"])
        self.branch(self.local_dir, self.NEW, ["new"])
        for placements in (
            None,
            ThreadPlacements(PlacementStatus.ABSENT, {}, frozenset()),
            ThreadPlacements(PlacementStatus.AVAILABLE, {SID: "sessions/elsewhere.jsonl"}, frozenset()),
        ):
            catalog = prefer_catalogued_copies(scan_sessions(self.local_dir), placements)
            plan = build_transfer_plan(
                catalog, scan_sessions(self.cloud_dir),
                local_root=self.local_dir, remote_root=self.cloud_dir,
                source_machine="desktop", target_machine="machine-a", placements=placements,
            )
            (item,) = plan.items
            self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
            self.assertIn("LOCAL_DUPLICATE_SESSION_ID", item.codes)

    def test_the_settled_copy_is_applied_end_to_end(self) -> None:
        self.branch(self.local_dir, self.OLD, ["old"])
        new = self.branch(self.local_dir, self.NEW, ["new", "more"])
        self.catalogue({SID: new})
        plan = self.scan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE, item.codes)
        self.assertEqual(self.apply(plan), 1)
        self.assertEqual((self.cloud_dir / Path(*self.NEW.split("/"))).read_bytes(), new.read_bytes())
        self.assertFalse((self.cloud_dir / Path(*self.OLD.split("/"))).exists(), "the stale copy never travels")


if __name__ == "__main__":
    unittest.main()
