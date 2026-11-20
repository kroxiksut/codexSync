"""Copies of the Codex state directory (CS-276, `D-017`).

What is checked is what makes a copy trustworthy: it takes the valuable files
and never a secret, it is taken only while Codex is closed (waiting for that
when asked), a file that moves during the copy fails the whole copy, nothing
is committed before it reads back as written, old copies go only after a new
one is committed and only this machine's, and `.codex` is never written to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest import mock
import uuid
import zipfile

from codexsync.config import load_config
from codexsync.exceptions import ConfigError, FailSafeError, SafetyPreconditionError
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision, SafetyGate
from codexsync.state_backup import (
    ARCHIVE_MANIFEST,
    create_state_backup,
    list_state_backups,
    select_state_files,
)

try:
    from tests.test_guardian_state_isolation import forbid_writes_under
except ImportError:  # collected with tests/ itself on sys.path
    from test_guardian_state_isolation import forbid_writes_under

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"


class _Gate:
    """Answers from a script of states; records the final check."""

    def __init__(self, *states: ProcessState) -> None:
        self.states = list(states) or [ProcessState.STOPPED]
        self.finals = 0
        self.final_state = ProcessState.STOPPED

    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return SafetyDecision(operation, state, state is ProcessState.STOPPED, f"test gate: {state.value}")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        self.finals += 1
        if self.final_state is not ProcessState.STOPPED:
            raise SafetyPreconditionError("Codex opened during the copy")
        return SafetyDecision(operation, ProcessState.STOPPED, True, "final")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


class StateBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"state-backup-{uuid.uuid4().hex[:8]}"
        self.addCleanup(shutil.rmtree, self.root, True)
        self.codex = self.root / "codex"
        self.copies = self.root / "copies"
        _write(self.codex / "sessions" / "2026" / "09" / "25" / "rollout-a.jsonl", '{"a":1}\n')
        _write(self.codex / "archived_sessions" / "rollout-b.jsonl", '{"b":1}\n')
        _write(self.codex / ".codex-global-state.json", "{}")
        _write(self.codex / ".codex-global-state.json.bak", "{}")
        _write(self.codex / "state_5.sqlite", b"SQLite format 3\x00" + b"\x01" * 64)
        _write(self.codex / "state_5.sqlite-wal", b"wal")
        _write(self.codex / "session_index.jsonl", "")
        _write(self.codex / "config.toml", "model = 'x'\n")
        _write(self.codex / "skills" / "mine" / "SKILL.md", "# skill")
        # Never copied: secrets, caches, logs, temporaries, binaries.
        _write(self.codex / "auth.json", '{"token": "secret"}')
        _write(self.codex / "cap_sid", "sid")
        _write(self.codex / ".sandbox-secrets" / "key", "secret")
        _write(self.codex / "skills" / "mine" / "auth.json", "nested secret")
        _write(self.codex / "cache" / "blob", "cache")
        _write(self.codex / "logs_2.sqlite", "logs")
        _write(self.codex / ".tmp" / "x", "tmp")
        _write(self.codex / "sessions" / "half.jsonl.tmp", "partial")
        self.config_path = self._config()

    def _config(self, **state_backup: object) -> Path:
        values = {"root_dir": str(self.copies).replace("\\", "/"), "keep": 5, **state_backup}
        lines = "\n".join(
            f"{key} = {json.dumps(value)}" if not isinstance(value, bool) else f"{key} = {str(value).lower()}"
            for key, value in values.items()
        )
        path = self.root / "config.toml"
        workspace = (self.root / "workspace").as_posix()
        path.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-a"

            [paths]
            workspace_root_dir = "{workspace}"
            local_state_dir = "{self.codex.as_posix()}"
            cloud_root_dir = "${{workspace_root}}/sync"
            backup_dir = "${{workspace_root}}/backups"
            temp_dir = "${{workspace_root}}/.tmp"

            [state_backup]
        """) + lines + "\n", encoding="utf-8")
        return path

    def _backup(self, gate=None, **kwargs):
        cfg = load_config(self.config_path)
        clock = _Clock()
        kwargs.setdefault("monotonic", clock.monotonic)
        kwargs.setdefault("sleep", clock.sleep)
        return create_state_backup(cfg, gate=gate or _Gate(), **kwargs), clock

    # --- what is taken ---------------------------------------------------------

    def test_the_valuable_files_are_taken_and_no_secret_ever(self) -> None:
        chosen = [relative for relative, _ in select_state_files(self.codex)]
        self.assertIn("sessions/2026/09/25/rollout-a.jsonl", chosen)
        self.assertIn("archived_sessions/rollout-b.jsonl", chosen)
        self.assertIn(".codex-global-state.json", chosen)
        self.assertIn("state_5.sqlite", chosen)
        self.assertIn("state_5.sqlite-wal", chosen)
        self.assertIn("skills/mine/SKILL.md", chosen)
        for absent in ("auth.json", "cap_sid", ".sandbox-secrets/key", "skills/mine/auth.json",
                       "cache/blob", "logs_2.sqlite", ".tmp/x", "sessions/half.jsonl.tmp"):
            self.assertNotIn(absent, chosen)

    def test_a_junction_inside_codex_is_not_followed(self) -> None:
        """CS-301: `skills` holds junctions into folders outside `.codex`."""
        if os.name != "nt":
            self.skipTest("junctions exist only on Windows")
        import _winapi

        elsewhere = self.root / "elsewhere"
        _write(elsewhere / "not-ours.md", "someone else's file")
        link = self.codex / "skills" / "linked"
        _winapi.CreateJunction(str(elsewhere), str(link))
        self.addCleanup(os.rmdir, link)  # the junction itself, never its target
        chosen = [relative for relative, _ in select_state_files(self.codex)]
        self.assertIn("skills/mine/SKILL.md", chosen)
        self.assertFalse([item for item in chosen if item.startswith("skills/linked")])

    def test_a_junction_tag_is_a_link_on_every_python(self) -> None:
        """The rule does not rely on ``os.path.isjunction`` (3.12+ only)."""
        from types import SimpleNamespace
        import stat as stat_module

        from codexsync.fs_links import FILE_ATTRIBUTE_REPARSE_POINT, is_link

        junction = SimpleNamespace(
            st_mode=stat_module.S_IFDIR, st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=0xA0000003,
        )
        with mock.patch("os.path.isjunction", None, create=True):
            self.assertTrue(is_link(self.root / "missing", junction))

    def test_a_folder_that_cannot_be_listed_fails_the_selection(self) -> None:
        """CS-301: `os.walk` drops an unreadable folder silently unless told not to."""
        def walk(top, topdown=True, onerror=None, followlinks=False):
            if onerror is not None:
                onerror(PermissionError(13, "Access is denied", str(top)))
            return iter(())

        with mock.patch("codexsync.state_backup.os.walk", side_effect=walk):
            with self.assertRaises(FailSafeError):
                select_state_files(self.codex)

    def test_a_copy_is_a_verified_zip_with_a_manifest(self) -> None:
        (result, _clock) = self._backup()
        self.assertTrue(result.path.is_file())
        self.assertRegex(result.path.name, r"^codex-machine-a-\d{8}T\d{6}Z\.zip$")
        with zipfile.ZipFile(result.path) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read(ARCHIVE_MANIFEST))
            self.assertEqual(archive.read("sessions/2026/09/25/rollout-a.jsonl"), b'{"a":1}\n')
        self.assertNotIn("auth.json", names)
        self.assertEqual(manifest["files"], result.files)
        self.assertEqual(manifest["machine"], "machine-a")
        self.assertEqual({entry["path"] for entry in manifest["entries"]} | {ARCHIVE_MANIFEST}, names)
        self.assertEqual(list(self.copies.glob("*.partial")), [])

    def test_the_state_directory_is_never_written(self) -> None:
        before = sorted(path.relative_to(self.codex) for path in self.codex.rglob("*"))
        with forbid_writes_under(self.codex):
            self._backup()
        self.assertEqual(sorted(path.relative_to(self.codex) for path in self.codex.rglob("*")), before)

    # --- only while Codex is closed ---------------------------------------------

    def test_codex_open_refuses_without_wait(self) -> None:
        with self.assertRaises(SafetyPreconditionError):
            self._backup(_Gate(ProcessState.RUNNING))
        self.assertFalse(self.copies.exists() and any(self.copies.iterdir()))

    def test_with_wait_the_copy_waits_for_codex_to_close(self) -> None:
        gate = _Gate(ProcessState.RUNNING, ProcessState.RUNNING, ProcessState.STOPPED)
        result, clock = self._backup(gate, wait=True, poll_seconds=30)
        self.assertEqual(clock.sleeps, [30, 30])
        self.assertEqual(result.waited_seconds, 60)
        self.assertTrue(result.path.is_file())

    def test_the_wait_gives_up_and_says_so(self) -> None:
        with self.assertRaises(SafetyPreconditionError) as caught:
            self._backup(_Gate(ProcessState.RUNNING), wait=True, poll_seconds=30, max_wait_seconds=90)
        self.assertIn("stayed open", str(caught.exception))

    def test_an_undetermined_process_state_is_never_waited_out(self) -> None:
        with self.assertRaises(FailSafeError):
            self._backup(_Gate(ProcessState.UNKNOWN), wait=True)

    def test_codex_opening_during_the_copy_keeps_nothing(self) -> None:
        gate = _Gate()
        gate.final_state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            self._backup(gate)
        self.assertEqual(list(self.copies.iterdir()), [])

    def test_the_real_gate_class_is_what_the_profile_asks_for(self) -> None:
        """STATE_BACKUP needs a stopped Codex although it mutates nothing."""
        gate = SafetyGate(lambda: ProcessState.RUNNING, monotonic=lambda: 0.0, sleep=lambda _s: None)
        self.assertFalse(gate.check(OperationKind.STATE_BACKUP).allowed)

    # --- a torn copy is no copy -------------------------------------------------

    def test_a_file_that_changes_while_it_is_read_fails_the_copy(self) -> None:
        target = self.codex / "state_5.sqlite"
        real_stat = Path.stat
        seen = {"count": 0}

        def moving_stat(path: Path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if Path(path) == target:
                seen["count"] += 1
                if seen["count"] >= 3:
                    changed = mock.Mock(wraps=info)
                    changed.st_size = info.st_size + 1
                    return changed
            return info

        with mock.patch.object(Path, "stat", moving_stat):
            with self.assertRaises(FailSafeError) as caught:
                self._backup()
        self.assertIn("state_5.sqlite", str(caught.exception))
        self.assertEqual(list(self.copies.iterdir()), [])

    def test_a_copy_that_does_not_read_back_is_not_committed(self) -> None:
        from codexsync import state_backup

        with mock.patch.object(state_backup, "_verify", side_effect=FailSafeError("mismatch")):
            with self.assertRaises(FailSafeError):
                self._backup()
        self.assertEqual(list(self.copies.iterdir()), [])

    def test_a_changed_source_set_keeps_the_previous_readable_copy(self) -> None:
        self.config_path = self._config(keep=1)
        start = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        session = self.codex / "sessions" / "2026" / "09" / "25" / "rollout-a.jsonl"
        wal = self.codex / "state_5.sqlite-wal"
        shm = self.codex / "state_5.sqlite-shm"

        def replace_session() -> None:
            info = session.stat()
            replacement = session.with_suffix(".tmp")
            _write(replacement, '{"a":2}\n')
            os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
            replacement.replace(session)

        cases = (
            ("session disappeared", session.unlink, lambda: _write(session, '{"a":1}\n'), 0),
            ("WAL disappeared", wal.unlink, lambda: _write(wal, b"wal"), 0),
            ("SHM appeared", lambda: _write(shm, b"shm"), shm.unlink, 1),
            ("session replaced", replace_session, lambda: _write(session, '{"a":1}\n'), 1),
        )
        for step, (name, change, undo, when) in enumerate(cases):
            with self.subTest(name=name):
                created = start + timedelta(hours=2 * step)
                previous, _ = self._backup(now=lambda: created)
                changed = False

                def progress(_phase, done, total):
                    nonlocal changed
                    if not changed and done == when * total:
                        changed = True
                        change()

                try:
                    with self.assertRaises(FailSafeError):
                        self._backup(now=lambda: created + timedelta(hours=1), progress=progress)
                finally:
                    if changed:
                        undo()
                self.assertEqual(list(self.copies.iterdir()), [previous.path])
                with zipfile.ZipFile(previous.path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read("state_5.sqlite-wal"), b"wal")

    # --- keeping N ------------------------------------------------------------

    def test_only_the_newest_copies_of_this_machine_are_kept(self) -> None:
        self.config_path = self._config(keep=2)
        self.copies.mkdir(parents=True)
        other = self.copies / "codex-machine-b-20260101T000000Z.zip"
        other.write_bytes(b"not ours")
        unrelated = self.copies / "notes.txt"
        unrelated.write_text("mine")
        start = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        made = []
        for step in range(3):
            result, _ = self._backup(now=lambda step=step: start + timedelta(hours=step))
            made.append(result.path.name)
        remaining = sorted(path.name for path in self.copies.iterdir())
        self.assertEqual(remaining, sorted([made[1], made[2], other.name, unrelated.name]))
        listed = list_state_backups(load_config(self.config_path))
        self.assertEqual([entry.name for entry in listed][:2], [made[2], made[1]])
        self.assertEqual([entry.own for entry in listed], [True, True, False])

    def test_a_partial_left_by_a_killed_run_is_swept(self) -> None:
        self.copies.mkdir(parents=True)
        left = self.copies / "codex-machine-a-20260101T000000Z.zip.partial"
        left.write_bytes(b"half")
        foreign = self.copies / "codex-machine-b-20260101T000000Z.zip.partial"
        foreign.write_bytes(b"half")
        self._backup()
        self.assertFalse(left.exists())
        self.assertTrue(foreign.exists(), "another machine's copy in progress is not ours to remove")

    # --- configuration ------------------------------------------------------------

    def test_no_folder_no_copy(self) -> None:
        self.config_path = self._config(root_dir="")
        with self.assertRaises(ConfigError):
            self._backup()

    def test_a_schedule_without_a_folder_is_refused(self) -> None:
        self.config_path = self._config(root_dir="", at_login=True)
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_the_folder_may_not_sit_where_something_prunes_or_mirrors(self) -> None:
        for inside in (
            str(self.codex / "copies"),
            "${workspace_root}/backups/codex",
            "${workspace_root}/.tmp/codex",
            "${workspace_root}/sync/codex",
        ):
            with self.subTest(inside=inside):
                self.config_path = self._config(root_dir=inside.replace("\\", "/"))
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)

    def test_values_are_checked_not_coerced(self) -> None:
        for bad in ({"at_login": "yes"}, {"interval_hours": -1}, {"keep": 0}, {"interval_hours": True}):
            with self.subTest(bad=bad):
                self.config_path = self._config(**bad)
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)

    def test_the_interval_stops_at_what_the_os_scheduler_repeats(self) -> None:
        """CS-277: Task Scheduler refuses a repetition over 31 days at registration."""
        self.config_path = self._config(interval_hours=31 * 24)
        self.assertEqual(load_config(self.config_path).state_backup.interval_hours, 744)
        self.config_path = self._config(interval_hours=31 * 24 + 1)
        with self.assertRaisesRegex(ConfigError, "interval_hours"):
            load_config(self.config_path)

    def test_the_folder_is_checked_against_the_state_folder_actually_read(self) -> None:
        """CS-278: validation sees the configured folder; the locator may fall back.

        With the configured `.codex` missing, the locator picks CODEX_HOME or
        ~/.codex, which validation never compared the copy folder with.
        """
        inside = self.codex / "copies"
        text = self._config(root_dir=inside.as_posix()).read_text(encoding="utf-8")
        text = text.replace(self.codex.as_posix(), (self.root / "missing-codex").as_posix(), 1)
        self.config_path.write_text(text, encoding="utf-8")
        cfg = load_config(self.config_path)
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(self.codex)}), forbid_writes_under(self.codex):
            with self.assertRaisesRegex(ConfigError, "state_backup.root_dir .* Codex state folder in use"):
                create_state_backup(cfg, gate=_Gate())
        self.assertFalse(inside.exists())

    def test_an_odd_file_in_the_folder_does_not_hide_the_copies(self) -> None:
        """CS-283: a name that looks like a copy but holds no real time is skipped."""
        (result, _clock) = self._backup(now=lambda: datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc))
        _write(self.copies / "codex-machine-a-20261399T999999Z.zip", "not a copy")
        listed = list_state_backups(load_config(self.config_path))
        self.assertEqual([entry.name for entry in listed], [result.path.name])
        # ...and pruning never counts or removes it.
        self.config_path = self._config(keep=1)
        self._backup(now=lambda: datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc))
        self.assertTrue((self.copies / "codex-machine-a-20261399T999999Z.zip").exists())


