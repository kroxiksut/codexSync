"""A file in Codex's own folder is never opened as CodexSync's config (CS-412).

Codex keeps a `config.toml` of its own in `.codex`, under the very name
CodexSync uses. Creating and saving there were refused already; opening was
not -- from `-c`, from the window, or from a start whose current folder was
`.codex`, which found Codex's file first and reported a parse error about keys
that are not CodexSync's. The refusal now comes before the file is read, and
says whose file it is.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from codexsync.cli import main
from codexsync.config import codex_folder_holding, load_config
from codexsync.config_edit import create_config, read_config_document
from codexsync.config_locations import CONFIG_NAME, choose_config_path, write_config_pointer
from codexsync.exceptions import ConfigError
from codexsync.exit_codes import ExitCode

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
#: What Codex's own file looks like: nothing CodexSync would accept.
CODEX_OWN = 'model = "gpt-5"\napproval_policy = "on-request"\n[mcp_servers.x]\ncommand = "x"\n'


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"config-in-codex-{uuid.uuid4().hex[:8]}"
        self.codex_home = self.root / "home" / ".codex"
        self.codex_home.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        env = mock.patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)})
        env.start()
        self.addCleanup(env.stop)
        self.codex_own = self.codex_home / CONFIG_NAME
        self.codex_own.write_text(CODEX_OWN, encoding="utf-8")


class LoadingTests(_Sandbox):
    def test_codexs_own_config_is_refused_before_it_is_read(self) -> None:
        with mock.patch("codexsync.config.parse_config_text") as parse:
            with self.assertRaisesRegex(ConfigError, "Codex's configuration, not CodexSync's"):
                load_config(self.codex_own)
        parse.assert_not_called()

    def test_any_depth_and_any_name_inside_the_folder_is_refused(self) -> None:
        nested = self.codex_home / "rules" / "mine.toml"
        nested.parent.mkdir()
        nested.write_text("", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(nested)

    def test_the_editor_does_not_open_it_either(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Codex's own folder"):
            read_config_document(self.codex_own)

    def test_the_configured_state_folder_counts_too(self) -> None:
        state = self.root / "elsewhere" / "codex-state"
        state.mkdir(parents=True)
        outside = self.root / "cfg" / CONFIG_NAME
        create_config(
            outside, machine_id="laptop", local_state_dir=state.as_posix(),
            workspace_root_dir=(self.root / "workspace").as_posix(),
        )
        self.assertEqual(load_config(outside).paths.local_state_dir, state)
        inside = state / CONFIG_NAME
        inside.write_bytes(outside.read_bytes())
        with self.assertRaisesRegex(ConfigError, "outside paths.local_state_dir"):
            load_config(inside)

    def test_a_folder_beside_codex_is_fine(self) -> None:
        beside = self.codex_home.parent / ".codex-sync" / CONFIG_NAME
        self.assertIsNone(codex_folder_holding(beside))
        self.assertIsNone(codex_folder_holding(self.codex_home.parent / CONFIG_NAME))
        self.assertEqual(codex_folder_holding(self.codex_own), self.codex_home)

    def test_creating_one_in_codexs_folder_is_refused_even_when_it_is_not_the_configured_one(self) -> None:
        target = self.codex_home / "codexsync" / CONFIG_NAME
        with self.assertRaises(ConfigError):
            create_config(
                target, machine_id="laptop", local_state_dir=(self.root / "other").as_posix(),
                workspace_root_dir=(self.root / "workspace").as_posix(),
            )
        self.assertFalse(target.parent.exists(), "nothing created inside .codex")

    def test_the_command_line_says_so_and_exits_4(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("codexsync.cli.configure_logging"):
            code = main(["-c", str(self.codex_own), "validate"])
        self.assertEqual(code, int(ExitCode.BAD_INPUT))
        self.assertEqual(self.codex_own.read_text(encoding="utf-8"), CODEX_OWN, "left exactly as it was")


class ChoosingTests(_Sandbox):
    def test_a_start_from_inside_codex_does_not_pick_codexs_file(self) -> None:
        choice = choose_config_path(None, None, cwd=self.codex_home)
        self.assertIsNone(choice.path)
        self.assertEqual(choice.source, "none")

    def test_the_next_place_is_tried_instead(self) -> None:
        beside = self.root / "exe"
        beside.mkdir()
        (beside / CONFIG_NAME).write_text("", encoding="utf-8")
        choice = choose_config_path(None, None, cwd=self.codex_home, executable_dir=beside)
        self.assertEqual((choice.path, choice.source), (beside / CONFIG_NAME, "executable"))

    def test_a_remembered_path_inside_codex_is_rejected_and_reported(self) -> None:
        choice = choose_config_path(None, self.codex_own, cwd=self.root)
        self.assertIsNone(choice.path)
        self.assertEqual(choice.rejected_remembered, self.codex_own)

    def test_an_explicit_path_is_kept_so_the_refusal_can_name_it(self) -> None:
        choice = choose_config_path(self.codex_own, None, cwd=self.root)
        self.assertEqual((choice.path, choice.source), (self.codex_own, "explicit"))

    def test_it_is_never_remembered(self) -> None:
        pointer = self.root / "pointer.txt"
        self.assertFalse(write_config_pointer(self.codex_own, pointer))
        self.assertFalse(pointer.exists())


if __name__ == "__main__":
    unittest.main()
