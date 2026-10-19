"""A format of Codex's this version does not know blocks the write it would feed (CS-394).

Found on the machine this was built against on 2026-10-18: Codex 0.163 had
stored 316 of its 329 chats as `rollout-...jsonl.zst` since 2026-10-15, while
its catalogue still named each plain `.jsonl`. codexSync did not know the
container, so the scan skipped those files, the chats looked absent here, and
the plan put the mirror's older copy of 305 of them into `.codex` beside the
newer compressed file -- at exactly the path the catalogue names. Nothing had
been written yet; these tests hold the refusals that replaced it, and the same
rule for the chat list, its rebuild status and the global state.
"""
from __future__ import annotations

import json
import lzma
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.chat_archive import build_archive_plan
from codexsync.codex_compat import CHAT_LIST, CHATS, GLOBAL_STATE, REBUILD_STATUS, check_codex_compat, read_codex_version
from codexsync.codex_health import (
    CATALOGUE_STATUS_UNKNOWN,
    build_settle_plan,
    diagnose_catalogue,
    ChatFiles,
)
from codexsync.exceptions import FailSafeError
from codexsync.jsonl_codec import held_in_any_container, logical_name, unsupported_container
from codexsync.preflight import _check_codex_compat, _check_session_format
from codexsync.recovery import _plan_global_state_rollback
from codexsync.safety_gate import ProcessState
from codexsync.semantic_transfer import (
    SAME_PATH_LAYOUT_ID,
    TransferAction,
    _destination_check,
    build_transfer_plan,
)
from codexsync.session_catalog import (
    ID_FROM_FILE_NAME,
    RECORD_FORMAT_UNSUPPORTED,
    UNSUPPORTED_CONTAINER,
    SessionCatalog,
    SessionState,
    peek_record_formats,
    scan_sessions,
)
from codexsync.sqlite_audit import (
    CATALOG_SHAPE_UNKNOWN,
    BackfillReading,
    PlacementStatus,
    ThreadArchiveRows,
    read_backfill_state,
    read_thread_placements,
)
from codexsync.thread_catalogue import find_unnamed_chat_files

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
CHAT = "019ce072-1068-7270-ade9-d4cdff67902e"
NAME = f"rollout-2026-03-12T13-08-17-{CHAT}.jsonl"
DAY = "sessions/2026/03/12"
#: A container no version reads (`.br`): it stands for whatever Codex uses
#: next. `.zst` itself is read where Python has zstd (CS-425, test_zstd_chats).
PACKED = b"\xce\xb2\xcf\x81" + b"\x00compressed history\x00" * 8


def _records(session_id: str, turns: int) -> bytes:
    lines = [{"type": "session_meta", "timestamp": "2026-03-12T13:08:17Z",
              "payload": {"id": session_id, "cwd": "D:/work", "timestamp": "2026-03-12T13:08:17Z"}}]
    lines += [{"type": "event", "timestamp": f"2026-03-12T13:{10 + n:02d}:00Z", "n": n} for n in range(turns)]
    return b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)


class _Roots(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"unknown-formats-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "codex"
        self.mirror = self.root / "mirror"
        self.local.mkdir(parents=True)
        self.mirror.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, root: Path, relative: str, data: bytes) -> Path:
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def catalogue(self, rows: list[tuple[str, str, int]], *, name: str = "state_5.sqlite",
                  columns: str = "id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER, archived_at INTEGER") -> Path:
        database = self.local / name
        database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database)
        connection.execute(f"CREATE TABLE threads ({columns})")
        for thread_id, relative, archived in rows:
            connection.execute(
                "INSERT INTO threads (id, rollout_path, archived) VALUES (?, ?, ?)",
                (thread_id, str(self.local / Path(*relative.split("/"))), archived),
            )
        connection.commit()
        connection.close()
        return database


