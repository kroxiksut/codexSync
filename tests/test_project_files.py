"""Whether a project's own files came along with its chats (D-026).

Chats and the project list travel; project folders do not. On the reference
machine eight of nineteen project folders sit outside the cloud folder and
four are not git repositories at all, so a chat continued on the other machine
can meet an older copy of the code. These tests run real git when it is
installed, in folders under the sandbox, and plain folders always.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from codexsync.app import check_project_files, run_handoff
from codexsync.project_files import (
    FilesKind,
    Verdict,
    compare,
    read_files_board,
    read_files_state,
)

try:
    from tests.test_handoff import _Workspace
except ImportError:  # bare `pytest` from the repo root, as CI runs it
    from test_handoff import _Workspace

GIT = shutil.which("git")


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class PlainFolderTests(_Workspace):
    def test_the_same_files_are_in_step(self) -> None:
        here, there = self.root / "here", self.root / "there"
        for root in (here, there):
            write(root / "notes" / "chapter.md", "one")
            write(root / "node_modules" / "x.js", "ignored")
        write(there / "node_modules" / "y.js", "a tool folder says nothing")
        self.assertIs(compare(here, read_files_state(here), read_files_state(there)).verdict, Verdict.IN_STEP)

    def test_newer_files_there_warn_and_newer_files_here_do_not(self) -> None:
        here, there = self.root / "here", self.root / "there"
        write(here / "a.md", "one")
        write(there / "a.md", "one, then more")
        os.utime(here / "a.md", ns=(1_000_000_000, 1_000_000_000))
        os.utime(there / "a.md", ns=(2_000_000_000, 2_000_000_000))
        result = compare(here, read_files_state(here), read_files_state(there))
        self.assertIs(result.verdict, Verdict.FILES_CHANGED_THERE)
        self.assertEqual(result.newer_there, ("a.md",))
        self.assertIs(compare(there, read_files_state(there), read_files_state(here)).verdict, Verdict.AHEAD_HERE)

    def test_an_edit_that_keeps_the_size_is_seen(self) -> None:
        here, there = self.root / "here", self.root / "there"
        write(here / "a.md", "cat")
        write(there / "a.md", "dog")
        os.utime(here / "a.md", ns=(1_000_000_000, 1_000_000_000))
        os.utime(there / "a.md", ns=(2_000_000_000, 2_000_000_000))
        self.assertEqual(compare(here, read_files_state(here), read_files_state(there)).newer_there, ("a.md",))

    def test_a_file_only_there_is_listed_and_a_copy_with_another_time_is_the_same(self) -> None:
        here, there = self.root / "here", self.root / "there"
        write(here / "a.md", "same")
        write(there / "a.md", "same")
        write(there / "new" / "b.md", "new")
        os.utime(there / "a.md", ns=(9_000_000_000, 9_000_000_000))  # copied later, same bytes
        result = compare(here, read_files_state(here), read_files_state(there))
        self.assertEqual((result.newer_there, result.missing_here), ((), ("new/b.md",)))

    def test_an_unchanged_file_is_not_read_twice(self) -> None:
        from unittest import mock
        import codexsync.project_files as module

        folder = self.root / "here"
        write(folder / "a.md", "text")
        cache = self.root / "cache"
        read_files_state(folder, cache_root=cache)
        with mock.patch.object(module, "_hash", side_effect=AssertionError("read again")):
            state = read_files_state(folder, cache_root=cache)
        self.assertIsNotNone(state.entries["a.md"][1])

    def test_a_missing_folder_is_said(self) -> None:
        there = self.root / "there"
        write(there / "a.md", "x")
        state = read_files_state(self.root / "nowhere")
        self.assertIs(state.kind, FilesKind.MISSING)
        self.assertIs(compare(self.root / "nowhere", state, read_files_state(there)).verdict, Verdict.MISSING_HERE)


def make_writable(root: Path) -> None:
    """Git writes its objects read-only, which Windows refuses to delete."""
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                os.chmod(Path(directory) / name, 0o666)
            except OSError:
                pass


@unittest.skipUnless(GIT, "git is not installed")
class GitFolderTests(_Workspace):
    def setUp(self) -> None:
        super().setUp()
        # Runs before the workspace's own removal (cleanups run last-in first).
        self.addCleanup(make_writable, self.root)
        self.there = self.root / "there"
        self.there.mkdir()
        git(self.there, "init", "-q", "-b", "main")
        write(self.there / "app.py", "print(1)\n")
        git(self.there, "add", "-A")
        git(self.there, "commit", "-q", "-m", "one")
        self.here = self.root / "here"
        subprocess.run(["git", "clone", "-q", str(self.there), str(self.here)], check=True, capture_output=True)

    def verdict(self) -> Verdict:
        return compare(self.here, read_files_state(self.here), read_files_state(self.there)).verdict

    def test_the_same_commit_is_in_step(self) -> None:
        self.assertIs(read_files_state(self.here).kind, FilesKind.GIT)
        self.assertIs(self.verdict(), Verdict.IN_STEP)

    def test_a_commit_made_there_and_not_pulled_is_behind(self) -> None:
        write(self.there / "app.py", "print(2)\n")
        git(self.there, "commit", "-q", "-am", "two")
        self.assertIs(self.verdict(), Verdict.BEHIND)
        git(self.here, "fetch", "-q")
        self.assertIs(self.verdict(), Verdict.BEHIND, "fetched is not checked out")
        git(self.here, "merge", "-q", "--ff-only", "origin/main")
        self.assertIs(self.verdict(), Verdict.IN_STEP)

    def test_more_commits_here_say_nothing(self) -> None:
        write(self.here / "app.py", "print(3)\n")
        git(self.here, "commit", "-q", "-am", "three")
        self.assertIs(self.verdict(), Verdict.AHEAD_HERE)

    def test_commits_on_both_sides_have_diverged(self) -> None:
        write(self.there / "a.txt", "there\n")
        git(self.there, "add", "-A")
        git(self.there, "commit", "-q", "-m", "there")
        write(self.here / "b.txt", "here\n")
        git(self.here, "add", "-A")
        git(self.here, "commit", "-q", "-m", "here")
        git(self.here, "fetch", "-q")
        self.assertIs(self.verdict(), Verdict.DIVERGED)

    def test_changes_left_uncommitted_there_are_said_unless_they_are_here_too(self) -> None:
        write(self.there / "app.py", "print('wip')\n")
        self.assertIs(self.verdict(), Verdict.UNCOMMITTED_THERE)
        write(self.here / "app.py", "print('wip')\n")
        self.assertIs(self.verdict(), Verdict.IN_STEP, "a cloud folder carried the same edit")

    def test_reading_writes_nothing_into_the_repository(self) -> None:
        write(self.here / "app.py", "print('touched')\n")
        index = self.here / ".git" / "index"
        before = index.stat().st_mtime_ns
        read_files_state(self.here)
        self.assertEqual(index.stat().st_mtime_ns, before)


class FullSyncTests(_Workspace):
    def codex_state(self, local: Path, projects: dict[str, Path]) -> None:
        state = {
            "local-projects": {
                key: {"createdAt": 1, "id": key, "name": key, "rootPaths": [str(root)], "updatedAt": 2}
                for key, root in projects.items()
            },
            "project-order": list(projects),
            "pinned-project-ids": [],
            "thread-project-assignments": {},
        }
        (local / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")

    def test_the_laptop_is_told_its_folder_is_older(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        on_desktop = write(self.root / "desktop-work" / "book" / "chapter.md", "chapter two").parent
        on_laptop = write(self.root / "laptop-work" / "book" / "chapter.md", "chapter").parent
        os.utime(on_laptop / "chapter.md", ns=(1_000_000_000, 1_000_000_000))
        self.codex_state(desktop_codex, {"book": on_desktop})
        self.codex_state(laptop_codex, {"book": on_laptop})

        run_handoff(desktop)
        self.assertIn("desktop", read_files_board(self.workspace / "project-files").publications)
        loaded = run_handoff(laptop)
        self.assertEqual(
            [(item.name, item.verdict, item.peer) for item in loaded.project_files_behind],
            [("book", Verdict.FILES_CHANGED_THERE, "desktop")],
        )
        # Brought over by hand: the next check is quiet.
        shutil.copy2(on_desktop / "chapter.md", on_laptop / "chapter.md")
        self.assertEqual(check_project_files(laptop).warnings, ())

    def test_files_and_folders_that_come_and_go_are_followed(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        there = self.root / "desktop-work" / "book"
        here = self.root / "laptop-work" / "book"
        for root in (there, here):
            write(root / "keep.md", "kept")
            write(root / "old.md", "old")
        os.utime(here / "old.md", ns=(1_000_000_000, 1_000_000_000))
        gone = self.root / "laptop-work" / "gone"
        write(gone / "a.md", "a")
        self.codex_state(desktop_codex, {"book": there, "gone": self.root / "desktop-work" / "gone"})
        self.codex_state(laptop_codex, {"book": here, "gone": gone})

        # The desktop publishes on its first check, before any full sync.
        check_project_files(desktop, publish=True)
        (there / "old.md").unlink()
        check_project_files(desktop, publish=True)

        report = check_project_files(laptop)
        by_name = {item.name: item for item in report.warnings}
        self.assertEqual(by_name["book"].removed_there, ("old.md",))
        self.assertIs(by_name["gone"].verdict, Verdict.MISSING_THERE)

        # Brought back there: forgotten again, nothing left to say for it.
        write(there / "old.md", "old")
        os.utime(there / "old.md", ns=(1_000_000_000, 1_000_000_000))
        check_project_files(desktop, publish=True)
        published = read_files_board(self.workspace / "project-files").publications["desktop"]
        self.assertEqual(published.projects["book"]["state"].removed, {})
        self.assertNotIn("book", {item.name for item in check_project_files(laptop).warnings})

    def test_a_project_whose_chats_moved_on_there_is_said_first(self) -> None:
        import sqlite3

        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        roots = {}
        for name in ("alpha", "book"):
            there = write(self.root / "desktop-work" / name / "a.md", f"{name} two").parent
            here = write(self.root / "laptop-work" / name / "a.md", f"{name} one").parent
            os.utime(here / "a.md", ns=(1_000_000_000, 1_000_000_000))
            roots[name] = (there, here)
        self.codex_state(desktop_codex, {name: pair[0] for name, pair in roots.items()})
        self.codex_state(laptop_codex, {name: pair[1] for name, pair in roots.items()})
        connection = sqlite3.connect(desktop_codex / "state_5.sqlite")
        connection.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER, cwd TEXT, updated_at INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES ('t1', 'x', 0, ?, 1775000000)", ("\\\\?\\" + str(roots["book"][0]),)
        )
        connection.commit()
        connection.close()

        run_handoff(desktop)
        report = check_project_files(laptop)
        self.assertEqual([item.name for item in report.warnings], ["book", "alpha"])
        self.assertEqual(report.warnings[0].chats_there_at, 1775000000)
        self.assertEqual(report.warnings[1].chats_there_at, 0, "files moved outside any chat")


if __name__ == "__main__":
    unittest.main()
