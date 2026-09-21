"""Upgrading a config written by an older codexSync.

The fixture is not invented: `tests/fixtures/config-0.1.2.toml` is byte for
byte what `init-config` handed every 0.1 user, and it sets two values this
version refuses for any mutating command. So the first test here is the one
that matters -- that file, migrated, must load and be accepted for mutations --
and the rest guard the promises made along the way: the user's comments and
values survive, a list grows rather than being replaced, an optional finding
can be declined, a plan built for one version of the file cannot be applied to
another, and the shipped template is a fixed point (a migration that wants to
change what this version itself writes has a rule that is wrong).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import tomllib
import unittest
import uuid

from codexsync.config_edit import config_history_dir, validate_config_text
from codexsync.config_migrate import (
    BLOCKER,
    DETECTION_LIST_OUTDATED,
    LEGACY_SCHEDULER_KEYS,
    MISSING_EXCLUDE_SKILLS_SYSTEM,
    OBSOLETE_INCLUDE_ROOT,
    SCHEDULER_INTERVAL_MIGRATED,
    SESSION_MODE_LAST_DATE,
    TERMINATE_FLAG_SET,
    apply_migration,
    inspect_config,
    read_config_source,
    render_migrated_text,
)
from codexsync.exceptions import ConfigError, ConflictError
from codexsync.preflight import run_preflight
from codexsync.process_knowledge import BACKGROUND_PROCESS_NAMES, PROCESS_NAMES
from codexsync.runtime import _require_mutation_compatible_config

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "config-0.1.2.toml"
PACKAGED_TEMPLATE = REPO_ROOT / "src" / "codexsync" / "config.example.toml"


def _sandbox(name: str) -> Path:
    root = SANDBOX / f"{name}-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def _installed(root: Path, source: Path = FIXTURE) -> Path:
    """The fixture, with its paths pointed at a sandbox so it can be saved."""
    text = source.read_text(encoding="utf-8")
    workspace = (root / "ws").resolve().as_posix()
    state = (root / ".codex").resolve().as_posix()
    # The fixture points at the paths 0.1 suggested. Redirecting them is not
    # decoration: `config_history_dir` follows `workspace_root_dir`, so a test
    # that left it alone would write a history entry onto the real D: drive.
    replacements = {
        'workspace_root_dir = "D:/GDrive/codexSync"': f'workspace_root_dir = "{workspace}"',
        'workspace_root_dir = "D:/Cloud/codexSync"': f'workspace_root_dir = "{workspace}"',
        'local_state_dir = "C:/Users/USERNAME/.codex"': f'local_state_dir = "{state}"',
        'local_state_dir = "C:/Users/<user>/.codex"': f'local_state_dir = "{state}"',
    }
    replaced = 0
    for needle, value in replacements.items():
        if needle in text:
            text = text.replace(needle, value)
            replaced += 1
    assert replaced == 2, f"the fixture's paths were not redirected into the sandbox ({replaced})"
    for relative in ("ws/sync", "ws/backups", "ws/.tmp", "ws/logs", ".codex"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    config_path = root / "config.toml"
    config_path.write_text(text, encoding="utf-8", newline="")
    return config_path


class ZeroOneConfigTests(unittest.TestCase):
    """The file 0.1 shipped, and what this version makes of it."""

    def test_the_shipped_01_config_is_refused_for_mutations_as_it_stands(self) -> None:
        cfg = validate_config_text(FIXTURE.read_text(encoding="utf-8"), path=FIXTURE)
        with self.assertRaises(ConfigError):
            _require_mutation_compatible_config(cfg)

    def test_migrating_it_makes_it_usable_and_keeps_every_comment(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        plan = inspect_config(text)
        self.assertEqual(
            sorted(finding.code for finding in plan.blockers),
            [SESSION_MODE_LAST_DATE, TERMINATE_FLAG_SET],
        )
        migrated = render_migrated_text(text, plan)
        _require_mutation_compatible_config(validate_config_text(migrated, path=FIXTURE))
        comments = lambda body: [line for line in body.splitlines() if line.strip().startswith("#")]
        self.assertEqual(comments(text), comments(migrated), "a comment line was lost")

    def test_migrating_twice_finds_nothing_the_second_time(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        migrated = render_migrated_text(text, inspect_config(text))
        self.assertTrue(inspect_config(migrated).is_current, inspect_config(migrated).codes())

    def test_the_process_lists_gain_this_versions_knowledge(self) -> None:
        """The finding that is about safety: a name absent here is never matched."""
        text = FIXTURE.read_text(encoding="utf-8")
        migrated = render_migrated_text(text, inspect_config(text))
        document = tomllib.loads(migrated)["process_detection"]
        self.assertEqual(tuple(document["process_names"]), PROCESS_NAMES)
        for os_key, names in BACKGROUND_PROCESS_NAMES.items():
            self.assertEqual(tuple(document["background_process_names"][os_key]), names)

    def test_the_period_survives_the_move_to_seconds(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        migrated = render_migrated_text(text, inspect_config(text))
        scheduler = tomllib.loads(migrated)["scheduler"]
        self.assertEqual(scheduler["interval_seconds"], 600)
        self.assertNotIn("interval_minutes", scheduler)

    def test_doctor_says_so_before_any_command_refuses(self) -> None:
        root = _sandbox("migrate-doctor")
        try:
            config_path = _installed(root)
            report = run_preflight(config_path)
            check = next(item for item in report.checks if item.name == "config_compat")
            self.assertEqual(check.status, "FAIL")
            self.assertIn(TERMINATE_FLAG_SET, check.details)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class PlanTests(unittest.TestCase):
    def test_the_shipped_template_is_a_fixed_point(self) -> None:
        """A rule that wants to change what this version writes is a wrong rule."""
        plan = inspect_config(PACKAGED_TEMPLATE.read_text(encoding="utf-8"))
        self.assertTrue(plan.is_current, plan.codes())

    def test_the_plan_id_follows_the_file(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        first = inspect_config(text)
        self.assertEqual(first.plan_id, inspect_config(text).plan_id)
        changed = text.replace("grace_period_seconds = 2", "grace_period_seconds = 3")
        self.assertNotEqual(first.plan_id, inspect_config(changed).plan_id)

    def test_the_plan_id_is_over_the_bytes_the_save_will_compare(self) -> None:
        """A byte-order mark must not make every apply look like a conflict."""
        root = _sandbox("migrate-bom")
        try:
            config_path = _installed(root)
            data = config_path.read_bytes()
            config_path.write_bytes(b"\xef\xbb\xbf" + data)
            import hashlib

            _text, digest = read_config_source(config_path)
            self.assertEqual(digest, hashlib.sha256(config_path.read_bytes()).hexdigest())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_blocker_cannot_be_declined(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        plan = inspect_config(text)
        with self.assertRaises(ConfigError):
            render_migrated_text(text, plan, skip=[TERMINATE_FLAG_SET])

    def test_an_unknown_code_is_refused_rather_than_ignored(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        with self.assertRaises(ConfigError):
            render_migrated_text(text, inspect_config(text), skip=["NO_SUCH_FINDING"])

    def test_declining_the_optional_list_change_leaves_the_lists_alone(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        plan = inspect_config(text)
        migrated = render_migrated_text(text, plan, skip=[DETECTION_LIST_OUTDATED])
        before = tomllib.loads(text)["process_detection"]
        after = tomllib.loads(migrated)["process_detection"]
        self.assertEqual(before["process_names"], after["process_names"])
        self.assertEqual(before["background_process_names"], after["background_process_names"])

    def test_a_users_own_entries_are_kept_when_a_list_grows(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").replace(
            'process_names = ["codex.exe", "codex"]',
            'process_names = ["codex.exe", "codex", "my-own-codex"]',
        )
        migrated = render_migrated_text(text, inspect_config(text))
        self.assertIn("my-own-codex", tomllib.loads(migrated)["process_detection"]["process_names"])

    def test_a_shortened_list_is_reported_as_optional_not_enforced(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        finding = next(
            item for item in inspect_config(text).findings if item.code == DETECTION_LIST_OUTDATED
        )
        self.assertTrue(finding.optional)
        self.assertNotEqual(finding.level, BLOCKER)

    def test_state_database_entries_are_reported_only_for_a_config_that_has_them(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8").replace(
            '  "skills",', '  "skills",\n  "state_5.sqlite",\n  "state_5.sqlite-wal",'
        )
        finding = next(
            item for item in inspect_config(text).findings if item.code == OBSOLETE_INCLUDE_ROOT
        )
        self.assertTrue(finding.optional)
        migrated = render_migrated_text(text, inspect_config(text))
        roots = tomllib.loads(migrated)["targets"]["include_roots"]
        self.assertNotIn("state_5.sqlite", roots)
        self.assertIn("skills", roots)
        # The template lists this one, so it is not an obsolete entry.
        self.assertIn("session_index.jsonl", roots)


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _sandbox("migrate-apply")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config_path = _installed(self.root)

    def _plan_id(self) -> str:
        text, digest = read_config_source(self.config_path)
        return inspect_config(text, source_sha256=digest).plan_id

    def test_a_confirmed_plan_rewrites_the_file_once_and_keeps_the_old_one(self) -> None:
        outcome = apply_migration(self.config_path, confirm_plan_id=self._plan_id())
        self.assertIn(TERMINATE_FLAG_SET, outcome.applied)
        self.assertIsNotNone(outcome.saved.history_entry)
        self.assertTrue(outcome.saved.history_entry.is_file())
        _require_mutation_compatible_config(
            validate_config_text(self.config_path.read_text(encoding="utf-8"), path=self.config_path)
        )

    def test_a_stale_plan_id_is_refused(self) -> None:
        plan_id = self._plan_id()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8") + "\n# edited elsewhere\n",
            encoding="utf-8", newline="",
        )
        with self.assertRaises(ConflictError):
            apply_migration(self.config_path, confirm_plan_id=plan_id)

    def test_applying_a_current_config_is_refused_rather_than_writing_nothing(self) -> None:
        apply_migration(self.config_path, confirm_plan_id=self._plan_id())
        with self.assertRaises(ConfigError):
            apply_migration(self.config_path, confirm_plan_id=self._plan_id())

    def test_nothing_is_written_outside_the_config_and_its_history(self) -> None:
        before = {path for path in self.root.rglob("*") if path.is_file()}
        outcome = apply_migration(self.config_path, confirm_plan_id=self._plan_id())
        after = {path for path in self.root.rglob("*") if path.is_file()}
        created = after - before
        history = config_history_dir(
            validate_config_text(self.config_path.read_text(encoding="utf-8"), path=self.config_path),
            self.config_path,
        )
        self.assertEqual(created, {outcome.saved.history_entry})
        self.assertEqual(outcome.saved.history_entry.parent, history)
        # The state directory is never touched, not even to read-and-rewrite.
        self.assertEqual(list((self.root / ".codex").iterdir()), [])

    def test_a_declined_finding_is_reported_as_skipped(self) -> None:
        outcome = apply_migration(
            self.config_path,
            confirm_plan_id=self._plan_id(),
            skip=[DETECTION_LIST_OUTDATED, LEGACY_SCHEDULER_KEYS],
        )
        self.assertEqual(
            sorted(outcome.skipped), sorted([DETECTION_LIST_OUTDATED, LEGACY_SCHEDULER_KEYS])
        )
        self.assertIn(MISSING_EXCLUDE_SKILLS_SYSTEM, outcome.applied)
        self.assertIn(SCHEDULER_INTERVAL_MIGRATED, outcome.applied)
        remaining = inspect_config(self.config_path.read_text(encoding="utf-8")).codes()
        self.assertEqual(sorted(remaining), sorted([DETECTION_LIST_OUTDATED, LEGACY_SCHEDULER_KEYS]))


if __name__ == "__main__":
    unittest.main()
