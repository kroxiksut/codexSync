"""Session transfer findings of the 2026-09-27 review, one regression each.

Every test here failed before its fix. What they share is the question of what
a plan may assume about a copy it cannot read cleanly: that it is absent (so a
copy may land on it), that its bytes mean what a parser says (so two records
collapse), or that a directory with the right name holds the right thing.
"""
from __future__ import annotations

import gzip
from dataclasses import replace
import json
import lzma
import os
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest import mock
import uuid
import zlib

from codexsync.exceptions import FailSafeError
from codexsync.jsonl_codec import JSONL_READ_ERRORS
from codexsync.semantic_merge import BranchRelation, canonical_digest, compare_session_branches
from codexsync.semantic_store import SemanticStore, session_hash_for
from codexsync.semantic_transfer import (
    COMPARISON_FAILED,
    DESTINATION_OCCUPIED,
    FORMAT_MIGRATION,
    MIRROR_PATH_KEPT,
    MOVES_BRANCH,
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    conflict_id_for,
    format_migration_resolutions,
)
from codexsync.session_catalog import SessionState, scan_sessions
from codexsync.sqlite_audit import PlacementStatus, read_thread_placements


SANDBOX = Path.cwd() / "test-sandbox"


def _session(path: Path, session_id: str, records: list[dict], *, tail: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "payload": {"id": session_id}}, *records]
    body = b"\n".join(json.dumps(line).encode("utf-8") for line in lines)
    path.write_bytes(body + (b"\n" if tail else b""))
    return path