class ContainerNameTests(_Roots):
    def test_a_container_after_jsonl_is_recognised_as_unsupported(self) -> None:
        self.assertEqual(unsupported_container(NAME + ".br"), "br")
        self.assertEqual(logical_name(NAME + ".br"), NAME)
        for known in (NAME, NAME + ".xz", NAME + ".gz", "notes.txt", "." + NAME + ".br", NAME + ".br.tmp"):
            self.assertIsNone(unsupported_container(known), known)

    def test_a_file_named_plain_is_held_when_only_its_compressed_copy_exists(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.assertTrue(held_in_any_container(self.local / DAY / NAME))
        self.assertFalse(held_in_any_container(self.local / DAY / ("rollout-other.jsonl")))


class ScanTests(_Roots):
    def test_a_compressed_chat_is_an_unreadable_copy_of_its_own_chat(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        (item,) = scan_sessions(self.local).descriptors
        self.assertIs(item.state, SessionState.INVALID)
        self.assertEqual(item.session_id, CHAT)
        self.assertEqual(item.byte_count, len(PACKED))
        self.assertIn(UNSUPPORTED_CONTAINER, item.codes)
        self.assertIn(ID_FROM_FILE_NAME, item.codes)

    def test_doctor_does_not_call_it_a_format_difference(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.put(self.mirror, f"{DAY}/{NAME}.xz", lzma.compress(_records(CHAT, 2)))
        self.assertEqual(peek_record_formats(self.local), {f"{DAY}/{NAME}": RECORD_FORMAT_UNSUPPORTED})
        result = _check_session_format(self.local, self.mirror, 1 << 20)
        self.assertEqual(result.status, "PASS")
        self.assertIn("differing=0", result.details)


class PlanTests(_Roots):
    def plan(self):
        return build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.mirror),
            local_root=self.local, remote_root=self.mirror,
            source_machine="desktop", target_machine="laptop",
            layout_id=SAME_PATH_LAYOUT_ID, placements=read_thread_placements(self.local),
        )

    def test_the_mirrors_older_copy_is_not_put_beside_codexs_compressed_chat(self) -> None:
        # The case found on the real machine, as it stood.
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.put(self.mirror, f"{DAY}/{NAME}.xz", lzma.compress(_records(CHAT, 2)))
        self.catalogue([(CHAT, f"{DAY}/{NAME}", 0)])
        (item,) = self.plan().items
        self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH, item.codes)
        self.assertIn("LOCAL_BRANCH_INVALID", item.codes)
        self.assertIn(UNSUPPORTED_CONTAINER, item.codes)
        self.assertIsNone(item.target_relative_path)

    def test_a_compressed_chat_the_mirror_lacks_is_not_copied_either(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.catalogue([(CHAT, f"{DAY}/{NAME}", 0)])
        (item,) = self.plan().items
        self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH, item.codes)

    def test_a_compressed_file_without_an_id_in_its_name_still_occupies_the_place(self) -> None:
        odd = "rollout-unnamed.jsonl"
        self.put(self.local, f"{DAY}/{odd}.br", PACKED)
        self.put(self.mirror, f"{DAY}/{odd}.xz", lzma.compress(_records(CHAT, 2)))
        self.catalogue([(CHAT, f"{DAY}/{odd}", 0)])
        (item,) = self.plan().items
        self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH, item.codes)
        self.assertIn("DESTINATION_OCCUPIED", item.codes)

    def test_the_disk_is_checked_for_any_container_too(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        empty = SessionCatalog([], {})
        occupied = _destination_check(self.local, self.mirror, empty, empty)
        self.assertTrue(occupied("local", f"{DAY}/{NAME}"))
        self.assertFalse(occupied("local", f"{DAY}/rollout-elsewhere.jsonl"))


class CatalogueReadersTests(_Roots):
    def test_a_row_naming_the_plain_file_of_a_compressed_chat_is_not_moved(self) -> None:
        # The row is right: Codex reads the `.br`. A stale plain copy in the
        # archive must not be taken for the place the chat moved to.
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.put(self.local, f"archived_sessions/{NAME}", _records(CHAT, 1))
        rows = ThreadArchiveRows(
            PlacementStatus.AVAILABLE, "state_5.sqlite",
            {CHAT: (str(self.local / DAY / NAME), 0, None)},
        )
        self.assertEqual(build_archive_plan(rows, self.local).changes, ())

    def test_a_compressed_catalogued_file_is_not_a_chat_codex_cannot_reach(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.put(self.local, f"archived_sessions/{NAME}", _records(CHAT, 1))
        self.catalogue([(CHAT, f"{DAY}/{NAME}", 0)])
        self.assertEqual(find_unnamed_chat_files(self.local, read_thread_placements(self.local)), ())


class ChatListShapeTests(_Roots):
    def test_a_threads_table_without_a_column_read_is_an_unknown_shape_not_absence(self) -> None:
        self.catalogue([], columns="id TEXT PRIMARY KEY, archived INTEGER")
        placements = read_thread_placements(self.local)
        self.assertIs(placements.status, PlacementStatus.INDETERMINATE)
        self.assertEqual(placements.codes, (CATALOG_SHAPE_UNKNOWN,))
        self.assertIs(read_backfill_state(self.local).status, PlacementStatus.INDETERMINATE)

    def test_a_state_database_without_a_threads_table_is_unknown_too(self) -> None:
        connection = sqlite3.connect(self.local / "state_6.sqlite")
        connection.execute("CREATE TABLE conversations (id TEXT)")
        connection.commit()
        connection.close()
        self.assertEqual(read_thread_placements(self.local).codes, (CATALOG_SHAPE_UNKNOWN,))

    def test_another_database_without_threads_is_not_a_catalogue(self) -> None:
        (self.local / "sqlite").mkdir()
        connection = sqlite3.connect(self.local / "sqlite" / "other.db")
        connection.execute("CREATE TABLE things (id TEXT)")
        connection.commit()
        connection.close()
        self.assertIs(read_thread_placements(self.local).status, PlacementStatus.ABSENT)

    def test_two_catalogues_are_not_merged(self) -> None:
        self.catalogue([("a", f"{DAY}/a.jsonl", 0)])
        self.catalogue([("b", f"{DAY}/b.jsonl", 0)], name="state_6.sqlite")
        placements = read_thread_placements(self.local)
        self.assertIs(placements.status, PlacementStatus.INDETERMINATE)
        self.assertEqual(placements.codes, ("SEVERAL_CATALOGUES",))

    def test_a_new_chat_is_then_refused(self) -> None:
        self.catalogue([], columns="id TEXT PRIMARY KEY, archived INTEGER")
        self.put(self.mirror, f"{DAY}/{NAME}.xz", lzma.compress(_records(CHAT, 2)))
        plan = build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.mirror),
            local_root=self.local, remote_root=self.mirror,
            source_machine="desktop", target_machine="laptop",
            layout_id=SAME_PATH_LAYOUT_ID, placements=read_thread_placements(self.local),
        )
        (item,) = plan.items
        self.assertEqual(item.action, TransferAction.BLOCKED_UNSUPPORTED_BACKEND, item.codes)


