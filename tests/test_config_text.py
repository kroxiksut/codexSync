"""`parse_config_text`, TOML syntax errors and the `[scheduler]` section (CS-232)."""
from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.config import load_config, parse_config_text
from codexsync.exceptions import ConfigError
from codexsync.models import SchedulerConfig


REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"
PACKAGED_TEMPLATE = REPO_ROOT / "src" / "codexsync" / "config.example.toml"

BASE = (
    "[paths]\n"
    'cloud_root_dir = "sync"\n'
    'backup_dir = "backups"\n'
    'temp_dir = ".tmp"\n'
)


def _parse(extra: str = "") -> object:
    # parse_config_text resolves paths but never touches the file system, so
    # the base directory does not have to exist.
    return parse_config_text(BASE + extra, base_dir=SANDBOX / "unused")


class ParseConfigTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"config-text-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_relative_paths_resolve_against_base_dir(self) -> None:
        cfg = parse_config_text(BASE, base_dir=self.root)
        self.assertEqual(cfg.paths.cloud_root_dir, (self.root / "sync").resolve())
        self.assertEqual(cfg.paths.backup_dir, (self.root / "backups").resolve())

    def test_text_and_file_produce_the_same_config(self) -> None:
        path = self.root / "config.toml"
        path.write_bytes(PACKAGED_TEMPLATE.read_bytes())
        from_file = load_config(path)
        from_text = parse_config_text(
            PACKAGED_TEMPLATE.read_bytes().decode("utf-8"), base_dir=self.root.resolve()
        )
        self.assertEqual(from_file, from_text)

    def test_missing_file_message_is_unchanged(self) -> None:
        missing = self.root / "nope.toml"
        with self.assertRaisesRegex(ConfigError, "Config file not found"):
            load_config(missing)

    def test_toml_syntax_error_is_a_config_error_from_text(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Invalid TOML"):
            parse_config_text(BASE + "[sync\nmode = 'cold'\n", base_dir=self.root)

    def test_toml_syntax_error_is_a_config_error_from_file(self) -> None:
        path = self.root / "config.toml"
        path.write_text(BASE + "compare = = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "Invalid TOML"):
            load_config(path)

    def test_file_with_byte_order_mark_loads(self) -> None:
        path = self.root / "config.toml"
        path.write_bytes(b"\xef\xbb\xbf" + BASE.encode("utf-8"))
        self.assertEqual(load_config(path).paths.temp_dir, (self.root / ".tmp").resolve())

    def test_non_utf8_file_is_a_config_error(self) -> None:
        path = self.root / "config.toml"
        path.write_bytes(BASE.encode("utf-8") + b"# \xff\xfe\n")
        with self.assertRaisesRegex(ConfigError, "UTF-8"):
            load_config(path)


class SchedulerConfigTests(unittest.TestCase):
    def test_missing_section_uses_defaults(self) -> None:
        self.assertEqual(_parse().scheduler, SchedulerConfig())

    def test_defaults_match_the_contract(self) -> None:
        self.assertEqual(
            SchedulerConfig(),
            SchedulerConfig(
                enabled=False,
                mode="guardian_snapshot",
                interval_seconds=60,
                run_at_login=True,
                startup_delay_seconds=0,
                jitter_seconds=0,
            ),
        )

    def test_shipped_template_carries_the_contract(self) -> None:
        cfg = load_config(PACKAGED_TEMPLATE)
        self.assertEqual(cfg.scheduler, SchedulerConfig())

    def test_all_fields_are_read(self) -> None:
        cfg = _parse(
            "[scheduler]\n"
            "enabled = true\n"
            'mode = " SYNC_DRY_RUN "\n'
            "interval_seconds = 900\n"
            "run_at_login = false\n"
            "startup_delay_seconds = 30\n"
            "jitter_seconds = 45\n"
        )
        self.assertEqual(
            cfg.scheduler,
            SchedulerConfig(True, "sync_dry_run", 900, False, 30, 45),
        )

    def test_every_safe_mode_is_accepted(self) -> None:
        for mode in ("guardian_snapshot", "preflight", "sync_dry_run"):
            with self.subTest(mode=mode):
                self.assertEqual(_parse(f'[scheduler]\nmode = "{mode}"\n').scheduler.mode, mode)

    def test_legacy_keys_are_ignored(self) -> None:
        cfg = _parse(
            "[scheduler]\n"
            "enabled = false\n"
            'kind = "windows_task_scheduler"\n'
            "interval_minutes = 10\n"
            "jitter_seconds = 30\n"
        )
        self.assertEqual(cfg.scheduler.jitter_seconds, 30)
        self.assertEqual(cfg.scheduler.interval_seconds, 60)

    def test_mutating_mode_is_refused_with_a_reason(self) -> None:
        for mode in ("sync", "restore", "repair", ""):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ConfigError, "never mutates"):
                    _parse(f'[scheduler]\nmode = "{mode}"\n')

    def test_numeric_bounds(self) -> None:
        cases = {
            "interval_seconds = 59": "scheduler.interval_seconds must be >= 60",
            "startup_delay_seconds = -1": "scheduler.startup_delay_seconds must be >= 0",
            "jitter_seconds = -1": "scheduler.jitter_seconds must be >= 0",
        }
        for line, message in cases.items():
            with self.subTest(line=line):
                with self.assertRaisesRegex(ConfigError, message):
                    _parse(f"[scheduler]\n{line}\n")
        self.assertEqual(_parse("[scheduler]\ninterval_seconds = 60\n").scheduler.interval_seconds, 60)

    def test_wrong_types_are_refused_not_coerced(self) -> None:
        cases = {
            'enabled = "true"': "scheduler.enabled must be a boolean",
            "run_at_login = 1": "scheduler.run_at_login must be a boolean",
            "interval_seconds = 60.0": "scheduler.interval_seconds must be an integer",
            "interval_seconds = true": "scheduler.interval_seconds must be an integer",
            'jitter_seconds = "5"': "scheduler.jitter_seconds must be an integer",
            "mode = 1": "scheduler.mode must be one of",
        }
        for line, message in cases.items():
            with self.subTest(line=line):
                with self.assertRaisesRegex(ConfigError, message):
                    _parse(f"[scheduler]\n{line}\n")

    def test_scheduler_must_be_a_table(self) -> None:
        with self.assertRaisesRegex(ConfigError, "scheduler must be a table"):
            parse_config_text("scheduler = 5\n" + BASE, base_dir=SANDBOX / "unused")


if __name__ == "__main__":
    unittest.main()
