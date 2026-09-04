from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.exceptions import FailSafeError
from codexsync.mutation_journal import JournalState, JournalStore


class MutationJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"journal-{uuid.uuid4().hex}"
        self.store = JournalStore(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_nonterminal_operation_blocks_next_begin(self) -> None:
        first = self.store.begin("sync", "a" * 64, 2)
        with self.assertRaises(FailSafeError):
            self.store.begin("restore", "b" * 64, 1)
        first = self.store.transition(first, JournalState.FAILED)
        self.assertEqual(self.store.begin("restore", "b" * 64, 1).family, "restore")

    def test_commit_transition_sequence_round_trips(self) -> None:
        journal = self.store.begin("sync", "a" * 64, 1)
        journal = self.store.transition(journal, JournalState.BACKED_UP)
        journal = self.store.transition(journal, JournalState.COMMITTING)
        journal = self.store.transition(journal, JournalState.COMMITTED)
        self.assertEqual(self.store.load(journal.operation_id), journal)
        self.assertEqual(self.store.non_terminal(), [])

    def test_invalid_transition_is_rejected(self) -> None:
        journal = self.store.begin("sync", "a" * 64, 1)
        with self.assertRaises(FailSafeError):
            self.store.transition(journal, JournalState.COMMITTED)


if __name__ == "__main__":
    unittest.main()
