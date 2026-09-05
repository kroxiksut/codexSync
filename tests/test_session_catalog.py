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


class ResumedSessionTests(unittest.TestCase):
    """A second `session_meta` is a resume, and a resume is not damage.

    The runtime writes a fresh `session_meta` every time a session is picked up
    again. On the machine this was measured against, 54 of 252 files carry one
    -- up to 298 in a single file, 267 MiB of 864 MiB in total -- and in none of
    them does the id, cwd, timestamp, source, originator or parent differ from
    the first record. Treating that as invalid dropped every one of those
    sessions out of `valid`, so they were never mirrored and never listed by
    `chats`, with no error anywhere: the chat list showed 98 chats instead of
    152. What must still be refused is a file carrying two *different*
    identities, because which history it is cannot be decided.
    """

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"resumed-{uuid.uuid4().hex}"
        (self.root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name: str, metas: list[dict]) -> Path:
        path = self.root / "sessions" / name
        records: list[dict] = []
        for index, payload in enumerate(metas):
            if index:
                records.append({"type": "event", "payload": {"n": index}})
            records.append({"type": "session_meta", "payload": payload})
        path.write_bytes(
            b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in records)
        )
        return path

    def _only(self):
        catalog = scan_sessions(self.root)
        self.assertEqual(len(catalog.descriptors), 1)
        return catalog, catalog.descriptors[0]

    def test_a_resumed_session_stays_usable(self) -> None:
        self._write("a.jsonl", [
            {"id": "s1", "cwd": "C:/p", "timestamp": "2026-01-01T00:00:00Z"},
            {"id": "s1", "cwd": "C:/p", "timestamp": "2026-01-01T00:00:00Z", "memory_mode": "on"},
        ])
        catalog, item = self._only()
        self.assertEqual(item.state, SessionState.ACTIVE)
        self.assertIn("RESUMED_SESSION", item.codes)
        self.assertEqual(catalog.valid, [item], "a resumed session must reach every plan")

    def test_many_resumes_are_still_one_session(self) -> None:
        """298 was the real maximum in one file; the count itself proves nothing."""
        self._write("a.jsonl", [{"id": "s1"}] * 40)
        _, item = self._only()
        self.assertEqual(item.state, SessionState.ACTIVE)
        self.assertEqual(item.session_id, "s1")

    def test_two_identities_in_one_file_are_refused(self) -> None:
        self._write("a.jsonl", [{"id": "s1"}, {"id": "s2"}])
        catalog, item = self._only()
        self.assertEqual(item.state, SessionState.INVALID)
        self.assertIn("CONFLICTING_SESSION_META", item.codes)
        self.assertEqual(catalog.valid, [])

    def test_a_later_meta_without_an_id_is_refused(self) -> None:
        """Unable to prove it is the same session is not the same as proving it."""
        self._write("a.jsonl", [{"id": "s1"}, {"cwd": "C:/p"}])
        _, item = self._only()
        self.assertEqual(item.state, SessionState.INVALID)
        self.assertIn("CONFLICTING_SESSION_META", item.codes)

    def test_a_resume_after_an_unidentified_first_record_is_refused(self) -> None:
        self._write("a.jsonl", [{"cwd": "C:/p"}, {"id": "s1"}])
        _, item = self._only()
        self.assertEqual(item.state, SessionState.INVALID)
        self.assertIn("MISSING_SESSION_ID", item.codes)
        self.assertIn("CONFLICTING_SESSION_META", item.codes)


if __name__ == "__main__":
    unittest.main()
