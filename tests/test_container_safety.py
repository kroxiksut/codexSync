"""What a container must never cost: a lost session, or a rewritten user file.

Compression is a property of the mirror's container and never of the history,
and three things follow from that which no other test covers.

A container is part of the destination *name*. Nothing deletes the old name --
`delete_policy` is never -- so changing the container of a branch the mirror
already holds leaves two files for one session id, which the catalogue reads as
`DUPLICATE_SESSION_ID`: both drop out of `valid` and the session is never
compared, mirrored or fast-forwarded again, with no error at all.

A name is not evidence about content. A user's file called `notes.jsonl.gz`
under an included root is a user's file, and an ordinary `sync` that unpacked
it because of its name would leave content that no longer matches the name.

And a half-written container is the expected condition in a folder a cloud
client writes on its own schedule. `gzip` raises `EOFError`, `lzma` raises
`LZMAError`, and neither is an `OSError`.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.jsonl_codec import JsonlCodec, compress_file, open_jsonl
from codexsync.semantic_merge import BranchRelation, compare_session_branches
from codexsync.semantic_transfer import TransferAction, build_transfer_plan
from codexsync.session_catalog import SessionState, scan_sessions
from codexsync.sync_engine import SyncEngine

NEWLINE = b"\n"


def _history(session_id: str, records: list[str]) -> bytes:
    rows: list[dict] = [{"type": "session_meta", "payload": {"id": session_id}}]
    rows += [{"type": "event", "record": value} for value in records]
    return b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + NEWLINE for row in rows)


class ContainerChangeTests(unittest.TestCase):
    """A branch the mirror already holds keeps the container it is held in."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"container-{uuid.uuid4().hex}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        (self.local / "sessions").mkdir(parents=True)
        (self.cloud / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _plan(self, codec: JsonlCodec):
        return build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.cloud),
            local_root=self.local, remote_root=self.cloud,
            source_machine="a", target_machine="b", mirror_codec=codec,
        )

    def test_two_containers_of_one_session_make_it_invisible(self) -> None:
        """The failure this guards against, stated directly."""
        plain = self.cloud / "sessions" / "rollout-s1.jsonl"
        plain.write_bytes(_history("s1", ["one"]))
        compress_file(plain, self.cloud / "sessions" / "rollout-s1.jsonl.xz", JsonlCodec.XZ)

        catalog = scan_sessions(self.cloud)
        self.assertEqual([item.state for item in catalog.descriptors], [SessionState.AMBIGUOUS] * 2)
        self.assertEqual(catalog.valid, [], "the session is gone from every plan")

    def test_a_branch_the_mirror_holds_plainly_stays_plain(self) -> None:
        (self.local / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one", "two"]))
        (self.cloud / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one"]))

        item = self._plan(JsonlCodec.XZ).items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(item.target_relative_path, "sessions/rollout-s1.jsonl")
        self.assertIn("MIRROR_CONTAINER_KEPT", item.codes)

    def test_a_branch_the_mirror_lacks_uses_the_configured_container(self) -> None:
        (self.local / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one"]))

        item = self._plan(JsonlCodec.XZ).items[0]
        self.assertEqual(item.target_relative_path, "sessions/rollout-s1.jsonl.xz")
        self.assertNotIn("MIRROR_CONTAINER_KEPT", item.codes)

    def test_a_branch_already_compressed_is_not_written_back_plain(self) -> None:
        (self.local / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one", "two"]))
        staging = self.root / "s1.jsonl"
        staging.write_bytes(_history("s1", ["one"]))
        compress_file(staging, self.cloud / "sessions" / "rollout-s1.jsonl.xz", JsonlCodec.XZ)

        item = self._plan(JsonlCodec.NONE).items[0]
        self.assertEqual(item.target_relative_path, "sessions/rollout-s1.jsonl.xz")
        self.assertIn("MIRROR_CONTAINER_KEPT", item.codes)

    def test_a_mixed_mirror_writes_each_branch_in_its_own_container(self) -> None:
        """One plan, two containers: the item decides, not the plan-wide codec."""
        (self.local / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one", "two"]))
        (self.local / "sessions" / "rollout-s2.jsonl").write_bytes(_history("s2", ["one", "two"]))
        (self.cloud / "sessions" / "rollout-s1.jsonl").write_bytes(_history("s1", ["one"]))

        targets = sorted(item.target_relative_path for item in self._plan(JsonlCodec.XZ).items)
        self.assertEqual(targets, ["sessions/rollout-s1.jsonl", "sessions/rollout-s2.jsonl.xz"])


class OrdinaryFilesAreNeverTransformedTests(unittest.TestCase):
    """A plain copy carries bytes across, whatever the file is called."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"verbatim-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_user_file_that_looks_like_a_container_is_copied_verbatim(self) -> None:
        source = self.root / "notes.jsonl.gz"
        source.write_bytes(gzip.compress(b'{"a":1}' + NEWLINE))
        staged = self.root / "staged.payload"

        SyncEngine._stage_verified(source, staged)

        self.assertEqual(staged.read_bytes(), source.read_bytes())

    def test_a_transfer_that_asks_for_a_container_still_transcodes(self) -> None:
        source = self.root / "branch.jsonl"
        source.write_bytes(b'{"a":1}' + NEWLINE)
        staged = self.root / "staged.payload"

        SyncEngine._stage_verified(source, staged, JsonlCodec.XZ)

        self.assertNotEqual(staged.read_bytes(), source.read_bytes())
        with open_jsonl(staged, JsonlCodec.XZ) as handle:
            self.assertEqual(handle.read(), source.read_bytes())

    def test_a_branch_out_of_a_container_is_still_decompressed(self) -> None:
        plain = self.root / "branch.jsonl"
        plain.write_bytes(b'{"a":1}' + NEWLINE)
        stored = self.root / "branch.jsonl.xz"
        compress_file(plain, stored, JsonlCodec.XZ)
        staged = self.root / "staged.payload"

        SyncEngine._stage_verified(stored, staged, JsonlCodec.NONE)

        self.assertEqual(staged.read_bytes(), plain.read_bytes())


class UnreadableContainerTests(unittest.TestCase):
    """A truncated container is a classification, not a traceback."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"truncated-{uuid.uuid4().hex}"
        (self.root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _truncated(self, name: str, codec: JsonlCodec) -> Path:
        plain = self.root / "whole.jsonl"
        plain.write_bytes(_history("s1", ["one", "two", "three"]))
        target = self.root / "sessions" / name
        compress_file(plain, target, codec)
        target.write_bytes(target.read_bytes()[:-12])
        plain.unlink()
        return target

    def test_a_truncated_xz_branch_is_invalid_rather_than_a_traceback(self) -> None:
        self._truncated("rollout-s1.jsonl.xz", JsonlCodec.XZ)
        item = scan_sessions(self.root).descriptors[0]
        self.assertEqual(item.state, SessionState.INVALID)
        self.assertIn("READ_ERROR", item.codes)

    def test_a_truncated_gzip_branch_is_invalid_rather_than_a_traceback(self) -> None:
        self._truncated("rollout-s1.jsonl.gz", JsonlCodec.GZIP)
        item = scan_sessions(self.root).descriptors[0]
        self.assertEqual(item.state, SessionState.INVALID)
        self.assertIn("READ_ERROR", item.codes)

    def test_comparing_against_a_truncated_branch_is_invalid_not_a_crash(self) -> None:
        broken = self._truncated("rollout-s1.jsonl.xz", JsonlCodec.XZ)
        whole = self.root / "sessions" / "rollout-s2.jsonl"
        whole.write_bytes(_history("s1", ["one"]))

        result = compare_session_branches(whole, broken)

        self.assertEqual(result.relation, BranchRelation.INVALID)


if __name__ == "__main__":
    unittest.main()
