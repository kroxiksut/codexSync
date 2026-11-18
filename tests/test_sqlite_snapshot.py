"""A reader must not lose committed WAL rows or invent an uncommitted row."""
from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.sqlite_snapshot import SnapshotRefused, materialize_database


class SQLiteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / "test-sandbox" / f"sqlite-snapshot-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.database = self.root / "state_1.sqlite"
        self.writer = sqlite3.connect(self.database)
        self.writer.execute("CREATE TABLE marker (value TEXT)")
        self.writer.execute("INSERT INTO marker VALUES ('old')")
        self.writer.commit()
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("UPDATE marker SET value='committed'")
        self.writer.commit()
        self.main = self.database.read_bytes()
        self.wal = self.database.with_name(self.database.name + "-wal").read_bytes()

    def tearDown(self):
        self.writer.close()
        shutil.rmtree(self.root)

    def value(self, image):
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(image)
            return connection.execute("SELECT value FROM marker").fetchone()[0]
        finally:
            connection.close()

    def test_committed_wal_frames_are_the_truth_without_opening_the_source_database(self):
        self.assertEqual(self.value(materialize_database(self.main, self.wal)), "committed")

    def test_valid_uncommitted_tail_does_not_replace_last_committed_state(self):
        # Capture a second real commit then clear its commit mark and re-checksum
        # it. This models SQLite spilling a transaction before COMMIT.
        self.writer.execute("UPDATE marker SET value='uncommitted'")
        self.writer.commit()
        wal = bytearray(self.database.with_name(self.database.name + "-wal").read_bytes())
        page_size = int.from_bytes(wal[8:12], "big")
        last = len(wal) - 24 - page_size
        wal[last + 4:last + 8] = bytes(4)
        self.rechecksum(wal)
        self.assertEqual(self.value(materialize_database(self.main, bytes(wal))), "committed")

    @staticmethod
    def rechecksum(wal):
        little = int.from_bytes(wal[:4], "big") == 0x377F0682
        order = "little" if little else "big"
        s1 = s2 = 0
        def checksum(payload):
            nonlocal s1, s2
            for offset in range(0, len(payload), 8):
                s1 = (s1 + int.from_bytes(payload[offset:offset+4], order) + s2) & 0xFFFFFFFF
                s2 = (s2 + int.from_bytes(payload[offset+4:offset+8], order) + s1) & 0xFFFFFFFF
        checksum(wal[:24])
        wal[24:32] = s1.to_bytes(4, "big") + s2.to_bytes(4, "big")
        size = int.from_bytes(wal[8:12], "big")
        for offset in range(32, len(wal), size + 24):
            checksum(wal[offset:offset+8] + wal[offset+24:offset+24+size])
            wal[offset+16:offset+24] = s1.to_bytes(4, "big") + s2.to_bytes(4, "big")

    def test_damaged_or_partial_wal_is_refused_instead_of_showing_checkpointed_old_rows(self):
        cases = [self.wal[:-1], self.wal[:31]]
        for index in (0, 4, 24, 40, 48, len(self.wal)-1):
            broken = bytearray(self.wal)
            broken[index] ^= 1
            cases.append(bytes(broken))
        for wal in cases:
            with self.subTest(length=len(wal)), self.assertRaises(SnapshotRefused):
                materialize_database(self.main, wal)

    def test_unknown_format_and_oversize_image_are_refused_before_allocation(self):
        with self.assertRaises(SnapshotRefused):
            materialize_database(self.main, self.wal, max_bytes=len(self.main)-1)
        wal = bytearray(self.wal)
        wal[36:40] = (1000000000).to_bytes(4, "big")
        self.rechecksum(wal)
        with self.assertRaises(SnapshotRefused):
            materialize_database(self.main, bytes(wal))

    def test_opposite_checksum_byte_order_keeps_the_same_committed_rows(self):
        wal = bytearray(self.wal)
        wal[:4] = (0x377F0683).to_bytes(4, "big")
        self.rechecksum(wal)
        self.assertEqual(self.value(materialize_database(self.main, bytes(wal))), "committed")

    def test_wal_without_any_commit_keeps_checkpointed_main_rows(self):
        wal = bytearray(self.wal)
        page_size = int.from_bytes(wal[8:12], "big")
        for offset in range(32, len(wal), 24 + page_size):
            wal[offset+4:offset+8] = bytes(4)
        self.rechecksum(wal)
        self.assertEqual(self.value(materialize_database(self.main, bytes(wal))), "old")

    def test_growth_commit_cannot_fill_missing_pages_with_zeros_or_resurrect_truncated_main_pages(self):
        size = int.from_bytes(self.wal[8:12], "big")
        salt = self.wal[16:24]
        def frame(page, commit):
            return page.to_bytes(4, "big") + commit.to_bytes(4, "big") + salt + bytes(8) + self.main[:size]
        for frames in (frame(1, len(self.main)//size + 1), frame(1, 1) + frame(1, 2)):
            wal = bytearray(self.wal[:32] + frames)
            self.rechecksum(wal)
            with self.assertRaises(SnapshotRefused):
                materialize_database(self.main, bytes(wal))
