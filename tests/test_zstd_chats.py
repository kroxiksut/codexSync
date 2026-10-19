"""Chats Codex keeps as `.jsonl.zst` are read where Python has zstd (CS-425).

Codex 0.163 compresses every chat it has not touched for a while (316 of 329 on
the machine this was built against), and its catalogue keeps naming the plain
`.jsonl`. The standard library reads zstd from Python 3.14 on; the release
programs are built with it. Where it is missing the file is an unreadable copy
and the chat is blocked (CS-394) -- both halves are tested, whichever
interpreter runs the suite. Nothing ever writes zstd: `.codex` is written
plain and the mirror in a container every interpreter reads.
"""
from __future__ import annotations

import json
import lzma
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

from codexsync.jsonl_codec import (
    READABLE_CODECS,
    WRITABLE_CODECS,
    ZSTD_READABLE,
    JsonlCodec,
    codec_of,
    open_jsonl,
    parse_codec,
    transcode,
    unsupported_container,
)
from codexsync.semantic_transfer import IN_PLACE_CONTAINER, SAME_PATH_LAYOUT_ID, TransferAction, build_transfer_plan
from codexsync.session_catalog import UNSUPPORTED_CONTAINER, SessionState, scan_sessions
from codexsync.sqlite_audit import read_thread_placements

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
CHAT = "019ce7a8-1ae2-7533-8555-bfb8511bd1b4"
NAME = f"rollout-2026-03-13T22-44-39-{CHAT}.jsonl"
DAY = "sessions/2026/03/13"


def _records(turns: int) -> bytes:
    lines = [{"type": "session_meta", "timestamp": "2026-03-13T22:44:39Z",
              "payload": {"id": CHAT, "cwd": "D:/work", "timestamp": "2026-03-13T22:44:39Z"}}]
    lines += [{"type": "event", "timestamp": f"2026-03-13T22:{45 + n:02d}:00Z", "n": n} for n in range(turns)]
    return b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)


def _zstd(data: bytes) -> bytes:
    from compression import zstd  # only reached where ZSTD_READABLE

    return zstd.compress(data)


class NeverWrittenTests(unittest.TestCase):
    def test_the_config_cannot_name_it(self) -> None:
        self.assertNotIn(JsonlCodec.ZSTD, WRITABLE_CODECS)
        with self.assertRaisesRegex(ValueError, "read, never written"):
            parse_codec("zstd")

    def test_no_copy_is_ever_made_in_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "never writes zstd"):
            transcode(Path("a.jsonl"), Path("b.jsonl.zst"), JsonlCodec.NONE, JsonlCodec.ZSTD)

    def test_it_is_readable_exactly_when_the_interpreter_reads_it(self) -> None:
        self.assertEqual(JsonlCodec.ZSTD in READABLE_CODECS, ZSTD_READABLE)
        if ZSTD_READABLE:
            self.assertIs(codec_of(NAME + ".zst"), JsonlCodec.ZSTD)
            self.assertIsNone(unsupported_container(NAME + ".zst"))
        else:
            self.assertIsNone(codec_of(NAME + ".zst"))
            self.assertEqual(unsupported_container(NAME + ".zst"), "zst")


class _Roots(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"zstd-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "codex"
        self.mirror = self.root / "mirror"
        for folder in (self.local / DAY, self.mirror / DAY):
            folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        database = self.local / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER)")
        # Codex's catalogue names the plain file, as observed.
        connection.execute("INSERT INTO threads VALUES (?, ?, 0)", (CHAT, str(self.local / DAY / NAME)))
        connection.commit()
        connection.close()

    def plan(self, local_turns: int, mirror_turns: int):
        (self.mirror / DAY / f"{NAME}.xz").write_bytes(lzma.compress(_records(mirror_turns)))
        return build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.mirror),
            local_root=self.local, remote_root=self.mirror,
            source_machine="desktop", target_machine="laptop",
            layout_id=SAME_PATH_LAYOUT_ID, placements=read_thread_placements(self.local),
        )


