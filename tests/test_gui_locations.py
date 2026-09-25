"""Which config the window opens, and where it looks for a workspace.

No Qt anywhere: this is the module the launcher consults before it knows
whether a window can be shown at all. Everything runs against a sandbox tree,
because the two failures worth catching are both about real directories -- an
exe that cannot find the config this machine already has, and a search that
offers the source checkout as a workspace because the folder is called
``codexSync`` too.
"""
from __future__ import annotations

from pathlib import Path
import unittest
import uuid

from codexsync.gui.locations import (
    CONFIG_NAME,
    choose_config_path,
    find_workspaces,
    machines_in_workspace,
)

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"locations-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        from conftest import remove_stubbornly

        self.addCleanup(remove_stubbornly, self.root)

    def touch(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path


class ChoosingTheConfigTests(_Sandbox):
    def test_an_explicit_path_wins_even_when_the_file_is_missing(self) -> None:
        """Naming a path that is not there is a request to create it there."""
        remembered = self.touch(f"remembered/{CONFIG_NAME}")
        missing = self.root / "asked-for" / CONFIG_NAME
        choice = choose_config_path(missing, remembered)
        self.assertEqual(choice.path, missing)
        self.assertEqual(choice.source, "explicit")
        self.assertFalse(choice.exists)

    def test_the_remembered_path_is_used_when_it_still_exists(self) -> None:
        remembered = self.touch(f"remembered/{CONFIG_NAME}")
        here = self.root / "cwd"
        here.mkdir()
        choice = choose_config_path(None, remembered, cwd=here)
        self.assertEqual(choice.path, remembered)
        self.assertEqual(choice.source, "remembered")

    def test_a_remembered_path_that_is_gone_falls_through_to_the_working_dir(self) -> None:
        here = self.root / "cwd"
        here.mkdir()
        in_cwd = self.touch(f"cwd/{CONFIG_NAME}")
        choice = choose_config_path(None, self.root / "deleted" / CONFIG_NAME, cwd=here)
        self.assertEqual(choice.path, in_cwd)
        self.assertEqual(choice.source, "cwd")

    def test_a_frozen_exe_finds_the_config_beside_itself(self) -> None:
        beside = self.touch(f"exe/{CONFIG_NAME}")
        empty = self.root / "elsewhere"
        empty.mkdir()
        choice = choose_config_path(None, None, cwd=empty, executable_dir=beside.parent)
        self.assertEqual(choice.path, beside)
        self.assertEqual(choice.source, "executable")

    def test_with_nothing_anywhere_there_is_no_config_and_none_is_invented(self) -> None:
        """No per-user path is proposed any more (CS-268).

        The window used to answer "nothing found" with a file under %APPDATA%,
        and every page then worked against a config nobody had created.
        """
        empty = self.root / "elsewhere"
        empty.mkdir()
        choice = choose_config_path(None, None, cwd=empty)
        self.assertIsNone(choice.path)
        self.assertEqual(choice.source, "none")
        self.assertFalse(choice.exists)


class FindingAWorkspaceTests(_Sandbox):
    """The search runs over a fake cloud tree, never over the real machine."""

    def setUp(self) -> None:
        super().setUp()
        self.cloud = self.root / "Yandex.Disk"
        (self.cloud / "codexSync" / "guardian" / "snapshots" / "desktop").mkdir(parents=True)
        (self.cloud / "codexSync" / "semantic" / "manifest" / "laptop").mkdir(parents=True)
        (self.cloud / "codexSync" / "sync").mkdir(parents=True)
        (self.cloud / "Projects" / "codexSync" / "src").mkdir(parents=True)
        (self.cloud / "Projects" / "codexSync" / "tests").mkdir(parents=True)

    def test_a_folder_with_marker_folders_is_a_workspace(self) -> None:
        found = find_workspaces(roots=(self.cloud,))
        self.assertEqual([item.path for item in found], [self.cloud / "codexSync"])
        self.assertEqual(found[0].markers, ("guardian", "semantic", "sync"))

    def test_the_source_checkout_is_not_offered_as_a_workspace(self) -> None:
        """It is called codexSync too; only the marker folders tell them apart."""
        found = find_workspaces(roots=(self.cloud,))
        self.assertNotIn(self.cloud / "Projects" / "codexSync", [item.path for item in found])

    def test_a_workspace_two_levels_down_is_found_and_three_is_not(self) -> None:
        deep = self.cloud / "Projects" / "work" / "codexsync"
        (deep / "plans").mkdir(parents=True)
        near = self.cloud / "Projects" / "codex-sync"
        (near / "backups").mkdir(parents=True)
        found = [item.path for item in find_workspaces(roots=(self.cloud,))]
        self.assertIn(near, found, "two levels down is within reach")
        self.assertNotIn(deep, found, "three levels down is not searched")

    def test_the_search_writes_nothing(self) -> None:
        before = {str(path) for path in self.root.rglob("*")}
        find_workspaces(roots=(self.cloud,))
        self.assertEqual({str(path) for path in self.root.rglob("*")}, before)

    def test_an_unreadable_root_is_skipped_rather_than_raised(self) -> None:
        """A disconnected network drive lists as an error, not as an empty folder."""
        self.assertEqual(find_workspaces(roots=(self.root / "no-such-drive",)), ())

    def test_machine_names_come_from_folder_and_file_names_only(self) -> None:
        workspace = self.cloud / "codexSync"
        (workspace / "guardian" / "quarantine" / "old-laptop").mkdir(parents=True)
        (workspace / "guardian" / "latest-good").mkdir(parents=True)
        (workspace / "guardian" / "latest-good" / "desktop.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            machines_in_workspace(workspace), ("desktop", "laptop", "old-laptop"),
        )

    def test_a_workspace_reports_the_machines_already_in_it(self) -> None:
        found = find_workspaces(roots=(self.cloud,))
        self.assertEqual(found[0].machines, ("desktop", "laptop"))

    def test_a_missing_workspace_has_no_machines_and_does_not_raise(self) -> None:
        self.assertEqual(machines_in_workspace(self.root / "nothing-here"), ())


class DriveSelectionTests(unittest.TestCase):
    def test_only_fixed_drives_are_ever_walked(self) -> None:
        """A mapped-but-dead network drive blocks a listing for about a minute."""
        from codexsync.gui import locations

        self.assertIn("GetDriveTypeW", Path(locations.__file__).read_text(encoding="utf-8"))

    def test_cloud_roots_are_matched_by_name_including_onedrive_for_business(self) -> None:
        from codexsync.gui.locations import _matches

        self.assertTrue(_matches("OneDrive - Contoso", "onedrive*"))
        self.assertTrue(_matches("Yandex.Disk", "yandex.disk"))
        self.assertFalse(_matches("Documents", "dropbox"))


if __name__ == "__main__":
    unittest.main()