if __name__ == "__main__":
    unittest.main()


class StateBackupCliTests(unittest.TestCase):
    """`codexsync state-backup create|list` and the exit codes it maps to."""

    def _run(self, argv):
        import contextlib
        import io

        from codexsync.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, out.getvalue()

    def test_create_passes_wait_and_prints_the_copy(self) -> None:
        from codexsync.state_backup import StateBackupResult

        result = StateBackupResult(Path("C:/copies/codex-a-20260925T070000Z.zip"), 12, 3456, 90.0, ("old.zip",))
        with mock.patch("codexsync.cli.create_codex_backup", return_value=result) as create, \
                mock.patch("codexsync.cli._require_config_path", return_value=Path("cfg.toml")), \
                mock.patch("codexsync.cli._warn_about_outdated_config"), \
                mock.patch("codexsync.cli.configure_logging", create=True):
            code, out = self._run(["-c", "cfg.toml", "state-backup", "create", "--wait"])
        self.assertEqual(code, 0)
        self.assertTrue(create.call_args.kwargs["wait"])
        self.assertIn("codex-a-20260925T070000Z.zip", out)
        self.assertIn("removed old copy: old.zip", out)

    def test_codex_open_is_exit_three(self) -> None:
        with mock.patch("codexsync.cli.create_codex_backup", side_effect=SafetyPreconditionError("open")), \
                mock.patch("codexsync.cli._require_config_path", return_value=Path("cfg.toml")), \
                mock.patch("codexsync.cli._warn_about_outdated_config"):
            code, _ = self._run(["-c", "cfg.toml", "state-backup", "create"])
        self.assertEqual(code, 3)
