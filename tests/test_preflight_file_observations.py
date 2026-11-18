"""One diagnostic must not mix config or session files from two moments."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import gzip
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import ProcessSnapshot
from codexsync.issue_report import build_issue_report
from codexsync.preflight import _check_config_compat, run_preflight
from codexsync import session_catalog

try:
    from tests.test_preflight import _write_config
except ImportError:
    from test_preflight import _write_config


class PreflightFileObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"preflight-files-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = _write_config(self.root)
        self.local = self.root / "local-state"
        self.cloud = self.root / "cloud"
        self.stopped = ProcessSnapshot(main_processes=[], subprocesses=[], sandbox_detected=False)

    def _branch(self, root: Path, *, packed: bool = False, payload: bytes | None = None) -> Path:
        name = "rollout-2026-11-17T00-00-00-11111111-1111-1111-1111-111111111111.jsonl"
        path = root / "sessions" / (name + (".gz" if packed else ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None:
            payload = (json.dumps({"type": "session_meta", "payload": {
                "id": "11111111-1111-1111-1111-111111111111", "cwd": "C:/project",
            }}) + "\n" + json.dumps({"ordinal": 1, "type": "event_msg"}) + "\n").encode()
        path.write_bytes(gzip.compress(payload) if packed else payload)
        return path

    def test_config_replaced_after_load_cannot_change_compatibility_in_the_same_report(self) -> None:
        expected = _check_config_compat(self.config)
        original_read = Path.read_bytes
        reads = []

        def moving_source(path):
            if path == self.config:
                reads.append(path)
                if len(reads) > 1:
                    return b"[this is a partial cloud replacement"
            return original_read(path)

        facts = {}
        with patch.object(Path, "read_bytes", moving_source), patch(
            "codexsync.runtime.collect_process_snapshot", return_value=self.stopped,
        ):
            report = run_preflight(self.config, facts=facts)
        actual = next(item for item in report.checks if item.name == "config_compat")
        self.assertEqual(actual, expected, "load and compatibility must inspect the same source bytes")
        self.assertEqual(actual.parts, expected.parts)
        self.assertEqual(len(reads), 1)
        self.assertEqual(facts["observations"].config.payload, original_read(self.config))

    def test_issue_report_reuses_one_tree_walk_and_decode_including_compressed_mirror(self) -> None:
        local = self._branch(self.local)
        mirror = self._branch(self.cloud, packed=True)
        # Startup counts even a rollout in an unfamiliar container. Its disk
        # bytes must not be confused with decompressed catalog byte_count.
        extra = local.with_name(local.name + ".pending")
        extra.write_bytes(b"not a readable container")
        reads, walks = Counter(), Counter()
        original_open, original_walk = session_catalog.open_jsonl, session_catalog.os.walk

        @contextmanager
        def opening(path):
            reads[path] += 1
            if path == local and reads[path] > 1:
                # Codex rewrites the first record between the scan and peek.
                yield io.BytesIO(b'{"ordinal":0}\n')
            else:
                with original_open(path) as handle:
                    yield handle

        def walking(path, *args, **kwargs):
            walks[Path(path)] += 1
            return original_walk(path, *args, **kwargs)

        with patch("codexsync.session_catalog.open_jsonl", opening), patch(
            "codexsync.session_catalog.os.walk", walking,
        ), patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped), patch(
            "codexsync.issue_report._process", return_value={},
        ):
            report = build_issue_report(self.config)
        self.assertEqual(reads[local], 1, "format diagnostics must reuse the catalog's first decoded record")
        self.assertEqual(reads[mirror], 1, "doctor --report must reuse the observed mirror formats")
        self.assertEqual(walks[self.local / "sessions"], 1)
        self.assertEqual(walks[self.cloud / "sessions"], 1)
        self.assertEqual(report["sessions"]["record_formats"], {"mixed": 1})
        self.assertEqual(report["sessions"]["mirror_record_formats"], {"legacy": 1})
        self.assertEqual(report["checks"]["session_format"], "PASS")

    def test_a_supplied_missing_index_and_formats_never_retry_after_files_arrive(self) -> None:
        from codexsync.preflight import _check_session_format, _check_session_index

        facts = {}
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped):
            before = run_preflight(self.config, facts=facts)
        observations = facts["observations"]
        self._branch(self.local)
        (self.local / "session_index.jsonl").write_bytes(b"partial")
        with patch("codexsync.session_catalog.open_jsonl", side_effect=AssertionError("fallback decode")), patch(
            "codexsync.session_index.Path.read_bytes", side_effect=AssertionError("fallback index read"),
        ):
            formats = _check_session_format(self.local, self.cloud, 1024,
                                            sessions=observations.sessions, cloud_formats=observations.cloud_formats)
            index = _check_session_index(self.local, index=observations.session_index)
        previous = {item.name: item for item in before.checks}
        self.assertEqual(formats, previous["session_format"])
        self.assertEqual(index, previous["session_index"])

    def test_nested_observation_data_cannot_be_changed_by_a_later_check(self) -> None:
        self._branch(self.local)
        index = self.local / "session_index.jsonl"
        index.write_text('{"id":"chat","thread_name":"name","updated_at":"2026-11-17T00:00:00Z"}\n', encoding="utf-8")
        facts = {}
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped):
            run_preflight(self.config, facts=facts)
        observed = facts["observations"]
        for value, key, replacement in (
            (observed.sessions.formats, "new", "ordinal"),
            (observed.sessions.catalog.branches, "new", None),
            (observed.cloud_formats.formats, "new", "legacy"),
            (observed.session_index.result.reduced, "chat", None),
            (observed.session_index.result.reduced_by_updated_at, "chat", None),
        ):
            with self.subTest(mapping=type(value)), self.assertRaises(TypeError):
                value[key] = replacement
        for value, name, replacement in (
            (observed.config, "text", "changed"),
            (observed.sessions.catalog, "descriptors", ()),
            (observed.sessions.catalog.descriptors[0], "record_format", "ordinal"),
            (observed.sessions.files, "size", 0),
            (observed.session_index.result.records[0], "thread_name", "changed"),
        ):
            with self.subTest(attribute=name), self.assertRaises(FrozenInstanceError):
                setattr(value, name, replacement)
        for source in (observed.config, observed.sessions, observed.cloud_formats, observed.session_index):
            self.assertIsNotNone(source.boundary.started_at_utc.tzinfo)
            self.assertEqual(source.boundary.started_at_utc.utcoffset().total_seconds(), 0)
            self.assertLessEqual(source.boundary.started_at_utc, source.boundary.completed_at_utc)

    def test_stored_file_sizes_and_first_record_formats_keep_the_previous_diagnostics(self) -> None:
        from codexsync.codex_health import measure_chat_files
        from codexsync.preflight import _check_session_format, _check_session_index
        from codexsync.preflight_observations import observe_formats, observe_sessions, observe_session_index

        payloads = (
            b'{"ordinal":0}\n{"type":"event_msg"}\n',
            b'{"ordinal":true}\n', b'{"ordinal":0.0}\n', b'[]\n',
            b'\xef\xbb\xbf{"ordinal":0}\n', b'\n', b'{partial', b'',
            b'{"ordinal":1}' + b' ' * 32 + b'\n',
        )
        for number, payload in enumerate(payloads):
            local = self.local / "sessions" / f"example-{number}.jsonl"
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(payload)
        packed = self._branch(self.local, packed=True)
        packed.with_name(packed.name + ".unfinished-copy").write_bytes(b"partial")
        # A named rollout outside known branch extensions is counted at startup
        # too, but must never be opened or decoded by the catalog.
        packed.with_suffix(".notes").write_bytes(b"private notes")
        sessions = observe_sessions(self.local, 32)
        self.assertEqual(dict(sessions.formats), session_catalog.peek_record_formats(self.local, max_line_bytes=32))
        self.assertEqual(sessions.files, measure_chat_files(self.local))
        before = _check_session_format(self.local, self.cloud, 32)
        observed = _check_session_format(self.local, self.cloud, 32, sessions=sessions,
                                         cloud_formats=observe_formats(self.cloud, 32))
        self.assertEqual(observed, before)
        self.assertEqual(observed.parts, before.parts)
        for payload in (b"", b"partial", b'[]\n', b'{"id":"chat"}\n'):
            index = self.local / "session_index.jsonl"
            index.write_bytes(payload)
            expected = _check_session_index(self.local)
            captured = _check_session_index(self.local, index=observe_session_index(index))
            self.assertEqual(captured, expected)
            self.assertEqual(captured.parts, expected.parts)

    def test_unreadable_sources_remain_unreadable_when_a_later_check_could_read_them(self) -> None:
        from codexsync.config import load_config
        from codexsync.preflight import _check_codex_startup, _check_session_format, _check_session_index
        from codexsync.preflight_observations import (
            observe_config_source, observe_formats, observe_sessions, observe_session_index,
        )
        from codexsync.safety_gate import ProcessState

        cfg = load_config(self.config)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("file locked")):
            config = observe_config_source(self.config)
            index = observe_session_index(self.local / "session_index.jsonl")
        with patch("codexsync.preflight_observations.scan_sessions", side_effect=PermissionError("tree locked")):
            sessions = observe_sessions(self.local, 1024)
        with patch("codexsync.preflight_observations.peek_record_formats", side_effect=PermissionError("mirror locked")):
            cloud = observe_formats(self.cloud, 1024)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("fallback source read")), patch(
            "codexsync.preflight.read_backfill_state", side_effect=AssertionError("fallback SQLite read"),
        ), patch("codexsync.session_catalog.open_jsonl", side_effect=AssertionError("fallback session read")):
            checks = (
                _check_config_compat(self.config, config=config),
                _check_session_index(self.local, index=index),
                _check_session_format(self.local, self.cloud, 1024, sessions=sessions, cloud_formats=cloud),
                _check_codex_startup(cfg, self.local, process=ProcessState.STOPPED, sessions=sessions),
            )
        self.assertEqual([check.status for check in checks], ["WARN"] * 4)
        self.assertIn("file locked", checks[0].details)
        self.assertIn("tree locked", checks[-1].details)

    def test_report_never_changes_bytes_mtimes_or_creates_missing_runtime_folders(self) -> None:
        self._branch(self.local)
        self._branch(self.cloud, packed=True)
        (self.local / "session_index.jsonl").write_bytes(b"partial")
        self.config.write_bytes(b"\xef\xbb\xbf" + self.config.read_bytes())
        for name in ("backups", ".tmp", "state"):
            (self.root / name).rmdir()

        def snapshot():
            return {path.relative_to(self.root).as_posix(): (
                path.stat().st_mtime_ns, None if path.is_dir() else path.read_bytes(),
            ) for path in self.root.rglob("*")}

        before = snapshot()
        with patch("codexsync.runtime.collect_process_snapshot", return_value=self.stopped), patch(
            "codexsync.issue_report._process", return_value={},
        ):
            first = build_issue_report(self.config)
            second = build_issue_report(self.config)
        self.assertEqual(first, second)
        self.assertEqual(snapshot(), before)

    def test_decode_failure_after_enumeration_keeps_startup_counts_and_status(self) -> None:
        """A bad history cannot invalidate a completed, independent file measurement."""
        from codexsync.config import load_config
        from codexsync.preflight import _check_codex_startup
        from codexsync.preflight_observations import observe_sessions
        from codexsync.safety_gate import ProcessState
        from codexsync.sqlite_audit import BackfillReading, PlacementStatus

        local = self._branch(self.local)
        local.with_suffix(".notes").write_bytes(b"a rollout name still counts")
        cfg = load_config(self.config)
        with patch("codexsync.session_catalog._scan_jsonl", side_effect=RuntimeError("decoder failed")):
            observed = observe_sessions(self.local, 1024)
        self.assertIsNone(observed.catalog)
        self.assertEqual(observed.files.count, 2)
        for status in ("complete", "pending"):
            reading = BackfillReading(PlacementStatus.AVAILABLE, "state_5.sqlite", status)
            with self.subTest(backfill=status), patch("codexsync.preflight.read_backfill_state", return_value=reading):
                expected = _check_codex_startup(cfg, self.local, process=ProcessState.STOPPED)
                with patch("codexsync.preflight.measure_chat_files", side_effect=AssertionError("fallback tree walk")):
                    actual = _check_codex_startup(cfg, self.local, process=ProcessState.STOPPED, sessions=observed)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.parts, expected.parts)


if __name__ == "__main__":
    unittest.main()