class RebuildStatusTests(unittest.TestCase):
    def reading(self, status: str) -> BackfillReading:
        return BackfillReading(PlacementStatus.AVAILABLE, "state_5.sqlite", status, updated_at=1791524862)

    def test_an_unknown_status_is_reported_and_never_repaired(self) -> None:
        found = diagnose_catalogue(self.reading("paused"), ProcessState.STOPPED, ChatFiles(3, 0))
        self.assertEqual([item.code for item in found], [CATALOGUE_STATUS_UNKNOWN])
        self.assertIsNone(found[0].fix)
        root = SANDBOX / "never-read"
        self.assertFalse(build_settle_plan(root, self.reading("paused")).writes)

    def test_the_known_statuses_still_repair(self) -> None:
        root = SANDBOX / "never-read"
        for status in ("running", "pending"):
            plan = build_settle_plan(root, BackfillReading(PlacementStatus.AVAILABLE, None, status, updated_at=1))
            self.assertTrue(plan.writes, status)


class RollbackTests(_Roots):
    SNAPSHOT_STATE = json.dumps({
        "local-projects": {"p": {"id": "p", "name": "p", "rootPaths": ["D:/p"], "createdAt": 1, "updatedAt": 2}},
        "project-order": ["p"],
        "thread-project-assignments": {},
        "app-server-project-id-by-legacy-project-id-by-host": {},
    }).encode("utf-8")

    def rollback_over(self, live: bytes):
        snapshot = self.root / "snapshot"
        self.put(snapshot, ".codex-global-state.json", self.SNAPSHOT_STATE)
        self.put(self.local, ".codex-global-state.json", live)
        return _plan_global_state_rollback(snapshot, self.local, None)

    def test_projects_in_an_unknown_shape_are_not_written_over(self) -> None:
        with self.assertRaisesRegex(FailSafeError, "format this version does not know"):
            self.rollback_over(json.dumps({"local-projects": [{"id": "p"}]}).encode("utf-8"))

    def test_a_damaged_file_is_still_put_back(self) -> None:
        self.assertEqual(self.rollback_over(b"{ not json").candidate, self.SNAPSHOT_STATE)


