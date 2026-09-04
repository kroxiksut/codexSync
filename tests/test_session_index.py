from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.exceptions import FailSafeError
from codexsync.session_index import (
    PROVEN_CONTRACTS,
    IndexContract,
    Reduction,
    merge_session_indexes,
    parse_session_index,
    render_session_index,
)


class SessionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"session-index-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name: str, records: list[dict], *, tail: bytes = b"") -> Path:
        path = self.root / name
        payload = b"".join(
            json.dumps(record, sort_keys=True).encode("utf-8") + b"\n" for record in records
        ) + tail
        path.write_bytes(payload)
        return path

    def _index(self, name: str, records: list[tuple[str, str, str]], *, tail: bytes = b""):
        rows = [
            {"id": sid, "thread_name": title, "updated_at": updated}
            for sid, title, updated in records
        ]
        return parse_session_index(self._write(name, rows, tail=tail))

    # --- record contract -------------------------------------------------

    def test_v1_shape_is_recognised_without_exposing_the_thread_name(self) -> None:
        result = self._index("index.jsonl", [("a", "private one", "2"), ("a", "private two", "3")])
        self.assertEqual(result.contract, IndexContract.V1)
        self.assertEqual(result.reduced["a"].thread_name, "private two")
        self.assertNotIn("private", repr(result))

    def test_unknown_fields_survive_verbatim(self) -> None:
        path = self._write("index.jsonl", [
            {"id": "a", "thread_name": "n", "updated_at": "1", "future_field": {"k": [1, 2]}},
        ])
        result = parse_session_index(path)
        self.assertEqual(result.contract, IndexContract.V1)
        self.assertIn(b"future_field", result.reduced["a"].raw)

    def test_missing_or_mistyped_required_fields_are_isolated(self) -> None:
        path = self._write("index.jsonl", [
            {"id": "a", "thread_name": "n", "updated_at": "1"},
            {"id": "b", "thread_name": 7, "updated_at": "1"},
            {"thread_name": "n", "updated_at": "1"},
        ])
        result = parse_session_index(path)
        self.assertIn("INVALID_REQUIRED_FIELDS", result.codes)
        self.assertEqual(set(result.reduced), {"a"}, "a bad record must not remove a good one")
        self.assertEqual(result.contract, IndexContract.UNKNOWN)

    def test_duplicate_ids_are_journal_entries_not_errors(self) -> None:
        result = self._index("index.jsonl", [("a", "first", "1"), ("a", "second", "2"), ("b", "x", "1")])
        self.assertEqual(len(result.records), 3)
        self.assertEqual(set(result.reduced), {"a", "b"})

    def test_incomplete_tail_is_preserved_as_a_digest_and_flagged(self) -> None:
        result = self._index("index.jsonl", [("a", "n", "1")], tail=b'{"id":"partial"')
        self.assertIn("INCOMPLETE_TAIL", result.codes)
        self.assertIsNotNone(result.raw_tail_digest)
        self.assertEqual(set(result.reduced), {"a"}, "the valid part stays readable")

    def test_missing_index_does_not_mean_missing_sessions(self) -> None:
        result = parse_session_index(self.root / "absent.jsonl")
        self.assertIn("MISSING_INDEX", result.codes)
        self.assertEqual(result.records, ())

    # --- reduction ambiguity ---------------------------------------------

    def test_agreeing_reductions_are_reported_as_unambiguous(self) -> None:
        result = self._index("index.jsonl", [("a", "old", "1"), ("a", "new", "2")])
        self.assertTrue(result.reductions_agree)
        self.assertNotIn("REDUCTION_AMBIGUOUS", result.codes)

    def test_timestamp_rollback_makes_the_reduction_ambiguous(self) -> None:
        # Last line wins says "second"; max updated_at says "first".
        result = self._index("index.jsonl", [("a", "first", "9"), ("a", "second", "1")])
        self.assertFalse(result.reductions_agree)
        self.assertIn("REDUCTION_AMBIGUOUS", result.codes)

    def test_equal_timestamps_with_different_names_are_ambiguous(self) -> None:
        # Equal timestamps: max-updated-at keeps the first, last-line-wins the
        # second, so the two readings differ and must be reported.
        result = self._index("index.jsonl", [("a", "left", "5"), ("a", "right", "5")])
        self.assertFalse(result.reductions_agree)
        self.assertIn("REDUCTION_AMBIGUOUS", result.codes)

    # --- three-way merge --------------------------------------------------

    def test_one_sided_rename_transfers(self) -> None:
        base = self._index("base", [("a", "original", "1")])
        local = self._index("local", [("a", "original", "1")])
        remote = self._index("remote", [("a", "renamed", "2")])
        result = merge_session_indexes(base, local, remote)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.merged["a"].thread_name, "renamed")

    def test_identical_rename_on_both_sides_deduplicates(self) -> None:
        base = self._index("base", [("a", "original", "1")])
        local = self._index("local", [("a", "renamed", "2")])
        remote = self._index("remote", [("a", "renamed", "2")])
        result = merge_session_indexes(base, local, remote)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.merged["a"].thread_name, "renamed")

    def test_divergent_rename_conflicts_without_trusting_the_clock(self) -> None:
        base = self._index("base", [("a", "base", "5")])
        local = self._index("local", [("a", "left", "4")])
        remote = self._index("remote", [("a", "right", "999")])
        result = merge_session_indexes(base, local, remote)
        self.assertEqual(len(result.conflicts), 1)
        self.assertIn("INDEX_CONFLICT", result.codes)
        self.assertNotIn("a", result.merged, "neither candidate may be silently chosen")

    def test_missing_base_is_not_downgraded_to_two_way(self) -> None:
        local = self._index("local", [("a", "left", "1")])
        remote = self._index("remote", [("a", "right", "2")])
        result = merge_session_indexes(None, local, remote)
        self.assertEqual(result.codes, ("MISSING_BASE",))
        self.assertEqual(result.merged, {})

    def test_an_id_present_only_in_the_base_is_dropped_by_neither_side(self) -> None:
        base = self._index("base", [("a", "n", "1")])
        local = self._index("local", [("a", "n", "1")])
        remote = self._index("remote", [("a", "n", "1")])
        result = merge_session_indexes(base, local, remote)
        self.assertIn("a", result.merged)

    def test_unrecognised_contract_blocks_the_merge(self) -> None:
        base = self._index("base", [("a", "n", "1")])
        broken = parse_session_index(self._write("broken", [{"id": "a"}]))
        result = merge_session_indexes(base, base, broken)
        self.assertEqual(result.codes, ("UNRECOGNISED_CONSUMER_CONTRACT",))

    # --- the materialization gate ----------------------------------------

    def test_rendering_is_refused_while_the_contract_is_unproven(self) -> None:
        self.assertEqual(PROVEN_CONTRACTS, {}, "no contract may be assumed proven yet")
        base = self._index("base", [("a", "n", "1")])
        local = self._index("local", [("a", "n", "1")])
        remote = self._index("remote", [("a", "renamed", "2")])
        merged = merge_session_indexes(base, local, remote)
        self.assertEqual(merged.conflicts, ())
        with self.assertRaises(FailSafeError) as caught:
            render_session_index(merged)
        self.assertIn("not proven", str(caught.exception))

    def test_rendering_is_refused_on_a_conflict_even_once_proven(self) -> None:
        base = self._index("base", [("a", "base", "1")])
        local = self._index("local", [("a", "left", "2")])
        remote = self._index("remote", [("a", "right", "3")])
        merged = merge_session_indexes(base, local, remote)
        PROVEN_CONTRACTS[IndexContract.V1] = Reduction.LAST_LINE_WINS
        try:
            with self.assertRaises(FailSafeError):
                render_session_index(merged)
        finally:
            PROVEN_CONTRACTS.clear()

    def test_a_proven_contract_renders_records_verbatim(self) -> None:
        base = self._index("base", [("a", "n", "1")])
        local = self._index("local", [("a", "n", "1")])
        remote = self._index("remote", [("a", "renamed", "2")])
        merged = merge_session_indexes(base, local, remote)
        PROVEN_CONTRACTS[IndexContract.V1] = Reduction.LAST_LINE_WINS
        try:
            rendered = render_session_index(merged)
        finally:
            PROVEN_CONTRACTS.clear()
        reparsed = parse_session_index(self._write_bytes("out.jsonl", rendered))
        self.assertEqual(set(reparsed.reduced), {"a"})
        self.assertEqual(reparsed.reduced["a"].thread_name, "renamed")

    def _write_bytes(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path


if __name__ == "__main__":
    unittest.main()
