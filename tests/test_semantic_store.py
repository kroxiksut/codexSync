"""The semantic manifest, and the bundles that keep divergent branches.

The manifest is metadata by design: it says what a branch was at a moment two
machines agreed, so a later ancestry decision has something to rest on, without
either side keeping a second copy of the history. The tests below hold that line
— an entry carries no payload, verifies itself, and is refused rather than
repaired when it does not.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.semantic_store import (
    SEMANTIC_MANIFEST_FORMAT,
    SemanticStore,
    branch_id_for,
    session_hash_for,
)


class SemanticManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"semantic-store-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.store_root = self.root / "store"
        self.store = SemanticStore(self.store_root, "machine-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _record(self, store=None, *, session="private-session", sha="a" * 64,
                records=3, state="ACTIVE", agreed=True, parent=None):
        return (store or self.store).record(
            session, state=state, sha256=sha, record_count=records,
            byte_count=records * 10, parent_id=parent, agreed=agreed,
        )

    def _entry_file(self, machine="machine-a", session="private-session") -> Path:
        return self.store_root / "manifest" / machine / f"{session_hash_for(session)}.json"

    # --- what an entry is -------------------------------------------------

    def test_an_entry_records_the_branch_and_never_its_payload(self) -> None:
        entry = self._record()
        raw = json.loads(self._entry_file().read_text(encoding="utf-8"))
        self.assertEqual(raw["format"], SEMANTIC_MANIFEST_FORMAT)
        self.assertEqual(entry.branch_id, branch_id_for(entry.session_hash, "a" * 64))
        self.assertEqual(raw["record_count"], 3)
        self.assertNotIn(
            "private-session", self._entry_file().read_text(encoding="utf-8"),
            "the session id is hashed, never written",
        )
        self.assertEqual([p.suffix for p in (self.store_root / "manifest").rglob("*") if p.is_file()], [".json"])

    def test_an_agreement_becomes_the_base_and_a_bare_record_does_not(self) -> None:
        noted = self._record(session="s-unagreed", agreed=False)
        self.assertFalse(noted.has_base)
        agreed = self._record(session="s-agreed", agreed=True)
        self.assertTrue(agreed.has_base)
        self.assertEqual(self.store.confirmed_bases(), {agreed.session_hash})

    def test_a_base_survives_a_later_record_that_is_not_an_agreement(self) -> None:
        """The branch moved on; the history both sides once shared did not."""
        first = self._record(sha="a" * 64, agreed=True)
        later = self._record(sha="b" * 64, records=5, agreed=False)
        self.assertEqual(later.base_sha256, first.sha256)
        self.assertEqual(later.sha256, "b" * 64)

    # --- generations -------------------------------------------------------

    def test_the_generation_advances_only_when_something_changed(self) -> None:
        self.assertEqual(self._record().generation, 1)
        self.assertEqual(self._record().generation, 1, "the same content is the same entry")
        self.assertEqual(self._record(sha="c" * 64).generation, 2)

    def test_an_unchanged_record_does_not_rewrite_the_file(self) -> None:
        """A cloud client should not have to sync a file that says nothing new."""
        self._record()
        before = self._entry_file().read_bytes()
        self._record()
        self.assertEqual(self._entry_file().read_bytes(), before)

    # --- self-verification -------------------------------------------------

    def test_a_tampered_entry_is_dropped_rather_than_trusted(self) -> None:
        self._record()
        raw = json.loads(self._entry_file().read_text(encoding="utf-8"))
        raw["sha256"] = "f" * 64
        self._entry_file().write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.store.confirmed_bases(), set())

    def test_a_truncated_entry_is_dropped(self) -> None:
        self._record()
        self._entry_file().write_text('{"format": "codexsync-sem', encoding="utf-8")
        self.assertEqual(self.store.confirmed_bases(), set())

    def test_an_entry_moved_under_another_name_is_not_that_session(self) -> None:
        """The file's own place is part of its identity."""
        self._record()
        moved = self._entry_file().with_name(f"{'d' * 64}.json")
        shutil.copyfile(self._entry_file(), moved)
        self._entry_file().unlink()
        self.assertEqual(self.store.confirmed_bases(), set())

    def test_an_entry_copied_into_another_machines_directory_is_refused(self) -> None:
        self._record()
        other = self.store_root / "manifest" / "machine-b"
        other.mkdir(parents=True)
        shutil.copyfile(self._entry_file(), other / self._entry_file().name)
        self._entry_file().unlink()
        self.assertEqual(
            self.store.confirmed_bases(), set(),
            "an entry claims the machine that wrote it, not the folder it sits in",
        )

    # --- across machines ---------------------------------------------------

    def test_a_base_recorded_by_another_machine_is_accepted(self) -> None:
        peer = SemanticStore(self.store_root, "machine-b")
        entry = self._record(store=peer, session="shared-session")
        self.assertEqual(self.store.confirmed_bases(), {entry.session_hash})
        self.assertEqual(len(self.store.entries(entry.session_hash)), 1)

    def test_both_machines_entries_are_read_for_one_session(self) -> None:
        peer = SemanticStore(self.store_root, "machine-b")
        self._record(session="shared", sha="a" * 64)
        self._record(store=peer, session="shared", sha="b" * 64)
        entries = self.store.entries(session_hash_for("shared"))
        self.assertEqual({item.machine_id for item in entries}, {"machine-a", "machine-b"})

    def test_a_peer_entry_that_went_backwards_is_ignored(self) -> None:
        """A cloud client restoring an old copy must not undo what we saw."""
        peer = SemanticStore(self.store_root, "machine-b")
        self._record(store=peer, session="shared", sha="a" * 64)
        self._record(store=peer, session="shared", sha="b" * 64)
        peer_file = self._entry_file("machine-b", "shared")
        current = peer_file.read_bytes()

        # Our own entry records the peer generation we have seen.
        self._record(session="shared", sha="c" * 64)
        self.assertEqual(self.store.own_entry(session_hash_for("shared")).peers, {"machine-b": 2})

        rolled_back = SemanticStore(self.store_root, "machine-b")
        peer_file.unlink()
        rolled_back.record(
            "shared", state="ACTIVE", sha256="a" * 64, record_count=3, byte_count=30, agreed=True
        )
        self.assertEqual(json.loads(peer_file.read_text(encoding="utf-8"))["generation"], 1)
        self.assertNotEqual(peer_file.read_bytes(), current)

        machines = {item.machine_id for item in self.store.entries(session_hash_for("shared"))}
        self.assertEqual(machines, {"machine-a"}, "the regressed peer entry is not accepted")

    def test_a_store_needs_a_stable_machine_id(self) -> None:
        with self.assertRaises(ValueError):
            SemanticStore(self.store_root, "unknown-machine")


