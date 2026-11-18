"""Second review, handoff/config/CLI part (CS-494, 501-504, 507, 511, 536-542).

Each test names the finding it pins; each failed before its fix.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from test_handoff import SANDBOX, _Workspace

from codexsync.app import handoff_status, run_handoff
from codexsync.config import _paths_overlap, codex_folder_holding, load_config
from codexsync.config_edit import change_config_value, list_config_history, parse_config_value
from codexsync.exceptions import ConfigError
from codexsync.handoff import (
    Fingerprinter,
    HandoffError,
    HandoffRecord,
    STATE_HANDED_OFF,
    delivery,
    mark_working,
    read_board,
    record_handoff,
    record_path,
    wanted_keys,
    write_record,
)
from codexsync.operation_lock import OperationLock


def _set_roots(config: Path, roots: list[str]) -> None:
    text = config.read_text(encoding="utf-8")
    text = text.replace('include_roots = ["sessions", "skills"]', f"include_roots = {json.dumps(roots)}")
    config.write_text(text, encoding="utf-8")


class DeliveryIgnoresTheCheckersFiltersTests(_Workspace):
    """CS-501: whether A's handoff arrived does not depend on C's roots or filters."""

    def test_a_file_outside_this_machines_roots_still_counts_as_arrived(self) -> None:
        laptop, _ = self.machine("laptop")
        desktop, _ = self.machine("desktop")
        _set_roots(laptop, ["sessions", "skills", "rules"])
        self.write(self.cloud / "rules" / "default.rules", "allow")
        self.write(self.cloud / "skills" / "a.md", "one")
        own = Fingerprinter().files(load_config(laptop), strict=True)
        self.assertEqual(len(own), 2)
        record_handoff(self.handoff_root, "laptop", files=own, taken=())

        record = read_board(self.handoff_root).own("laptop")
        seen = Fingerprinter().files(load_config(desktop), wanted=wanted_keys([record]))
        self.assertTrue(delivery(record, seen).delivered, "desktop's roots leave out rules/")
        status = handoff_status(desktop, check_delivery=True)
        self.assertEqual([item.delivered for item in status.deliveries], [True])

    def test_a_file_this_machine_excludes_still_counts_as_arrived(self) -> None:
        laptop, _ = self.machine("laptop")
        desktop, _ = self.machine("desktop")
        text = desktop.read_text(encoding="utf-8").replace("[filters]", "[filters_unused]")
        desktop.write_text(text + '\n[filters]\nexclude_globs = ["**/*.md"]\n', encoding="utf-8")
        self.write(self.cloud / "skills" / "a.md", "one")
        own = Fingerprinter().files(load_config(laptop), strict=True)
        record = HandoffRecord("laptop", STATE_HANDED_OFF, "t", "l1", files=own)
        seen = Fingerprinter().files(load_config(desktop), wanted=wanted_keys([record]))
        self.assertTrue(delivery(record, seen).delivered)

    def test_only_the_wanted_files_are_hashed(self) -> None:
        desktop, _ = self.machine("desktop")
        self.write(self.cloud / "skills" / "a.md", "one")
        self.write(self.cloud / "skills" / "b.md", "two")
        self.assertEqual(Fingerprinter().files(load_config(desktop), wanted=set()), {})


class OwnHandoffFileUnreadableTests(_Workspace):
    """CS-502: a moment's failure to read this machine's file never starts it over."""

    def _corrupt(self) -> tuple[Path, str]:
        record_handoff(self.handoff_root, "desktop", files={}, taken=())
        write_record(self.handoff_root, HandoffRecord(
            "laptop", STATE_HANDED_OFF, "t", "l1", 4, "t", {"desktop": "d1"},
        ))
        path = record_path(self.handoff_root, "laptop")
        good = path.read_text(encoding="utf-8")
        path.write_text(good[: len(good) // 2], encoding="utf-8")
        return path, good

    def test_a_file_that_stays_unreadable_refuses_the_write(self) -> None:
        path, _good = self._corrupt()
        half = path.read_text(encoding="utf-8")
        for call in (
            lambda: record_handoff(self.handoff_root, "laptop", files={}, taken=(), sleep=lambda s: None),
            lambda: mark_working(self.handoff_root, "laptop", sleep=lambda s: None),
        ):
            with self.assertRaises(HandoffError):
                call()
            self.assertEqual(path.read_text(encoding="utf-8"), half, "not rewritten from nothing")

    def test_a_file_that_reads_again_keeps_what_it_had(self) -> None:
        path, good = self._corrupt()

        def cloud_client_lets_go(seconds: float) -> None:
            path.write_text(good, encoding="utf-8")

        record = record_handoff(
            self.handoff_root, "laptop", files={}, taken=(), new_handoff=False, sleep=cloud_client_lets_go,
        )
        self.assertEqual(record.handoff_id, "l1")
        self.assertEqual(record.generation, 4)
        self.assertEqual(record.accepted, {"desktop": "d1"})


    def test_a_full_sync_says_so_and_keeps_the_handoff_owed(self) -> None:
        from codexsync import handoff as handoff_module
        from codexsync.exceptions import FailSafeError

        desktop, desktop_codex = self.machine("desktop")
        self.write(desktop_codex / "skills" / "tool.md", "one")
        run_handoff(desktop)
        path = record_path(self.handoff_root, "desktop")
        path.write_text(path.read_text(encoding="utf-8")[:20], encoding="utf-8")
        self.write(desktop_codex / "skills" / "tool.md", "two")
        with mock.patch.object(handoff_module, "OWN_READ_PAUSES", ()):
            with self.assertRaises(FailSafeError) as caught:
                run_handoff(desktop)
        self.assertEqual(caught.exception.code, "HANDOFF_FILE_UNREADABLE")
        path.unlink()
        self.assertTrue(run_handoff(desktop).handed_off, "the work written is still announced")


class SendOnlyRunTakesNothingTests(_Workspace):
    """CS-503: `sync.direction = to_cloud` loads nothing, so it takes no handoff."""

    def test_a_send_only_run_leaves_the_other_handoff_pending(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, _ = self.machine("laptop")
        self.write(desktop_codex / "skills" / "tool.md", "from desktop")
        run_handoff(desktop)

        result = run_handoff(laptop, direction="to_cloud")
        self.assertEqual(result.taken, ())
        self.assertGreaterEqual(result.files_held_back, 1)
        board = read_board(self.handoff_root)
        self.assertEqual([record.machine for record in board.pending("laptop")], ["desktop"])
        self.assertNotIn("desktop", board.own("laptop").accepted)

        both = run_handoff(laptop)
        self.assertEqual(both.taken, ("desktop",))
        self.assertEqual(read_board(self.handoff_root).pending("laptop"), [])

    def test_the_console_says_what_was_not_loaded(self) -> None:
        from codexsync.app import HandoffResult

        out = io.StringIO()
        from codexsync.cli import _print_handoff_result

        record = HandoffRecord("laptop", STATE_HANDED_OFF, "t", "l1")
        result = HandoffResult("laptop", (), 0, 0, False, record, files_held_back=2, chats_held_back=1)
        with contextlib.redirect_stdout(out):
            _print_handoff_result(result)
        self.assertIn("loaded from: nothing new", out.getvalue())
        self.assertIn("not loaded: 2", out.getvalue())
        self.assertIn("not loaded: 1", out.getvalue())


class HandoffOwedFromTheFirstCloudWriteTests(_Workspace):
    """CS-504: a run that fails after writing to the cloud still hands it off next time."""

    def test_a_failure_in_the_chat_half_after_the_settings_were_written(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        self.write(desktop_codex / "skills" / "tool.md", "one")
        first = run_handoff(desktop)
        self.write(desktop_codex / "skills" / "tool.md", "two")
        with mock.patch("codexsync.app.apply_session_transfer", side_effect=RuntimeError("chats failed")):
            with self.assertRaises(RuntimeError):
                run_handoff(desktop)
        self.assertEqual((self.cloud / "skills" / "tool.md").read_text(encoding="utf-8"), "two")

        again = run_handoff(desktop)
        self.assertTrue(again.handed_off, "what the failed run wrote is announced")
        self.assertNotEqual(again.record.handoff_id, first.record.handoff_id)
        settled = run_handoff(desktop)
        self.assertFalse(settled.handed_off, "and only once")


class MachineIdTests(unittest.TestCase):
    """CS-494, CS-537: only an id every record keys alike is accepted."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"machine-id-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def load(self, value: str) -> object:
        config = self.root / "config.toml"
        config.write_text(
            f"[identity]\nmachine_id = {value}\n\n[paths]\nlocal_state_dir = \"{(self.root / 'codex').as_posix()}\"\n"
            f"cloud_root_dir = \"{(self.root / 'cloud').as_posix()}\"\n"
            f"backup_dir = \"{(self.root / 'backup').as_posix()}\"\n"
            f"temp_dir = \"{(self.root / 'tmp').as_posix()}\"\n",
            encoding="utf-8",
        )
        return load_config(config)

    def test_ids_that_lose_meaning_are_refused_with_the_stored_form(self) -> None:
        for value, code, named in (
            ('"Laptop-дом"', "MACHINE_ID_NOT_FILE_SAFE", "Laptop"),
            ('"My PC"', "MACHINE_ID_NOT_FILE_SAFE", "My-PC"),
            ('"Ноутбук"', "MACHINE_ID_UNUSABLE", None),
            ('"unknown-machine"', "MACHINE_ID_RESERVED", None),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    self.load(value)
                self.assertEqual(caught.exception.code, code)
                if named:
                    self.assertIn(f'machine_id = "{named}"', str(caught.exception))

    def test_a_usable_id_loads_as_written(self) -> None:
        for value in ("machine-a", "DESKTOP-ABC123", "oldNout", "Fyodors-MacBook-Pro.local"):
            with self.subTest(value=value):
                self.assertEqual(self.load(json.dumps(value)).identity.machine_id, value)
        self.assertIsNone(self.load('""').identity.machine_id)

    def test_an_id_that_is_not_text_is_a_config_error(self) -> None:
        for value in ("42", "[\"a\"]", "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "must be a string"):
                self.load(value)

    def test_one_lock_for_both_spellings_of_one_machine(self) -> None:
        # CS-511: `state-backup` keyed the lock by the stored id, the rest by the raw one.
        state = self.root / "codex"
        state.mkdir()
        raw = OperationLock(self.root, state_root=state, machine_id="My PC", family="sync")
        stored = OperationLock(self.root, state_root=state, machine_id="My-PC", family="state-backup")
        self.assertEqual(raw.path, stored.path)


class CaseTwinTests(_Workspace):
    """CS-494: two machines whose ids differ only in case share one file in the cloud."""

    def test_a_handoff_refuses_while_another_machine_shares_the_id_but_for_case(self) -> None:
        write_record(self.handoff_root, HandoffRecord("Laptop", STATE_HANDED_OFF, "t", "x1"))
        laptop, _ = self.machine("laptop")
        with self.assertRaises(ConfigError) as caught:
            run_handoff(laptop)
        self.assertEqual(caught.exception.code, "MACHINE_ID_CASE_TWIN")

    def test_a_file_overwritten_by_the_case_twin_says_so(self) -> None:
        write_record(self.handoff_root, HandoffRecord("laptop", STATE_HANDED_OFF, "t", "x1"))
        record_path(self.handoff_root, "laptop").rename(self.handoff_root / "tmp.json")
        (self.handoff_root / "tmp.json").rename(self.handoff_root / "LAPTOP.json")
        board = read_board(self.handoff_root)
        self.assertIn("differ only in case", board.unreadable["LAPTOP.json"])


class DoctorGuardianPointerTests(unittest.TestCase):
    """CS-536: doctor looks for the pointer under the name Guardian files it by."""

    def test_the_pointer_is_found_by_the_normalised_id(self) -> None:
        from codexsync.preflight import _check_guardian_latest_good

        root = SANDBOX / f"doctor-pointer-{uuid.uuid4().hex[:8]}"
        self.addCleanup(shutil.rmtree, root, True)
        (root / "latest-good").mkdir(parents=True)
        (root / "latest-good" / "Office-PC.json").write_text('{"snapshot_id": "s1"}', encoding="utf-8")
        cfg = mock.Mock()
        cfg.identity.machine_id = "Office PC"
        cfg.guardian.root_dir = root
        with mock.patch("codexsync.preflight.GUARDIAN_LATEST_GOOD_DIR_NAME", "latest-good"):
            result = _check_guardian_latest_good(cfg)
        self.assertNotIn("No latest-good pointer", result.details)


class ConfigSetNumbersTests(unittest.TestCase):
    """CS-538: `config set` takes a fraction where the loader does."""

    def test_a_fraction_for_a_whole_number_in_the_template(self) -> None:
        self.assertEqual(parse_config_value("guardian.poll_interval_seconds", "0.5"), 0.5)

    def test_the_loader_still_refuses_a_fraction_where_it_wants_a_whole_number(self) -> None:
        from codexsync.config_edit import create_config

        root = SANDBOX / f"config-set-{uuid.uuid4().hex[:8]}"
        (root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root, True)
        config = root / "config.toml"
        create_config(config, machine_id="laptop", local_state_dir=str(root / "codex"),
                      workspace_root_dir=str(root / "workspace"))
        change_config_value(config, "guardian.poll_interval_seconds", 0.5)
        self.assertEqual(load_config(config).guardian.poll_interval_seconds, 0.5)
        with self.assertRaisesRegex(ConfigError, "must be an integer"):
            change_config_value(config, "backup.retention_days", parse_config_value("backup.retention_days", "2.5"))


class DoctorReportExitTests(unittest.TestCase):
    """CS-539: `doctor --report` exits like `doctor` (AI_RULES 7)."""

    def test_a_failed_check_exits_five(self) -> None:
        from codexsync.cli import main

        for checks, code in (({"a": "PASS", "b": "FAIL"}, 5), ({"a": "PASS", "b": "WARN"}, 0)):
            with self.subTest(checks=checks):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        mock.patch("codexsync.cli.configure_logging"), \
                        mock.patch("codexsync.cli.build_issue_report", return_value={"checks": checks}):
                    self.assertEqual(main(["-c", "config.toml", "doctor", "--report"]), code)
                self.assertEqual(json.loads(out.getvalue())["checks"], checks)


class RootsListingTests(_Workspace):
    """CS-540: `config roots` lists only inside `.codex`, never a secret folder."""

    def test_outside_and_secret_folders_are_refused(self) -> None:
        from codexsync.sync_candidates import list_sync_candidates

        config, local = self.machine("laptop")
        (local / ".sandbox-secrets").mkdir()
        (local / ".sandbox-secrets" / "key").write_text("x", encoding="utf-8")
        cfg = load_config(config)
        for node, code in (
            ("..", "ROOT_OUTSIDE_STATE"),
            ("skills/../..", "ROOT_OUTSIDE_STATE"),
            ("C:/Windows", "ROOT_OUTSIDE_STATE"),
            (".sandbox-secrets", "ROOT_IS_SECRET"),
            ("skills/auth.json", "ROOT_IS_SECRET"),
        ):
            with self.subTest(node=node), self.assertRaises(ConfigError) as caught:
                list_sync_candidates(cfg, node)
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn(".sandbox-secrets", [item.name for item in list_sync_candidates(cfg, "")])


class CaseBlindPathsTests(unittest.TestCase):
    """CS-541: on macOS `~/.Codex` is `~/.codex`."""

    def test_overlap_ignores_case_where_the_system_does(self) -> None:
        with mock.patch("codexsync.config._ignores_case", return_value=True):
            self.assertTrue(_paths_overlap(Path("/Users/a/.Codex/x"), Path("/Users/a/.codex")))
            self.assertEqual(
                codex_folder_holding(Path("/Users/a/.CODEX/config.toml"), Path("/Users/a/.codex"), homes=False),
                Path("/Users/a/.codex"),
            )
        with mock.patch("codexsync.config._ignores_case", return_value=False):
            if Path("/A") != Path("/a"):  # a case-sensitive path type
                self.assertFalse(_paths_overlap(Path("/Users/a/.Codex/x"), Path("/Users/a/.codex")))


class SmallConfigTests(_Workspace):
    """CS-542: advice names a setting that exists; history shows this machine's versions."""

    def test_the_handoff_folder_advice_names_the_manifest(self) -> None:
        config, _ = self.machine("laptop", enabled=True)
        text = config.read_text(encoding="utf-8").replace(
            f'root_dir = "{self.handoff_root.as_posix()}"', 'root_dir = ""',
        ).replace(f'manifest_file = "{(self.workspace / "manifest.json").as_posix()}"', "")
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "state.manifest_file") as caught:
            load_config(config)
        self.assertNotIn("handoff.root_dir is empty", str(caught.exception))

    def test_history_lists_only_this_machines_versions(self) -> None:
        laptop, _ = self.machine("laptop")
        desktop, _ = self.machine("desktop")
        change_config_value(laptop, "backup.retention_days", 9)
        change_config_value(desktop, "backup.retention_days", 8)
        mine = list_config_history(load_config(laptop), laptop)
        self.assertEqual(len(mine), 1)
        self.assertIn('machine_id = "laptop"', mine[0].path.read_text(encoding="utf-8"))
        self.assertEqual(len(list_config_history(load_config(desktop), desktop)), 1)


if __name__ == "__main__":
    unittest.main()
