"""A branch bound for `.codex` says when its working folder is not on this machine (CS-250).

Projects kept outside the synced folder do not exist on the other machine, and
a chat carried there without a working set arrives with a folder that is not
there. The plan says so instead of leaving it to be found by opening the chat;
it blocks nothing, and it never changes a plan whose folders all exist.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import _rebuild_transfer_plan, scan_session_transfer
from codexsync.cli import main
from codexsync.config import load_config
from codexsync.path_mapping import PathMappingRule
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_transfer import (
    CWD_ABSENT_HERE,
    CWD_MAPPING_AMBIGUOUS,
    PROVEN_LAYOUTS,
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    build_transfer_plan,
    conflict_id_for,
    local_folder_exists,
)
from codexsync.session_catalog import SessionCatalog, SessionDescriptor, SessionState


LAYOUT = "test-layout"
ELSEWHERE = "D:\\Projects\\offline"
HERE = "D:\\Yandex.Disk\\Projects\\synced"


class _Folders:
    """A disk that holds exactly the folders it is given, and counts questions."""

    def __init__(self, *present: str) -> None:
        self.present = set(present)
        self.asked: list[str] = []

    def __call__(self, path: str) -> bool:
        self.asked.append(path)
        return path in self.present


class WorkingFolderPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"working-folder-{uuid.uuid4().hex}"
        self.local_root = self.root / "local"
        self.remote_root = self.root / "remote"
        (self.local_root / "sessions").mkdir(parents=True)
        (self.remote_root / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        PROVEN_LAYOUTS.clear()

    def _branch(self, root: Path, name: str, lines: list[str]) -> None:
        payload = b"".join(json.dumps({"r": line}).encode("utf-8") + b"\n" for line in lines)
        (root / "sessions" / name).write_bytes(payload)

    def _descriptor(self, session_id: str, name: str, lines: int, cwd: str | None):
        return SessionDescriptor(
            session_id, SessionState.ACTIVE, f"sessions/{name}", "0" * 64, 0, lines, cwd=cwd
        )

    def _plan(self, local, remote, **kwargs):
        return build_transfer_plan(
            SessionCatalog(list(local), {}),
            SessionCatalog(list(remote), {}),
            local_root=self.local_root,
            remote_root=self.remote_root,
            source_machine="desktop",
            target_machine="laptop",
            **kwargs,
        )

    def _cloud_ahead(self, cwd: str | None = ELSEWHERE, *, session_id: str = "s1", name: str = "a.jsonl"):
        """One chat the other machine continued: the cloud copy is ahead of this one."""
        self._branch(self.local_root, name, ["1"])
        self._branch(self.remote_root, name, ["1", "2"])
        return (
            [self._descriptor(session_id, name, 1, cwd)],
            [self._descriptor(session_id, name, 2, cwd)],
        )

    def test_a_branch_for_codex_without_its_folder_here_says_so(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead()
        plan = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders(HERE))
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertIn(CWD_ABSENT_HERE, item.codes)
        self.assertIn(CWD_ABSENT_HERE, plan.codes)

    def test_it_blocks_nothing(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead()
        plan = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders())
        self.assertEqual(len(plan.writable_items), 1)
        self.assertEqual(plan.blocked_items, ())
        self.assertNotIn("PLAN_HAS_BLOCKED_ITEMS", plan.codes)

    def test_a_present_folder_changes_nothing_not_even_the_id(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead(HERE)
        checked = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders(HERE))
        unchecked = self._plan(local, remote, layout_id=LAYOUT)
        self.assertEqual(checked.items[0].codes, unchecked.items[0].codes)
        self.assertEqual(checked.plan_id, unchecked.plan_id)

    def test_without_the_check_a_plan_is_what_it_always_was(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead()
        plan = self._plan(local, remote, layout_id=LAYOUT)
        self.assertNotIn(CWD_ABSENT_HERE, plan.items[0].codes)
        self.assertNotIn(CWD_ABSENT_HERE, plan.codes)

    def test_the_code_is_part_of_the_plan_id(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead()
        absent = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders())
        present = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders(ELSEWHERE))
        self.assertNotEqual(absent.plan_id, present.plan_id)

    def test_a_branch_towards_the_cloud_is_never_checked(self) -> None:
        # Its folder is this machine's own; the mirror is not a place anyone works in.
        self._branch(self.local_root, "a.jsonl", ["1", "2"])
        self._branch(self.remote_root, "a.jsonl", ["1"])
        folders = _Folders()
        plan = self._plan(
            [self._descriptor("s1", "a.jsonl", 2, ELSEWHERE)],
            [self._descriptor("s1", "a.jsonl", 1, ELSEWHERE)],
            folder_exists=folders,
        )
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertNotIn(CWD_ABSENT_HERE, plan.items[0].codes)
        self.assertEqual(folders.asked, [])

    def test_a_chat_only_the_other_machine_has_is_checked_too(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        self._branch(self.remote_root, "a.jsonl", ["1"])
        plan = self._plan(
            [], [self._descriptor("s1", "a.jsonl", 1, ELSEWHERE)],
            layout_id=LAYOUT, folder_exists=_Folders(),
        )
        self.assertIn("SESSION_ON_ONE_SIDE_ONLY", plan.items[0].codes)
        self.assertIn(CWD_ABSENT_HERE, plan.items[0].codes)
        self.assertIn(CWD_ABSENT_HERE, plan.codes)

    def test_the_locked_direction_still_says_it(self) -> None:
        # Behind the empty layout gate nothing reaches `.codex`, but a person
        # planning a handoff should still learn what would arrive without a folder.
        self.assertEqual(PROVEN_LAYOUTS, {}, "no layout may be assumed proven")
        local, remote = self._cloud_ahead()
        plan = self._plan(local, remote, folder_exists=_Folders())
        self.assertEqual(plan.items[0].action, TransferAction.BLOCKED_UNPROVEN_LAYOUT)
        self.assertIn(CWD_ABSENT_HERE, plan.items[0].codes)

    def test_a_chat_held_back_by_the_working_set_keeps_the_code(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead()
        plan = self._plan(
            local, remote, layout_id=LAYOUT, folder_exists=_Folders(), scope=("not-this-one",),
        )
        item = plan.items[0]
        self.assertEqual(item.action, TransferAction.OUT_OF_SCOPE)
        self.assertIn(CWD_ABSENT_HERE, item.codes)
        self.assertIn("WOULD_BE_FAST_FORWARD_LOCAL", item.codes)

    def test_a_resolution_keeping_the_cloud_branch_is_checked(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        self._branch(self.local_root, "a.jsonl", ["1", "left"])
        self._branch(self.remote_root, "a.jsonl", ["1", "right"])
        local = [self._descriptor("s1", "a.jsonl", 2, ELSEWHERE)]
        remote = [self._descriptor("s1", "a.jsonl", 2, ELSEWHERE)]
        blocked = self._plan(local, remote, layout_id=LAYOUT, folder_exists=_Folders())
        item = blocked.items[0]
        self.assertEqual(item.action, TransferAction.BLOCKED_CONFLICT)
        conflict = conflict_id_for(item.session_hash, item.local_sha256, item.remote_sha256)
        resolution = BranchResolution(
            conflict, item.session_hash, item.local_sha256, item.remote_sha256,
            ResolutionChoice.KEEP_REMOTE,
        )
        plan = self._plan(
            local, remote, layout_id=LAYOUT, folder_exists=_Folders(),
            resolutions={conflict: resolution},
        )
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertIn(CWD_ABSENT_HERE, plan.items[0].codes)

    def test_a_chat_with_no_recorded_folder_is_not_guessed_about(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead(None)
        folders = _Folders()
        plan = self._plan(local, remote, layout_id=LAYOUT, folder_exists=folders)
        self.assertNotIn(CWD_ABSENT_HERE, plan.items[0].codes)
        self.assertEqual(folders.asked, [])

    def test_the_folder_is_looked_for_where_a_mapping_rule_puts_it(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead("D:\\Projects\\offline\\app")
        rules = [PathMappingRule("move", "desktop", "laptop", "D:\\Projects", "E:\\Work")]
        folders = _Folders("E:\\Work\\offline\\app")
        plan = self._plan(
            local, remote, layout_id=LAYOUT, folder_exists=folders, path_rules=rules,
        )
        self.assertEqual(folders.asked, ["E:\\Work\\offline\\app"])
        self.assertNotIn(CWD_ABSENT_HERE, plan.items[0].codes)

    def test_a_rule_for_another_pair_of_machines_is_not_applied(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead("D:\\Projects\\offline")
        rules = [PathMappingRule("other", "laptop", "desktop", "D:\\Projects", "E:\\Work")]
        folders = _Folders("E:\\Work\\offline")
        plan = self._plan(
            local, remote, layout_id=LAYOUT, folder_exists=folders, path_rules=rules,
        )
        self.assertEqual(folders.asked, ["D:\\Projects\\offline"])
        self.assertIn(CWD_ABSENT_HERE, plan.items[0].codes)

    def test_an_ambiguous_rule_is_reported_not_resolved(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        local, remote = self._cloud_ahead("D:\\Projects\\offline")
        rules = [
            PathMappingRule("one", "desktop", "laptop", "D:\\Projects", "E:\\Work"),
            PathMappingRule("two", "desktop", "laptop", "D:\\Projects", "F:\\Work"),
        ]
        folders = _Folders("E:\\Work\\offline", "F:\\Work\\offline")
        plan = self._plan(
            local, remote, layout_id=LAYOUT, folder_exists=folders, path_rules=rules,
        )
        self.assertIn(CWD_MAPPING_AMBIGUOUS, plan.items[0].codes)
        self.assertNotIn(CWD_ABSENT_HERE, plan.items[0].codes)
        self.assertEqual(folders.asked, [])

    def test_each_folder_is_looked_up_once(self) -> None:
        PROVEN_LAYOUTS[LAYOUT] = "{state}/{file_name}"
        first_local, first_remote = self._cloud_ahead(session_id="s1", name="a.jsonl")
        second_local, second_remote = self._cloud_ahead(session_id="s2", name="b.jsonl")
        folders = _Folders()
        plan = self._plan(
            first_local + second_local, first_remote + second_remote,
            layout_id=LAYOUT, folder_exists=folders,
        )
        self.assertEqual(folders.asked, [ELSEWHERE])
        self.assertTrue(all(CWD_ABSENT_HERE in item.codes for item in plan.items))


class LocalFolderExistsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"folder-exists-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_existing_directory_is_found(self) -> None:
        self.assertTrue(local_folder_exists(str(self.root.resolve())))

    def test_a_missing_directory_is_absent(self) -> None:
        self.assertFalse(local_folder_exists(str((self.root / "gone").resolve())))

    def test_a_file_is_not_a_working_folder(self) -> None:
        path = self.root / "file.txt"
        path.write_text("x", encoding="utf-8")
        self.assertFalse(local_folder_exists(str(path.resolve())))

    def test_a_path_of_another_platform_is_absent_rather_than_tried_here(self) -> None:
        # On Windows "/" would otherwise resolve to the root of the current drive.
        foreign = "/" if os.name == "nt" else "C:\\Windows"
        self.assertFalse(local_folder_exists(foreign))

    def test_a_relative_path_is_absent(self) -> None:
        self.assertFalse(local_folder_exists("."))

    def test_nothing_is_created_by_asking(self) -> None:
        before = sorted(self.root.rglob("*"))
        local_folder_exists(str((self.root / "missing" / "deeper").resolve()))
        self.assertEqual(sorted(self.root.rglob("*")), before)


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class WorkingFolderWiringTests(unittest.TestCase):
    """`sessions scan` and the rebuild an apply runs must reach the same answer."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"working-folder-app-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        self.offline = (self.root / "projects" / "offline").resolve()
        self.mapped = (self.root / "work" / "offline").resolve()
        (self.local_dir / "sessions").mkdir(parents=True)
        (self.cloud_dir / "sessions").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _config(self, *, mapping: bool = False) -> Path:
        rule = textwrap.dedent(
            f"""
            [[path_mappings]]
            rule_id = "move"
            source_machine = "desktop"
            target_machine = "laptop"
            from = "{self.offline.parent}"
            to = "{self.mapped.parent}"
            """
        ).replace("\\", "\\\\") if mapping else ""
        path = self.root / "config.toml"
        path.write_text(
            textwrap.dedent(
                f"""
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
                """
            ).strip()
            + "\n"
            + rule,
            encoding="utf-8",
        )
        return path

    def _cloud_only_chat(self) -> None:
        rows = [
            {"type": "session_meta", "payload": {"id": "s1", "cwd": str(self.offline)}},
            {"type": "event", "record": "1"},
        ]
        (self.cloud_dir / "sessions" / "s1.jsonl").write_bytes(
            b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows)
        )

    def _scan(self, config: Path):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return scan_session_transfer(config, source_machine="desktop", target_machine="laptop")

    def _codes(self, plan) -> tuple[str, ...]:
        (item,) = plan.items
        return item.codes

    def test_a_scan_reports_a_chat_whose_folder_is_not_here(self) -> None:
        self._cloud_only_chat()
        plan = self._scan(self._config())
        self.assertIn(CWD_ABSENT_HERE, self._codes(plan))

    def test_a_scan_finds_the_folder_where_the_config_rule_puts_it(self) -> None:
        self._cloud_only_chat()
        self.mapped.mkdir(parents=True)
        plan = self._scan(self._config(mapping=True))
        self.assertNotIn(CWD_ABSENT_HERE, self._codes(plan))

    def test_the_rebuild_an_apply_runs_agrees_with_the_scan(self) -> None:
        self._cloud_only_chat()
        config = self._config()
        plan = self._scan(config)
        fresh, _, _ = _rebuild_transfer_plan(
            load_config(config), self.local_dir, self.cloud_dir, plan, None
        )
        self.assertIn(CWD_ABSENT_HERE, self._codes(fresh))
        self.assertEqual(fresh.plan_id, plan.plan_id)

    def test_the_cli_report_counts_them_without_naming_a_folder(self) -> None:
        self._cloud_only_chat()
        config = self._config()
        out = io.StringIO()
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()), redirect_stdout(out):
            main([
                "-c", str(config), "sessions", "scan",
                "--source-machine", "desktop", "--target-machine", "laptop",
            ])
        report = json.loads(out.getvalue())
        self.assertEqual(report["cwd_absent_here"], 1)
        self.assertNotIn("offline", out.getvalue())

    def test_a_folder_created_after_the_scan_asks_for_a_rescan(self) -> None:
        self._cloud_only_chat()
        config = self._config()
        plan = self._scan(config)
        self.offline.mkdir(parents=True)
        fresh, _, _ = _rebuild_transfer_plan(
            load_config(config), self.local_dir, self.cloud_dir, plan, None
        )
        self.assertNotIn(CWD_ABSENT_HERE, self._codes(fresh))
        self.assertNotEqual(fresh.plan_id, plan.plan_id)


if __name__ == "__main__":
    unittest.main()
