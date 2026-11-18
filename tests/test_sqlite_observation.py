"""Doctor's SQLite facts must describe the same reading without disk effects."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import uuid

from codexsync import sqlite_audit
from codexsync.sqlite_observation import observe_sqlite


class SQLiteObservationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / "test-sandbox" / f"sqlite-observation-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.writer = None

    def tearDown(self):
        if self.writer is not None:
            self.writer.close()
        shutil.rmtree(self.root)

    def catalogue(self, wal=False):
        database = self.root / "state_9.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
        connection.execute("CREATE TABLE backfill_state (id INTEGER, status TEXT)")
        connection.execute("INSERT INTO threads VALUES ('chat', ?, 0)", (str(self.root / "sessions/chat.jsonl"),))
        connection.execute("INSERT INTO backfill_state VALUES (1, 'complete')")
        connection.commit()
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE backfill_state SET status='running'")
            connection.commit()
            # Detach a stable set while the fixture writer owns its original.
            # Raw POSIX reads must refuse a database opened by this process:
            # closing a raw fd can release that writer's POSIX SQLite locks.
            payloads = {suffix: database.with_name(database.name + suffix).read_bytes()
                        for suffix in ("", "-wal", "-shm")}
            connection.close()
            for suffix, payload in payloads.items():
                database.with_name(database.name + suffix).write_bytes(payload)
        else:
            connection.close()
        return database

    def disk(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_wal_rows_backfill_and_audit_share_one_memory_connection_and_leave_every_file_unchanged(self):
        self.catalogue(wal=True)
        before = self.disk()
        real_connect = sqlite3.connect
        calls = []
        def connect(source, *args, **kwargs):
            calls.append(source)
            self.assertEqual(source, ":memory:")
            return real_connect(source, *args, **kwargs)
        with patch("codexsync.sqlite_observation.sqlite3.connect", side_effect=connect):
            observation = observe_sqlite(self.root)
        self.assertEqual(calls, [":memory:"])
        self.assertEqual(observation.backfill.backfill, "running")
        self.assertEqual(observation.placements.by_session, {"chat": "sessions/chat.jsonl"})
        self.assertEqual(observation.reports[0].status, "PASS")
        self.assertEqual(observation.reports[0].journal_mode, "wal")
        self.assertEqual(before, self.disk())

    def test_supplied_facts_are_authoritative_after_source_changes_and_cannot_be_mutated(self):
        self.catalogue()
        observation = observe_sqlite(self.root)
        self.writer = sqlite3.connect(self.root / "state_9.sqlite")
        self.writer.execute("UPDATE backfill_state SET status='pending'")
        self.writer.execute("DELETE FROM threads")
        self.writer.commit()
        with patch("codexsync.sqlite_audit._read_only_connect", side_effect=AssertionError("reopened")):
            self.assertEqual(sqlite_audit.read_backfill_state(self.root, observation=observation).backfill, "complete")
            self.assertTrue(sqlite_audit.read_thread_placements(self.root, observation=observation).knows("chat"))
            self.assertEqual(sqlite_audit.audit_sqlite(self.root, observation=observation), list(observation.reports))
        with self.assertRaises(TypeError):
            observation.placements.by_session["new"] = "other"
        with self.assertRaises(FrozenInstanceError):
            observation.backfill.backfill = "pending"

    def test_available_missing_unknown_and_unreadable_keep_existing_readings_exactly(self):
        for scenario in ("missing", "available", "unknown", "unreadable"):
            with self.subTest(scenario=scenario):
                database = self.root / "state_9.sqlite"
                if scenario == "available":
                    self.catalogue()
                elif scenario == "unknown":
                    database.unlink()
                    connection = sqlite3.connect(database)
                    connection.execute("CREATE TABLE unexpected (id TEXT)")
                    connection.close()
                elif scenario == "unreadable":
                    database.write_bytes(b"damaged")
                before = self.disk()
                expected_backfill = sqlite_audit.read_backfill_state(self.root)
                expected_placements = sqlite_audit.read_thread_placements(self.root)
                expected_reports = sqlite_audit.audit_sqlite(self.root)
                observation = observe_sqlite(self.root)
                self.assertEqual(observation.backfill, expected_backfill)
                self.assertEqual(observation.placements, expected_placements)
                self.assertEqual(list(observation.reports), expected_reports)
                self.assertEqual(before, self.disk())

    def test_unreadable_wal_never_exposes_checkpointed_complete_backfill(self):
        self.catalogue(wal=True)
        before = self.disk()
        real = Path.stat
        def stat(path, *args, **kwargs):
            if path.name.endswith("-wal"):
                raise PermissionError("WAL metadata unavailable")
            return real(path, *args, **kwargs)
        with patch.object(Path, "stat", stat):
            observation = observe_sqlite(self.root)
        self.assertIs(observation.backfill.status, sqlite_audit.PlacementStatus.INDETERMINATE)
        self.assertIsNone(observation.backfill.backfill)
        self.assertEqual(before, self.disk())

    def test_pending_wal_without_shared_index_keeps_legacy_refusals_and_never_creates_one(self):
        database = self.catalogue(wal=True)
        database.with_name(database.name + "-shm").unlink()
        expected_backfill = sqlite_audit.read_backfill_state(self.root)
        expected_placements = sqlite_audit.read_thread_placements(self.root)
        expected_reports = sqlite_audit.audit_sqlite(self.root)
        before = self.disk()
        observation = observe_sqlite(self.root)
        self.assertEqual(observation.backfill, expected_backfill)
        self.assertEqual(observation.placements, expected_placements)
        self.assertEqual(list(observation.reports), expected_reports)
        self.assertEqual(before, self.disk())

    def test_changed_source_bytes_with_restored_mtime_are_never_combined_with_old_schema_facts(self):
        database = self.catalogue()
        from codexsync import sqlite_observation
        real = sqlite_observation._read_bytes
        changed = False
        def read(path, limit):
            nonlocal changed
            payload = real(path, limit)
            if path == database and not changed:
                changed = True
                stat = database.stat()
                body = bytearray(payload)
                body[-1] ^= 1
                database.write_bytes(body)
                os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return payload
        with patch("codexsync.sqlite_observation._read_bytes", side_effect=read):
            observation = observe_sqlite(self.root)
        self.assertIs(observation.backfill.status, sqlite_audit.PlacementStatus.INDETERMINATE)
        self.assertIs(observation.placements.status, sqlite_audit.PlacementStatus.INDETERMINATE)
        self.assertEqual(observation.reports[0].status, "INDETERMINATE")

    def test_a_source_or_sidecar_changed_during_capture_invalidates_all_facts_for_that_set(self):
        database = self.catalogue()
        from codexsync import sqlite_observation
        real = sqlite_observation._read_bytes
        count = 0
        def read(path, limit):
            nonlocal count
            payload = real(path, limit)
            if path == database and count == 0:
                count += 1
                database.with_name(database.name + "-shm").write_bytes(b"appeared")
            return payload
        with patch("codexsync.sqlite_observation._read_bytes", side_effect=read):
            observation = observe_sqlite(self.root)
        self.assertIs(observation.backfill.status, sqlite_audit.PlacementStatus.INDETERMINATE)
        self.assertIs(observation.placements.status, sqlite_audit.PlacementStatus.INDETERMINATE)
        self.assertEqual(observation.reports[0].status, "INDETERMINATE")

    def test_a_locked_catalogue_is_unreadable_even_when_its_checkpointed_bytes_are_stable(self):
        database = self.catalogue()
        program = (
            "import sqlite3, sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN EXCLUSIVE'); print('locked', flush=True); sys.stdin.readline(); c.close()"
        )
        process = subprocess.Popen([sys.executable, "-c", program, str(database)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            before = self.disk()
            observation = observe_sqlite(self.root)
            self.assertIs(observation.backfill.status, sqlite_audit.PlacementStatus.INDETERMINATE)
            self.assertIs(observation.placements.status, sqlite_audit.PlacementStatus.INDETERMINATE)
            self.assertEqual(before, self.disk())
        finally:
            process.communicate("release\n", timeout=10)

    def test_only_read_queries_run_and_private_connection_is_closed_before_returning(self):
        self.catalogue()
        statements = []
        connections = []
        real = sqlite3.connect
        def connect(*args, **kwargs):
            connection = real(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            connections.append(connection)
            return connection
        with patch("codexsync.sqlite_observation.sqlite3.connect", side_effect=connect):
            observe_sqlite(self.root, cold=True)
        self.assertEqual(sum(statement == "BEGIN" for statement in statements), 1)
        # CPython's deserialize emits this internal memory-only attach through
        # the trace hook. No filename supplied to SQLite names a source file.
        self.assertTrue(all(statement == "ATTACH x AS 'main'" or statement.startswith(("BEGIN", "SELECT", "PRAGMA"))
                            for statement in statements), statements)
        self.assertFalse(any("=" in statement for statement in statements if statement.startswith("PRAGMA")))
        self.assertFalse(any("query_only" in statement or "busy_timeout" in statement for statement in statements))
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_posix_self_open_guard_refuses_without_opening_or_releasing_the_existing_connection_locks(self):
        from codexsync import sqlite_read_lock
        database = self.catalogue()
        stat = database.stat()
        own_handle = SimpleNamespace(st_dev=stat.st_dev, st_ino=stat.st_ino)
        for platform, inventory in (("linux", "/proc/self/fd"), ("darwin", "/dev/fd")):
            with self.subTest(platform=platform), patch.object(sqlite_read_lock.sys, "platform", platform), \
                    patch.object(sqlite_read_lock.os, "listdir", return_value=["42"]) as listing, \
                    patch.object(sqlite_read_lock.os, "fstat", return_value=own_handle):
                with self.assertRaises(OSError):
                    sqlite_read_lock._refuse_same_process_handle(database)
                listing.assert_called_once_with(inventory)
        with patch.object(sqlite_read_lock.os, "listdir", side_effect=PermissionError("fd inventory denied")):
            with self.assertRaises(PermissionError):
                sqlite_read_lock._refuse_same_process_handle(database)

    def test_posix_inventory_failure_stops_before_raw_open_and_windows_needs_no_fd_inventory(self):
        from codexsync import sqlite_read_lock
        database = self.catalogue()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), patch.object(sqlite_read_lock.os, "name", "posix"), \
                    patch.object(sqlite_read_lock.sys, "platform", platform), \
                    patch.object(sqlite_read_lock.os, "listdir", side_effect=PermissionError("fd inventory denied")), \
                    patch.object(sqlite_read_lock, "open_shared") as opened:
                with self.assertRaises(PermissionError):
                    sqlite_read_lock.check_source_read_lock(database)
                opened.assert_not_called()
        with patch.object(sqlite_read_lock.os, "name", "nt"), \
                patch.object(sqlite_read_lock, "_refuse_same_process_handle", side_effect=AssertionError("POSIX guard")), \
                patch.object(sqlite_read_lock, "open_shared", return_value=io.BytesIO()) as opened, \
                patch.object(sqlite_read_lock, "_windows_probe"):
            opened.return_value.fileno = lambda: 42
            sqlite_read_lock.check_source_read_lock(database)

    def test_history_store_available_unknown_unreadable_and_multiple_match_legacy_status_and_codes(self):
        self.catalogue()
        path = self.root / "thread_history_1.sqlite"
        for scenario in ("available", "unknown", "unreadable", "multiple"):
            with self.subTest(scenario=scenario):
                if path.exists():
                    path.unlink()
                if scenario in ("available", "unknown"):
                    connection = sqlite3.connect(path)
                    table = sqlite_audit.PROJECTION_TABLE if scenario == "available" else "unexpected"
                    connection.execute(f'CREATE TABLE "{table}" (thread_id TEXT)')
                    connection.execute(f'INSERT INTO "{table}" VALUES (\'chat\')')
                    connection.commit()
                    connection.close()
                else:
                    path.write_bytes(b"damaged")
                    if scenario == "multiple":
                        (self.root / "thread_history_2.sqlite").write_bytes(b"damaged")
                expected = sqlite_audit.read_thread_placements(self.root)
                before = self.disk()
                observed = observe_sqlite(self.root)
                self.assertEqual(observed.placements, expected)
                self.assertEqual(before, self.disk())
