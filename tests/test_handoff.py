"""Handing work from one machine to the next (CS-328, `D-018`).

Two machines share one workspace, as they do through a cloud client: one
cloud copy, one manifest, one handoff folder, and a `.codex` each. Nothing
here waits on a real clock or reads the real process list: the gate is
replaced where `app` calls it, and time is injected.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import textwrap
import unittest
from unittest import mock
import uuid

from codexsync.app import (
    HandoffNotDelivered,
    HandoffResult,
    handoff_status,
    run_handoff,
    watch_handoff,
)
from codexsync import app as app_module
from codexsync.config import load_config
from codexsync.exceptions import ConfigError, ConflictError, SafetyPreconditionError
from codexsync.handoff import (
    STATE_HANDED_OFF,
    STATE_WORKING,
    FileMark,
    Fingerprinter,
    HandoffRecord,
    delivery,
    mark_working,
    read_board,
    record_handoff,
    record_path,
    write_record,
)
from codexsync.notifications import NOTIFY_KEYS, Notifier, message, notify
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class _Gate:
    def __init__(self, state: ProcessState = ProcessState.STOPPED) -> None:
        self.state = state

    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        allowed = self.state is ProcessState.STOPPED
        return SafetyDecision(operation, self.state, allowed, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        if self.state is ProcessState.RUNNING:
            raise SafetyPreconditionError("Codex is running")
        return self.check(operation, final=final)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"handoff-{uuid.uuid4().hex[:8]}"
        self.workspace = self.root / "workspace"
        self.cloud = self.workspace / "sync"
        self.handoff_root = self.workspace / "handoff"
        self.cloud.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.gate = _Gate()
        patcher = mock.patch("codexsync.app._make_safety_gate", side_effect=lambda cfg: self.gate)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Project file hashes are cached per machine; never in the real one here.
        cache = mock.patch("codexsync.app._project_hash_cache_root", return_value=self.root / "hash-cache")
        cache.start()
        self.addCleanup(cache.stop)

    def machine(self, name: str, *, conflict_policy: str | None = None, **handoff: object) -> tuple[Path, Path]:
        """A `.codex` and a config for machine ``name`` in the shared workspace."""
        local = self.root / name / "codex"
        local.mkdir(parents=True, exist_ok=True)
        extra = "".join(f"{key} = {json.dumps(value)}\n" for key, value in handoff.items())
        config = self.root / name / "config.toml"
        config.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "{name}"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            workspace_root_dir = "{self.workspace.as_posix()}"
            local_state_dir = "{local.as_posix()}"
            cloud_root_dir = "{self.cloud.as_posix()}"
            backup_dir = "{(self.workspace / 'backups').as_posix()}"
            temp_dir = "{(self.workspace / '.tmp').as_posix()}"

            [guardian]
            root_dir = "{(self.workspace / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(self.workspace / 'semantic').as_posix()}"

            [targets]
            include_roots = ["sessions", "skills"]

            [backup]
            backup_before_overwrite = true
            compression = "none"

            [state]
            manifest_file = "{(self.workspace / 'manifest.json').as_posix()}"

            [conflict]
            policy = "{conflict_policy or 'prefer_newer_mtime'}"
        """).strip() + f"""

[handoff]
root_dir = "{self.handoff_root.as_posix()}"
{extra}""", encoding="utf-8")
        return config, local

    @staticmethod
    def write(path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def session(root: Path, session_id: str, records: int = 1) -> Path:
        path = root / "sessions" / "2026" / "09" / "27" / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session_meta", "payload": {"id": session_id, "cwd": "C:/work"}}]
        rows += [{"type": "event", "n": index} for index in range(records)]
        path.write_bytes(b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows))
        return path


