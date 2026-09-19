"""Moving a project folder: copy, verify, rename into place, remap — never delete.

Everything here runs against real files in the sandbox, because the promises
under test are about the file system: the old folder is byte-identical after
every outcome, a target folder either does not exist or holds a verified copy,
a dry run leaves the tree exactly as it was, and the only thing ever removed is
a staging directory the module can prove it created for this very plan.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from codexsync import project_move
from codexsync.exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from codexsync.guardian_models import ValidationReport, ValidationStatus
from codexsync.project_move import (
    DIRECTORY_SIZE,
    STAGING_MARKER_NAME,
    ProjectMovePlan,
    apply_project_move,
    build_project_move_plan,
    load_project_move_plan,
    save_project_move_plan,
)

try:
    from tests.test_guardian_state_isolation import forbid_writes_under
except ImportError:  # collected with tests/ itself on sys.path
    from test_guardian_state_isolation import forbid_writes_under


SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
HOST = "local:C:\\Users\\user\\.codex"
OLD_MTIME_NS = 1_600_000_000_000_000_000
LARGE_SIZE = 3 * 1024 * 1024 + 17


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _inside(path, root) -> bool:
    child, parent = _norm(path), _norm(root)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


class _Gate:
    """``require_stopped`` that can be told to refuse on its n-th call."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self) -> None:
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise SafetyPreconditionError("Codex is running")


class ProjectMoveTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"project-move-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.addCleanup(self._cleanup)
        self.source = self.root / "old" / "proj"
        self._populate_source()
        self.other = self.root / "other"
        self.other.mkdir()
        (self.root / "old" / "proj-2").mkdir()
        self.codex = self.root / ".codex"
        self.codex.mkdir()
        self.protected = [
            self.codex, self.root / "cloud", self.root / "backups",
            self.root / ".tmp", self.root / "guardian", self.root / "semantic",
        ]
        self.work = self.root / "work"
        self.work.mkdir()
        self.target = self.work / "proj"
        self.state_file = self.codex / ".codex-global-state.json"
        self.write_state(self.electron_state())
        self.sessions: list[tuple[str, str | None]] = [
            ("s-free", str(self.source)),
            ("s-free-nested", str(self.source / "src" / "app")),
            # The same folder written the other way round must still be found.
            ("s-forward", self.source.as_posix() + "/docs"),
            ("s-bound-here", str(self.source)),
            ("s-bound-elsewhere", str(self.source)),
            ("s-app-server", str(self.source)),
            ("s-other", str(self.other)),
            ("s-no-cwd", None),
            # A sibling whose name merely starts with the root's name.
            ("s-sibling", str(self.root / "old" / "proj-2")),
        ]
        self.commits: list[tuple[bytes, bytes, str, int]] = []

    def _cleanup(self) -> None:
        for link in getattr(self, "_junctions", []):
            try:
                os.rmdir(link)  # removes the junction itself, never its target
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- fixtures

    def _populate_source(self) -> None:
        (self.source / "src" / "app").mkdir(parents=True)
        (self.source / "README.md").write_text("# project\n", encoding="utf-8")
        (self.source / "src" / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (self.source / "data").mkdir()
        (self.source / "data" / "blob.bin").write_bytes(bytes(range(256)) * 7)
        (self.source / "big").mkdir()
        (self.source / "big" / "large.bin").write_bytes(bytes((i * 31) % 251 for i in range(LARGE_SIZE)))
        (self.source / "empty").mkdir()
        (self.source / "deep" / "er").mkdir(parents=True)
        (self.source / "zero.txt").write_bytes(b"")
        os.utime(self.source / "README.md", ns=(OLD_MTIME_NS, OLD_MTIME_NS))

    def electron_state(self, **overrides) -> dict:
        state = {
            "local-projects": {
                "p1": {"id": "p1", "name": "proj", "rootPaths": [str(self.source)], "createdAt": 1, "updatedAt": 2},
                "p2": {"id": "p2", "name": "other", "rootPaths": [str(self.other)], "createdAt": 3, "updatedAt": 4},
            },
            "project-order": ["p1", "p2"],
            "thread-project-assignments": {
                "s-bound-here": {"projectKind": "local", "projectId": "p1"},
                "s-bound-elsewhere": {"projectKind": "local", "projectId": "p2"},
                "s-app-server": {"projectKind": "app-server", "projectId": "as1"},
            },
            "app-server-project-id-by-legacy-project-id-by-host": {HOST: {"p1": "as1", "p2": "as2"}},
            "selected-project": {"projectId": "p1", "type": "local"},
            "thread-writable-roots": {"s-bound-here": [str(self.source)]},
        }
        state.update(overrides)
        return state

    def write_state(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    def plan(self, target: Path | None = None, *, project_id: str = "p1", protected=None,
             volatile: bool = False, now: datetime | None = None) -> ProjectMovePlan:
        return build_project_move_plan(
            state_bytes=self.state_file.read_bytes(),
            project_id=project_id,
            new_root=self.target if target is None else target,
            sessions=list(self.sessions),
            protected_roots=self.protected if protected is None else protected,
            volatile=volatile,
            now=now,
        )

    def commit(self, original: bytes, candidate: bytes, plan_id: str, count: int) -> int:
        self.assertEqual(self.state_file.read_bytes(), original)
        self.commits.append((original, candidate, plan_id, count))
        self.state_file.write_bytes(candidate)
        return count

    def failing_commit(self, original: bytes, candidate: bytes, plan_id: str, count: int) -> int:
        self.commits.append((original, candidate, plan_id, count))
        raise FailSafeError("backup could not be verified")

    def apply(self, plan: ProjectMovePlan, **overrides):
        kwargs = dict(
            confirm_plan=plan.plan_id,
            rebuild=lambda: self.plan(Path(plan.new_root)),
            require_stopped=_Gate(),
            read_state=self.state_file.read_bytes,
            commit_state=self.commit,
        )
        kwargs.update(overrides)
        return apply_project_move(plan, **kwargs)

    def tree(self, base: Path | None = None) -> dict[str, tuple]:
        base = self.root if base is None else base
        out: dict[str, tuple] = {}
        for path in sorted(base.rglob("*")):
            info = path.lstat()
            if path.is_dir():
                out[path.relative_to(base).as_posix()] = ("dir", info.st_mtime_ns)
            else:
                out[path.relative_to(base).as_posix()] = ("file", info.st_size, info.st_mtime_ns, path.read_bytes())
        return out

    def move_artifacts(self) -> list[str]:
        return sorted(p.name for p in self.work.iterdir() if p.name.startswith(".codexsync-move-"))


class PlanTests(ProjectMoveTestCase):
    def test_a_clean_plan_inventories_every_file_and_empty_directory(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.codes, ())
        self.assertEqual(plan.schema_id, "electron-v2")
        self.assertEqual(plan.project_name, "proj")
        self.assertEqual(plan.old_root, str(self.source))
        self.assertEqual(plan.new_root, os.path.abspath(self.target))
        self.assertFalse(plan.copy_complete)
        paths = {entry.relative_path: entry for entry in plan.inventory}
        self.assertEqual(
            sorted(paths),
            ["README.md", "big/large.bin", "data/blob.bin", "deep/er", "empty", "src/app/main.py", "zero.txt"],
        )
        self.assertEqual(paths["empty"].size, DIRECTORY_SIZE)
        self.assertEqual(paths["deep/er"].size, DIRECTORY_SIZE)
        self.assertEqual(paths["big/large.bin"].size, LARGE_SIZE)
        self.assertEqual(plan.file_count, 5)
        self.assertEqual(plan.total_bytes, sum(e.size for e in plan.inventory if e.size != DIRECTORY_SIZE))
        self.assertEqual(plan.inventory_sha256, project_move.inventory_digest(plan.inventory))

    def test_bindings_pin_only_chats_that_reach_the_project_by_path(self) -> None:
        plan = self.plan()
        # Bound here already: nothing to do. Bound elsewhere (including an
        # app-server id): placed on purpose, left alone. Sibling and other
        # project: not under the root.
        self.assertEqual(plan.bindings, ("s-forward", "s-free", "s-free-nested"))

    def test_the_plan_id_is_reproducible_and_ignores_the_timestamp(self) -> None:
        first = self.plan(now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        second = self.plan(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertNotEqual(first.created_at_utc, second.created_at_utc)
        self.assertEqual(first.plan_id, second.plan_id)

    def test_the_plan_id_changes_when_a_source_file_changes(self) -> None:
        before = self.plan()
        (self.source / "src" / "app" / "main.py").write_text("print('changed')\n", encoding="utf-8")
        self.assertNotEqual(self.plan().plan_id, before.plan_id)

    def test_the_plan_id_changes_when_the_state_changes(self) -> None:
        before = self.plan()
        state = self.electron_state()
        state["local-projects"]["p2"]["name"] = "renamed"
        self.write_state(state)
        self.assertNotEqual(self.plan().plan_id, before.plan_id)

    def test_planning_writes_nothing(self) -> None:
        before = self.tree()
        self.plan()
        self.assertEqual(self.tree(), before)

    def test_a_legacy_state_moves_through_its_own_shape(self) -> None:
        self.write_state({
            "local-projects": {"p1": {"root": str(self.source)}},
            "project-order": ["p1"],
            "thread-project-assignments": {},
        })
        self.sessions = [("s-free", str(self.source))]
        plan = self.plan()
        self.assertEqual((plan.codes, plan.schema_id, plan.bindings), ((), "legacy-v1", ("s-free",)))
        self.apply(plan)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"]["p1"], {"root": plan.new_root})
        self.assertEqual(state["thread-project-assignments"], {"s-free": "p1"})


class RefusalCodeTests(ProjectMoveTestCase):
    def assertCode(self, plan: ProjectMovePlan, code: str) -> None:
        self.assertIn(code, plan.codes, f"expected {code}, got {plan.codes}")

    def test_unknown_schema(self) -> None:
        for payload in (b"not json", json.dumps({"something": "else"}).encode()):
            self.state_file.write_bytes(payload)
            plan = self.plan()
            self.assertEqual(plan.codes, ("UNKNOWN_SCHEMA",))
            self.assertEqual(plan.inventory, ())

    def test_project_not_found(self) -> None:
        self.assertEqual(self.plan(project_id="ghost").codes, ("PROJECT_NOT_FOUND",))

    def test_multi_root_project(self) -> None:
        state = self.electron_state()
        state["local-projects"]["p1"]["rootPaths"] = [str(self.source), str(self.root / "old" / "proj-2")]
        self.write_state(state)
        self.assertEqual(self.plan().codes, ("MULTI_ROOT_PROJECT",))

    def test_unsupported_schema(self) -> None:
        with patch("codexsync.project_move.supports_root_remap", return_value=False):
            self.assertEqual(self.plan().codes, ("UNSUPPORTED_SCHEMA",))

    def test_source_missing(self) -> None:
        state = self.electron_state()
        state["local-projects"]["p1"]["rootPaths"] = [str(self.root / "vanished")]
        self.write_state(state)
        self.assertCode(self.plan(), "SOURCE_MISSING")

    def test_same_root(self) -> None:
        self.assertCode(self.plan(self.source), "SAME_ROOT")
        if os.name == "nt":
            self.assertCode(self.plan(Path(str(self.source).upper())), "SAME_ROOT")

    def test_target_exists(self) -> None:
        self.target.mkdir()
        (self.target / "keep.txt").write_text("mine", encoding="utf-8")
        plan = self.plan()
        self.assertCode(plan, "TARGET_EXISTS")
        self.assertFalse(plan.copy_complete)

    def test_target_parent_missing(self) -> None:
        self.assertCode(self.plan(self.root / "nowhere" / "proj"), "TARGET_PARENT_MISSING")

    def test_target_inside_source(self) -> None:
        self.assertCode(self.plan(self.source / "moved"), "TARGET_INSIDE_SOURCE")

    def test_source_inside_target(self) -> None:
        self.assertCode(self.plan(self.root / "old"), "SOURCE_INSIDE_TARGET")

    def test_target_inside_a_protected_root(self) -> None:
        self.assertCode(self.plan(self.codex / "proj"), "PROTECTED_TARGET")

    def test_target_containing_a_protected_root(self) -> None:
        plan = self.plan(protected=[self.target / "backups"])
        self.assertEqual(plan.codes, ("PROTECTED_TARGET",))

    def test_target_equal_to_another_projects_root(self) -> None:
        state = self.electron_state()
        state["local-projects"]["p2"]["rootPaths"] = [str(self.target)]
        self.write_state(state)
        self.assertEqual(self.plan().codes, ("TARGET_BELONGS_TO_PROJECT",))

    def test_target_containing_another_projects_root(self) -> None:
        state = self.electron_state()
        state["local-projects"]["p2"]["rootPaths"] = [str(self.target / "inner")]
        self.write_state(state)
        self.assertEqual(self.plan().codes, ("TARGET_BELONGS_TO_PROJECT",))

    def test_source_containing_another_project(self) -> None:
        state = self.electron_state()
        state["local-projects"]["p2"]["rootPaths"] = [str(self.source / "src")]
        self.write_state(state)
        self.assertEqual(self.plan().codes, ("SOURCE_CONTAINS_PROJECT",))

    def test_source_carrying_the_staging_marker_name(self) -> None:
        (self.source / STAGING_MARKER_NAME).write_text("{}", encoding="utf-8")
        self.assertEqual(self.plan().codes, ("SOURCE_HAS_RESERVED_NAME",))

    def test_unreadable_source_file(self) -> None:
        with patch("codexsync.project_move._sha256_file", side_effect=PermissionError("locked")):
            plan = self.plan()
        self.assertEqual(plan.codes, ("SOURCE_UNREADABLE",))
        self.assertEqual(plan.inventory_sha256, "")
        self.assertIn(("SOURCE_UNREADABLE", "src/app/main.py"), plan.blocked_paths)
        self.assertLessEqual(len(plan.blocked_paths), project_move.MAX_BLOCKED_PATHS)

    def test_symlink_inside_the_source(self) -> None:
        try:
            os.symlink(self.source / "README.md", self.source / "data" / "link.md")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create a symlink here: {exc}")
        plan = self.plan()
        self.assertEqual(plan.codes, ("SOURCE_HAS_LINKS",))
        self.assertEqual(plan.blocked_paths, (("SOURCE_HAS_LINKS", "data/link.md"),))

    def test_junction_inside_the_source(self) -> None:
        if os.name != "nt":
            self.skipTest("junctions exist only on Windows")
        import _winapi

        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "not-ours.txt").write_text("x", encoding="utf-8")
        link = self.source / "data" / "junction"
        _winapi.CreateJunction(str(elsewhere), str(link))
        self._junctions = [link]
        self.assertEqual(self.plan().codes, ("SOURCE_HAS_LINKS",))

    def test_only_reparse_points_that_name_another_location_are_links(self) -> None:
        """A cloud placeholder is a reparse point that holds its own data.

        Yandex.Disk marks synced files and folders with ``IO_REPARSE_TAG_CLOUD_A``
        (``0x9000601a``). Treating every reparse point as a link refused 13 of
        18 projects on a real machine; the name-surrogate bit is what marks one.
        """
        reparse = project_move._FILE_ATTRIBUTE_REPARSE_POINT
        missing = str(self.root / "no-such-entry")

        def info(mode, attributes=0, tag=None):
            fields = {"st_mode": mode, "st_file_attributes": attributes}
            if tag is not None:
                fields["st_reparse_tag"] = tag
            return SimpleNamespace(**fields)

        cases = {
            "plain file": (info(stat.S_IFREG), False),
            "cloud file placeholder": (info(stat.S_IFREG, reparse, 0x9000601A), False),
            "cloud folder placeholder": (
                info(stat.S_IFDIR, reparse | stat.FILE_ATTRIBUTE_DIRECTORY, 0x9000601A), False,
            ),
            "deduplicated file": (info(stat.S_IFREG, reparse, 0x80000013), False),
            "symlink tag": (info(stat.S_IFREG, reparse, 0xA000000C), True),
            "junction tag": (info(stat.S_IFDIR, reparse, 0xA0000003), True),
            "WSL symlink tag": (info(stat.S_IFREG, reparse, 0xA000001D), True),
            "reparse point with no readable tag": (info(stat.S_IFREG, reparse), True),
            "POSIX symlink": (info(stat.S_IFLNK), True),
        }
        for name, (entry, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(project_move._is_link(missing, entry), expected)

    def test_a_blocked_plan_cannot_be_applied(self) -> None:
        self.target.mkdir()
        plan = self.plan()
        before = self.tree()
        with self.assertRaises(ConflictError) as caught:
            self.apply(plan)
        self.assertIn("TARGET_EXISTS", str(caught.exception))
        self.assertEqual(self.tree(), before)


class ApplyTests(ProjectMoveTestCase):
    def test_happy_path_copies_verifies_remaps_and_keeps_the_old_folder(self) -> None:
        original_state = json.loads(self.state_file.read_text(encoding="utf-8"))
        source_before = self.tree(self.source)
        plan = self.plan()
        gate = _Gate()
        result = self.apply(plan, require_stopped=gate)

        self.assertEqual(result.plan_id, plan.plan_id)
        self.assertEqual((result.copied_files, result.copied_bytes), (plan.file_count, plan.total_bytes))
        self.assertEqual(result.bindings_written, 3)
        self.assertEqual(result.old_root_kept, str(self.source))
        self.assertEqual(result.new_root, plan.new_root)
        self.assertFalse(result.dry_run)
        self.assertEqual(gate.calls, 3, "before any write, before the rename, before the commit")

        # The old folder is untouched, byte for byte and mtime for mtime.
        self.assertEqual(self.tree(self.source), source_before)
        # The copy holds the same files, bytes and file mtimes.
        for relative, value in source_before.items():
            copied = self.target / relative
            if value[0] == "dir":
                self.assertTrue(copied.is_dir(), relative)
                continue
            self.assertEqual(copied.read_bytes(), value[3], relative)
            self.assertEqual(copied.stat().st_mtime_ns, value[2], relative)
        self.assertEqual(
            sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*")),
            sorted(source_before),
        )
        self.assertEqual((self.target / "README.md").stat().st_mtime_ns, OLD_MTIME_NS)
        self.assertEqual(self.move_artifacts(), [], "no staging directory or marker is left behind")

        self.assertEqual(len(self.commits), 1)
        _, candidate, plan_id, count = self.commits[0]
        self.assertEqual((plan_id, count), (plan.plan_id, 4))
        state = json.loads(candidate)
        p1 = state["local-projects"]["p1"]
        self.assertEqual(p1["rootPaths"], [plan.new_root])
        expected_p1 = dict(original_state["local-projects"]["p1"], rootPaths=[plan.new_root])
        self.assertEqual(p1, expected_p1, "names, ids and timestamps stay as the runtime wrote them")
        self.assertEqual(state["local-projects"]["p2"], original_state["local-projects"]["p2"])
        self.assertEqual(state["project-order"], ["p1", "p2"])
        assignments = state["thread-project-assignments"]
        for session_id in ("s-free", "s-free-nested", "s-forward"):
            self.assertEqual(assignments[session_id], {"projectKind": "local", "projectId": "p1"})
        for session_id in ("s-bound-here", "s-bound-elsewhere", "s-app-server"):
            self.assertEqual(assignments[session_id], original_state["thread-project-assignments"][session_id])
        self.assertNotIn("s-other", assignments)
        self.assertNotIn("s-sibling", assignments)
        self.assertEqual(candidate, (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())

    def test_confirm_mismatch_is_a_config_error_and_writes_nothing(self) -> None:
        plan = self.plan()
        before = self.tree()
        with self.assertRaises(ConfigError):
            self.apply(plan, confirm_plan="0" * 64)
        self.assertEqual(self.tree(), before)

    def test_a_volatile_plan_is_refused(self) -> None:
        plan = self.plan(volatile=True)
        with self.assertRaises(FailSafeError):
            self.apply(plan)
        self.assertFalse(self.target.exists())

    def test_a_plan_that_no_longer_matches_the_disk_is_refused(self) -> None:
        plan = self.plan()
        (self.source / "README.md").write_text("# edited after the preview\n", encoding="utf-8")
        before = self.tree()
        with self.assertRaises(FailSafeError) as caught:
            self.apply(plan)
        self.assertIn("preview again", str(caught.exception))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.commits, [])

    def test_a_state_that_moved_since_the_preview_is_refused(self) -> None:
        plan = self.plan()
        state = self.electron_state()
        state["selected-project"] = {"projectId": "p2", "type": "local"}
        self.write_state(state)
        with self.assertRaises(FailSafeError):
            self.apply(plan, rebuild=lambda: plan)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.commits, [])

    def test_a_tampered_in_memory_plan_is_refused(self) -> None:
        from dataclasses import replace

        plan = self.plan()
        forged = replace(plan, new_root=str(self.codex / "proj"))
        with self.assertRaises(FailSafeError):
            self.apply(forged, confirm_plan=forged.plan_id, rebuild=lambda: forged)
        self.assertFalse((self.codex / "proj").exists())

    def test_the_candidate_state_is_validated_before_any_copy(self) -> None:
        plan = self.plan()
        before = self.tree()
        invalid = ValidationReport(ValidationStatus.INVALID, ("BROKEN_BINDING_REFERENCE",))
        with patch("codexsync.project_move.validate_global_state_references", return_value=invalid):
            with self.assertRaises(FailSafeError):
                self.apply(plan)
        self.assertEqual(self.tree(), before)

    def test_codex_running_before_any_write_leaves_everything_as_it_was(self) -> None:
        plan = self.plan()
        before = self.tree()
        with self.assertRaises(SafetyPreconditionError):
            self.apply(plan, require_stopped=_Gate(fail_on=1))
        self.assertEqual(self.tree(), before)

    def test_codex_starting_before_the_rename_discards_only_the_staging_copy(self) -> None:
        plan = self.plan()
        source_before = self.tree(self.source)
        with self.assertRaises(SafetyPreconditionError):
            self.apply(plan, require_stopped=_Gate(fail_on=2))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.move_artifacts(), [])
        self.assertEqual(self.tree(self.source), source_before)
        self.assertEqual(self.commits, [])
        # A rerun is not blocked by anything the refused run left.
        self.apply(self.plan())
        self.assertTrue((self.target / "big" / "large.bin").is_file())

    def test_codex_starting_before_the_commit_keeps_the_copy_for_a_resume(self) -> None:
        plan = self.plan()
        with self.assertRaises(SafetyPreconditionError):
            self.apply(plan, require_stopped=_Gate(fail_on=3))
        self.assertTrue(self.target.is_dir())
        self.assertEqual(self.commits, [])
        self.assertTrue(self.plan().copy_complete)

    def test_dry_run_writes_nothing_anywhere(self) -> None:
        plan = self.plan()
        before = self.tree()
        result = self.apply(plan, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual((result.copied_files, result.bindings_written), (plan.file_count, 3))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.commits, [])

    def test_a_source_file_changed_mid_copy_aborts_and_removes_the_staging(self) -> None:
        plan = self.plan()
        real_copy2 = shutil.copy2

        def edit_then_copy(src, dst, *args, **kwargs):
            if os.path.basename(src) == "main.py":
                Path(src).write_text("print('edited during the copy')\n", encoding="utf-8")
            return real_copy2(src, dst, *args, **kwargs)

        with patch("shutil.copy2", side_effect=edit_then_copy):
            with self.assertRaises(FailSafeError) as caught:
                self.apply(plan)
        self.assertIn("changed while it was being copied", str(caught.exception))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.move_artifacts(), [])
        self.assertEqual(self.commits, [])
        self.assertEqual(
            (self.source / "src" / "app" / "main.py").read_text(encoding="utf-8"),
            "print('edited during the copy')\n",
            "the person's edit survives in the old folder",
        )

    def test_a_file_added_to_the_source_mid_copy_aborts(self) -> None:
        plan = self.plan()
        real_copy2 = shutil.copy2

        def add_then_copy(src, dst, *args, **kwargs):
            (self.source / "new-during-copy.txt").write_text("new", encoding="utf-8")
            return real_copy2(src, dst, *args, **kwargs)

        with patch("shutil.copy2", side_effect=add_then_copy):
            with self.assertRaises(FailSafeError):
                self.apply(plan)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.move_artifacts(), [])
        self.assertTrue((self.source / "new-during-copy.txt").is_file())

    def test_a_failed_commit_keeps_the_copy_and_a_rerun_only_commits(self) -> None:
        state_before = self.state_file.read_bytes()
        source_before = self.tree(self.source)
        plan = self.plan()
        with self.assertRaises(FailSafeError):
            self.apply(plan, commit_state=self.failing_commit)
        self.assertEqual(self.state_file.read_bytes(), state_before)
        self.assertEqual(self.tree(self.source), source_before)
        self.assertTrue((self.target / "big" / "large.bin").is_file())
        marker = Path(project_move._resume_marker_path(plan.new_root))
        self.assertTrue(marker.is_file(), "the resume marker survives a failed commit")
        self.commits.clear()

        resumed = self.plan()
        self.assertEqual(resumed.codes, ())
        self.assertTrue(resumed.copy_complete)
        self.assertNotEqual(resumed.plan_id, plan.plan_id)

        with patch("shutil.copy2", side_effect=AssertionError("a resumed move must not copy")):
            result = self.apply(resumed)
        self.assertEqual((result.copied_files, result.copied_bytes), (0, 0))
        self.assertEqual(len(self.commits), 1)
        self.assertFalse(marker.exists())
        self.assertEqual(self.tree(self.source), source_before)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"]["p1"]["rootPaths"], [plan.new_root])

    def test_a_copy_that_no_longer_matches_the_source_is_not_resumed(self) -> None:
        plan = self.plan()
        with self.assertRaises(FailSafeError):
            self.apply(plan, commit_state=self.failing_commit)
        (self.source / "README.md").write_text("# edited after the copy\n", encoding="utf-8")
        resumed = self.plan()
        self.assertFalse(resumed.copy_complete)
        self.assertIn("TARGET_EXISTS", resumed.codes)

    def test_a_stale_staging_directory_of_this_plan_is_replaced(self) -> None:
        plan = self.plan()
        staging = Path(project_move._staging_path(plan.new_root, plan.plan_id))
        staging.mkdir()
        (staging / STAGING_MARKER_NAME).write_text(json.dumps({"format": 1, "plan_id": plan.plan_id}), encoding="utf-8")
        (staging / "half-copied.bin").write_bytes(b"partial")
        self.apply(plan)
        self.assertFalse(staging.exists())
        self.assertFalse((self.target / "half-copied.bin").exists())
        self.assertTrue((self.target / "README.md").is_file())

    def test_a_foreign_directory_at_the_staging_path_is_refused(self) -> None:
        plan = self.plan()
        staging = Path(project_move._staging_path(plan.new_root, plan.plan_id))
        for marker in (None, {"format": 1, "plan_id": "f" * 64}):
            with self.subTest(marker=marker):
                shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir()
                (staging / "someone-elses.txt").write_text("keep me", encoding="utf-8")
                if marker is not None:
                    (staging / STAGING_MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
                with self.assertRaises(FailSafeError) as caught:
                    self.apply(plan)
                self.assertIn("STAGING_OCCUPIED", str(caught.exception))
                self.assertEqual((staging / "someone-elses.txt").read_text(encoding="utf-8"), "keep me")
                self.assertFalse(self.target.exists())
                self.assertEqual(self.commits, [])

    def test_nothing_is_written_inside_a_protected_root(self) -> None:
        plan = self.plan()

        def record_only(original, candidate, plan_id, count):
            self.commits.append((original, candidate, plan_id, count))
            return count

        with ExitStack() as stack:
            for protected in self.protected:
                protected.mkdir(exist_ok=True)
                stack.enter_context(forbid_writes_under(protected))
            self.apply(plan, commit_state=record_only)
        self.assertEqual(len(self.commits), 1)


class NoDeletionOutsideStagingTests(ProjectMoveTestCase):
    """Across success, a failed commit and a source that moved mid-copy, no
    delete call of any kind is ever aimed at the old folder."""

    def _run_recording_deletes(self, action) -> list[str]:
        recorded: list[str] = []
        real = {
            "rmtree": shutil.rmtree, "remove": os.remove, "unlink": os.unlink,
            "rmdir": os.rmdir, "path_unlink": Path.unlink,
        }

        def recorder(name):
            def call(path, *args, **kwargs):
                recorded.append(os.fspath(path))
                return real[name](path, *args, **kwargs)
            return call

        with patch("shutil.rmtree", recorder("rmtree")), patch("os.remove", recorder("remove")), \
                patch("os.unlink", recorder("unlink")), patch("os.rmdir", recorder("rmdir")), \
                patch.object(Path, "unlink", recorder("path_unlink")):
            action()
        return recorded

    def _assert_source_never_targeted(self, recorded: list[str], source_before: dict) -> None:
        offenders = [path for path in recorded if _inside(path, self.source)]
        self.assertEqual(offenders, [])
        self.assertEqual(self.tree(self.source), source_before)

    def test_successful_move(self) -> None:
        source_before = self.tree(self.source)
        recorded = self._run_recording_deletes(lambda: self.apply(self.plan()))
        self.assertTrue(recorded, "the resume marker and staging marker removals are recorded")
        self._assert_source_never_targeted(recorded, source_before)

    def test_failed_commit(self) -> None:
        source_before = self.tree(self.source)

        def action():
            with self.assertRaises(FailSafeError):
                self.apply(self.plan(), commit_state=self.failing_commit)

        self._assert_source_never_targeted(self._run_recording_deletes(action), source_before)

    def test_source_changed_mid_copy(self) -> None:
        plan = self.plan()
        real_copy2 = shutil.copy2

        def edit_then_copy(src, dst, *args, **kwargs):
            if os.path.basename(src) == "blob.bin":
                Path(src).write_bytes(b"edited")
            return real_copy2(src, dst, *args, **kwargs)

        def action():
            with patch("shutil.copy2", side_effect=edit_then_copy):
                with self.assertRaises(FailSafeError):
                    self.apply(plan)

        recorded = self._run_recording_deletes(action)
        source_after_edit = self.tree(self.source)
        self.assertTrue(any(_inside(path, self.work) for path in recorded), "the staging copy was removed")
        self._assert_source_never_targeted(recorded, source_after_edit)
        self.assertEqual((self.source / "data" / "blob.bin").read_bytes(), b"edited")


class PlanFileTests(ProjectMoveTestCase):
    def test_round_trip(self) -> None:
        plan = self.plan()
        path = self.root / "plans" / "move.json"
        save_project_move_plan(plan, path)
        self.assertEqual(load_project_move_plan(path), plan)

    def test_a_blocked_plan_round_trips_with_its_codes(self) -> None:
        self.target.mkdir()
        plan = self.plan()
        path = save_project_move_plan(plan, self.root / "blocked.json")
        self.assertEqual(load_project_move_plan(path).codes, plan.codes)

    def test_tampering_is_detected(self) -> None:
        self.target.mkdir()
        blocked = self.target.parent / "blocked.json"
        save_project_move_plan(self.plan(), blocked)
        shutil.rmtree(self.target)
        clean = save_project_move_plan(self.plan(), self.root / "clean.json")

        edits = {
            "new_root": lambda raw: raw.update(new_root=str(self.codex / "proj")),
            "bindings": lambda raw: raw["bindings"].append("s-other"),
            "inventory": lambda raw: raw["inventory"][0].__setitem__(2, "0" * 64),
            "copy_complete": lambda raw: raw.update(copy_complete=True),
            "file_count": lambda raw: raw.update(file_count=raw["file_count"] + 1),
        }
        for name, edit in edits.items():
            with self.subTest(field=name):
                raw = json.loads(clean.read_text(encoding="utf-8"))
                edit(raw)
                forged = self.root / f"forged-{name}.json"
                forged.write_text(json.dumps(raw), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_project_move_plan(forged)

        raw = json.loads(blocked.read_text(encoding="utf-8"))
        raw["codes"] = []
        unblocked = self.root / "unblocked.json"
        unblocked.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_project_move_plan(unblocked)

        truncated = self.root / "truncated.json"
        truncated.write_text(clean.read_text(encoding="utf-8")[:100], encoding="utf-8")
        with self.assertRaises(ValueError):
            load_project_move_plan(truncated)


if __name__ == "__main__":
    unittest.main()
