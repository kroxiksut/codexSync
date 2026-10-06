"""One sync manifest, several machines (CS-288), and the sync-path review fixes.

The manifest lives beside the cloud mirror, so every machine reads the same
file. With a single two-sided entry per path, machine B compared its own stale
copy with *machine A's* local fingerprint, took it for a local edit, and copied
it over A's newer version in the cloud. The tests below play the handoff out on
disk with two configs that share a mirror and a manifest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import build_context, run_sync
from codexsync.exceptions import ConfigError, ConflictError, SafetyPreconditionError
from codexsync.manifest import build_manifest, load_manifest, save_manifest
from codexsync.models import DeleteAction, FileMeta, SyncPlan
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.planner import build_sync_plan
from codexsync.runtime import _plan_hash

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
SECOND = 1_000_000_000
BASE_NS = 1_700_000_000 * SECOND


class _StoppedGate:
    def require(self, *_args, **_kwargs):
        return None


def _touch(path: Path, text: str, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


class _TwoMachines(unittest.TestCase):
    """Machines A and B: own `.codex` and temp, one shared mirror and manifest."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"manifest-machines-{uuid.uuid4().hex[:8]}"
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cloud = self.root / "cloud"
        self.manifest = self.root / "state" / "manifest.json"
        self.local = {name: self.root / f"codex-{name}" for name in ("a", "b")}
        for path in (self.cloud, *self.local.values()):
            path.mkdir(parents=True)

    def config(self, machine: str, *, delete_policy: str = "never", extra: str = "", sync_lines: str = "") -> Path:
        path = self.root / f"config-{machine}.toml"
        path.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-{machine}"

            [sync]
            delete_policy = "{delete_policy}"
            {sync_lines}

            [paths]
            local_state_dir = "{self.local[machine].as_posix()}"
            cloud_root_dir = "{self.cloud.as_posix()}"
            backup_dir = "{(self.root / f"backups-{machine}").as_posix()}"
            temp_dir = "{(self.root / f"tmp-{machine}").as_posix()}"

            [guardian]
            root_dir = "{(self.root / f"guardian-{machine}").as_posix()}"

            [semantic]
            root_dir = "{(self.root / f"semantic-{machine}").as_posix()}"

            [targets]
            include_roots = ["data"]

            [state]
            manifest_file = "{self.manifest.as_posix()}"
        """) + extra, encoding="utf-8")
        return path

    def sync(self, machine: str, **config) -> None:
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(self.config(machine, **config), enforce_safety=True)
        run_sync(ctx, dry_run=False)

    def file(self, where: str) -> Path:
        base = self.cloud if where == "cloud" else self.local[where]
        return base / "data" / "notes.md"


class HandoffTests(_TwoMachines):
    def _agreed_start(self) -> None:
        for where in ("a", "b", "cloud"):
            _touch(self.file(where), "v0", BASE_NS)
        self.sync("a")
        self.sync("b")

    def test_a_edit_travels_a_to_b_and_back(self) -> None:
        self._agreed_start()

        _touch(self.file("a"), "v1 from A", BASE_NS + 10 * SECOND)
        self.sync("a")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v1 from A")

        # The bug: B read A's local fingerprint as its own, saw its untouched
        # v0 as "changed locally" and copied it over v1 in the cloud.
        self.sync("b")
        self.assertEqual(self.file("b").read_text(encoding="utf-8"), "v1 from A")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v1 from A")

        _touch(self.file("b"), "v2 from B", BASE_NS + 20 * SECOND)
        self.sync("b")
        self.sync("a")
        self.assertEqual(self.file("a").read_text(encoding="utf-8"), "v2 from B")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v2 from B")

    def test_an_unsynced_edit_on_b_meets_a_newer_cloud_as_a_conflict(self) -> None:
        self._agreed_start()
        _touch(self.file("a"), "v1 from A", BASE_NS + 10 * SECOND)
        self.sync("a")
        # B edited too, later, without syncing first. Both sides moved since
        # *B* last looked, which a shared cloud fingerprint could not tell.
        _touch(self.file("b"), "v1 from B", BASE_NS + 20 * SECOND)
        with self.assertRaises(ConflictError):
            self.sync("b", extra='\n[conflict]\npolicy = "manual_abort"\n')
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v1 from A")
        self.assertEqual(self.file("b").read_text(encoding="utf-8"), "v1 from B")

    def test_by_default_the_newer_of_two_edits_wins_and_the_other_is_backed_up(self) -> None:
        # The same handoff under the default policy (D-027): B's edit is the
        # later one, so it reaches the cloud, and A's goes into B's backup.
        self._agreed_start()
        _touch(self.file("a"), "v1 from A", BASE_NS + 10 * SECOND)
        self.sync("a")
        _touch(self.file("b"), "v1 from B", BASE_NS + 20 * SECOND)
        self.sync("b")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v1 from B")
        backups = list((self.root / "backups-b").rglob("*"))
        self.assertTrue(any(path.is_file() for path in backups), "the overwritten copy is backed up first")

    def test_each_machine_keeps_its_own_baseline_in_the_one_file(self) -> None:
        self._agreed_start()
        raw = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(sorted(raw["machines"]), ["machine-a", "machine-b"])
        self.assertNotIn("files", raw)
        self.assertIn("data/notes.md", raw["machines"]["machine-a"]["files"])


class LegacyManifestTests(_TwoMachines):
    """A manifest written before CS-288 has one unkeyed pair per path."""

    def _write_legacy(self, fingerprint: tuple[int, int]) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        entry = {"mtime_ns": fingerprint[0], "size": fingerprint[1]}
        self.manifest.write_text(json.dumps({
            "data_version": 1,
            "files": {"data/notes.md": {"local": entry, "cloud": entry}},
        }), encoding="utf-8")

    def test_the_unkeyed_pair_is_not_taken_for_this_machine(self) -> None:
        # A wrote v1 to the cloud with the old version; B still has v0.
        _touch(self.file("cloud"), "v1 from A", BASE_NS + 10 * SECOND)
        _touch(self.file("b"), "v0", BASE_NS)
        self._write_legacy((BASE_NS + 10 * SECOND, len("v1 from A")))

        self.sync("b")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "v1 from A")
        self.assertEqual(self.file("b").read_text(encoding="utf-8"), "v1 from A")
        raw = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(list(raw["machines"]), ["machine-b"])

    def test_the_unkeyed_pair_never_proves_a_deletion(self) -> None:
        # The legacy entry says "both sides held it". B never had the file;
        # read as B's own baseline that would delete it from the cloud.
        _touch(self.file("cloud"), "kept", BASE_NS)
        self._write_legacy((BASE_NS, len("kept")))

        self.sync("b", delete_policy="propagate")
        self.assertEqual(self.file("cloud").read_text(encoding="utf-8"), "kept")
        self.assertEqual(self.file("b").read_text(encoding="utf-8"), "kept")


class ManifestFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"manifest-file-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "manifest.json"

    def _index(self, mtime_ns: int, size: int) -> dict[str, FileMeta]:
        return {"x": FileMeta("x", self.root / "x", mtime_ns, size)}

    def test_saving_one_machine_keeps_the_others(self) -> None:
        for machine, mtime in (("a", 10), ("b", 20)):
            previous = load_manifest(self.path, 1, machine_id=machine)
            save_manifest(build_manifest(self._index(mtime, 1), self._index(mtime, 1), 1, previous), self.path)
        a = load_manifest(self.path, 1, machine_id="a")
        b = load_manifest(self.path, 1, machine_id="b")
        self.assertEqual(a.files["x"].local.mtime_ns, 10)
        self.assertEqual(b.files["x"].local.mtime_ns, 20)
        self.assertEqual(load_manifest(self.path, 1, machine_id="c").files, {})

    def test_a_malformed_entry_reads_as_never_synchronised(self) -> None:
        self.path.write_text(json.dumps({"data_version": 1, "machines": {"a": {"files": {
            "bad": {"local": {"mtime_ns": "x", "size": 1}, "cloud": {"mtime_ns": 1, "size": 1}},
            "half": {"local": [], "cloud": None},
            "good": {"local": {"mtime_ns": 1, "size": 1}, "cloud": None},
        }}}}), encoding="utf-8")
        self.assertEqual(list(load_manifest(self.path, 1, machine_id="a").files), ["good"])

    def test_a_structurally_broken_file_is_a_config_error(self) -> None:
        for payload in ("[]", '{"data_version": "1"}', '{"data_version": 1, "machines": []}', "{"):
            with self.subTest(payload=payload):
                self.path.write_text(payload, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_manifest(self.path, 1, machine_id="a")

    def test_an_unowned_manifest_with_entries_is_not_written(self) -> None:
        with self.assertRaises(ValueError):
            save_manifest(build_manifest(self._index(1, 1), {}, 1), self.path)


class PlanReviewTests(unittest.TestCase):
    """CS-295, CS-324 and the plan hash (CS-325)."""

    def setUp(self) -> None:
        self.root = SANDBOX / f"plan-review-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        self.addCleanup(shutil.rmtree, self.root, True)

    def meta(self, side: Path, rel: str, mtime_ns: int = BASE_NS, text: str = "x") -> FileMeta:
        _touch(side / rel, text, mtime_ns)
        return FileMeta(rel, side / rel, mtime_ns, len(text))

    def _synced(self, rels: list[str]) -> tuple[dict, dict, object]:
        local = {rel: self.meta(self.local, rel) for rel in rels}
        cloud = {rel: self.meta(self.cloud, rel) for rel in rels}
        return local, cloud, build_manifest(local, cloud, 1, machine_id="m")

    def test_a_root_emptied_on_one_side_does_not_empty_the_other(self) -> None:
        local, cloud, previous = self._synced(["rules/a.md", "rules/b.md", "skills/s.md"])
        # The mirror's `rules/` is gone (re-downloading, disconnected, ...).
        del cloud["rules/a.md"], cloud["rules/b.md"]
        plan = build_sync_plan(
            local, cloud, self.local, self.cloud, previous,
            delete_policy="propagate", include_roots=["rules", "skills"],
        )
        self.assertEqual(plan.deletions, [])
        self.assertEqual(sorted(plan.conflicts), ["rules/a.md", "rules/b.md"])

    def test_a_single_deletion_inside_a_populated_root_still_propagates(self) -> None:
        local, cloud, previous = self._synced(["rules/a.md", "rules/b.md"])
        del cloud["rules/a.md"]
        plan = build_sync_plan(
            local, cloud, self.local, self.cloud, previous,
            delete_policy="propagate", include_roots=["rules"],
        )
        self.assertEqual([(item.relative_path, item.side) for item in plan.deletions], [("rules/a.md", "local")])
        self.assertEqual(plan.conflicts, [])

    def test_an_empty_side_without_roots_deletes_nothing(self) -> None:
        local, _cloud, previous = self._synced(["a.md", "b.md"])
        plan = build_sync_plan(local, {}, self.local, self.cloud, previous, delete_policy="propagate")
        self.assertEqual(plan.deletions, [])
        self.assertEqual(sorted(plan.conflicts), ["a.md", "b.md"])

    def test_paths_differing_only_in_case_are_a_conflict(self) -> None:
        local = {"rules/A.md": self.meta(self.local, "rules/A.md")}
        cloud = {"rules/a.md": self.meta(self.cloud, "rules/a.md", BASE_NS + SECOND)}
        plan = build_sync_plan(local, cloud, self.local, self.cloud)
        self.assertEqual(plan.to_cloud, [])
        self.assertEqual(plan.to_local, [])
        self.assertEqual(sorted(plan.conflicts), ["rules/A.md", "rules/a.md"])

    def test_the_plan_hash_names_deletions(self) -> None:
        target = self.meta(self.local, "a.md", text="one")
        empty = _plan_hash(SyncPlan())
        with_deletion = SyncPlan(deletions=[DeleteAction(target.abs_path, "a.md", "local")])
        first = _plan_hash(with_deletion)
        self.assertNotEqual(first, empty)
        target.abs_path.write_text("two", encoding="utf-8")
        self.assertNotEqual(_plan_hash(with_deletion), first)


class RunSyncReviewTests(_TwoMachines):
    def test_a_conflict_left_under_a_resolving_policy_still_stops_the_run(self) -> None:
        # Equal times, different content, `equal_mtime_action = manual_abort`:
        # `prefer_local` does not decide that one, and it used to be written
        # down as agreement with exit 0 (CS-295).
        _touch(self.file("a"), "local!", BASE_NS)
        _touch(self.file("cloud"), "cloud", BASE_NS)
        config = self.config(
            "a", sync_lines='equal_mtime_action = "manual_abort"',
            extra='\n[conflict]\npolicy = "prefer_local"\n',
        )
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            ctx = build_context(config, enforce_safety=True)
        self.assertEqual(ctx.plan.conflicts, ["data/notes.md"])
        with self.assertRaises(ConflictError):
            run_sync(ctx, dry_run=False)
        self.assertFalse(self.manifest.exists() and "machine-a" in self.manifest.read_text(encoding="utf-8"))

    def test_the_gate_is_asked_again_before_anything_is_staged(self) -> None:
        _touch(self.file("a"), "new", BASE_NS + SECOND)
        _touch(self.file("cloud"), "old", BASE_NS)

        class _OpensAfterPlanning:
            calls: list[bool] = []

            def require(self, _operation, *, final: bool = False):
                self.calls.append(final)
                if len(self.calls) > 1 and not final:
                    raise SafetyPreconditionError("Codex started while the plan was being built")

        # Sync towards the local side, so staging would write into `.codex`.
        _touch(self.file("a"), "old", BASE_NS)
        _touch(self.file("cloud"), "new", BASE_NS + SECOND)
        with patch("codexsync.app._make_safety_gate", return_value=_OpensAfterPlanning()):
            ctx = build_context(self.config("a"), enforce_safety=True)
        self.assertEqual(len(ctx.plan.to_local), 1)
        with self.assertRaises(SafetyPreconditionError):
            run_sync(ctx, dry_run=False)
        self.assertEqual(list(self.file("a").parent.glob("*.codexsync.tmp")), [])
        self.assertEqual(self.file("a").read_text(encoding="utf-8"), "old")
        journals = list(JournalStore(self.root / "tmp-a").root.glob("*.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(
            JournalStore(self.root / "tmp-a").load(journals[0].stem).state, JournalState.FAILED
        )


if __name__ == "__main__":
    unittest.main()
