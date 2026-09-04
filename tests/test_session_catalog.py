from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.session_catalog import SessionState, scan_sessions


class SessionCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"sessions-{uuid.uuid4().hex}"
        (self.root / "sessions" / "2026" / "09").mkdir(parents=True)
        (self.root / "archived_sessions").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative: str, session_id: str, *, parent: str | None = None, tail: bool = True) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "payload": {"id": session_id, "cwd": "C:/private/project"}}
        if parent:
            meta["payload"]["parent_thread_id"] = parent
        records = [meta, {"type": "event", "payload": {"secret": "must-not-leak"}}]
        encoded = b"\n".join(json.dumps(item).encode() for item in records) + (b"\n" if tail else b"")
        path.write_bytes(encoded)
        return path

    def test_recursive_identity_comes_from_first_record(self) -> None:
        self._write("sessions/2026/09/not-the-id.jsonl", "session-a")
        catalog = scan_sessions(self.root)
        self.assertEqual(len(catalog.descriptors), 1)
        item = catalog.descriptors[0]
        self.assertEqual(item.session_id, "session-a")
        self.assertEqual(item.state, SessionState.ACTIVE)
        self.assertIn("FILENAME_ID_MISMATCH", item.codes)
        self.assertNotIn("must-not-leak", repr(item))
        self.assertNotIn("C:/private", repr(item))

    def test_duplicate_id_is_preserved_as_branches(self) -> None:
        self._write("sessions/2026/09/a.jsonl", "same-id")
        self._write("archived_sessions/b.jsonl", "same-id")
        catalog = scan_sessions(self.root)
        self.assertIn("same-id", catalog.branches)
        self.assertTrue(all(item.state is SessionState.AMBIGUOUS for item in catalog.descriptors))

    def test_incomplete_tail_is_invalid_when_cold_and_diagnostic_when_volatile(self) -> None:
        self._write("sessions/a.jsonl", "a", tail=False)
        cold = scan_sessions(self.root, volatile=False).descriptors[0]
        live = scan_sessions(self.root, volatile=True).descriptors[0]
        self.assertEqual(cold.state, SessionState.INVALID)
        self.assertIn("INVALID_TAIL", cold.codes)
        self.assertIn("INCOMPLETE_TAIL", live.codes)

    def test_parent_graph_reports_missing_and_cycle(self) -> None:
        self._write("sessions/a.jsonl", "a", parent="missing")
        self._write("sessions/b.jsonl", "b", parent="c")
        self._write("sessions/c.jsonl", "c", parent="b")
        codes = scan_sessions(self.root).codes
        self.assertIn("MISSING_PARENT", codes)
        self.assertIn("PARENT_CYCLE", codes)


if __name__ == "__main__":
    unittest.main()
