"""What 0.2.0a1 wrote, today's code still reads (`D-030`).

`tests/fixtures/ws-a1/` was written once by the 0.2.0a1 code through its real
writers (`scripts/make_a1_fixture.py`): two invented machines, `desk` and
`lap`, handed work to each other twice, decided one chat continued on both by
the configured rule, took a Guardian snapshot and quarantined a shrink, made a
copy of `.codex`, stored a working set and edited the config.

The fixture is frozen. When a test here fails, a reader stopped accepting what
an alpha tester already has on disk: fix the reader (or keep one for the old
format), never the fixture. Each check asserts that something was *accepted*,
not merely that nothing raised -- a reader that drops a file it cannot verify
returns an empty answer, which is exactly the failure this file exists for.
The last class tampers with copies to prove those checks can fail at all.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest import mock
import uuid
import zipfile

from codexsync import app as app_module
from codexsync.chat_names import read_names_board
from codexsync.config import load_config
from codexsync.config_edit import list_config_history
from codexsync.guardian_inventory import read_guardian_inventory
from codexsync.guardian_pointer import find_latest_good
from codexsync.handoff import read_board as read_handoff_board
from codexsync.jsonl_codec import open_jsonl
from codexsync.manifest import load_manifest
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.project_files import read_files_board
from codexsync.project_sync import read_board as read_projects_board
from codexsync.recovery import list_history
from codexsync.restore import list_backup_snapshots, read_backup_manifest_sides
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_store import SemanticStore, session_hash_for
from codexsync.semantic_transfer import load_transfer_plan
from codexsync.session_scope import load_session_scope
from codexsync.state_backup import list_state_backups

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ws-a1"
SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
MACHINES = ("desk", "lap")
CHAT_A = "0199a1a1-0000-7000-8000-00000000000a"
CHAT_B = "0199a1a1-0000-7000-8000-00000000000b"
CHAT_C = "0199a1a1-0000-7000-8000-00000000000c"
CHATS = (CHAT_A, CHAT_B, CHAT_C)


class _Gate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class _A1Workspace(unittest.TestCase):
    """A copy of the fixture, and a config per machine pointing into it."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"a1c-{uuid.uuid4().hex[:8]}"
        shutil.copytree(FIXTURE, self.root)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ws = self.root / "ws"

    def config(self, machine: str) -> Path:
        codex = self.root / machine / "codex"
        codex.mkdir(parents=True, exist_ok=True)
        path = self.root / machine / "config.toml"
        path.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "{machine}"

            [sync]
            mode = "cold"
            session_mode = "all"

            [paths]
            workspace_root_dir = "{self.ws.as_posix()}"
            local_state_dir = "{codex.as_posix()}"
            cloud_root_dir = "{(self.ws / 'mirror').as_posix()}"
            backup_dir = "{(self.ws / 'bk').as_posix()}"
            temp_dir = "{(self.ws / 't').as_posix()}"

            [guardian]
            root_dir = "{(self.ws / 'guardian').as_posix()}"

            [semantic]
            root_dir = "{(self.ws / 'semantic').as_posix()}"

            [targets]
            include_roots = ["sessions", "skills"]

            [backup]
            backup_before_overwrite = true
            compression = "none"

            [state]
            manifest_file = "{(self.ws / 'manifest.json').as_posix()}"

            [conflict]
            policy = "prefer_newer_mtime"

            [state_backup]
            root_dir = "{(self.root / 'copies').as_posix()}"

            [handoff]
            root_dir = "{(self.ws / 'handoff').as_posix()}"
        """).strip() + "\n", encoding="utf-8")
        return path


class CheckoutTests(unittest.TestCase):
    def test_the_fixture_was_checked_out_byte_for_byte(self) -> None:
        # `.gitattributes` marks it `-text`. A checkout that converted line
        # endings breaks every hash in it, and every test below would fail for
        # a reason that has nothing to do with the readers.
        listed = {}
        for line in (FIXTURE / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            digest, name = line.split("  ", 1)
            listed[name] = digest
        found = {
            path.relative_to(FIXTURE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in FIXTURE.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
        }
        self.assertEqual(found, listed)


class WorkspaceFilesTests(_A1Workspace):
    def test_each_machine_reads_its_own_sync_baseline(self) -> None:
        for machine, other in (("desk", "lap"), ("lap", "desk")):
            with self.subTest(machine=machine):
                manifest = load_manifest(self.ws / "manifest.json", 1, machine_id=machine)
                self.assertIn("skills/note.md", manifest.files)
                self.assertIn(other, manifest.others)

    def test_handoff_records_verify_and_say_who_loaded_what(self) -> None:
        board = read_handoff_board(self.ws / "handoff")
        self.assertEqual(board.unreadable, {})
        self.assertEqual(set(board.records), set(MACHINES))
        desk, lap = board.records["desk"], board.records["lap"]
        self.assertEqual(lap.accepted.get("desk"), desk.handoff_id)
        self.assertEqual(desk.accepted.get("lap"), lap.handoff_id)
        self.assertTrue(desk.files, "the fingerprint of the cloud copy")

    def test_peer_boards_verify(self) -> None:
        for name, board in (
            ("projects", read_projects_board(self.ws / "projects")),
            ("chat-names", read_names_board(self.ws / "chat-names")),
            ("project-files", read_files_board(self.ws / "project-files")),
        ):
            with self.subTest(board=name):
                self.assertEqual(board.unreadable, {})
                self.assertEqual(set(board.publications), set(MACHINES))

    def test_the_projects_board_carries_the_projects(self) -> None:
        board = read_projects_board(self.ws / "projects")
        self.assertEqual(set(board.publications["desk"].projects), {"alpha", "beta"})

    def test_semantic_entries_are_accepted_for_both_machines(self) -> None:
        for machine in MACHINES:
            store = SemanticStore(self.ws / "semantic", machine)
            with self.subTest(machine=machine):
                self.assertEqual(
                    set(store.own_states()), {session_hash_for(chat) for chat in CHATS},
                )
                for chat in CHATS:
                    entries = store.entries(session_hash_for(chat))
                    self.assertEqual({entry.machine_id for entry in entries}, set(MACHINES), chat)
                self.assertTrue(store.confirmed_bases(), "the agreements the two machines recorded")

    def test_the_conflict_bundle_is_found_again_and_not_rewritten(self) -> None:
        (bundle,) = (self.ws / "semantic" / "conflicts").iterdir()
        left, right = self.root / "left.jsonl", self.root / "right.jsonl"
        shutil.copyfile(bundle / "left.jsonl", left)
        shutil.copyfile(bundle / "right.jsonl", right)
        before = {path.name: path.read_bytes() for path in bundle.iterdir()}
        for machine, pair in (("desk", (left, right)), ("lap", (right, left))):
            with self.subTest(machine=machine):
                found = SemanticStore(self.ws / "semantic", machine).conflict_bundle(
                    *pair, session_id=CHAT_C, common_records=1,
                )
                self.assertEqual(found, bundle)
        self.assertEqual({path.name: path.read_bytes() for path in bundle.iterdir()}, before)

    def test_mirror_copies_decompress_to_their_chats(self) -> None:
        copies = sorted((self.ws / "mirror" / "sessions").rglob("rollout-*.jsonl.xz"))
        self.assertEqual(len(copies), 3)
        for path in copies:
            with self.subTest(copy=path.name), open_jsonl(path) as reader:
                first = json.loads(reader.readline())
                self.assertEqual(first["type"], "session_meta")
                self.assertIn(first["payload"]["id"], CHATS)

    def test_stored_plans_and_the_working_set_load(self) -> None:
        plans = sorted((self.ws / "plans").glob("handoff-sessions-plan-*.json"))
        self.assertEqual(len(plans), 3)
        for path in plans:
            with self.subTest(plan=path.name):
                plan = load_transfer_plan(path)
                self.assertTrue(plan.items)
        scope = load_session_scope(self.ws / "plans" / "sessions-scope-lap-desk.json")
        self.assertEqual(scope.projects, ("alpha",))

    def test_the_catalogue_markers_read_back(self) -> None:
        for machine in MACHINES:
            with self.subTest(machine=machine):
                marker = self.ws / "t" / "thread-catalogue" / f"{machine}.json"
                self.assertIsNotNone(app_module._read_catalogue_marker(marker))


class JournalTests(_A1Workspace):
    def test_every_journal_loads_and_is_closed(self) -> None:
        store = JournalStore(self.ws / "t")
        names = sorted((self.ws / "t" / "journals").glob("*.json"))
        self.assertGreaterEqual(len(names), 10)
        for path in names:
            with self.subTest(journal=path.stem):
                self.assertIs(store.load(path.stem).state, JournalState.COMMITTED)
        self.assertEqual(store.non_terminal(), [], "nothing blocks the next mutation")

    def test_the_history_lists_every_family(self) -> None:
        history = list_history(self.config("desk"))
        families = {item.family for item in history}
        self.assertTrue(
            {"sync", "sessions", "project-sync", "thread-catalogue"} <= families, families,
        )


class GuardianAndCopiesTests(_A1Workspace):
    def test_latest_good_is_found_without_a_rebuild(self) -> None:
        snapshot, resolved = find_latest_good(self.ws / "guardian", "desk")
        self.assertIsNotNone(snapshot)
        self.assertTrue(resolved)

    def test_the_inventory_lists_the_snapshot_and_the_quarantine(self) -> None:
        inventory = read_guardian_inventory(self.config("desk"))
        self.assertEqual(inventory.problems, ())
        self.assertIsNotNone(inventory.latest_good_id)
        self.assertEqual(len(inventory.snapshots), 1)
        self.assertEqual(len(inventory.quarantine), 1)

    def test_backup_snapshots_are_committed_and_name_their_sides(self) -> None:
        snapshots = list_backup_snapshots(self.config("desk"))
        self.assertGreaterEqual(len(snapshots), 6)
        for info in snapshots:
            with self.subTest(snapshot=info.name):
                self.assertTrue(info.committed)
                self.assertIsNone(info.problem)
                self.assertTrue(read_backup_manifest_sides(self.ws / "bk" / info.name))

    def test_the_copy_of_codex_is_listed_and_intact(self) -> None:
        (entry,) = list_state_backups(load_config(self.config("desk")))
        with zipfile.ZipFile(entry.path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(".codex-global-state.json", archive.namelist())

    def test_the_config_history_is_listed(self) -> None:
        config = self.config("desk")
        self.assertEqual(len(list_config_history(load_config(config), config)), 1)


class ContinueFromA1Tests(_A1Workspace):
    """A third machine joining the a1 workspace plans its first full sync."""

    def test_a_new_machine_plans_to_load_every_chat_and_file(self) -> None:
        config = self.config("third")
        with mock.patch("codexsync.app._make_safety_gate", side_effect=lambda cfg: _Gate()), \
                mock.patch("codexsync.app._project_hash_cache_root", return_value=self.root / "hash-cache"):
            status = app_module.handoff_status(config)
            preview = app_module.preview_full_sync(config)
        self.assertEqual(set(status.pending), set(MACHINES))
        self.assertEqual(preview.files.plan.action_count, 1, "the skill file")
        loads = [item for item in preview.chats.items if app_module.transfer_direction(item) == "local"]
        self.assertEqual(len(loads), 3, [item.action for item in preview.chats.items])


class TamperedCopiesAreRefusedTests(_A1Workspace):
    """The checks above can fail: a copy changed after a1 wrote it is not believed."""

    @staticmethod
    def _flip(path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        assert old in text, (path, old)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_a_handoff_record_edited_afterwards(self) -> None:
        self._flip(self.ws / "handoff" / "desk.json", '"generation": ', '"generation": 9')
        board = read_handoff_board(self.ws / "handoff")
        self.assertIn("desk.json", board.unreadable)
        self.assertNotIn("desk", board.records)

    def test_a_semantic_entry_edited_afterwards(self) -> None:
        path = next((self.ws / "semantic" / "manifest" / "desk").glob("*.json"))
        self._flip(path, '"record_count": ', '"record_count": 9')
        self.assertEqual(len(SemanticStore(self.ws / "semantic", "desk").own_states()), 2)

    def test_a_projects_board_edited_afterwards(self) -> None:
        self._flip(self.ws / "projects" / "lap.json", "Project beta", "Project gamma")
        board = read_projects_board(self.ws / "projects")
        self.assertIn("lap.json", board.unreadable)


if __name__ == "__main__":
    unittest.main()