@unittest.skipUnless(ZSTD_READABLE, "this Python has no compression.zstd (added in 3.14)")
class ReadTests(_Roots):
    def setUp(self) -> None:
        super().setUp()
        self.packed = self.local / DAY / f"{NAME}.zst"

    def test_a_compressed_chat_reads_as_its_plain_history(self) -> None:
        self.packed.write_bytes(_zstd(_records(3)))
        with open_jsonl(self.packed) as handle:
            self.assertEqual(handle.read(), _records(3))
        (item,) = scan_sessions(self.local).descriptors
        self.assertIs(item.state, SessionState.ACTIVE)
        self.assertEqual(item.session_id, CHAT)
        self.assertEqual(item.line_count, 4)

    def test_the_same_history_in_the_mirror_is_identical(self) -> None:
        self.packed.write_bytes(_zstd(_records(3)))
        (item,) = self.plan(3, 3).items
        self.assertEqual(item.action, TransferAction.NOOP, item.codes)

    def test_a_chat_that_grew_here_goes_to_the_mirror_in_its_own_container(self) -> None:
        self.packed.write_bytes(_zstd(_records(5)))
        (item,) = self.plan(5, 3).items
        self.assertEqual(item.action, TransferAction.FAST_FORWARD_REMOTE, item.codes)
        self.assertEqual(item.target_relative_path, f"{DAY}/{NAME}.xz")

    def test_a_chat_that_grew_elsewhere_is_not_written_over_the_compressed_file(self) -> None:
        # What Codex reads when `x.jsonl` sits beside `x.jsonl.zst` is not
        # known yet; until it is, the newer copy waits in the mirror.
        self.packed.write_bytes(_zstd(_records(3)))
        (item,) = self.plan(3, 5).items
        self.assertEqual(item.action, TransferAction.BLOCKED_UNPROVEN_LAYOUT, item.codes)
        self.assertIn(IN_PLACE_CONTAINER, item.codes)
        self.assertFalse((self.local / DAY / NAME).exists())

    def test_doctor_does_not_call_a_compressed_chat_hidden(self) -> None:
        # Codex's row names `x.jsonl` and Codex reads `x.jsonl.zst`: that is a
        # chat it shows. Read as two different paths, doctor reported 323 of
        # 340 real chats hidden and advised a catalogue rebuild (CS-338).
        from codexsync.preflight import _check_session_visibility

        self.packed.write_bytes(_zstd(_records(3)))
        result = _check_session_visibility(self.local, scan_sessions(self.local))
        self.assertEqual(result.status, "PASS", result.details)
        self.assertIn("listed_at_another_path=0", result.details)

    def test_a_damaged_compressed_file_is_an_unreadable_copy(self) -> None:
        self.packed.write_bytes(_zstd(_records(3))[:-7])
        (item,) = scan_sessions(self.local).descriptors
        self.assertIs(item.state, SessionState.INVALID)
        self.assertIn("READ_ERROR", item.codes)
        (planned,) = self.plan(3, 5).items
        self.assertEqual(planned.action, TransferAction.BLOCKED_INVALID_BRANCH, planned.codes)

    def test_transcoding_into_the_mirror_keeps_every_byte(self) -> None:
        self.packed.write_bytes(_zstd(_records(4)))
        stored = self.root / "stored.jsonl.xz"
        transcode(self.packed, stored, JsonlCodec.ZSTD, JsonlCodec.XZ)
        self.assertEqual(lzma.decompress(stored.read_bytes()), _records(4))


@unittest.skipIf(ZSTD_READABLE, "this Python reads zstd")
class WithoutZstdTests(_Roots):
    def test_a_compressed_chat_is_blocked_on_both_sides(self) -> None:
        (self.local / DAY / f"{NAME}.zst").write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 32)
        (item,) = self.plan(3, 5).items
        self.assertEqual(item.action, TransferAction.BLOCKED_INVALID_BRANCH, item.codes)
        self.assertIn(UNSUPPORTED_CONTAINER, item.codes)
        self.assertFalse((self.local / DAY / NAME).exists())


if __name__ == "__main__":
    unittest.main()