class ConflictBundleTests(unittest.TestCase):
    """The one place a full payload is kept, because it is the only copy left."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"semantic-bundle-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.left = self.root / "left.jsonl"
        self.right = self.root / "right.jsonl"
        self.left.write_bytes(b'{"a":1}\n')
        self.right.write_bytes(b'{"a":2}\n')
        self.store = SemanticStore(self.root / "store", "machine-a")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_conflict_bundle_preserves_both_raw_branches(self) -> None:
        bundle = self.store.conflict_bundle(self.left, self.right, session_id="s", common_records=0)
        self.assertEqual(bundle.joinpath("left.jsonl").read_bytes(), self.left.read_bytes())
        self.assertEqual(bundle.joinpath("right.jsonl").read_bytes(), self.right.read_bytes())
        self.assertTrue(bundle.joinpath("COMMITTED").is_file())

    def test_the_same_conflict_is_bundled_once(self) -> None:
        first = self.store.conflict_bundle(self.left, self.right, session_id="s", common_records=0)
        second = self.store.conflict_bundle(self.left, self.right, session_id="s", common_records=0)
        self.assertEqual(first, second)

    def test_the_bundle_manifest_names_no_session_id(self) -> None:
        bundle = self.store.conflict_bundle(self.left, self.right, session_id="s", common_records=0)
        self.assertNotIn(
            '"s"', bundle.joinpath("manifest.json").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
