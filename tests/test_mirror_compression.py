"""The cloud mirror stores a branch compressed; the history is unchanged.

The mirror is codexSync's own directory, so the container is ours to choose,
and on real data the choice is worth about two thirds of the upload (824 MiB
of sessions mirror as 299 MiB). What must
not change is what a branch *is*: these tests pin the one property everything
else rests on — a compressed mirror copy compares `IDENTICAL` to the plain
local branch instead of looking like a divergence — plus the proof that the
compressed bytes really are the source, and the refusals that keep a plan
frozen under one container from being applied under another.
"""
from __future__ import annotations

import json
import lzma
from pathlib import Path
import shutil
import textwrap
import unittest
from unittest.mock import patch
import uuid

from codexsync.app import apply_session_transfer, scan_session_transfer
from codexsync.exceptions import ConfigError, FailSafeError
from codexsync.jsonl_codec import (
    JsonlCodec,
    codec_of,
    compress_file,
    logical_relative_path,
    open_jsonl,
    with_codec,
)
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision
from codexsync.semantic_transfer import (
    MIRROR_LAYOUT_ID,
    PROVEN_LAYOUTS,
    TransferAction,
    mirror_codec_for,
    mirror_layout_id,
    save_transfer_plan,
    target_relative_path,
)
from codexsync.session_catalog import SessionState, scan_sessions


class _StoppedGate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "test gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class CodecNamingTests(unittest.TestCase):
    def test_a_container_adds_exactly_one_suffix_and_is_idempotent(self) -> None:
        plain = "sessions/2026/09/rollout-s1.jsonl"
        stored = with_codec(plain, JsonlCodec.XZ)
        self.assertEqual(stored, "sessions/2026/09/rollout-s1.jsonl.xz")
        self.assertEqual(with_codec(stored, JsonlCodec.XZ), stored, "no second suffix")
        self.assertEqual(logical_relative_path(stored), plain)

    def test_a_branch_can_be_restored_to_its_plain_name(self) -> None:
        stored = with_codec("sessions/rollout-s1.jsonl", JsonlCodec.GZIP)
        self.assertEqual(with_codec(stored, JsonlCodec.NONE), "sessions/rollout-s1.jsonl")

    def test_a_longer_suffix_is_never_read_as_a_shorter_one(self) -> None:
        self.assertIs(codec_of("a.jsonl.gz"), JsonlCodec.GZIP)
        self.assertIs(codec_of("a.jsonl.xz"), JsonlCodec.XZ)
        self.assertIs(codec_of("a.jsonl"), JsonlCodec.NONE)
        self.assertIsNone(codec_of("a.sqlite"), "not a branch at all")

    def test_every_container_has_its_own_mirror_layout_id(self) -> None:
        ids = {mirror_layout_id(codec) for codec in JsonlCodec}
        self.assertEqual(len(ids), len(JsonlCodec), "a codec must be visible in the layout id")
        self.assertEqual(mirror_layout_id(JsonlCodec.NONE), MIRROR_LAYOUT_ID)
        for codec in JsonlCodec:
            self.assertIs(mirror_codec_for(mirror_layout_id(codec)), codec)

    def test_an_unknown_mirror_layout_is_refused_rather_than_guessed(self) -> None:
        with self.assertRaises(FailSafeError):
            mirror_codec_for("codexsync-mirror-brotli-v9")


class CodecStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"codec-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _payload(self) -> bytes:
        rows = [{"type": "event", "n": index, "text": "repeated body " * 40} for index in range(500)]
        return b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows)

    def test_a_round_trip_is_byte_for_byte(self) -> None:
        source = self.root / "branch.jsonl"
        source.write_bytes(self._payload())
        for codec in JsonlCodec:
            with self.subTest(codec=codec.value):
                destination = self.root / f"stored-{codec.value}{codec.file_suffix}"
                compress_file(source, destination, codec)
                with open_jsonl(destination) as handle:
                    self.assertEqual(handle.read(), source.read_bytes())

    def test_compressing_the_same_branch_twice_produces_identical_bytes(self) -> None:
        source = self.root / "branch.jsonl"
        source.write_bytes(self._payload())
        for codec in (JsonlCodec.GZIP, JsonlCodec.XZ):
            with self.subTest(codec=codec.value):
                first, second = self.root / f"a{codec.suffix}", self.root / f"b{codec.suffix}"
                compress_file(source, first, codec)
                compress_file(source, second, codec)
                self.assertEqual(
                    first.read_bytes(), second.read_bytes(),
                    "an unchanged branch must not look changed to a cloud client",
                )

    def test_a_staged_payload_is_read_by_the_codec_it_was_written_with(self) -> None:
        # The staged file is named for its position in the plan, so its name
        # says nothing about its container: inferring one would read xz bytes
        # as JSONL and see a branch with no records at all.
        source = self.root / "branch.jsonl"
        source.write_bytes(self._payload())
        staged = self.root / "00000000.payload"
        compress_file(source, staged, JsonlCodec.XZ)
        self.assertIs(codec_of(staged), None, "the staged name carries no codec")
        with open_jsonl(staged, JsonlCodec.XZ) as handle:
            self.assertEqual(handle.read(), source.read_bytes())

    def test_a_compressed_branch_is_catalogued_by_its_decompressed_content(self) -> None:
        state = self.root / "state"
        (state / "sessions").mkdir(parents=True)
        plain = state / "sessions" / "s1.jsonl"
        plain.write_bytes(
            b'{"payload": {"id": "s1"}, "type": "session_meta"}\n{"type": "event"}\n'
        )
        plain_descriptor = scan_sessions(state).descriptors[0]

        mirror = self.root / "mirror"
        (mirror / "sessions").mkdir(parents=True)
        compress_file(plain, mirror / "sessions" / "s1.jsonl.xz", JsonlCodec.XZ)
        stored = scan_sessions(mirror).descriptors[0]

        self.assertEqual(stored.state, SessionState.ACTIVE)
        self.assertEqual(stored.session_id, "s1")
        self.assertEqual(stored.sha256, plain_descriptor.sha256, "the hash is of the history")
        self.assertEqual(stored.line_count, plain_descriptor.line_count)
        self.assertEqual(stored.byte_count, plain_descriptor.byte_count)
        self.assertEqual(stored.codes, (), "a container is not a defect")


