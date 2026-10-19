"""Review 2026-09-27: config validation, backup pruning, log rotation, temp sweep.

CS-289 (a written empty include list meant "everything", auth.json included),
CS-290/294 (overlapping owned folders; prune deleting what is not its own),
CS-318 (log file inside `.codex`; one zip per line with two processes),
CS-319 (bad values -> exit 1 or silent coercion), CS-320 (`\\\\?\\` prefix),
and three CS-325 items (tmp sweep age, migrated interval clamp).
"""
from __future__ import annotations

from datetime import date, timedelta
import logging
import os
from pathlib import Path
import shutil
import time
import tomllib
import unittest
from unittest import mock
import uuid

from codexsync.backup import BackupManager
from codexsync.config import parse_config_text, strip_extended_length_prefix
from codexsync.config_migrate import inspect_config, render_migrated_text
from codexsync.exceptions import ConfigError
from codexsync.filters import PathFilter
from codexsync.logging_setup import _DailySizeRotatingFileHandler
from codexsync.models import MAX_SCHEDULER_INTERVAL_SECONDS
from codexsync.mutation_journal import JournalStore
from codexsync.runtime import _build_indexes
from codexsync.scanner import scan_tree
from codexsync.sync_engine import SyncEngine

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"review-0927-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def parse(self, paths: str = "", extra: str = "") -> object:
        text = (
            "[paths]\n"
            f'local_state_dir = "{(self.root / "codex").as_posix()}"\n'
            'cloud_root_dir = "sync"\n'
            'backup_dir = "backups"\n'
            'temp_dir = ".tmp"\n'
            + paths + extra
        )
        return parse_config_text(text, base_dir=self.root)


class IncludeRootTests(_Sandbox):
    def test_a_written_empty_list_or_a_root_naming_everything_is_refused(self) -> None:
        for value in ("[]", '["."]', '[""]', '["./"]', '["skills", " "]', '["a/.."]', '["/etc"]'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "targets.include_roots"):
                    self.parse(extra=f"[targets]\ninclude_roots = {value}\n")

    def test_an_absent_key_loads_and_scans_nothing(self) -> None:
        cfg = self.parse()
        self.assertEqual(cfg.targets.include_roots, [])
        state = self.root / "codex"
        (state / "auth.json").parent.mkdir(parents=True)
        (state / "auth.json").write_text("{}", encoding="utf-8")
        self.assertEqual(scan_tree(state, [], PathFilter([])), {})

    def test_a_secret_under_an_included_root_is_never_indexed(self) -> None:
        cfg = self.parse(extra='[targets]\ninclude_roots = ["data"]\n')
        local, cloud = self.root / "codex", self.root / "sync"
        for rel in ("data/auth.json", "data/sub/.sandbox-secrets/key", "data/ok.md"):
            (local / rel).parent.mkdir(parents=True, exist_ok=True)
            (local / rel).write_text("x", encoding="utf-8")
        cloud.mkdir()
        local_idx, _ = _build_indexes(cfg, local, cloud)
        self.assertEqual(sorted(local_idx), ["data/ok.md"])