class _Sides(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"review-{uuid.uuid4().hex[:10]}"
        self.local = self.root / "local"
        self.remote = self.root / "remote"
        self.local.mkdir(parents=True)
        self.remote.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def _plan(self, *, stream_pair: bool = False, **kwargs):
        local, remote = scan_sessions(self.local), scan_sessions(self.remote)
        if stream_pair:
            for catalog in (local, remote):
                catalog.descriptors = [replace(item, scan_line_limit=None) for item in catalog.descriptors]
        return build_transfer_plan(
            local, remote,
            local_root=self.local, remote_root=self.remote,
            source_machine="desktop", target_machine="laptop",
            **kwargs,
        )

    @staticmethod
    def _only(plan):
        (item,) = plan.items
        return item


class UnusableBranchTests(_Sides):
    """CS-298: a copy that cannot be read is not a copy that is absent."""

    def test_a_persisted_catalog_only_parses_the_history_that_changed(self) -> None:
        from codexsync.session_catalog import SessionParseCache, _scan_jsonl

        left = _session(self.local / "sessions" / "rollout-a.jsonl", "a", [{"r": 1}])
        _session(self.local / "sessions" / "rollout-b.jsonl", "b", [{"r": 1}])
        index = self.root / "local-cache.json"
        cache = SessionParseCache(index)
        before = scan_sessions(self.local, parse_cache=cache)
        cache.publish()
        warm = SessionParseCache(index)
        with mock.patch("codexsync.session_catalog._scan_jsonl", wraps=_scan_jsonl) as parse:
            self.assertEqual(before, scan_sessions(self.local, parse_cache=warm))
            parse.assert_not_called()

            info = left.stat()
            _session(left, "a", [{"r": 2}])
            os.utime(left, ns=(info.st_atime_ns, info.st_mtime_ns))
            fresh = scan_sessions(self.local, parse_cache=warm)
            parse.assert_called_once()
        self.assertEqual(fresh, scan_sessions(self.local))
        self.assertNotEqual(before.descriptors[0].sha256, fresh.descriptors[0].sha256)

    def test_cache_reconstructs_archive_pages_and_duplicates_from_current_paths(self) -> None:
        from codexsync.session_catalog import SessionParseCache, _scan_jsonl

        source = _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"ordinal": 1}])
        page = self.local / "sessions" / "rollout-s_page.jsonl"
        page.write_bytes(
            b'{"type":"session_meta","payload":{"id":"s","history_base":'
            b'{"thread_id":"s","end_ordinal_exclusive":2}}}\n{"ordinal":2}\n'
        )
        remote = self.remote / "sessions" / "rollout-s.jsonl.xz"
        remote.parent.mkdir()
        remote.write_bytes(lzma.compress(source.read_bytes()))
        index = self.root / "local-cache.json"
        cache = SessionParseCache(index)
        for root in (self.local, self.remote):
            self.assertEqual(scan_sessions(root, parse_cache=cache), scan_sessions(root))
        self.assertIn("s#page-2", {item.branch_key for item in scan_sessions(self.local).descriptors})
        cache.publish()
        cache = SessionParseCache(index)
        with mock.patch("codexsync.session_catalog._scan_jsonl", wraps=_scan_jsonl) as parse:
            self.assertEqual(scan_sessions(self.remote, parse_cache=cache), scan_sessions(self.remote))
            parse.assert_called_once()  # Only the explicitly uncached reference.

        archive = self.remote / "archived_sessions" / remote.name
        archive.parent.mkdir()
        remote.rename(archive)
        duplicate = self.local / "archived_sessions" / source.name
        duplicate.parent.mkdir()
        shutil.copyfile(source, duplicate)
        cfg = dict(local_root=self.local, remote_root=self.remote, source_machine="desktop", target_machine="laptop")
        cached = build_transfer_plan(
            scan_sessions(self.local, parse_cache=cache), scan_sessions(self.remote, parse_cache=cache), **cfg,
        )
        self.assertEqual(cached.plan_id, self._plan().plan_id)
        duplicate.unlink()
        cached = build_transfer_plan(
            scan_sessions(self.local, parse_cache=cache), scan_sessions(self.remote, parse_cache=cache), **cfg,
        )
        self.assertEqual(cached.plan_id, self._plan().plan_id)

    def test_cache_corruption_and_parser_version_changes_are_misses(self) -> None:
        from codexsync.session_catalog import SessionParseCache, _scan_jsonl

        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        index = self.root / "local-cache.json"
        cache = SessionParseCache(index)
        expected = scan_sessions(self.local, parse_cache=cache)
        cache.publish()
        with mock.patch("codexsync.session_catalog._scan_jsonl", wraps=_scan_jsonl) as parse:
            with mock.patch("codexsync.session_catalog._PARSE_CACHE_ABI", 999):
                self.assertEqual(expected, scan_sessions(self.local, parse_cache=SessionParseCache(index)))
            parse.assert_called_once()
            parse.reset_mock()
            limited = scan_sessions(self.local, max_line_bytes=8, parse_cache=SessionParseCache(index))
            self.assertIn("LINE_TOO_LARGE", limited.descriptors[0].codes)
            parse.assert_called_once()
            parse.reset_mock()
            saved = json.loads(index.read_text())
            next(iter(saved["data"]["entries"].values()))["fields"]["session_id"] = "wrong"
            index.write_text(json.dumps(saved))
            self.assertEqual(expected, scan_sessions(self.local, parse_cache=SessionParseCache(index)))
            parse.assert_called_once()

    def test_a_file_changed_while_its_cached_physical_hash_is_read_is_rejected(self) -> None:
        from codexsync.session_catalog import SessionParseCache, _PhysicalReader

        path = _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        cache = SessionParseCache(self.root / "local-cache.json")
        previous = scan_sessions(self.local, parse_cache=cache)
        finish = _PhysicalReader.finish
        changed = False

        def finish_then_change(reader):
            nonlocal changed
            digest = finish(reader)
            if not changed:
                changed = True
                old = path.stat()
                if os.name == "nt":
                    _session(path, "s", [{"r": 2}])
                else:
                    replacement = _session(path.with_suffix(".new"), "s", [{"r": 2}])
                    replacement.replace(path)
                os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
            return digest

        with mock.patch.object(_PhysicalReader, "finish", finish_then_change):
            (item,) = scan_sessions(self.local, parse_cache=cache).descriptors
        self.assertIs(item.state, SessionState.INVALID)
        self.assertIn("READ_CHANGED", item.codes)
        self.assertNotEqual(previous, scan_sessions(self.local, parse_cache=cache))

    def test_parallel_cache_scans_can_reload_without_changing_the_catalog(self) -> None:
        from codexsync.session_catalog import SessionParseCache

        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        index = self.root / "local-cache.json"
        reference = scan_sessions(self.local)
        cache = SessionParseCache(index)
        with (
            mock.patch("codexsync.session_catalog.SCAN_WORKERS", 2),
            mock.patch("codexsync.session_catalog.PROCESS_SCAN_MIN_BYTES", 0),
        ):
            self.assertEqual(reference, scan_sessions(self.local, parse_cache=cache))
            cache.publish()
            self.assertTrue(index.is_file())
            warm = SessionParseCache(index)
            self.assertEqual(reference, scan_sessions(self.local, parse_cache=warm))
            self.assertFalse(warm.dirty)

    def test_identical_histories_do_not_reopen_the_compressed_mirror(self) -> None:
        local = _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        remote = self.remote / "sessions" / "rollout-s.jsonl.gz"
        remote.parent.mkdir()
        remote.write_bytes(gzip.compress(local.read_bytes(), mtime=0))
        with mock.patch(
            "codexsync.semantic_transfer.compare_session_branches", wraps=compare_session_branches,
        ) as compare:
            identical = self._plan()
            self.assertIs(self._only(identical).relation, BranchRelation.IDENTICAL)
            compare.assert_not_called()
        self.assertEqual(identical.plan_id, self._plan(stream_pair=True).plan_id)

        archived = self.remote / "archived_sessions" / remote.name
        archived.parent.mkdir()
        remote.rename(archived)
        bases = {session_hash_for("s")}
        with mock.patch(
            "codexsync.semantic_transfer.compare_session_branches", wraps=compare_session_branches,
        ) as compare:
            missing_base = self._plan()
            self.assertIs(self._only(missing_base).relation, BranchRelation.MISSING_BASE)
            confirmed = self._plan(confirmed_bases=bases)
            self.assertIs(self._only(confirmed).relation, BranchRelation.ARCHIVE_TRANSITION)
            compare.assert_not_called()
        self.assertEqual(missing_base.plan_id, self._plan(stream_pair=True).plan_id)
        self.assertEqual(confirmed.plan_id, self._plan(stream_pair=True, confirmed_bases=bases).plan_id)

    def test_a_pair_changed_after_the_catalog_is_not_classified_for_writes(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        remote = _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": 2}])
        local_catalog, remote_catalog = scan_sessions(self.local), scan_sessions(self.remote)
        _session(remote, "s", [{"r": 1}, {"r": 2}])
        plan = build_transfer_plan(
            local_catalog, remote_catalog,
            local_root=self.local, remote_root=self.remote,
            source_machine="desktop", target_machine="laptop",
        )
        item = self._only(plan)
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn(COMPARISON_FAILED, item.codes)

    def test_rewriting_an_identical_branch_in_place_with_its_old_mtime_blocks_it(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        remote = _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        local_catalog, remote_catalog = scan_sessions(self.local), scan_sessions(self.remote)
        original = remote.stat()
        _session(remote, "s", [{"r": 2}])
        os.utime(remote, ns=(original.st_atime_ns, original.st_mtime_ns))
        current = remote.stat()
        self.assertEqual(
            (current.st_ino, current.st_size, current.st_mtime_ns),
            (original.st_ino, original.st_size, original.st_mtime_ns),
        )
        plan = build_transfer_plan(
            local_catalog, remote_catalog,
            local_root=self.local, remote_root=self.remote,
            source_machine="desktop", target_machine="laptop",
        )
        self.assertIs(self._only(plan).action, TransferAction.BLOCKED_INVALID_BRANCH)

    def test_identical_branches_are_streamed_when_the_change_stamp_is_unavailable(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        with (
            mock.patch("codexsync.session_catalog._change_stamp", return_value=None),
            mock.patch("codexsync.semantic_transfer.compare_session_branches", wraps=compare_session_branches) as compare,
        ):
            self.assertIs(self._only(self._plan()).relation, BranchRelation.IDENTICAL)
            compare.assert_called_once()

    def test_a_broken_mirror_copy_is_not_overwritten_unread(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": 2}])
        _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": "other"}], tail=False)
        item = self._only(self._plan())
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn("REMOTE_BRANCH_INVALID", item.codes)
        self.assertIn("INVALID_TAIL", item.codes)
        self.assertIsNone(item.conflict_id, "nothing here can be resolved by a recorded choice")

    def test_a_duplicate_id_is_a_plan_item_and_a_plan_code(self) -> None:
        _session(self.local / "sessions" / "a" / "rollout-s.jsonl", "s", [{"r": 1}])
        _session(self.local / "archived_sessions" / "rollout-s.jsonl", "s", [{"r": 2}])
        plan = self._plan()
        item = self._only(plan)
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn("LOCAL_DUPLICATE_SESSION_ID", item.codes)
        self.assertIn("LOCAL_DUPLICATE_SESSION_ID", plan.codes)
        self.assertEqual(plan.writable_items, ())

    def test_a_one_sided_copy_never_lands_on_an_existing_file(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        # Where the copy would go, a file whose id cannot be read.
        target = self.remote / "sessions" / "rollout-s.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'{"type":"event"}\n')
        plan = self._plan()
        item = self._only(plan)
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn(DESTINATION_OCCUPIED, item.codes)
        self.assertIn("REMOTE_BRANCH_WITHOUT_ID", plan.codes)

    def test_an_occupied_destination_in_another_container_counts(self) -> None:
        from codexsync.jsonl_codec import JsonlCodec

        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        stray = self.remote / "sessions" / "rollout-s.jsonl.xz"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"not xz at all")
        item = self._only(self._plan(mirror_codec=JsonlCodec.NONE))
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)

    def test_a_comparison_that_fails_is_not_a_resolvable_conflict(self) -> None:
        """Its hashes were empty, so a resolution pinned to them pinned nothing."""
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": "x" * 200}])
        _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": "y" * 200}])
        item = self._only(self._plan(max_line_bytes=64))
        self.assertIs(item.action, TransferAction.BLOCKED_INVALID_BRANCH)
        self.assertIn(COMPARISON_FAILED, item.codes)
        self.assertIsNone(item.conflict_id)
        self.assertTrue(item.local_sha256 and item.remote_sha256)

    def test_an_unusable_branch_does_not_stop_the_rest_of_an_apply(self) -> None:
        """It names no decision anyone can record, like a layout limit."""
        from codexsync.app import _UNRESOLVED_TRANSFER_BLOCKS

        self.assertNotIn(TransferAction.BLOCKED_INVALID_BRANCH, _UNRESOLVED_TRANSFER_BLOCKS)
        self.assertTrue(TransferAction.BLOCKED_INVALID_BRANCH.is_blocked)


class MirrorPathTests(_Sides):
    """CS-299: a resolved conflict is written where the mirror keeps the branch."""

    def _keep_local(self):
        conflict = self._only(self._plan())
        self.assertIs(conflict.action, TransferAction.BLOCKED_CONFLICT)
        resolution = BranchResolution(
            conflict.conflict_id, conflict.session_hash,
            conflict.local_sha256, conflict.remote_sha256, ResolutionChoice.KEEP_LOCAL,
        )
        return self._only(self._plan(resolutions={conflict.conflict_id: resolution}))

    def test_keep_local_rewrites_the_mirror_copy_in_place(self) -> None:
        _session(self.local / "sessions" / "a" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": "mine"}])
        _session(self.remote / "sessions" / "2026" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": "theirs"}])
        item = self._keep_local()
        self.assertIs(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(item.target_relative_path, "sessions/2026/rollout-s.jsonl")
        self.assertIn(MIRROR_PATH_KEPT, item.codes)

    def test_keep_local_moves_the_mirror_copy_when_the_archive_state_differs(self) -> None:
        # Archived here, active in the mirror: the kept branch carries its state
        # with it, so the mirror's file moves and the old one goes (D-023).
        _session(self.local / "archived_sessions" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": "mine"}])
        _session(self.remote / "sessions" / "2026" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": "theirs"}])
        item = self._keep_local()
        self.assertIs(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(item.target_relative_path, "archived_sessions/rollout-s.jsonl")
        self.assertIn(MOVES_BRANCH, item.codes)
        self.assertNotIn(MIRROR_PATH_KEPT, item.codes)

    def test_the_usual_path_carries_no_new_code(self) -> None:
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}, {"r": 2}])
        _session(self.remote / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        item = self._only(self._plan())
        self.assertIs(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(item.target_relative_path, "sessions/rollout-s.jsonl")
        self.assertNotIn(MIRROR_PATH_KEPT, item.codes)

    def test_a_missing_base_is_never_labelled_a_format_migration(self) -> None:
        # The local copy is a prefix of the mirror's, in an older format, and
        # the two disagree about active/archived: a missing base, not a rewrite.
        _session(self.local / "sessions" / "rollout-s.jsonl", "s", [{"r": 1}])
        _session(
            self.remote / "archived_sessions" / "rollout-s.jsonl", "s",
            [{"r": 1}, {"r": 2, "ordinal": 2}],
        )
        plan = self._plan()
        item = self._only(plan)
        self.assertIs(item.relation, BranchRelation.MISSING_BASE)
        self.assertNotIn(FORMAT_MIGRATION, item.codes)
        decided, held = format_migration_resolutions(plan)
        self.assertEqual((decided, held), ([], []))


class CanonicalDigestTests(_Sides):
    """CS-323: a repeated key is not the same record as its last value."""

    def test_a_repeated_key_has_no_canonical_form(self) -> None:
        self.assertIsNone(canonical_digest(b'{"k":1,"k":2}'))
        self.assertIsNone(canonical_digest(b'{"a":{"k":1,"k":2}}'))
        self.assertIsNotNone(canonical_digest(b'{"k":2}'))

    def test_two_such_records_are_not_identical(self) -> None:
        left = self.root / "left.jsonl"
        right = self.root / "right.jsonl"
        left.write_bytes(b'{"k":1,"k":2}\n')
        right.write_bytes(b'{"k":2}\n')
        comparison = compare_session_branches(left, right)
        self.assertIsNot(comparison.relation, BranchRelation.IDENTICAL)


class CatalogReadTests(_Sides):
    def test_a_damaged_gzip_mirror_copy_is_a_read_error(self) -> None:
        """CS-321: `zlib.error` is neither an OSError nor an EOFError."""
        self.assertIn(zlib.error, JSONL_READ_ERRORS)
        plain = b"".join(
            json.dumps(line).encode() + b"\n"
            for line in [{"type": "session_meta", "payload": {"id": "s"}}]
            + [{"r": index, "text": "abc" * index} for index in range(40)]
        )
        packed = gzip.compress(plain, mtime=0)
        path = self.remote / "sessions" / "rollout-s.jsonl.gz"
        path.parent.mkdir(parents=True)
        damaged = None
        for position in range(20, len(packed) - 8):
            candidate = bytearray(packed)
            candidate[position] ^= 0xFF
            try:
                gzip.decompress(bytes(candidate))
            except zlib.error:
                damaged = bytes(candidate)
                break
            except (OSError, EOFError):
                continue
        self.assertIsNotNone(damaged, "no byte flip produced a zlib.error")
        path.write_bytes(damaged)
        (descriptor,) = scan_sessions(self.remote).descriptors
        self.assertIs(descriptor.state, SessionState.INVALID)
        self.assertIn("READ_ERROR", descriptor.codes)

    def test_a_branch_that_vanishes_mid_scan_does_not_end_the_scan(self) -> None:
        """CS-325: `stat()` sat outside the guard, so one vanished file raised."""
        from codexsync import session_catalog

        kept = _session(self.local / "sessions" / "rollout-kept.jsonl", "kept", [{"r": 1}])
        gone = self.local / "sessions" / "rollout-gone.jsonl"
        real_walk = session_catalog._walk_jsonl

        def walk(directory, root):
            yield from real_walk(directory, root)
            if directory.name == "sessions":
                yield gone

        with mock.patch.object(session_catalog, "_walk_jsonl", walk):
            catalog = scan_sessions(self.local)
        by_path = {item.relative_path: item for item in catalog.descriptors}
        self.assertIs(by_path["sessions/rollout-kept.jsonl"].state, SessionState.ACTIVE)
        self.assertIs(by_path["sessions/rollout-gone.jsonl"].state, SessionState.INVALID)
        self.assertIn("READ_ERROR", by_path["sessions/rollout-gone.jsonl"].codes)
        self.assertTrue(kept.is_file())


class ThreadCatalogueTests(_Sides):
    """CS-322: a catalogue that cannot be read is not a catalogue that is absent."""

    def test_an_unreadable_database_is_indeterminate(self) -> None:
        (self.local / "state_5.sqlite").write_bytes(b"this is not a database at all" * 10)
        placements = read_thread_placements(self.local)
        self.assertIs(placements.status, PlacementStatus.INDETERMINATE)

    def test_a_folder_named_with_uri_characters_is_read(self) -> None:
        state = self.root / "odd#name%20here"
        state.mkdir()
        database = state / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, 0)",
            ("s", str(state / "sessions" / "rollout-s.jsonl")),
        )
        connection.commit()
        connection.close()
        before = sorted(path.name for path in state.iterdir())
        placements = read_thread_placements(state)
        self.assertEqual(sorted(path.name for path in state.iterdir()), before)
        self.assertIs(placements.status, PlacementStatus.AVAILABLE)
        self.assertEqual(placements.placement_of("s"), "sessions/rollout-s.jsonl")


class ConflictBundleTests(_Sides):
    """CS-325: a bundle is named by, and holds, the conflict that was decided."""

    def setUp(self) -> None:
        super().setUp()
        self.left = self.root / "left.jsonl"
        self.right = self.root / "right.jsonl"
        self.left.write_bytes(b'{"a":1}\n')
        self.right.write_bytes(b'{"a":2}\n')
        self.store = SemanticStore(self.root / "store", "machine-a")
        import hashlib

        self.left_sha = hashlib.sha256(self.left.read_bytes()).hexdigest()
        self.right_sha = hashlib.sha256(self.right.read_bytes()).hexdigest()

    def _bundle(self, **kwargs):
        return self.store.conflict_bundle(
            self.left, self.right, session_id="s", common_records=0, **kwargs
        )

    def test_the_bundle_is_named_by_the_plan_conflict_id(self) -> None:
        bundle = self._bundle()
        self.assertEqual(bundle.name, conflict_id_for(session_hash_for("s"), self.left_sha, self.right_sha))

    def test_a_bundle_written_under_the_old_name_is_still_found(self) -> None:
        import hashlib

        legacy = self.root / "store" / "conflicts" / hashlib.sha256(
            f"s\0{self.left_sha}\0{self.right_sha}".encode()
        ).hexdigest()
        legacy.mkdir(parents=True)
        shutil.copyfile(self.left, legacy / "left.jsonl")
        shutil.copyfile(self.right, legacy / "right.jsonl")
        (legacy / "COMMITTED").write_text("{}", encoding="utf-8")
        self.assertEqual(self._bundle(), legacy)
        self.assertEqual(sorted(p.name for p in legacy.parent.iterdir()), [legacy.name])

    def test_an_uncommitted_directory_is_not_taken_for_a_bundle(self) -> None:
        name = conflict_id_for(session_hash_for("s"), self.left_sha, self.right_sha)
        half = self.root / "store" / "conflicts" / name
        half.mkdir(parents=True)
        (half / "left.jsonl").write_bytes(b"torn")
        bundle = self._bundle()
        self.assertEqual(bundle, half)
        self.assertTrue((bundle / "COMMITTED").is_file())
        self.assertEqual((bundle / "left.jsonl").read_bytes(), self.left.read_bytes())

    def test_the_copies_are_verified_before_the_commit(self) -> None:
        from codexsync import semantic_store

        def torn(source, destination):
            destination.write_bytes(b"torn\n")

        with mock.patch.object(semantic_store, "_copy_logical", torn):
            with self.assertRaises(FailSafeError):
                self._bundle()
        self.assertFalse((self.root / "store" / "conflicts").exists())

    def test_a_branch_other_than_the_decided_one_is_refused(self) -> None:
        with self.assertRaises(FailSafeError):
            self._bundle(expected=(self.left_sha, "0" * 64))


if __name__ == "__main__":
    unittest.main()


class HeldBackResolutionTests(unittest.TestCase):
    """CS-496: a resolution the apply does not carry out leaves no bundle."""

    def test_a_resolved_conflict_that_writes_nothing_is_not_bundled(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        import codexsync.app as app
        from codexsync.semantic_transfer import RESOLVED_BY_RULE, TransferAction

        held = [
            SimpleNamespace(
                action=action, codes=(RESOLVED_BY_RULE, code), session_hash=f"h{index}",
                local_sha256="l", remote_sha256="r",
            )
            for index, (action, code) in enumerate((
                (TransferAction.BLOCKED_UNPROVEN_LAYOUT, "IN_PLACE_PROJECTED_REWRITE"),
                (TransferAction.BLOCKED_UNPROVEN_LAYOUT, "IN_PLACE_HISTORY_UNKNOWN"),
                (TransferAction.OUT_OF_SCOPE, "WOULD_BE_FAST_FORWARD_LOCAL"),
                (TransferAction.HELD_BY_DIRECTION, "WOULD_BE_FAST_FORWARD_REMOTE"),
                (TransferAction.BLOCKED_INVALID_BRANCH, "PAGE_OVERLAP"),
            ))
        ]
        plan = SimpleNamespace(items=held, plan_id="plan")
        with patch.object(app, "SemanticStore") as store:
            bundles = app._bundle_resolved_conflicts(
                SimpleNamespace(), plan, Path("local"), Path("cloud"), {}, {},
            )
        self.assertEqual(bundles, [])
        store.assert_not_called()
