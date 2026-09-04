"""Put chosen chats under a chosen project, as a frozen, checkable decision.

The write itself is one line of JSON per chat: an entry in
``thread-project-assignments`` in the shape the detected schema uses. Everything
else here exists so that line is never written by accident.

A plan is identified by a hash over the decisions *and* over the exact bytes of
the state it was built from. That single property does the work a saved plan
file does elsewhere in this project: a person previews a move, gets an id, and
quoting that id back is only possible while the state is still the one they
looked at. Anything that moved in between — Codex reassigned a thread, a project
was renamed away, another machine synced in — changes the id and the move is
refused rather than applied to a picture nobody saw.

What this does **not** do is decide which chats to move. That is the caller's
choice, made from the chat directory, and it is recorded verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any

from .chat_directory import Association, ChatDirectory, ChatEntry, ChatKind
from .guardian_schema import build_binding_value


CHAT_MOVE_PLAN_VERSION = 1


class ChatMoveKind(str, Enum):
    #: The chat had no binding at all; the move pins it.
    BIND = "BIND"
    #: The chat was bound to another project; the move repoints it.
    REBIND = "REBIND"
    #: Already bound to this project. Kept in the plan so the person sees the
    #: chat they named was considered, and writes nothing.
    ALREADY_THERE = "ALREADY_THERE"

    @property
    def writes(self) -> bool:
        return self in {ChatMoveKind.BIND, ChatMoveKind.REBIND}


@dataclass(frozen=True, slots=True)
class ChatMoveAction:
    session_id: str
    kind: ChatMoveKind
    to_project_id: str
    #: Where the chat sits now, and why it sits there. A chat that only reaches
    #: its current project through the directory is about to stop doing so
    #: silently for anyone reading the state later, so the reason is recorded.
    from_project_id: str | None
    from_association: Association


@dataclass(frozen=True, slots=True)
class ChatMovePlan:
    version: int
    plan_id: str
    global_state_sha256: str
    schema_id: str
    to_project_id: str
    actions: tuple[ChatMoveAction, ...]
    codes: tuple[str, ...] = ()

    @property
    def writing_actions(self) -> tuple[ChatMoveAction, ...]:
        return tuple(action for action in self.actions if action.kind.writes)


def build_chat_move_plan(
    directory: ChatDirectory,
    global_state: bytes,
    *,
    chats: tuple[ChatEntry, ...],
    to_project_id: str,
) -> ChatMovePlan:
    """Decide, for each named chat, what moving it to this project means."""
    codes: list[str] = []
    if to_project_id not in directory.projects:
        codes.append("TARGET_PROJECT_MISSING")

    actions: list[ChatMoveAction] = []
    for chat in sorted(chats, key=lambda item: item.session_id):
        if chat.kind is not ChatKind.TOP_LEVEL:
            # A spawned thread is not shown under a project, so pinning it says
            # something the runtime never asked. Refuse rather than write it.
            codes.append("NOT_A_CHAT")
        if chat.association is Association.BOUND and chat.project_id == to_project_id:
            kind = ChatMoveKind.ALREADY_THERE
        elif chat.association is Association.BOUND:
            kind = ChatMoveKind.REBIND
        else:
            kind = ChatMoveKind.BIND
        actions.append(
            ChatMoveAction(
                chat.session_id, kind, to_project_id, chat.project_id, chat.association
            )
        )

    if not actions:
        codes.append("NO_CHATS_SELECTED")
    elif not any(action.kind.writes for action in actions):
        codes.append("NOTHING_TO_DO")

    plan = ChatMovePlan(
        CHAT_MOVE_PLAN_VERSION, "", hashlib.sha256(global_state).hexdigest(),
        directory.schema_id, to_project_id, tuple(actions), tuple(dict.fromkeys(codes)),
    )
    return _with_plan_id(plan)


def apply_chat_moves_to_state(state: dict[str, Any], plan: ChatMovePlan) -> int:
    """Write the plan's bindings into an already-parsed state. Nothing else.

    The binding shape comes from the schema recorded in the plan, so a state
    that changed family since the plan was built cannot be written with the old
    shape — the plan id would already have refused that, and this is the second
    door on the same room.
    """
    assignments = state.setdefault("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        raise ValueError("thread-project-assignments is not an object")
    written = 0
    for action in plan.writing_actions:
        assignments[action.session_id] = build_binding_value(plan.schema_id, action.to_project_id)
        written += 1
    return written


def _serialise(plan: ChatMovePlan) -> dict:
    return {
        "version": plan.version,
        "global_state_sha256": plan.global_state_sha256,
        "schema_id": plan.schema_id,
        "to_project_id": plan.to_project_id,
        "codes": list(plan.codes),
        "actions": [
            {
                "session_id": action.session_id,
                "kind": action.kind.value,
                "to_project_id": action.to_project_id,
                "from_project_id": action.from_project_id,
                "from_association": action.from_association.value,
            }
            for action in plan.actions
        ],
    }


def _with_plan_id(plan: ChatMovePlan) -> ChatMovePlan:
    digest = hashlib.sha256(
        json.dumps(_serialise(plan), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ChatMovePlan(
        plan.version, digest, plan.global_state_sha256, plan.schema_id,
        plan.to_project_id, plan.actions, plan.codes,
    )