class CompressedMirrorApplyTests(unittest.TestCase):
    """The write path, end to end, with a real config and a real cold apply."""

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"mirror-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        (self.local_dir / "sessions").mkdir(parents=True)
        (self.cloud_dir / "sessions").mkdir(parents=True)
        self.plan_path = self.root / "plan.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _config(self, compression: str = "xz") -> Path:
        path = self.root / f"config-{compression}.toml"
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
                mirror_compression = "{compression}"

                [targets]
                include_roots = ["sessions"]

                [backup]
                backup_before_overwrite = true
                compression = "none"

                [state]
                manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _branch(self, session_id: str, records: list[str]) -> Path:
        path = self.local_dir / "sessions" / f"{session_id}.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": session_id}}]
        rows += [{"type": "event", "record": value} for value in records]
        path.write_bytes(
            b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows)
        )
        return path

    def _scan(self, config_path: Path):
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return scan_session_transfer(
                config_path, source_machine="desktop", target_machine="mirror"
            )

    def _apply(self, config_path: Path, plan, **kwargs):
        save_transfer_plan(plan, self.plan_path)
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            return apply_session_transfer(
                config_path,
                plan_path=self.plan_path,
                confirm_plan=plan.plan_id,
                **kwargs,
            )

    def test_a_branch_lands_in_the_mirror_compressed_and_intact(self) -> None:
        config_path = self._config("xz")
        source = self._branch("s1", ["one", "two"])
        plan = self._scan(config_path)
        self.assertEqual(plan.mirror_layout_id, mirror_layout_id(JsonlCodec.XZ))
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(plan.items[0].target_relative_path, "sessions/s1.jsonl.xz")

        self.assertEqual(self._apply(config_path, plan), 1)
        written = self.cloud_dir / "sessions" / "s1.jsonl.xz"
        self.assertTrue(written.is_file())
        self.assertNotEqual(written.read_bytes(), source.read_bytes(), "it is a container")
        self.assertEqual(lzma.decompress(written.read_bytes()), source.read_bytes())
        self.assertFalse(
            (self.cloud_dir / "sessions" / "s1.jsonl").exists(), "no plain copy is left behind"
        )

    def test_the_stored_branch_is_not_a_divergence_on_the_next_scan(self) -> None:
        # The property the whole design rests on. If the container leaked into
        # the comparison every mirrored session would come back DIVERGED and
        # the mirror could never be updated again.
        config_path = self._config("xz")
        self._branch("s1", ["one", "two"])
        self._apply(config_path, self._scan(config_path))

        again = self._scan(config_path)
        self.assertEqual([item.action for item in again.items], [TransferAction.NOOP])
        self.assertEqual(again.codes, (), "nothing is blocked and nothing conflicts")

    def test_the_source_branch_is_never_transformed(self) -> None:
        config_path = self._config("xz")
        source = self._branch("s1", ["one"])
        before = source.read_bytes()
        self._apply(config_path, self._scan(config_path))
        self.assertEqual(source.read_bytes(), before)

    def test_an_uncompressed_mirror_is_still_available(self) -> None:
        config_path = self._config("none")
        source = self._branch("s1", ["one"])
        plan = self._scan(config_path)
        self.assertEqual(plan.mirror_layout_id, MIRROR_LAYOUT_ID)
        self.assertEqual(plan.items[0].target_relative_path, "sessions/s1.jsonl")
        self._apply(config_path, plan)
        self.assertEqual(
            (self.cloud_dir / "sessions" / "s1.jsonl").read_bytes(), source.read_bytes()
        )

    def test_changing_the_container_changes_the_plan_id(self) -> None:
        self._branch("s1", ["one"])
        plain = self._scan(self._config("none"))
        compressed = self._scan(self._config("xz"))
        self.assertNotEqual(
            plain.plan_id, compressed.plan_id,
            "a confirmation given for one container must not carry to another",
        )

    def test_an_apply_writes_the_container_the_confirmed_plan_named(self) -> None:
        # The plan is the contract. A config edited after the scan applies to
        # the next scan, never underneath a confirmation already given.
        self._branch("s1", ["one"])
        plan = self._scan(self._config("none"))
        self._apply(self._config("xz"), plan)
        self.assertTrue((self.cloud_dir / "sessions" / "s1.jsonl").is_file())
        self.assertFalse((self.cloud_dir / "sessions" / "s1.jsonl.xz").exists())

    def test_a_compressor_that_loses_a_byte_fails_before_anything_is_replaced(self) -> None:
        config_path = self._config("xz")
        self._branch("s1", ["one"])
        plan = self._scan(config_path)

        def truncating(
            source: Path, destination: Path, source_codec: JsonlCodec, target: JsonlCodec
        ) -> None:
            with lzma.open(destination, "wb", preset=1) as writer:
                writer.write(source.read_bytes()[:-5])

        with patch("codexsync.sync_engine.transcode", truncating):
            with self.assertRaises(FailSafeError):
                self._apply(config_path, plan)
        self.assertFalse(
            list((self.cloud_dir / "sessions").iterdir()), "no destination was touched"
        )

    def test_an_unknown_container_is_refused_at_config_load(self) -> None:
        with self.assertRaises(ConfigError):
            self._scan(self._config("brotli"))