class HandoffRecordTests(_Workspace):
    def test_a_record_reads_back_exactly(self) -> None:
        record = HandoffRecord(
            "laptop", STATE_HANDED_OFF, "2026-09-27T10:00:00Z", "abc", 3, "2026-09-27T10:00:00Z",
            {"desktop": "def"}, {"k": FileMark(5, "0" * 64)},
        )
        write_record(self.handoff_root, record)
        board = read_board(self.handoff_root)
        self.assertEqual(board.records, {"laptop": record})
        self.assertEqual(board.unreadable, {})
        leftovers = [path.name for path in self.handoff_root.iterdir() if path.name != "laptop.json"]
        self.assertEqual(leftovers, [], "the staging file must not stay behind")

    def test_a_half_delivered_file_is_not_believed(self) -> None:
        write_record(self.handoff_root, HandoffRecord("laptop", STATE_WORKING, "t"))
        path = record_path(self.handoff_root, "laptop")
        path.write_text(path.read_text(encoding="utf-8").replace("working", "handed_off"), encoding="utf-8")
        board = read_board(self.handoff_root)
        self.assertEqual(board.records, {})
        self.assertIn("laptop.json", board.unreadable)

    def test_a_copy_under_another_machines_name_is_not_adopted(self) -> None:
        write_record(self.handoff_root, HandoffRecord("laptop", STATE_WORKING, "t"))
        shutil.copyfile(record_path(self.handoff_root, "laptop"), self.handoff_root / "desktop.json")
        board = read_board(self.handoff_root)
        self.assertEqual(set(board.records), {"laptop"})
        self.assertIn("desktop.json", board.unreadable)

    def test_pending_is_decided_by_id_never_by_time(self) -> None:
        write_record(self.handoff_root, HandoffRecord("desktop", STATE_HANDED_OFF, "2000-01-01T00:00:00Z", "d1"))
        write_record(self.handoff_root, HandoffRecord(
            "laptop", STATE_HANDED_OFF, "2099-01-01T00:00:00Z", "l1", accepted={"desktop": "d0"},
        ))
        board = read_board(self.handoff_root)
        self.assertEqual([record.machine for record in board.pending("laptop")], ["desktop"])
        write_record(self.handoff_root, HandoffRecord(
            "laptop", STATE_HANDED_OFF, "1990-01-01T00:00:00Z", "l1", accepted={"desktop": "d1"},
        ))
        self.assertEqual(read_board(self.handoff_root).pending("laptop"), [])

    def test_a_run_that_handed_nothing_off_keeps_the_previous_handoff(self) -> None:
        first = record_handoff(self.handoff_root, "laptop", files={"k": FileMark(1, "a")}, taken=())
        again = record_handoff(self.handoff_root, "laptop", files={"k": FileMark(2, "b")}, taken=(), new_handoff=False)
        self.assertEqual(again.handoff_id, first.handoff_id)
        self.assertEqual(again.files, first.files)
        self.assertEqual(again.generation, first.generation)
        fresh = record_handoff(self.handoff_root, "laptop", files={"k": FileMark(2, "b")}, taken=())
        self.assertNotEqual(fresh.handoff_id, first.handoff_id)
        self.assertEqual(fresh.generation, first.generation + 1)

    def test_marking_working_keeps_the_last_handoff(self) -> None:
        handed = record_handoff(self.handoff_root, "laptop", files={"k": FileMark(1, "a")}, taken=())
        working = mark_working(self.handoff_root, "laptop")
        self.assertEqual(working.state, STATE_WORKING)
        self.assertEqual((working.handoff_id, working.files), (handed.handoff_id, handed.files))