class ValueTypeTests(_Sandbox):
    def test_a_word_where_a_number_belongs_is_a_config_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "backup.retention_days"):
            self.parse(extra='[backup]\nretention_days = "a week"\n')
        with self.assertRaisesRegex(ConfigError, "guardian.shrink_ratio"):
            self.parse(extra='[guardian]\nshrink_ratio = "lots"\n')

    def test_a_quoted_whole_number_is_still_read(self) -> None:
        self.assertEqual(self.parse(extra='[backup]\nretention_days = "14"\n').backup.retention_days, 14)

    def test_a_string_is_not_split_into_letters(self) -> None:
        with self.assertRaisesRegex(ConfigError, "filters.exclude_globs must be a list"):
            self.parse(extra='[filters]\nexclude_globs = "**/*.lock"\n')

    def test_an_unknown_log_level_and_negative_counts_are_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "logging.level"):
            self.parse(extra='[logging]\nlevel = "LOUD"\n')
        for extra in (
            "[process_detection]\ngrace_period_seconds = -1\n",
            "[backup]\nmax_backups = -1\n",
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(ConfigError, ">= 0"):
                self.parse(extra=extra)

    def test_a_section_that_is_not_a_table_is_a_config_error(self) -> None:
        text = 'backup = "yes"\n[paths]\ncloud_root_dir = "sync"\nbackup_dir = "b"\ntemp_dir = "t"\n'
        with self.assertRaisesRegex(ConfigError, "backup must be a table"):
            parse_config_text(text, base_dir=self.root)


class OverlapTests(_Sandbox):
    def test_owned_folders_may_not_nest(self) -> None:
        cases = {
            "paths.cloud_root_dir and paths.backup_dir": ('backup_dir = "sync/backups"\n', ""),
            "paths.backup_dir and paths.temp_dir": ('temp_dir = "backups/tmp"\n', ""),
            "paths.backup_dir and state.manifest_file": ("", '[state]\nmanifest_file = "backups/manifest.json"\n'),
            "paths.cloud_root_dir and state.manifest_file": ("", '[state]\nmanifest_file = "sync/manifest.json"\n'),
        }
        for message, (path_line, extra) in cases.items():
            with self.subTest(message=message):
                text = (
                    "[paths]\n"
                    'cloud_root_dir = "sync"\n'
                    + (path_line if path_line.startswith("backup_dir") else 'backup_dir = "backups"\n')
                    + (path_line if path_line.startswith("temp_dir") else 'temp_dir = ".tmp"\n')
                    + extra
                )
                with self.assertRaisesRegex(ConfigError, message):
                    parse_config_text(text, base_dir=self.root)

    def test_the_log_file_may_not_sit_inside_the_codex_state(self) -> None:
        log = (self.root / "codex" / "logs" / "codexsync.log").as_posix()
        with self.assertRaisesRegex(ConfigError, "logging.file must be outside"):
            self.parse(extra=f'[logging]\nfile = "{log}"\n')

    def test_the_extended_length_prefix_is_removed(self) -> None:
        self.assertEqual(strip_extended_length_prefix("\\\\?\\C:\\Users\\x"), "C:\\Users\\x")
        self.assertEqual(strip_extended_length_prefix("\\\\?\\UNC\\srv\\share\\a"), "\\\\srv\\share\\a")
        self.assertEqual(strip_extended_length_prefix("//?/C:/x"), "C:/x")
        self.assertEqual(strip_extended_length_prefix("C:/x"), "C:/x")

    @unittest.skipUnless(os.name == "nt", "extended-length paths are a Windows form")
    def test_a_prefixed_path_is_still_seen_to_overlap(self) -> None:
        inside = "\\\\?\\" + str((self.root / "sync" / "backups").resolve())
        text = (
            "[paths]\n"
            'cloud_root_dir = "sync"\n'
            f"backup_dir = '{inside}'\n"
            'temp_dir = ".tmp"\n'
        )
        with self.assertRaisesRegex(ConfigError, "must not overlap"):
            parse_config_text(text, base_dir=self.root)


class BackupPruneTests(_Sandbox):
    OLD = time.time() - 90 * 24 * 3600

    def _snapshot(self, name: str, *, old: bool = True, directory: bool = True) -> Path:
        path = self.root / "backups" / name
        if directory:
            (path / "x").mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PK")
        if old:
            os.utime(path, (self.OLD, self.OLD))
        return path

    def manager(self, **kwargs) -> BackupManager:
        return BackupManager(
            self.root / "backups", "machine-a", retention_days=30,
            journal_root=self.root / "tmp", **kwargs,
        )

    def test_only_this_machines_own_snapshots_are_pruned(self) -> None:
        own = self._snapshot("machine-a-20260101T000000Z-0123456789ab")
        own_zip = self._snapshot("machine-a-20260102T000000Z-0123456789ab.zip", directory=False)
        other = self._snapshot("machine-b-20260101T000000Z-0123456789ab")
        similar = self._snapshot("machine-a-2-20260101T000000Z-0123456789ab")
        mirror = self._snapshot("sessions")
        self.manager().prune()
        self.assertFalse(own.exists())
        self.assertFalse(own_zip.exists())
        for kept in (other, similar, mirror):
            self.assertTrue(kept.exists(), kept.name)

    def test_a_snapshot_an_unfinished_journal_names_is_kept(self) -> None:
        name = "machine-a-20260101T000000Z-0123456789ab"
        needed = self._snapshot(name)
        newer = [
            self._snapshot(f"machine-a-2026020{i}T000000Z-0123456789ab", old=False) for i in range(1, 4)
        ]
        JournalStore(self.root / "tmp").begin("sync", "hash", 1, backup_snapshot=name)
        self.manager(max_backups=1).prune()
        self.assertTrue(needed.exists())
        self.assertEqual(sum(path.exists() for path in newer), 1)

    def test_unreadable_journals_prune_nothing(self) -> None:
        own = self._snapshot("machine-a-20260101T000000Z-0123456789ab")
        journals = JournalStore(self.root / "tmp").root
        journals.mkdir(parents=True)
        (journals / "broken.json").write_text("{", encoding="utf-8")
        self.manager().prune()
        self.assertTrue(own.exists())

    def test_a_folder_that_cannot_be_listed_is_a_warning_not_a_failure(self) -> None:
        """Every caller prunes after COMMITTED: an error there reported a delivered sync as failed."""
        self._snapshot("machine-a-20260101T000000Z-0123456789ab")
        manager = self.manager()
        with mock.patch.object(Path, "iterdir", side_effect=FileNotFoundError("gone")):
            with self.assertLogs("codexsync.backup", level="WARNING") as logs:
                manager.prune()
        self.assertIn("next run tries again", logs.output[0])


class LogRotationTests(_Sandbox):
    def handler(self, max_bytes: int = 10_000) -> _DailySizeRotatingFileHandler:
        handler = _DailySizeRotatingFileHandler(
            base_file=self.root / "logs" / "codexsync.log", retention_days=0,
            archive_mode="zip", max_bytes=max_bytes, machine_name="m",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def test_a_same_day_log_another_process_may_hold_is_left_alone(self) -> None:
        logs = self.root / "logs"
        logs.mkdir()
        today, yesterday = date.today(), date.today() - timedelta(days=1)
        (logs / f"codexsync-m-{today.isoformat()}.log").write_text("other process\n", encoding="utf-8")
        (logs / f"codexsync-m-{today.isoformat()}.1.log").write_text("mine\n", encoding="utf-8")
        (logs / f"codexsync-m-{yesterday.isoformat()}.log").write_text("old\n", encoding="utf-8")
        self.handler()
        self.assertTrue((logs / f"codexsync-m-{today.isoformat()}.log").exists())
        self.assertFalse((logs / f"codexsync-m-{yesterday.isoformat()}.log").exists())
        self.assertTrue((logs / f"codexsync-m-{yesterday.isoformat()}.log.zip").exists())

    def test_a_log_that_cannot_be_claimed_is_not_zipped_again_and_again(self) -> None:
        handler = self.handler(max_bytes=200)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x" * 120, None, None)
        with mock.patch("codexsync.logging_setup.os.replace", side_effect=PermissionError("in use")):
            for _ in range(20):
                handler.emit(record)
        self.assertEqual(list((self.root / "logs").glob("*.zip")), [])


class TempSweepTests(_Sandbox):
    def test_a_fresh_tmp_file_in_temp_dir_is_not_swept(self) -> None:
        temp = self.root / "tmp"
        temp.mkdir()
        fresh = temp / "0123.tmp"
        fresh.write_text("{}", encoding="utf-8")
        engine = SyncEngine(BackupManager(self.root / "backups", "m"), temp)
        engine._cleanup_orphaned_temp_files()
        self.assertTrue(fresh.exists(), "a journal being written right now")
        later = SyncEngine(BackupManager(self.root / "backups", "m"), temp, now=lambda: time.time() + 7200)
        later._cleanup_orphaned_temp_files()
        self.assertFalse(fresh.exists())


class MigrationClampTests(unittest.TestCase):
    def test_a_huge_interval_in_minutes_migrates_to_the_largest_accepted(self) -> None:
        text = "[scheduler]\ninterval_minutes = 1000000\n"
        migrated = render_migrated_text(text, inspect_config(text))
        self.assertEqual(tomllib.loads(migrated)["scheduler"]["interval_seconds"], MAX_SCHEDULER_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
