"""Sidebar sections between machines (CS-408).

Codex groups projects and single chats into named sections of its sidebar.
The desktop build keeps them in the global state, per signed-in account::

    electron-persisted-atom-state
      sidebar-custom-sections-v3
        <account id>
          sections:      [{id, name, hostSectionIds, itemKeys, appearance}]
          sectionOrder:  ["custom:<id>", ...]
          threadHostIds: {<thread id>: "local"}
          ...

``itemKeys`` name a project (``codex:project:<legacy id>``) or a chat
(``codex:thread:local:<thread id>``). Codex also keeps a section in
``thread_sections`` of its SQLite catalogue and a chat's section in
``threads.thread_section_id``; ``hostSectionIds.local`` is that per-machine
row id. Observed on Linux 2026-10-10 (Codex 26.1002): a section written into
the JSON alone, without ``hostSectionIds`` and without a row, is shown with its
project and its chat, and Codex does not create the row. So sections travel
in the JSON only, like projects (D-022), and ``hostSectionIds`` never travels:
another machine's row id names nothing here.

The merge follows the project merge: match by section id, add what is new,
never remove a section; from a list this machine has not taken yet the peer's
name, items and order win (the person worked there last), and an item the
peer put into a section leaves any other section here. A list already taken
only adds sections that are new here.
"""
from __future__ import annotations

from typing import Any, Iterable

_ATOMS_KEY = "electron-persisted-atom-state"
_SECTIONS_KEY = "sidebar-custom-sections-v3"
_PROJECT_PREFIX = "codex:project:"
_THREAD_PREFIX = "codex:thread:local:"
_ORDER_PREFIX = "custom:"


def _account(state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The one account's section store, or ``None`` when there is not exactly one."""
    atoms = state.get(_ATOMS_KEY)
    if not isinstance(atoms, dict):
        return None
    by_account = atoms.get(_SECTIONS_KEY)
    if not isinstance(by_account, dict) or len(by_account) != 1:
        return None
    account, store = next(iter(by_account.items()))
    if not isinstance(account, str) or not isinstance(store, dict) or not isinstance(store.get("sections"), list):
        return None
    return account, store


def published_sections(state: dict[str, Any]) -> dict[str, Any] | None:
    """What a machine publishes: its account and its sections in sidebar order.

    Project items carry the legacy project id, chat items the thread id;
    ``hostSectionIds`` is left out. ``None`` when there is nothing to carry.
    """
    found = _account(state)
    if found is None:
        return None
    account, store = found
    sections = [item for item in store["sections"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not sections:
        return None
    order = [
        value[len(_ORDER_PREFIX):] for value in store.get("sectionOrder") or ()
        if isinstance(value, str) and value.startswith(_ORDER_PREFIX)
    ]
    rank = {section_id: index for index, section_id in enumerate(order)}
    sections.sort(key=lambda item: (rank.get(item["id"], len(rank)), item["id"]))
    return {
        "account": account,
        "sections": [
            {
                "id": item["id"],
                "name": item.get("name") if isinstance(item.get("name"), str) else "",
                "items": [key for key in item.get("itemKeys") or () if isinstance(key, str)],
                "appearance": item.get("appearance"),
            }
            for item in sections
        ],
    }


def _translate(key: str, projects: dict[str, str], chats: frozenset[str] | None) -> str | None:
    if key.startswith(_PROJECT_PREFIX):
        local = projects.get(key[len(_PROJECT_PREFIX):])
        return _PROJECT_PREFIX + local if local else None
    if key.startswith(_THREAD_PREFIX):
        thread_id = key[len(_THREAD_PREFIX):]
        return key if thread_id and (chats is None or thread_id in chats) else None
    return None


def merge_sections(
    state: dict[str, Any],
    peer: dict[str, Any] | None,
    *,
    projects: dict[str, str],
    fresh: bool,
    chats: Iterable[str] | None = None,
) -> int:
    """Merge one peer's sections into ``state`` in place; returns the changes made.

    ``projects`` maps the peer's project ids to this machine's (the project
    merge's translation); an item for a project or a chat not here is dropped.
    ``chats`` are the thread ids of this machine's chats, ``None`` for "do not
    filter". Sections of another account are not carried.
    """
    if not peer or not isinstance(peer.get("sections"), list):
        return 0
    found = _account(state)
    if found is None or found[0] != peer.get("account"):
        return 0
    _, store = found
    here_chats = frozenset(chats) if chats is not None else None
    sections: list[dict[str, Any]] = store["sections"]
    by_id = {item.get("id"): item for item in sections if isinstance(item, dict)}
    order: list[str] = [value for value in store.get("sectionOrder") or [] if isinstance(value, str)]
    host_ids = store.get("threadHostIds")
    changes = 0
    for offered in peer["sections"]:
        section_id = offered.get("id") if isinstance(offered, dict) else None
        if not isinstance(section_id, str) or not section_id:
            continue
        items = list(dict.fromkeys(
            key for key in (_translate(raw, projects, here_chats) for raw in offered.get("items") or ())
            if key is not None
        ))
        current = by_id.get(section_id)
        if current is None and not fresh:
            # From a list already taken: only what no section here holds.
            placed = {key for item in sections if isinstance(item, dict) for key in item.get("itemKeys") or ()}
            items = [key for key in items if key not in placed]
        if current is None:
            current = {
                "id": section_id, "name": offered.get("name") or "", "hostSectionIds": {},
                "itemKeys": [], "appearance": offered.get("appearance"),
            }
            sections.append(current)
            by_id[section_id] = current
            order.append(_ORDER_PREFIX + section_id)
            changes += 1
        elif not fresh:
            continue
        else:
            for field_name, value in (("name", offered.get("name") or ""), ("appearance", offered.get("appearance"))):
                if current.get(field_name) != value:
                    current[field_name] = value
                    changes += 1
        known = [key for key in current.get("itemKeys") or [] if isinstance(key, str)]
        wanted = items + [key for key in known if key not in items and not _peer_knows(key, projects, here_chats)]
        if wanted != known:
            current["itemKeys"] = wanted
            changes += 1
        # An item sits in one section: the one the peer put it in.
        for other in sections:
            if other is current or not isinstance(other, dict):
                continue
            keys = [key for key in other.get("itemKeys") or [] if key not in items]
            if keys != list(other.get("itemKeys") or []):
                other["itemKeys"] = keys
                changes += 1
        for key in items:
            if key.startswith(_THREAD_PREFIX):
                if not isinstance(host_ids, dict):
                    host_ids = store["threadHostIds"] = {}
                thread_id = key[len(_THREAD_PREFIX):]
                if host_ids.get(thread_id) != "local":
                    host_ids[thread_id] = "local"
    if fresh:
        peer_order = [
            _ORDER_PREFIX + item["id"] for item in peer["sections"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        reordered = [value for value in peer_order if value in order]
        reordered += [value for value in order if value not in set(reordered)]
        if reordered != order:
            order = reordered
            changes += 1
    if order != store.get("sectionOrder"):
        store["sectionOrder"] = order
    return changes


def _peer_knows(key: str, projects: dict[str, str], chats: frozenset[str] | None) -> bool:
    """Whether leaving this local item out was the peer's decision.

    A project the peer has: yes. A chat: the publication does not say which
    chats the peer holds, so a chat it put nowhere stays where it is here (one
    it put into another section leaves this one anyway).
    """
    if key.startswith(_PROJECT_PREFIX):
        return key[len(_PROJECT_PREFIX):] in set(projects.values())
    return False