class VerdictTests(_Roots):
    def test_codexs_version_is_read_from_one_key(self) -> None:
        self.put(self.local, "models_cache.json", json.dumps({"client_version": "0.163.0", "models": []}).encode())
        self.assertEqual(read_codex_version(self.local), "0.163.0")
        self.put(self.local, "models_cache.json", json.dumps({"client_version": "0.1\nevil"}).encode())
        self.assertIsNone(read_codex_version(self.local))

    def test_a_compatible_codex(self) -> None:
        self.put(self.local, "models_cache.json", json.dumps({"client_version": "0.163.0"}).encode())
        self.put(self.local, f"{DAY}/{NAME}", _records(CHAT, 1))
        report = check_codex_compat(self.local, scan_sessions(self.local), max_state_bytes=1 << 20)
        self.assertTrue(report.compatible, report)

    def test_every_unknown_part_is_named(self) -> None:
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        self.catalogue([], columns="id TEXT PRIMARY KEY, archived INTEGER")
        self.put(self.local, ".codex-global-state.json", json.dumps({"local-projects": [1]}).encode())
        report = check_codex_compat(self.local, scan_sessions(self.local), max_state_bytes=1 << 20)
        self.assertEqual({part.part for part in report.unknown}, {CHATS, CHAT_LIST, GLOBAL_STATE})
        chats = next(part for part in report.unknown if part.part == CHATS)
        self.assertEqual((chats.count, chats.containers), (1, ("br",)))

    def test_an_unknown_rebuild_status_is_named(self) -> None:
        database = self.catalogue([])
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE backfill_state (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO backfill_state VALUES (1, 'paused')")
        connection.commit()
        connection.close()
        report = check_codex_compat(self.local, scan_sessions(self.local), max_state_bytes=1 << 20)
        self.assertEqual([part.part for part in report.unknown], [REBUILD_STATUS])

    def test_doctor_gives_one_answer_with_the_version(self) -> None:
        class Guardian:
            max_state_bytes = 1 << 20

        class Config:
            guardian = Guardian()

        self.put(self.local, "models_cache.json", json.dumps({"client_version": "0.163.0"}).encode())
        self.put(self.local, f"{DAY}/{NAME}.br", PACKED)
        result = _check_codex_compat(self.local, scan_sessions(self.local), Config())  # type: ignore[arg-type]
        self.assertEqual(result.status, "WARN")
        self.assertIn("Codex 0.163.0", result.details)
        self.assertIn("1 chat file(s) in a container it cannot read (br)", result.details)
        (self.local / DAY / f"{NAME}.br").unlink()
        result = _check_codex_compat(self.local, scan_sessions(self.local), Config())  # type: ignore[arg-type]
        self.assertEqual(result.status, "PASS")
        self.assertIn("every format codexSync writes into is known", result.details)


if __name__ == "__main__":
    unittest.main()
