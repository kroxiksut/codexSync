"""A transfer copy is bound to the branch its plan was built from.

CS-515: the plan is rebuilt and compared before the lock is taken, and a cloud
client may deliver a newer branch into the mirror after that. Staging proved
only that the source did not change *while it was copied*, so the newer branch
was staged, replaced the chat, and the mismatch surfaced afterwards as
RECOVERY_REQUIRED. Now staging compares with the hash the plan recorded and
refuses before any destination is touched.

CS-518: a container that cannot be decoded (a cloud client still writing it)
is a changed source to retry, not an unexpected internal error.
"""
from __future__ import annotations

import hashlib
import lzma
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.backup import BackupManager
from codexsync.exceptions import FailSafeError
from codexsync.jsonl_codec import JsonlCodec
from codexsync.models import CopyAction, SyncPlan
from codexsync.sync_engine import SyncEngine

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
PLANNED = b'{"type":"session_meta"}\n{"n":1}\n'
NEWER = PLANNED + b'{"n":2}\n'


class StageBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"stage-binding-{uuid.uuid4().hex[:8]}"
        (self.root / "mirror").mkdir(parents=True)
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.destination = self.root / "codex" / "chat.jsonl"
        self.destination.write_bytes(b'{"type":"session_meta"}\n')

    def engine(self) -> SyncEngine:
        return SyncEngine(
            backup_manager=BackupManager(self.root / "backups", "machine-a", 7, 0),
            temp_dir=self.root / ".tmp", backup_before_overwrite=True, fail_on_unknown=True,
        )

    def copy(self, source: Path, expected: bytes | None) -> SyncPlan:
        return SyncPlan(to_local=[CopyAction(
            src=source, dst=self.destination, relative_path="chat.jsonl", codec=JsonlCodec.NONE,
            expected_sha256=hashlib.sha256(expected).hexdigest() if expected is not None else None,
        )])

    def test_a_branch_that_moved_after_the_plan_is_never_written(self) -> None:
        source = self.root / "mirror" / "chat.jsonl.xz"
        source.write_bytes(lzma.compress(NEWER))
        with self.assertRaises(FailSafeError) as raised:
            self.engine().execute(self.copy(source, PLANNED), dry_run=False)
        self.assertEqual(raised.exception.code, "PLAN_CHANGED")
        self.assertEqual(self.destination.read_bytes(), b'{"type":"session_meta"}\n')
        self.assertEqual(
            [item.name for item in self.destination.parent.iterdir()], ["chat.jsonl"],
            "no staged sibling is left behind",
        )

    def test_the_branch_the_plan_named_is_written(self) -> None:
        source = self.root / "mirror" / "chat.jsonl.xz"
        source.write_bytes(lzma.compress(PLANNED))
        self.engine().execute(self.copy(source, PLANNED), dry_run=False)
        self.assertEqual(self.destination.read_bytes(), PLANNED)

    def test_a_like_container_is_compared_by_what_it_holds(self) -> None:
        """Mirror to mirror keeps the bytes, but the plan's hash is the stream's."""
        source = self.root / "mirror" / "chat.jsonl.xz"
        source.write_bytes(lzma.compress(PLANNED))
        staged = self.root / "mirror" / "staged"
        SyncEngine._stage_verified(
            source, staged, JsonlCodec.XZ, hashlib.sha256(PLANNED).hexdigest(),
        )
        self.assertEqual(staged.read_bytes(), source.read_bytes())

    def test_an_ordinary_copy_carries_no_expectation(self) -> None:
        self.assertIsNone(CopyAction(Path("a"), Path("b"), "a").expected_sha256)

    def test_a_truncated_container_is_a_changed_source(self) -> None:
        """CS-518: retry, not an internal error."""
        source = self.root / "mirror" / "chat.jsonl.xz"
        source.write_bytes(lzma.compress(PLANNED)[:-8])
        with self.assertRaises(FailSafeError) as raised:
            self.engine().execute(self.copy(source, PLANNED), dry_run=False)
        self.assertEqual(raised.exception.code, "STATE_CHANGED_RETRY")
        self.assertEqual(self.destination.read_bytes(), b'{"type":"session_meta"}\n')



class DamagedContainerTests(unittest.TestCase):
    """CS-518: the semantic store fails safe on a container it cannot decode."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"damaged-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def store(self):
        from codexsync.semantic_store import SemanticStore

        return SemanticStore(self.root / "store", "machine-a")

    def damaged(self) -> Path:
        path = self.root / "chat.jsonl.xz"
        path.write_bytes(lzma.compress(PLANNED)[:-8])
        return path

    def test_a_superseded_branch_that_cannot_be_decoded_is_retried(self) -> None:
        with self.assertRaises(FailSafeError) as raised:
            self.store().archive_superseded(self.damaged(), session_id="s", reason="FORMAT_MIGRATION")
        self.assertEqual(raised.exception.code, "STATE_CHANGED_RETRY")
        self.assertFalse((self.root / "store" / "superseded").exists())

    def test_a_conflict_branch_that_cannot_be_decoded_is_retried(self) -> None:
        plain = self.root / "other.jsonl"
        plain.write_bytes(PLANNED)
        with self.assertRaises(FailSafeError) as raised:
            self.store().conflict_bundle(plain, self.damaged(), session_id="s", common_records=0)
        self.assertEqual(raised.exception.code, "STATE_CHANGED_RETRY")
        self.assertFalse((self.root / "store" / "conflicts").exists())


if __name__ == "__main__":
    unittest.main()
