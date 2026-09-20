"""Codex rewrote every session into a newer record format (CS-252).

The September 2026 desktop build rewrote each session file into numbered
records, kept each file's mtime, and dropped records on the way (rolled-back
turns, repeated `session_meta`, injected instructions). A mirror written before
that then disagrees with every session it holds. These tests pin down what
codexSync does about it: it names the conflict for what it is, never decides it
on its own, lets one explicit decision cover all of them except where the old
copy holds something later, and keeps each superseded copy compressed.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import lzma
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import (
    apply_session_transfer,
    record_format_migrations,
    scan_session_transfer,
)
from codexsync.chat_directory import _read_title
from codexsync.cli import main
from codexsync.exceptions import ConflictError, FailSafeError
from codexsync.jsonl_codec import JsonlCodec
from codexsync.preflight import _check_session_format
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_store import SemanticStore, session_hash_for
from codexsync.semantic_transfer import (
    FORMAT_MIGRATION,
    NEWER_FORMAT_LOCAL,
    NEWER_FORMAT_REMOTE,
    OLDER_FORMAT_HAS_LATER_RECORDS,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    format_migration_resolutions,
    save_transfer_plan,
)
from codexsync.session_catalog import (
    RECORD_FORMAT_LEGACY,
    RECORD_FORMAT_MIXED,
    RECORD_FORMAT_ORDINAL,
    peek_record_formats,
    scan_sessions,
)


def _legacy(session_id: str, turns: list[tuple[str, str]], *, rolled_back: str | None = None) -> bytes:
    """A history as the runtime wrote it before the rewrite."""
    rows = [{"timestamp": "2026-03-11T12:00:00.000Z", "type": "session_meta",
             "payload": {"id": session_id, "cwd": "D:\\Projects\\app"}}]
    for stamp, text in turns:
        rows.append({"timestamp": stamp, "type": "event_msg",
                     "payload": {"type": "user_message", "message": text}})
    if rolled_back is not None:
        rows.append({"timestamp": rolled_back, "type": "event_msg",
                     "payload": {"type": "user_message", "message": "an undone turn"}})
    return b"".join(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n" for row in rows)


def _ordinal(session_id: str, turns: list[tuple[str, str]]) -> bytes:
    """The same history after the rewrite: numbered, messages moved into items."""
    rows = [{"timestamp": "2026-03-11T12:00:00.000Z", "ordinal": 0, "type": "session_meta",
             "payload": {"session_id": session_id, "id": session_id, "cwd": "D:\\Projects\\app",
                         "history_mode": "paginated"}}]
    for index, (stamp, text) in enumerate(turns, start=1):
        rows.append({"timestamp": stamp, "ordinal": index, "type": "event_msg",
                     "payload": {"type": "item_completed", "thread_id": session_id,
                                 "item": {"type": "UserMessage", "id": f"item-{index}",
                                          "content": [{"type": "text", "text": text}]}}})
    return b"".join(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n" for row in rows)


TURNS = [("2026-03-11T12:01:00.000Z", "first question"), ("2026-03-11T12:05:00.000Z", "second question")]


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"format-migration-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        (self.local_dir / "sessions").mkdir(parents=True)
        (self.cloud_dir / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, root: Path, name: str, payload: bytes, *, xz: bool = False) -> Path:
        path = root / "sessions" / (name + (".xz" if xz else ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(payload) if xz else payload)
        return path


class RecordFormatCatalogTests(_Sandbox):
    def _only(self, root: Path):
        (descriptor,) = scan_sessions(root).descriptors
        return descriptor

    def test_an_old_history_is_legacy(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _legacy("s1", TURNS))
        self.assertEqual(self._only(self.local_dir).record_format, RECORD_FORMAT_LEGACY)

    def test_a_rewritten_history_is_ordinal(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self.assertEqual(self._only(self.local_dir).record_format, RECORD_FORMAT_ORDINAL)

    def test_an_old_history_appended_to_by_the_new_build_is_mixed(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _legacy("s1", TURNS) + _ordinal("s1", TURNS).splitlines(True)[1])
        self.assertEqual(self._only(self.local_dir).record_format, RECORD_FORMAT_MIXED)

    def test_the_latest_record_time_is_the_latest_not_the_last(self) -> None:
        turns = [("2026-03-11T12:09:00.000Z", "late"), ("2026-03-11T12:02:00.000Z", "written after")]
        self._write(self.local_dir, "s1.jsonl", _legacy("s1", turns))
        self.assertEqual(self._only(self.local_dir).last_record_at, "2026-03-11T12:09:00.000Z")

    def test_a_compressed_mirror_copy_is_read_through_its_container(self) -> None:
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS), xz=True)
        self.assertEqual(self._only(self.cloud_dir).record_format, RECORD_FORMAT_LEGACY)

    def test_peeking_counts_each_side_by_its_first_record(self) -> None:
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS), xz=True)
        self._write(self.cloud_dir, "s2.jsonl", _ordinal("s2", TURNS))
        self._write(self.cloud_dir, "s3.jsonl", b"not json\n")
        self.assertEqual(
            peek_record_formats(self.cloud_dir),
            {
                "sessions/s1.jsonl": RECORD_FORMAT_LEGACY,
                "sessions/s2.jsonl": RECORD_FORMAT_ORDINAL,
                "sessions/s3.jsonl": "unreadable",
            },
        )


class FormatMigrationPlanTests(_Sandbox):
    def _plan(self, **kwargs):
        return build_transfer_plan(
            scan_sessions(self.local_dir), scan_sessions(self.cloud_dir),
            local_root=self.local_dir, remote_root=self.cloud_dir,
            source_machine="desktop", target_machine="laptop", **kwargs,
        )

    def test_a_rewrite_stays_a_conflict_and_says_what_kind(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS), xz=True)
        plan = self._plan()
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertIn(FORMAT_MIGRATION, item.codes)
        self.assertIn(NEWER_FORMAT_LOCAL, item.codes)
        self.assertNotIn(OLDER_FORMAT_HAS_LATER_RECORDS, item.codes)
        self.assertIn(FORMAT_MIGRATION, plan.codes)

    def test_the_newer_side_is_named_whichever_side_it_is(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _legacy("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _ordinal("s1", TURNS))
        (item,) = self._plan().items
        self.assertIn(NEWER_FORMAT_REMOTE, item.codes)

    def test_a_divergence_within_one_format_is_not_called_a_rewrite(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _legacy("s1", TURNS + [("2026-03-11T12:06:00.000Z", "left")]))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS + [("2026-03-11T12:06:00.000Z", "right")]))
        (item,) = self._plan().items
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        self.assertNotIn(FORMAT_MIGRATION, item.codes)

    def test_an_old_copy_with_a_later_record_is_flagged(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS, rolled_back="2026-03-11T12:30:00.000Z"))
        (item,) = self._plan().items
        self.assertIn(OLDER_FORMAT_HAS_LATER_RECORDS, item.codes)

    def test_the_bulk_decision_keeps_the_newer_side_and_leaves_the_later_old_copy(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS))
        self._write(self.local_dir, "s2.jsonl", _legacy("s2", TURNS))
        self._write(self.cloud_dir, "s2.jsonl", _ordinal("s2", TURNS))
        self._write(self.local_dir, "s3.jsonl", _ordinal("s3", TURNS))
        self._write(self.cloud_dir, "s3.jsonl", _legacy("s3", TURNS, rolled_back="2026-03-11T12:30:00.000Z"))
        plan = self._plan()
        decided, held = format_migration_resolutions(plan)
        choices = {resolution.session_hash: resolution.choice for resolution in decided}
        self.assertEqual(choices, {
            session_hash_for("s1"): ResolutionChoice.KEEP_LOCAL,
            session_hash_for("s2"): ResolutionChoice.KEEP_REMOTE,
        })
        self.assertEqual([item.session_hash for item in held], [session_hash_for("s3")])

    def test_a_decided_rewrite_writes_the_mirror_and_remains_labelled(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS))
        decided, _ = format_migration_resolutions(self._plan())
        plan = self._plan(resolutions={resolution.conflict_id: resolution for resolution in decided})
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertIn("RESOLVED_BY_USER", item.codes)
        self.assertIn(FORMAT_MIGRATION, item.codes)

    def test_nothing_is_decided_without_being_asked(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS))
        plan = self._plan()
        self.assertEqual(plan.writable_items, ())
        self.assertIn("PLAN_HAS_BLOCKED_ITEMS", plan.codes)


class SupersededArchiveTests(_Sandbox):
    def test_the_branch_is_kept_compressed_and_decompresses_to_itself(self) -> None:
        original = _legacy("s1", TURNS)
        source = self._write(self.cloud_dir, "s1.jsonl", original, xz=True)
        store = SemanticStore(self.root / "semantic", "machine-a")
        kept = store.archive_superseded(source, session_id="s1", reason=FORMAT_MIGRATION)
        body = kept / "branch.jsonl.xz"
        self.assertEqual(lzma.decompress(body.read_bytes()), original)
        manifest = json.loads((kept / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["reason"], FORMAT_MIGRATION)
        self.assertTrue((kept / "COMMITTED").is_file())

    def test_keeping_the_same_branch_twice_keeps_it_once(self) -> None:
        source = self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS))
        store = SemanticStore(self.root / "semantic", "machine-a")
        first = store.archive_superseded(source, session_id="s1", reason=FORMAT_MIGRATION)
        second = store.archive_superseded(source, session_id="s1", reason=FORMAT_MIGRATION)
        self.assertEqual(first, second)
        self.assertEqual(len(list(first.parent.iterdir())), 1)

    def test_a_copy_that_does_not_decompress_to_the_branch_is_never_committed(self) -> None:
        source = self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS))
        store = SemanticStore(self.root / "semantic", "machine-a")

        def lossy(src, destination, source_codec, destination_codec):
            destination.write_bytes(lzma.compress(b"something else\n"))

        with patch("codexsync.semantic_store.transcode", side_effect=lossy):
            with self.assertRaises(FailSafeError):
                store.archive_superseded(source, session_id="s1", reason=FORMAT_MIGRATION)
        self.assertFalse((self.root / "semantic" / "superseded").exists())


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class FormatMigrationApplyTests(_Sandbox):
    """The whole path on a machine whose mirror predates the rewrite."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config.toml"
        self.config.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-a"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            local_state_dir = "{self.local_dir.as_posix()}"
            cloud_root_dir = "{self.cloud_dir.as_posix()}"
            backup_dir = "{(self.root / 'backups').as_posix()}"
            temp_dir = "{(self.root / '.tmp').as_posix()}"

            [guardian]
            root_dir = "{(self.root / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(self.root / 'semantic').as_posix()}"

            [targets]
            include_roots = ["sessions"]

            [state]
            manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
            """).strip() + "\n", encoding="utf-8")
        self.plan_path = self.root / "plan.json"
        self.resolutions = self.root / "resolutions.json"
        self.old = _legacy("s1", TURNS)
        self.new = _ordinal("s1", TURNS)
        self._write(self.local_dir, "s1.jsonl", self.new)
        self._write(self.cloud_dir, "s1.jsonl", self.old, xz=True)

    def _scan(self, resolutions: Path | None = None):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            plan = scan_session_transfer(
                self.config, source_machine="desktop", target_machine="laptop",
                resolutions_path=resolutions,
            )
        save_transfer_plan(plan, self.plan_path)
        return plan

    def _apply(self, plan):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return apply_session_transfer(
                self.config, plan_path=self.plan_path, confirm_plan=plan.plan_id,
                resolutions_path=self.resolutions if self.resolutions.exists() else None,
            )

    def test_an_undecided_rewrite_stops_the_apply(self) -> None:
        plan = self._scan()
        with self.assertRaises(ConflictError):
            self._apply(plan)
        self.assertEqual(lzma.decompress((self.cloud_dir / "sessions" / "s1.jsonl.xz").read_bytes()), self.old)

    def test_one_decision_then_the_mirror_holds_the_new_copy_and_the_old_one_is_kept(self) -> None:
        self._scan()
        decided, held = record_format_migrations(self.plan_path, output_path=self.resolutions)
        self.assertEqual((len(decided), held), (1, []))
        plan = self._scan(self.resolutions)
        self._apply(plan)

        mirror = self.cloud_dir / "sessions" / "s1.jsonl.xz"
        self.assertEqual(lzma.decompress(mirror.read_bytes()), self.new)
        self.assertEqual((self.local_dir / "sessions" / "s1.jsonl").read_bytes(), self.new)
        (kept,) = (self.root / "semantic" / "superseded").iterdir()
        manifest = json.loads((kept / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["session_hash"], session_hash_for("s1"))
        self.assertEqual(lzma.decompress((kept / "branch.jsonl.xz").read_bytes()), self.old)
        # Only the loser, never a bundle of both uncompressed branches.
        self.assertFalse((self.root / "semantic" / "conflicts").exists())

    def test_the_cli_decides_them_in_one_step(self) -> None:
        self._scan()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([
                "-c", str(self.config), "sessions", "resolve", "--plan", str(self.plan_path),
                "--format-migrations", "--output", str(self.resolutions),
            ])
        self.assertEqual(code, 0)
        self.assertIn("for 1 conflict", out.getvalue())
        self.assertTrue(self.resolutions.is_file())

    def test_the_cli_names_what_it_left_by_conflict_id(self) -> None:
        # The old copy carries a turn later than anything in the rewrite.
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS, rolled_back="2026-03-11T12:30:00.000Z"), xz=True)
        plan = self._scan()
        (item,) = plan.items
        out = io.StringIO()
        with redirect_stdout(out):
            main([
                "-c", str(self.config), "sessions", "resolve", "--plan", str(self.plan_path),
                "--format-migrations", "--output", str(self.resolutions),
            ])
        self.assertIn("1 left for you", out.getvalue())
        self.assertIn(item.conflict_id, out.getvalue())
        self.assertNotIn("s1", out.getvalue().replace(item.conflict_id, ""))

    def test_the_cli_refuses_a_bulk_decision_mixed_with_a_single_one(self) -> None:
        self._scan()
        code = main([
            "-c", str(self.config), "sessions", "resolve", "--plan", str(self.plan_path),
            "--format-migrations", "--choice", "KEEP_REMOTE", "--output", str(self.resolutions),
        ])
        self.assertEqual(code, 4)
        self.assertFalse(self.resolutions.exists())


class RewrittenTitleTests(_Sandbox):
    def test_a_rewritten_chat_keeps_its_title(self) -> None:
        path = self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self.assertEqual(_read_title(path, 1 << 20), "first question")


class SessionFormatDoctorTests(_Sandbox):
    def test_a_side_left_in_the_older_format_is_a_warning_with_the_way_out(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _legacy("s1", TURNS), xz=True)
        result = _check_session_format(self.local_dir, self.cloud_dir, 1 << 20)
        self.assertEqual(result.status, "WARN")
        self.assertIn("--format-migrations", result.details)

    def test_both_formats_on_both_sides_agreeing_per_branch_pass(self) -> None:
        # A fresh session is written in the old format and rewritten later, so
        # a machine in sync holds both formats on both sides.
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _ordinal("s1", TURNS), xz=True)
        self._write(self.local_dir, "s2.jsonl", _legacy("s2", TURNS))
        self._write(self.cloud_dir, "s2.jsonl", _legacy("s2", TURNS), xz=True)
        result = _check_session_format(self.local_dir, self.cloud_dir, 1 << 20)
        self.assertEqual(result.status, "PASS")
        self.assertIn("differing=0", result.details)

    def test_both_sides_in_one_format_pass(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        self._write(self.cloud_dir, "s1.jsonl", _ordinal("s1", TURNS), xz=True)
        result = _check_session_format(self.local_dir, self.cloud_dir, 1 << 20)
        self.assertEqual(result.status, "PASS")

    def test_it_reads_nothing_it_does_not_need_and_writes_nothing(self) -> None:
        self._write(self.local_dir, "s1.jsonl", _ordinal("s1", TURNS))
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        _check_session_format(self.local_dir, self.cloud_dir, 1 << 20)
        self.assertEqual(sorted(p.relative_to(self.root) for p in self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
