"""A long read says how far it has got, and never fails because of saying it.

Thirteen seconds of hashing with nothing on screen is what this is for, so the
properties worth holding are the ones a progress bar is judged by: the total is
the real number of files, the count only goes up, and the last report is the
total rather than one short of it. The fourth is the important one -- a
callback that raises must not lose the catalogue the caller actually asked for.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.chat_directory import build_chat_directory
from codexsync.progress import PHASES, report
from codexsync.session_catalog import scan_sessions

STATE = {
    "local-projects": {
        "p1": {"name": "One", "rootPaths": ["C:/work/one"], "projectKind": "local"},
    },
    "project-order": ["p1"],
    "thread-project-assignments": {},
}


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"progress-{uuid.uuid4().hex[:8]}"
        (self.root / "sessions" / "2026" / "09").mkdir(parents=True)
        (self.root / "archived_sessions").mkdir()
        for index in range(5):
            self._write(f"sessions/2026/09/s{index}.jsonl", f"session-{index}")
        self._write("archived_sessions/old.jsonl", "session-old")
        self.reports: list[tuple[str, int, int]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, relative: str, session_id: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"id": session_id, "cwd": "C:/work/one"}},
            {"type": "event", "payload": {"n": 1}},
        ]
        path.write_bytes(b"\n".join(json.dumps(item).encode() for item in records) + b"\n")

    def _collect(self, phase: str, done: int, total: int) -> None:
        self.reports.append((phase, done, total))

    def test_the_total_is_the_number_of_files_and_the_count_reaches_it(self) -> None:
        catalog = scan_sessions(self.root, progress=self._collect)
        self.assertEqual(len(catalog.descriptors), 6)
        totals = {total for _phase, _done, total in self.reports}
        self.assertEqual(totals, {6}, "the total is known before the hashing starts")
        self.assertEqual(self.reports[0][1], 0)
        self.assertEqual(self.reports[-1][1], 6)

    def test_the_count_never_goes_backwards(self) -> None:
        scan_sessions(self.root, progress=self._collect)
        counts = [done for _phase, done, _total in self.reports]
        self.assertEqual(counts, sorted(counts))

    def test_a_callback_that_raises_does_not_lose_the_catalogue(self) -> None:
        def broken(_phase: str, _done: int, _total: int) -> None:
            raise RuntimeError("the indicator is not the work")

        catalog = scan_sessions(self.root, progress=broken)
        self.assertEqual(len(catalog.descriptors), 6)

    def test_the_phase_can_be_named_by_the_caller(self) -> None:
        """Two catalogues are read for a transfer plan; they are told apart."""
        scan_sessions(self.root, progress=self._collect, phase="sessions_cloud")
        self.assertEqual({phase for phase, _d, _t in self.reports}, {"sessions_cloud"})

    def test_reading_the_chats_reports_its_own_second_pass(self) -> None:
        build_chat_directory(
            self.root, json.dumps(STATE).encode("utf-8"), progress=self._collect,
        )
        phases = [phase for phase, _done, _total in self.reports]
        self.assertEqual(set(phases), {"sessions", "chats"})
        self.assertLess(phases.index("sessions"), phases.index("chats"), "hashing comes first")
        chats = [(done, total) for phase, done, total in self.reports if phase == "chats"]
        self.assertEqual(chats[-1], (6, 6))

    def test_no_callback_is_the_normal_case_and_costs_nothing(self) -> None:
        self.assertIsNone(report(None, "sessions", 1, 2))
        self.assertEqual(len(scan_sessions(self.root).descriptors), 6)

    def test_every_phase_a_scan_reports_is_declared(self) -> None:
        """The catalogue of labels is generated from this list; a stray id has no label."""
        scan_sessions(self.root, progress=self._collect)
        build_chat_directory(self.root, json.dumps(STATE).encode("utf-8"), progress=self._collect)
        self.assertTrue({phase for phase, _d, _t in self.reports} <= set(PHASES))


if __name__ == "__main__":
    unittest.main()
