from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.semantic_merge import (
    BranchRelation,
    BranchState,
    CanonicalStatus,
    canonical_digest,
    compare_session_branches,
)


class SemanticMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"semantic-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _file(self, name: str, lines: list[bytes]) -> Path:
        path = self.root / name
        path.write_bytes(b"".join(line + b"\n" for line in lines))
        return path

    # --- ancestry ---------------------------------------------------------

    def test_identical_branches(self) -> None:
        left = self._file("left", [b"a", b"b"])
        right = self._file("right", [b"a", b"b"])
        self.assertEqual(compare_session_branches(left, right).relation, BranchRelation.IDENTICAL)

    def test_local_behind_remote_fast_forwards_local(self) -> None:
        local = self._file("local", [b"a", b"b"])
        remote = self._file("remote", [b"a", b"b", b"c"])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.FAST_FORWARD_LOCAL)
        self.assertEqual(result.common_records, 2)
        self.assertFalse(result.is_conflict)

    def test_remote_behind_local_fast_forwards_remote(self) -> None:
        local = self._file("local", [b"a", b"b", b"c"])
        remote = self._file("remote", [b"a", b"b"])
        self.assertEqual(
            compare_session_branches(local, remote).relation, BranchRelation.FAST_FORWARD_REMOTE
        )

    # --- divergence -------------------------------------------------------

    def test_divergence_after_a_common_prefix_is_never_interleaved(self) -> None:
        local = self._file("local", [b"a", b"left"])
        remote = self._file("remote", [b"a", b"right"])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.DIVERGED)
        self.assertEqual(result.common_records, 1)
        self.assertTrue(result.is_conflict)

    def test_same_id_without_a_single_common_record(self) -> None:
        local = self._file("local", [b"x", b"y"])
        remote = self._file("remote", [b"p", b"q"])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.DIVERGED_NO_COMMON_RECORDS)
        self.assertEqual(result.common_records, 0)

    def test_a_longer_branch_that_rewrote_history_is_diverged_not_prefix(self) -> None:
        local = self._file("local", [b"a", b"b"])
        remote = self._file("remote", [b"a", b"CHANGED", b"c"])
        self.assertEqual(compare_session_branches(local, remote).relation, BranchRelation.DIVERGED)

    def test_an_unreadable_branch_is_invalid_not_a_guess(self) -> None:
        local = self._file("local", [b"a"])
        result = compare_session_branches(local, self.root / "absent.jsonl")
        self.assertEqual(result.relation, BranchRelation.INVALID)
        self.assertTrue(result.is_conflict)

    def test_an_oversized_record_is_invalid(self) -> None:
        local = self._file("local", [b"a" * 100])
        remote = self._file("remote", [b"a" * 100])
        result = compare_session_branches(local, remote, max_line_bytes=10)
        self.assertEqual(result.relation, BranchRelation.INVALID)

    # --- archive transitions ---------------------------------------------

    def test_archive_transition_needs_proven_ancestry(self) -> None:
        local = self._file("local", [b"a", b"b"])
        remote = self._file("remote", [b"a", b"b"])
        result = compare_session_branches(
            local, remote,
            local_state=BranchState.ACTIVE, remote_state=BranchState.ARCHIVED,
        )
        self.assertEqual(result.relation, BranchRelation.ARCHIVE_TRANSITION)

    def test_archive_transition_without_a_base_is_missing_base(self) -> None:
        local = self._file("local", [b"a"])
        remote = self._file("remote", [b"a"])
        result = compare_session_branches(
            local, remote,
            local_state=BranchState.ACTIVE, remote_state=BranchState.ARCHIVED,
            has_confirmed_base=False,
        )
        self.assertEqual(result.relation, BranchRelation.MISSING_BASE)
        self.assertTrue(result.is_conflict)

    def test_divergent_archive_versus_active_is_always_a_conflict(self) -> None:
        local = self._file("local", [b"a", b"left"])
        remote = self._file("remote", [b"a", b"right"])
        result = compare_session_branches(
            local, remote,
            local_state=BranchState.ACTIVE, remote_state=BranchState.ARCHIVED,
        )
        self.assertEqual(result.relation, BranchRelation.DIVERGED)

    # --- canonical digest -------------------------------------------------

    def test_key_order_alone_is_not_a_divergence(self) -> None:
        local = self._file("local", [b'{"a":1,"b":2}'])
        remote = self._file("remote", [b'{"b":2,"a":1}'])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.IDENTICAL)
        self.assertEqual(result.canonical_only_matches, 1)
        self.assertEqual(result.canonical_status, CanonicalStatus.EXACT)

    def test_floats_are_never_canonicalised(self) -> None:
        self.assertIsNone(canonical_digest(b'{"a":1.0}'))
        self.assertIsNotNone(canonical_digest(b'{"a":1}'))

    def test_non_nfc_strings_are_never_canonicalised(self) -> None:
        composed = json.dumps({"a": "é"}).encode("utf-8")          # NFC
        decomposed = json.dumps({"a": "é"}).encode("utf-8")       # NFD
        self.assertIsNotNone(canonical_digest(composed))
        self.assertIsNone(canonical_digest(decomposed))

    def test_records_that_cannot_be_canonicalised_stay_different(self) -> None:
        # Same value, two spellings, one of them a float: they must not collapse.
        local = self._file("local", [b'{"a":1.0}'])
        remote = self._file("remote", [b'{"a":1}'])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.DIVERGED_NO_COMMON_RECORDS)
        self.assertEqual(result.canonical_status, CanonicalStatus.INDETERMINATE)

    def test_non_json_records_compare_by_raw_bytes(self) -> None:
        local = self._file("local", [b"not json", b"tail"])
        remote = self._file("remote", [b"not json", b"tail"])
        self.assertEqual(compare_session_branches(local, remote).relation, BranchRelation.IDENTICAL)

    # --- streaming --------------------------------------------------------

    def test_large_branches_stream_without_loading_whole_files(self) -> None:
        many = [json.dumps({"i": index}).encode("utf-8") for index in range(20_000)]
        local = self._file("local", many)
        remote = self._file("remote", many + [b'{"i":20000}'])
        result = compare_session_branches(local, remote)
        self.assertEqual(result.relation, BranchRelation.FAST_FORWARD_LOCAL)
        self.assertEqual(result.local_records, 20_000)
        self.assertEqual(result.remote_records, 20_001)

    def test_record_counts_and_digests_are_reported_per_branch(self) -> None:
        local = self._file("local", [b"a"])
        remote = self._file("remote", [b"a", b"b"])
        result = compare_session_branches(local, remote)
        self.assertEqual((result.local_records, result.remote_records), (1, 2))
        self.assertNotEqual(result.local_sha256, result.remote_sha256)
        self.assertEqual(len(result.local_sha256), 64)


if __name__ == "__main__":
    unittest.main()