class DeliveryTests(_Workspace):
    def test_every_file_must_match_and_extra_files_do_not_matter(self) -> None:
        config, _ = self.machine("laptop")
        cfg = load_config(config)
        self.write(self.cloud / "skills" / "a.md", "one")
        fingerprints = Fingerprinter()
        record = HandoffRecord("desktop", STATE_HANDED_OFF, "t", "d1", files=fingerprints.files(cfg))
        self.assertTrue(delivery(record, fingerprints.files(cfg)).delivered)
        self.write(self.cloud / "skills" / "b.md", "extra")
        self.assertTrue(delivery(record, fingerprints.files(cfg)).delivered)
        self.write(self.cloud / "skills" / "a.md", "different")
        late = delivery(record, fingerprints.files(cfg))
        self.assertEqual((late.delivered, late.differing, late.missing), (False, 1, 0))
        (self.cloud / "skills" / "a.md").unlink()
        self.assertEqual(delivery(record, fingerprints.files(cfg)).missing, 1)

    def test_secrets_and_staging_files_are_never_fingerprinted(self) -> None:
        config, _ = self.machine("laptop")
        cfg = load_config(config)
        self.write(self.cloud / "skills" / "auth.json", "{}")
        self.write(self.cloud / "skills" / ".a.md.x.codexsync.tmp", "half")
        self.assertEqual(Fingerprinter().files(cfg), {})

    def test_the_fingerprint_names_no_path(self) -> None:
        config, _ = self.machine("laptop")
        self.write(self.cloud / "skills" / "secret-project-name.md", "x")
        keys = list(Fingerprinter().files(load_config(config)))
        self.assertEqual(len(keys), 1)
        self.assertNotIn("secret", keys[0])


