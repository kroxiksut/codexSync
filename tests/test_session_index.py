from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import textwrap
import unittest
import uuid

from codexsync.app import audit_session_index
from codexsync.exceptions import FailSafeError
from codexsync.session_index import (
    PROVEN_CONTRACTS,
    SESSION_INDEX_FILE,
    IndexContract,
    Reduction,
    merge_session_indexes,
    parse_session_index,
    render_session_index,
)

INDEX_NEWLINE = b"\n"


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


class SessionIndexAuditTests(unittest.TestCase):
    """`sessions index`: what the two indexes hold, and where they disagree.

    Reading is always safe, so this is available while the consumer contract is
    still unproven -- and it says so, because a user who sees no index being
    written deserves the reason rather than silence.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"index-audit-{uuid.uuid4().hex}"
        self.local = self.root / "local-state"
        self.cloud = self.root / "cloud"
        self.local.mkdir(parents=True)
        self.cloud.mkdir(parents=True)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            textwrap.dedent(
                f"""
                [sync]
                mode = "cold"

                [paths]
                local_state_dir = "{self.local.as_posix()}"
                cloud_root_dir = "{self.cloud.as_posix()}"
                backup_dir = "{(self.root / 'backups').as_posix()}"
                temp_dir = "{(self.root / '.tmp').as_posix()}"

                [state]
                manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"

                [targets]
                include_roots = ["sessions"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _index(self, directory: Path, rows: list[dict]) -> None:
        (directory / SESSION_INDEX_FILE).write_bytes(
            b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + INDEX_NEWLINE for row in rows)
        )

    def test_an_absent_index_on_both_sides_is_reported_and_not_an_error(self) -> None:
        report = audit_session_index(self.config_path)
        self.assertFalse(report["local"]["present"])
        self.assertFalse(report["cloud"]["present"])
        self.assertEqual(report["differing"], 0)

    def test_a_repeated_id_counts_once_as_a_session(self) -> None:
        self._index(self.local, [
            {"id": "s1", "thread_name": "one", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": "s1", "thread_name": "renamed", "updated_at": "2026-01-02T00:00:00Z"},
        ])
        report = audit_session_index(self.config_path)
        self.assertEqual(report["local"]["records"], 2)
        self.assertEqual(report["local"]["sessions"], 1)

    def test_a_session_only_one_side_knows_is_counted_not_conflicted(self) -> None:
        self._index(self.local, [{"id": "s1", "thread_name": "a", "updated_at": "2026-01-01T00:00:00Z"}])
        self._index(self.cloud, [])
        report = audit_session_index(self.config_path)
        self.assertEqual(report["only_local"], 1)
        self.assertEqual(report["differing"], 0)
        self.assertNotIn("INDEX_CONFLICT", report["codes"])

    def test_a_divergent_rename_is_a_conflict_addressed_by_a_hashed_id(self) -> None:
        self._index(self.local, [{"id": "s1", "thread_name": "here", "updated_at": "2026-01-02T00:00:00Z"}])
        self._index(self.cloud, [{"id": "s1", "thread_name": "there", "updated_at": "2026-01-03T00:00:00Z"}])
        report = audit_session_index(self.config_path)
        self.assertEqual(report["differing"], 1)
        self.assertIn("INDEX_CONFLICT", report["codes"])
        self.assertEqual(
            report["differing_ids"],
            [hashlib.sha256(b"s1").hexdigest()],
            "a session id is user content and leaves only as a hash",
        )

    def test_no_thread_name_or_plain_session_id_reaches_the_report(self) -> None:
        self._index(self.local, [{"id": "s1", "thread_name": "a private title", "updated_at": "2026-01-02T00:00:00Z"}])
        self._index(self.cloud, [{"id": "s1", "thread_name": "another title", "updated_at": "2026-01-03T00:00:00Z"}])
        rendered = json.dumps(audit_session_index(self.config_path))
        self.assertNotIn("a private title", rendered)
        self.assertNotIn("another title", rendered)
        self.assertNotIn('"s1"', rendered)

    def test_the_unproven_contract_is_reported_as_the_standing_reason(self) -> None:
        self.assertEqual(PROVEN_CONTRACTS, {}, "no contract may be assumed proven")
        self._index(self.local, [{"id": "s1", "thread_name": "a", "updated_at": "2026-01-01T00:00:00Z"}])
        report = audit_session_index(self.config_path)
        self.assertFalse(report["contract_proven"])
        self.assertIn("UNPROVEN_CONSUMER_CONTRACT", report["codes"])


class EmptyIndexTests(unittest.TestCase):
    """A file that exists but holds nothing says what an absent one says."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"empty-index-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_empty_index_is_empty_and_not_an_unknown_contract(self) -> None:
        path = self.root / SESSION_INDEX_FILE
        path.write_bytes(b"")
        result = parse_session_index(path)
        self.assertEqual(result.codes, ("EMPTY_INDEX",))
        self.assertEqual(result.records, ())
        self.assertTrue(result.reductions_agree)

    def test_a_whitespace_only_index_is_treated_the_same(self) -> None:
        path = self.root / SESSION_INDEX_FILE
        path.write_bytes(b"\n\n")
        self.assertEqual(parse_session_index(path).codes, ("EMPTY_INDEX",))

if __name__ == "__main__":
    unittest.main()
