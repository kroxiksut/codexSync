"""A chat continued in pages is one chat in several files (CS-356).

Codex 0.160 carries a long chat on in a second file,
`rollout-<time>-<id>_<other id>.jsonl`, that opens with the same `session_meta`
id and a `history_base` naming the record it continues from. codexSync read the
two files as one session id in two places, settled that by the catalogue
(CS-348) and compared the page against the first file in the mirror: a
divergence with "no common records" that stopped every sync, while in fact the
first file had only grown and the page was new. Worse, a mirror that held only
a page never received the first part at all.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.semantic_transfer import (
    CATALOG_NAMES_OTHER_PART,
    SAME_PATH_LAYOUT_ID,
    TransferAction,
    build_transfer_plan,
    descriptors_by_session_hash,
)
from codexsync.session_catalog import (
    DUPLICATE_SESSION_ID,
    HISTORY_PAGE,
    UNREADABLE_HISTORY_BASE,
    SessionState,
    latest_page,
    one_per_chat,
    scan_sessions,
)
from codexsync.sqlite_audit import PlacementStatus, ThreadPlacements

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"
CHAT = "01a0dc5e-bb77-7a21-8e44-f5b4e27fc152"
PAGE_ID = "01a10697-e26e-7921-b16b-2582c41a376c"
BASE_NAME = f"rollout-2026-09-26T14-19-51-{CHAT}.jsonl"
PAGE_NAME = f"rollout-2026-10-04T19-06-19-{CHAT}_{PAGE_ID}.jsonl"
BASE_REL = f"sessions/2026/09/26/{BASE_NAME}"
PAGE_REL = f"sessions/2026/10/04/{PAGE_NAME}"


def _rows(first: dict, count: int, *, start: int = 1, day: str = "2026-09-26") -> bytes:
    rows = [first]
    for index in range(start, start + count):
        rows.append({
            "timestamp": f"{day}T10:{index // 60 % 60:02d}:{index % 60:02d}.000Z", "ordinal": index,
            "type": "event_msg", "payload": {"type": "user_message", "message": f"turn {index}"},
        })
    return b"".join(json.dumps(row).encode("utf-8") + b"\n" for row in rows)


def _base(turns: int) -> bytes:
    return _rows({
        "timestamp": "2026-09-26T06:19:51.032Z", "ordinal": 0, "type": "session_meta",
        "payload": {"id": CHAT, "cwd": "D:\\Projects\\app", "history_mode": "paginated"},
    }, turns)


def _page(turns: int, *, base: object = None) -> bytes:
    history_base = base if base is not None else {
        "thread_id": CHAT, "end_ordinal_exclusive": 9127, "end_byte_offset": 37003511,
    }
    return _rows({
        "timestamp": "2026-10-04T11:06:19.630Z", "ordinal": 0, "type": "session_meta",
        "payload": {"id": CHAT, "session_id": CHAT, "cwd": "D:\\Projects\\app",
                    "history_mode": "paginated", "history_base": history_base},
    }, turns, day="2026-10-04")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"history-pages-{uuid.uuid4().hex[:8]}"
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        for side in (self.local, self.cloud):
            (side / "sessions").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, side: Path, relative: str, payload: bytes) -> None:
        path = side / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def plan(self, **kwargs):
        return build_transfer_plan(
            scan_sessions(self.local), scan_sessions(self.cloud),
            local_root=self.local, remote_root=self.cloud,
            source_machine="desktop", target_machine="laptop", **kwargs,
        )


class PageCatalogTests(_Sandbox):
    def test_a_page_and_the_file_it_continues_are_two_branches_not_a_duplicate(self) -> None:
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3))
        catalog = scan_sessions(self.local)
        self.assertEqual(catalog.branches, {})
        by_path = {item.relative_path: item for item in catalog.descriptors}
        base, page = by_path[BASE_REL], by_path[PAGE_REL]
        for item in (base, page):
            self.assertEqual(item.state, SessionState.ACTIVE)
            self.assertNotIn(DUPLICATE_SESSION_ID, item.codes)
        self.assertEqual(base.branch_key, CHAT, "the first file keeps the session's identity")
        self.assertEqual(page.page, "page-9127")
        self.assertEqual(page.branch_key, f"{CHAT}#page-9127")
        self.assertIn(HISTORY_PAGE, page.codes)

    def test_a_base_in_an_unknown_shape_is_not_guessed_into_a_page(self) -> None:
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3, base={"thread_id": CHAT, "end_ordinal_exclusive": "9127"}))
        catalog = scan_sessions(self.local)
        page = next(item for item in catalog.descriptors if item.relative_path == PAGE_REL)
        self.assertIn(UNREADABLE_HISTORY_BASE, page.codes)
        self.assertIsNone(page.page)
        self.assertEqual(page.state, SessionState.AMBIGUOUS, "still one id in two files")

    def test_chat_level_readers_see_one_chat(self) -> None:
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3))
        descriptors = scan_sessions(self.local).descriptors
        (chat,) = one_per_chat(descriptors)
        self.assertEqual(chat.relative_path, BASE_REL)
        self.assertEqual(latest_page(descriptors)[CHAT].relative_path, PAGE_REL)

    def test_a_page_without_its_first_file_is_still_the_chat(self) -> None:
        self.write(self.local, PAGE_REL, _page(3))
        (chat,) = one_per_chat(scan_sessions(self.local).descriptors)
        self.assertEqual(chat.relative_path, PAGE_REL)


class PagePlanTests(_Sandbox):
    def test_a_grown_first_file_and_a_new_page_are_carried_not_a_conflict(self) -> None:
        # The observed case: the mirror holds the first file as it was; here it
        # grew and a page followed.
        self.write(self.cloud, BASE_REL, _base(5))
        self.write(self.local, BASE_REL, _base(8))
        self.write(self.local, PAGE_REL, _page(3))
        plan = self.plan()
        by_hash = {item.session_hash: item for item in plan.items}
        base = by_hash[_hash(CHAT)]
        page = by_hash[_hash(f"{CHAT}#page-9127")]
        self.assertEqual(base.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(page.action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertIn(HISTORY_PAGE, page.codes)
        self.assertEqual(page.target_relative_path, PAGE_REL)
        self.assertFalse(plan.blocked_items)

    def test_a_mirror_holding_only_the_page_receives_the_first_part(self) -> None:
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3))
        self.write(self.cloud, PAGE_REL, _page(3))
        plan = self.plan()
        by_hash = {item.session_hash: item for item in plan.items}
        self.assertEqual(by_hash[_hash(CHAT)].action, TransferAction.FAST_FORWARD_REMOTE)
        self.assertEqual(by_hash[_hash(f"{CHAT}#page-9127")].action, TransferAction.NOOP)

    def test_the_first_file_is_written_in_place_where_the_catalogue_names_its_page(self) -> None:
        # Here the catalogue's row names the page; the first file is reached
        # through it, so growing it in place is the same chat.
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3))
        self.write(self.cloud, BASE_REL, _base(8))
        self.write(self.cloud, PAGE_REL, _page(3))
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {CHAT: PAGE_REL})
        plan = self.plan(placements=placements)
        base = next(item for item in plan.items if item.session_hash == _hash(CHAT))
        self.assertEqual(base.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(base.target_relative_path, BASE_REL)
        self.assertIn(CATALOG_NAMES_OTHER_PART, base.codes)

    def test_a_page_arrives_where_the_catalogue_still_names_the_first_file(self) -> None:
        # The other machine: it never paginated, so its row names the first file.
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.cloud, BASE_REL, _base(5))
        self.write(self.cloud, PAGE_REL, _page(3))
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {CHAT: BASE_REL})
        plan = self.plan(placements=placements, layout_id=SAME_PATH_LAYOUT_ID)
        page = next(item for item in plan.items if item.session_hash == _hash(f"{CHAT}#page-9127"))
        self.assertEqual(page.action, TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(page.target_relative_path, PAGE_REL)
        self.assertIn(CATALOG_NAMES_OTHER_PART, page.codes)

    def test_a_working_set_naming_the_chat_covers_its_pages(self) -> None:
        self.write(self.cloud, BASE_REL, _base(8))
        self.write(self.cloud, PAGE_REL, _page(3))
        self.write(self.local, BASE_REL, _base(5))
        placements = ThreadPlacements(PlacementStatus.AVAILABLE, {CHAT: BASE_REL})
        plan = self.plan(placements=placements, layout_id=SAME_PATH_LAYOUT_ID, scope={_hash(CHAT)})
        actions = {item.session_hash: item.action for item in plan.items}
        self.assertEqual(actions[_hash(CHAT)], TransferAction.FAST_FORWARD_LOCAL)
        self.assertEqual(actions[_hash(f"{CHAT}#page-9127")], TransferAction.FAST_FORWARD_LOCAL)

    def test_an_apply_finds_each_part_by_its_own_hash(self) -> None:
        self.write(self.local, BASE_REL, _base(5))
        self.write(self.local, PAGE_REL, _page(3))
        found = descriptors_by_session_hash(scan_sessions(self.local))
        self.assertEqual(found[_hash(CHAT)].relative_path, BASE_REL)
        self.assertEqual(found[_hash(f"{CHAT}#page-9127")].relative_path, PAGE_REL)


if __name__ == "__main__":
    unittest.main()