class RunHandoffTests(_Workspace):
    def test_one_machine_hands_off_and_the_next_loads_it(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.write(desktop_codex / "skills" / "tool.md", "from the desktop")
        self.session(desktop_codex, "11111111-1111-1111-1111-111111111111")

        sent = run_handoff(desktop)
        self.assertTrue(sent.handed_off)
        self.assertEqual(sent.taken, ())
        self.assertTrue((self.cloud / "skills" / "tool.md").is_file())
        mirrored = list((self.cloud / "sessions").rglob("rollout-*"))
        self.assertEqual(len(mirrored), 1, "the chat goes to the cloud copy")

        loaded = run_handoff(laptop)
        self.assertEqual(loaded.taken, ("desktop",))
        self.assertEqual((laptop_codex / "skills" / "tool.md").read_text(encoding="utf-8"), "from the desktop")
        self.assertFalse(loaded.handed_off, "loading hands nothing new off")
        board = read_board(self.handoff_root)
        self.assertEqual(board.records["laptop"].accepted, {"desktop": sent.record.handoff_id})
        self.assertEqual(board.pending("laptop"), [])
        self.assertEqual(
            board.pending("desktop"), [], "a machine that handed nothing off leaves nothing to wait for"
        )

    def test_a_later_step_that_fails_does_not_cost_the_handoff(self) -> None:
        # A peer's names file arriving mid-run once made the names step refuse
        # its own plan after settings and chats were in the cloud, so no record
        # was written and nobody was told of the work that had been delivered.
        desktop, desktop_codex = self.machine("desktop")
        self.session(desktop_codex, "11111111-1111-1111-1111-111111111111")
        failing = mock.patch(
            "codexsync.app.sync_chat_names", side_effect=ConfigError("chat names changed since then"),
        )
        with failing, self.assertLogs("codexsync", level="ERROR"):
            sent = run_handoff(desktop)
        self.assertTrue(sent.handed_off)
        self.assertEqual(sent.steps_not_done, ("chat_names",))
        self.assertEqual(sent.chat_names_set, 0)
        board = read_board(self.handoff_root)
        self.assertEqual(board.records["desktop"].handoff_id, sent.record.handoff_id, "the record is written")
        self.assertEqual(
            board.pending("laptop")[0].machine, "desktop", "the other machine learns of the handoff"
        )

    def test_a_full_preview_sees_files_and_chats_and_writes_nothing(self) -> None:
        # `sync --dry-run` at `[sync] scope = "full"` (D-028).
        desktop, desktop_codex = self.machine("desktop")
        self.write(desktop_codex / "skills" / "tool.md", "from the desktop")
        self.session(desktop_codex, "11111111-1111-1111-1111-111111111111")
        preview = app_module.preview_full_sync(desktop)
        self.assertEqual(preview.machine, "desktop")
        self.assertEqual(preview.files.plan.action_count, 1, "the skill file")
        self.assertEqual(
            sum(1 for item in preview.chats.items if app_module.transfer_direction(item) == "mirror"), 1,
        )
        # As `sync --dry-run` always has, codexSync's own empty folders and
        # manifest may be prepared; no file and no chat is copied.
        copied = [path for path in self.cloud.rglob("*") if path.is_file()]
        self.assertEqual(copied, [], "nothing copied into the cloud folder")
        self.assertEqual(read_board(self.handoff_root).records, {}, "no handoff is recorded")

    def test_each_later_step_builds_its_plan_once(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        self.session(desktop_codex, "11111111-1111-1111-1111-111111111111")
        calls = {}
        patches = []
        for name in ("sync_projects", "sync_chat_names", "refresh_thread_catalogue"):
            real = getattr(app_module, name)
            spy = mock.patch(f"codexsync.app.{name}", side_effect=real)
            calls[name] = spy.start()
            patches.append(spy)
        try:
            sent = run_handoff(desktop)
        finally:
            for spy in patches:
                spy.stop()
        self.assertEqual(sent.steps_not_done, ())
        for name, spy in calls.items():
            self.assertEqual(spy.call_count, 1, name)
            self.assertIs(spy.call_args.kwargs["planned_here"], True, name)

    def test_a_chat_started_on_the_other_machine_is_carried_by_default(self) -> None:
        # D-020 amendment (2026-10-03): carrying new chats is the job, so a
        # config without a `new_chats` line writes them into `.codex`.
        desktop, desktop_codex = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        self.session(desktop_codex, "22222222-2222-2222-2222-222222222222")
        run_handoff(desktop)
        loaded = run_handoff(laptop)
        self.assertEqual(loaded.new_chats_written, 1)
        self.assertEqual(loaded.chats_not_loaded, 0)

    def test_a_chat_that_cannot_be_placed_in_codex_is_counted_not_called_loaded(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        laptop.write_text(
            laptop.read_text(encoding="utf-8").replace("[semantic]\n", '[semantic]\nnew_chats = "keep_in_cloud"\n', 1),
            encoding="utf-8",
        )
        self.session(desktop_codex, "22222222-2222-2222-2222-222222222222")
        run_handoff(desktop)
        loaded = run_handoff(laptop)
        # Kept in the cloud by choice: the chat stays in the mirror, and says so.
        self.assertEqual(loaded.chats_not_loaded, 1)
        self.assertEqual(loaded.new_chats_kept_in_cloud, 1)

    def test_a_chat_continued_on_the_other_machine_is_loaded_in_place(self) -> None:
        # CS-330a: both machines hold the chat, the laptop's catalogue names its
        # file, and the continuation made on the desktop replaces that file.
        session_id = "33333333-3333-3333-3333-333333333333"
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.session(desktop_codex, session_id, records=1)
        here = self.session(laptop_codex, session_id, records=1)
        connection = sqlite3.connect(laptop_codex / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (session_id, str(here)))
            connection.commit()
        finally:
            connection.close()
        run_handoff(desktop)
        run_handoff(laptop)

        continued = self.session(desktop_codex, session_id, records=3)
        run_handoff(desktop)
        loaded = run_handoff(laptop)
        self.assertEqual(loaded.chats_not_loaded, 0)
        self.assertEqual(loaded.session_actions, 1)
        self.assertEqual(here.read_bytes(), continued.read_bytes())
        self.assertEqual(
            [path.name for path in here.parent.iterdir()], [here.name], "one file for one chat"
        )

    def test_the_other_machine_is_known_without_any_path_rule(self) -> None:
        # CS-345: the laptop's config had no [[path_mappings]], so the Sessions
        # page opened by hand had no other machine to offer.
        from codexsync.app import known_machines

        desktop, _ = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        self.assertEqual(known_machines(laptop).usual_source, None, "nobody has handed off yet")
        run_handoff(desktop)
        known = known_machines(laptop)
        self.assertIn("desktop", known.names)
        self.assertIn("laptop", known.names)
        self.assertEqual(known.usual_source, "desktop", "the pair the full sync uses")

    def test_a_chat_archived_on_one_machine_is_archived_on_the_other(self) -> None:
        # D-023: the laptop run of 2026-10-02 stopped for good on chats the
        # desktop had archived, with nothing to decide anywhere.
        session_id = "55555555-5555-5555-5555-555555555555"
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        there = self.session(desktop_codex, session_id, records=1)
        here = self.session(laptop_codex, session_id, records=1)
        connection = sqlite3.connect(laptop_codex / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (session_id, str(here)))
            connection.commit()
        finally:
            connection.close()
        run_handoff(desktop)
        run_handoff(laptop)

        archived = desktop_codex / "archived_sessions" / there.name
        archived.parent.mkdir(parents=True)
        there.replace(archived)
        run_handoff(desktop)
        mirrored = sorted(path.relative_to(self.cloud).as_posix() for path in self.cloud.rglob("rollout-*"))
        self.assertEqual(len(mirrored), 1, mirrored)
        self.assertTrue(mirrored[0].startswith("archived_sessions/"), "the mirror follows the desktop")

        loaded = run_handoff(laptop)
        self.assertEqual(loaded.chats_not_loaded, 0)
        self.assertTrue((laptop_codex / "archived_sessions" / here.name).is_file())
        self.assertFalse(here.exists(), "one file for one chat")

    def test_a_diverged_chat_stops_the_sync_naming_the_pair_and_the_kind(self) -> None:
        # CS-342: the window needs to know which pair and what kind of decision.
        from codexsync.exceptions import ChatDecisionsNeeded

        session_id = "44444444-4444-4444-4444-444444444444"
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.session(desktop_codex, session_id, records=1)
        run_handoff(desktop)
        # The laptop holds a different history of the same chat.
        path = self.session(laptop_codex, session_id, records=0)
        path.write_bytes(path.read_bytes() + b'{"type": "event", "n": "laptop"}\n')
        with self.assertRaises(ChatDecisionsNeeded) as caught:
            run_handoff(laptop)
        self.assertEqual((caught.exception.source, caught.exception.target), ("desktop", "laptop"))
        self.assertEqual(caught.exception.divergences, 1)
        self.assertIsInstance(caught.exception, ConflictError, "still exit 2 on the command line")

    def test_an_undelivered_handoff_is_waited_for_then_refused_with_nothing_written(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop", delivery_wait_minutes=1)
        self.write(desktop_codex / "skills" / "tool.md", "from the desktop")
        run_handoff(desktop)
        # The cloud client has not brought the file here yet.
        (self.cloud / "skills" / "tool.md").unlink()
        clock = _Clock()
        seen: list = []
        with self.assertRaises(HandoffNotDelivered) as caught:
            run_handoff(laptop, monotonic=clock.monotonic, sleep=clock.sleep, on_wait=seen.append)
        self.assertEqual(caught.exception.deliveries[0].missing, 1)
        self.assertGreaterEqual(clock.now, 60.0)
        self.assertTrue(seen, "the wait is reported while it lasts")
        self.assertFalse((laptop_codex / "skills").exists(), "nothing was loaded")
        self.assertNotIn("laptop", read_board(self.handoff_root).records)

    def test_a_handoff_that_arrives_while_waiting_is_loaded(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.write(desktop_codex / "skills" / "tool.md", "from the desktop")
        run_handoff(desktop)
        moved = self.cloud / "skills" / "tool.md"
        held = moved.read_bytes()
        moved.unlink()
        clock = _Clock()

        def arrive(seconds: float) -> None:
            clock.sleep(seconds)
            moved.write_bytes(held)

        result = run_handoff(laptop, monotonic=clock.monotonic, sleep=arrive)
        self.assertEqual(result.taken, ("desktop",))
        self.assertGreater(result.waited_seconds, 0)
        self.assertTrue((laptop_codex / "skills" / "tool.md").is_file())

    def test_accept_undelivered_loads_anyway_when_asked(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        self.write(desktop_codex / "skills" / "tool.md", "one")
        run_handoff(desktop)
        (self.cloud / "skills" / "tool.md").write_text("changed by someone else", encoding="utf-8")
        result = run_handoff(laptop, wait_seconds=0, accept_undelivered=True)
        self.assertEqual(result.taken, ("desktop",))

    def test_a_conflict_stops_the_handoff_before_any_write(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop", conflict_policy="manual_abort")
        self.write(desktop_codex / "skills" / "tool.md", "base")
        run_handoff(desktop)
        run_handoff(laptop)
        # Both machines changed the same file since they last agreed.
        self.write(desktop_codex / "skills" / "tool.md", "desktop edit")
        self.write(laptop_codex / "skills" / "tool.md", "laptop edit")
        run_handoff(desktop)
        before = read_board(self.handoff_root).records["laptop"]
        with self.assertRaises(ConflictError):
            run_handoff(laptop)
        self.assertEqual((laptop_codex / "skills" / "tool.md").read_text(encoding="utf-8"), "laptop edit")
        self.assertEqual(read_board(self.handoff_root).records["laptop"], before, "no handoff was recorded")

    def test_codex_open_refuses_before_waiting_or_writing(self) -> None:
        laptop, _ = self.machine("laptop")
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            run_handoff(laptop)
        self.assertFalse(self.handoff_root.exists())

    def test_without_a_handoff_folder_it_sits_beside_the_manifest(self) -> None:
        # CS-335: the shared folder is already chosen; a second one is never asked for.
        config, _ = self.machine("laptop")
        text = config.read_text(encoding="utf-8").replace(f'root_dir = "{self.handoff_root.as_posix()}"', 'root_dir = ""')
        config.write_text(text, encoding="utf-8")
        self.assertEqual(handoff_status(config).root, self.workspace / "handoff")
        run_handoff(config)
        self.assertTrue((self.workspace / "handoff" / "laptop.json").is_file())

    def test_without_a_handoff_folder_or_a_manifest_there_is_no_handoff(self) -> None:
        config, _ = self.machine("laptop")
        text = config.read_text(encoding="utf-8").replace(f'root_dir = "{self.handoff_root.as_posix()}"', 'root_dir = ""')
        manifest = (self.workspace / "manifest.json").as_posix()
        text = text.replace(f'manifest_file = "{manifest}"', "")
        config.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            handoff_status(config)

    def test_status_reports_what_is_pending_and_how_much_arrived(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        self.write(desktop_codex / "skills" / "tool.md", "x")
        run_handoff(desktop)
        mark_working(self.handoff_root, "desktop")
        status = handoff_status(laptop, check_delivery=True)
        self.assertEqual(status.pending, ("desktop",))
        self.assertEqual(status.working_elsewhere, ("desktop",))
        self.assertTrue(status.deliveries[0].delivered)


class WatchTests(_Workspace):
    def _watch(self, states: list[ProcessState], outcome=None):
        config, _ = self.machine("laptop")
        calls: list[str] = []
        notes: list[tuple[str, dict]] = []
        sequence = iter(states)
        remaining = [len(states) - 1]

        def act(path):
            calls.append("handoff")
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or HandoffResult("laptop", (), 0, 0, True, HandoffRecord("laptop", STATE_HANDED_OFF, "t"))

        def stop() -> bool:
            remaining[0] -= 1
            return remaining[0] < 0

        watch_handoff(
            config, probe=lambda: next(sequence), handoff=act,
            notifier=lambda key, **values: notes.append((key, values)),
            sleep=lambda seconds: None, should_stop=stop,
        )
        return calls, notes

    def test_a_start_with_codex_closed_loads(self) -> None:
        calls, _ = self._watch([ProcessState.STOPPED])
        self.assertEqual(calls, ["handoff"])

    def test_closing_codex_hands_off_and_says_so(self) -> None:
        calls, notes = self._watch([ProcessState.RUNNING, ProcessState.RUNNING, ProcessState.STOPPED])
        self.assertEqual(calls, ["handoff"])
        self.assertEqual(notes[-1][0], "notify.handed_off")
        self.assertEqual(read_board(self.handoff_root).records["laptop"].state, STATE_WORKING)

    def test_an_undetermined_state_is_neither_a_start_nor_a_close(self) -> None:
        calls, _ = self._watch([ProcessState.RUNNING, ProcessState.UNKNOWN, ProcessState.UNKNOWN])
        self.assertEqual(calls, [])
        calls, _ = self._watch([ProcessState.UNKNOWN, ProcessState.STOPPED])
        self.assertEqual(calls, [], "a close is only a close after a start that was seen")

    def test_starting_codex_warns_about_work_not_handed_off_elsewhere(self) -> None:
        write_record(self.handoff_root, HandoffRecord("desktop", STATE_WORKING, "2026-09-27T08:00:00Z", "d1"))
        _, notes = self._watch([ProcessState.RUNNING])
        keys = [key for key, _ in notes]
        self.assertIn("notify.working_elsewhere", keys)
        self.assertEqual(dict(notes)["notify.working_elsewhere"]["machine"], "desktop")

    def test_starting_codex_warns_about_a_handoff_not_loaded_here(self) -> None:
        write_record(self.handoff_root, HandoffRecord(
            "desktop", STATE_HANDED_OFF, "t", "d1", 1, "2026-09-27T08:00:00Z",
        ))
        _, notes = self._watch([ProcessState.RUNNING])
        self.assertEqual([key for key, _ in notes], ["notify.not_taken"])

    def test_every_refusal_is_reported_and_the_watcher_goes_on(self) -> None:
        from codexsync.handoff import Delivery

        for error, key in (
            (ConflictError("x"), "notify.conflict"),
            (HandoffNotDelivered([Delivery("desktop", "d1", 3, 1, 0)]), "notify.not_delivered"),
            (RuntimeError("boom"), "notify.failed"),
        ):
            with self.subTest(key=key):
                self.tearDown_machine()
                with self.assertLogs("codexsync.app", level="INFO"):
                    calls, notes = self._watch(
                        [ProcessState.STOPPED, ProcessState.RUNNING, ProcessState.STOPPED], outcome=error,
                    )
                self.assertEqual(calls, ["handoff", "handoff"])
                self.assertEqual([k for k, _ in notes].count(key), 2)

    def tearDown_machine(self) -> None:
        shutil.rmtree(self.root / "laptop", ignore_errors=True)
        shutil.rmtree(self.handoff_root, ignore_errors=True)


class NotificationTests(unittest.TestCase):
    def test_windows_shows_a_toast_without_a_window_and_passes_text_out_of_band(self) -> None:
        seen: dict = {}

        def run(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertTrue(notify("T", "Body with 'quotes' & <tags>", run=run, platform="win32"))
        self.assertEqual(seen["argv"][0], "powershell.exe")
        self.assertNotIn("quotes", " ".join(seen["argv"]), "text never goes on the command line")
        self.assertEqual(seen["kwargs"]["env"]["CODEXSYNC_NOTIFY_BODY"], "Body with 'quotes' & <tags>")
        self.assertEqual(seen["kwargs"]["creationflags"], 0x08000000)

    def test_macos_and_linux(self) -> None:
        argvs: list = []

        def run(argv, **kwargs):
            argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        notify("T", "B", run=run, platform="darwin")
        notify("T", "B", run=run, platform="linux")
        self.assertEqual(argvs[0][0], "osascript")
        self.assertEqual(argvs[0][-2:], ["T", "B"])
        self.assertEqual(argvs[1][0], "notify-send")

    def test_a_failing_notification_never_raises(self) -> None:
        def run(argv, **kwargs):
            raise FileNotFoundError("no powershell")

        with self.assertLogs("codexsync.notifications", level="WARNING"):
            self.assertFalse(notify("T", "B", run=run, platform="win32"))

    def test_switched_off_shows_nothing(self) -> None:
        run = mock.Mock()
        with self.assertLogs("codexsync.notifications", level="INFO"):
            self.assertFalse(Notifier(enabled=False, run=run)("notify.conflict"))
        run.assert_not_called()

    def test_every_key_has_a_sentence_in_every_language(self) -> None:
        for language in ("en", "ru", "zh"):
            for key in NOTIFY_KEYS:
                with self.subTest(language=language, key=key):
                    text = message(key, language=language, machine="m", since="s", arrived=1, total=2,
                                   reason="r", count=3)
                    self.assertNotEqual(text, key)
                    self.assertNotIn("{", text)

    def test_russian_is_russian(self) -> None:
        self.assertIn("Работа", message("notify.loaded", language="ru", machine="desktop"))
        self.assertIn("desktop", message("notify.loaded", language="ru", machine="desktop"))


class HandoffConfigTests(_Workspace):
    def test_switching_on_without_a_folder_is_refused(self) -> None:
        config, _ = self.machine("laptop", enabled=True)
        text = config.read_text(encoding="utf-8").replace(f'root_dir = "{self.handoff_root.as_posix()}"', 'root_dir = ""')
        manifest = (self.workspace / "manifest.json").as_posix()
        text = text.replace(f'manifest_file = "{manifest}"', "")
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "handoff.root_dir is empty"):
            load_config(config)

    def test_the_watcher_replaces_the_sign_in_sync(self) -> None:
        config, _ = self.machine("laptop", enabled=True)
        config.write_text(
            config.read_text(encoding="utf-8") + "\n[scheduler]\nsync_at_login = true\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "sync_at_login"):
            load_config(config)

    def test_a_quoted_boolean_is_refused(self) -> None:
        config, _ = self.machine("laptop", enabled="true")
        with self.assertRaisesRegex(ConfigError, "handoff.enabled must be a boolean"):
            load_config(config)

    def test_the_folder_may_not_sit_in_the_cloud_mirror(self) -> None:
        config, _ = self.machine("laptop")
        text = config.read_text(encoding="utf-8").replace(
            f'root_dir = "{self.handoff_root.as_posix()}"', f'root_dir = "{(self.cloud / "handoff").as_posix()}"'
        )
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "handoff.root_dir must not overlap paths.cloud_root_dir"):
            load_config(config)

    def test_the_wait_is_bounded(self) -> None:
        config, _ = self.machine("laptop", delivery_wait_minutes=100000)
        with self.assertRaisesRegex(ConfigError, "delivery_wait_minutes"):
            load_config(config)


class HandoffTaskTests(unittest.TestCase):
    def test_the_watcher_starts_at_sign_in_and_has_no_time_limit(self) -> None:
        from codexsync.system_scheduler import (
            HANDOFF_MODE,
            HANDOFF_SLOT,
            JobDefinition,
            ScheduledJob,
            SystemdUserScheduler,
            job_arguments,
            render_task_xml,
            task_name_for,
        )

        root = Path(__file__).resolve().anchor
        job = ScheduledJob(HANDOFF_MODE, None, True, 30, 0)
        self.assertEqual(job_arguments(HANDOFF_MODE, Path(root) / "c.toml")[-2:], ["handoff", "watch"])
        with self.assertRaises(ValueError):
            ScheduledJob(HANDOFF_MODE, 600, True)
        with self.assertRaises(ValueError):
            ScheduledJob(HANDOFF_MODE, None, False)
        definition = JobDefinition(job, (str(Path(root) / "bin" / "codexsync"),), Path(root) / "c.toml", Path(root) / "logs")
        from datetime import datetime, timezone

        xml = render_task_xml(definition, user_id="u", now=datetime(2026, 9, 27, tzinfo=timezone.utc))
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)
        self.assertIn("LogonTrigger", xml)
        self.assertTrue(task_name_for("u", HANDOFF_SLOT).endswith("CodexSync Handoff (u)"))
        units = SystemdUserScheduler(home=Path(root) / "home", slot=HANDOFF_SLOT).render(definition)
        self.assertIn("TimeoutStartSec=infinity", units["codexsync-handoff.service"].decode())


if __name__ == "__main__":
    unittest.main()