class WayBackFromACompressedMirrorTests(unittest.TestCase):
    """Reading a branch out of the mirror and into a directory Codex reads.

    This direction is gated on a proven layout, so none of it runs today. That
    is exactly why it is tested: a container that leaked into the destination
    would sit here unnoticed until the controlled experiment opened the gate,
    and would then write xz bytes under a `.jsonl` name — a session invisible
    to the runtime with no error at all, which is the failure mode this project
    already suffered twice.
    """

    LAYOUT = "test-layout"

    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"wayback-{uuid.uuid4().hex}"
        self.local_dir = self.root / "local-state"
        self.cloud_dir = self.root / "cloud"
        (self.local_dir / "sessions").mkdir(parents=True)
        (self.cloud_dir / "sessions").mkdir(parents=True)
        self.plan_path = self.root / "plan.json"
        PROVEN_LAYOUTS[self.LAYOUT] = "{state}/{file_name}"

    def tearDown(self) -> None:
        PROVEN_LAYOUTS.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    def _config(self) -> Path:
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
                mirror_compression = "xz"

                [targets]
                include_roots = ["sessions"]

                [backup]
                backup_before_overwrite = true
                compression = "none"

                [state]
                manifest_file = "{(self.root / 'state' / 'manifest.json').as_posix()}"
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def _history(self, session_id: str, records: list[str]) -> bytes:
        rows = [{"type": "session_meta", "payload": {"id": session_id}}]
        rows += [{"type": "event", "record": value} for value in records]
        return b"".join(
            json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in rows
        )

    def test_a_branch_out_of_the_mirror_is_named_as_plain_jsonl(self) -> None:
        from codexsync.session_catalog import scan_sessions as _scan

        plain = self.cloud_dir / "sessions" / "s1.jsonl"
        plain.write_bytes(self._history("s1", ["one"]))
        stored = self.cloud_dir / "sessions" / "s1.jsonl.xz"
        compress_file(plain, stored, JsonlCodec.XZ)
        plain.unlink()

        descriptor = _scan(self.cloud_dir).descriptors[0]
        self.assertEqual(descriptor.relative_path, "sessions/s1.jsonl.xz")
        self.assertEqual(
            target_relative_path(self.LAYOUT, descriptor), "sessions/s1.jsonl",
            "the container must not survive into a directory the runtime reads",
        )

    def test_a_branch_out_of_the_mirror_lands_decompressed(self) -> None:
        history = self._history("s1", ["one", "two"])
        local = self.local_dir / "sessions" / "s1.jsonl"
        local.write_bytes(self._history("s1", ["one"]))

        plain = self.cloud_dir / "sessions" / "s1.jsonl"
        plain.write_bytes(history)
        compress_file(plain, self.cloud_dir / "sessions" / "s1.jsonl.xz", JsonlCodec.XZ)
        plain.unlink()

        config_path = self._config()
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            scanned = scan_session_transfer(
                config_path, source_machine="desktop", target_machine="mirror"
            )
        from codexsync.app import _rebuild_transfer_plan
        from codexsync.config import load_config
        from codexsync.semantic_transfer import TransferPlan

        cfg = load_config(config_path)
        plan, _, _ = _rebuild_transfer_plan(
            cfg, self.local_dir, self.cloud_dir,
            TransferPlan(
                scanned.version, scanned.plan_id, scanned.created_at_utc, "desktop", "mirror",
                self.LAYOUT, scanned.canonical_version, False, scanned.items, scanned.codes,
                scanned.mirror_layout_id,
            ),
            None,
        )
        self.assertEqual(plan.items[0].action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(plan.items[0].target_relative_path, "sessions/s1.jsonl")

        save_transfer_plan(plan, self.plan_path)
        with patch("codexsync.app._make_safety_gate", return_value=_StoppedGate()):
            apply_session_transfer(
                config_path, plan_path=self.plan_path, confirm_plan=plan.plan_id
            )
        self.assertEqual(
            local.read_bytes(), history,
            "Codex reads this file: it must be JSONL, not a container",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
