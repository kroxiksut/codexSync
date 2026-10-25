"""CS-468: Codex's SQLite catalogues are never copied by plain `sync`."""

from __future__ import annotations

import unittest

from codexsync.runtime import _is_semantic_owned


class SemanticOwnedCatalogueTests(unittest.TestCase):
    def test_thread_history_store_is_semantic_owned(self) -> None:
        for suffix in (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal"):
            self.assertTrue(_is_semantic_owned(f"thread_history_1{suffix}"), suffix)
            self.assertTrue(_is_semantic_owned(f"state_5{suffix}"), suffix)

    def test_an_unrelated_file_is_not(self) -> None:
        self.assertFalse(_is_semantic_owned("thread_history_notes.md"))
        self.assertFalse(_is_semantic_owned("skills/thread_history.txt"))


if __name__ == "__main__":
    unittest.main()
