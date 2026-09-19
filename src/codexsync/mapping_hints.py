"""What to offer when someone writes a `[[path_mappings]]` rule.

A rule says "this folder over there is that folder over here", and both halves
were being typed from memory into a table cell. The machine already knows most
of the answer: the chats it holds record the working directory they ran in, and
the state records each project's root. A folder that a chat names and that does
not exist here is precisely what a rule is for -- it is the path the other
machine used.

Read-only, and it never decides anything: it produces candidates and counts, so
a screen can say "this rule will reach 26 chats" before the rule is saved.
Nothing here writes, and no path is created to test whether it exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .chat_directory import ChatDirectory

__all__ = ["MappingHints", "build_mapping_hints"]


@dataclass(frozen=True, slots=True)
class MappingHints:
    """Suggestions for both halves of a rule, and what a rule would reach."""

    #: Machine names worth offering: this config's, and both ends of its rules.
    machines: tuple[str, ...]
    #: ``(folder, how many chats ran there)``, most chats first.
    chat_roots: tuple[tuple[str, int], ...]
    #: Chat folders that do not exist on this machine -- what a rule is for.
    unmapped_roots: tuple[str, ...]
    #: Project roots from the state that are here, and that are not.
    local_project_roots: tuple[str, ...]
    remote_project_roots: tuple[str, ...]

    def chats_under(self, folder: str) -> int:
        """How many chats a rule for ``folder`` would reach."""
        prefix = _normalise(folder)
        if not prefix:
            return 0
        return sum(
            count for root, count in self.chat_roots
            if _normalise(root) == prefix or _normalise(root).startswith(prefix.rstrip("/") + "/")
        )

    def local_matches(self, folder: str) -> tuple[str, ...]:
        """Local project roots whose last folder name is the same as ``folder``'s.

        A project that moved keeps its name, so the folder here is nearly
        always the one with the matching tail -- offered, never chosen.
        """
        tail = _normalise(folder).rstrip("/").rsplit("/", 1)[-1]
        if not tail:
            return ()
        return tuple(
            root for root in self.local_project_roots
            if _normalise(root).rstrip("/").rsplit("/", 1)[-1] == tail
        )


def _normalise(value: str) -> str:
    return value.replace("\\", "/").casefold().strip()


def _exists(value: str) -> bool:
    try:
        return Path(value).exists()
    except OSError:  # pragma: no cover - an unmappable name on this platform
        return False


def build_mapping_hints(
    directory: ChatDirectory,
    *,
    machines: tuple[str, ...] = (),
) -> MappingHints:
    """Turn a chat directory into the two lists a rule form offers."""
    counts: dict[str, int] = {}
    for chat in directory.chats:
        if chat.cwd:
            counts[chat.cwd] = counts.get(chat.cwd, 0) + 1

    project_roots: list[str] = []
    for project in directory.projects.values():
        for root in project.roots:
            if root not in project_roots:
                project_roots.append(root)

    here: set[str] = set()
    missing: set[str] = set()
    for value in list(counts) + project_roots:
        (here if _exists(value) else missing).add(value)

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return MappingHints(
        machines=tuple(sorted({name for name in machines if name})),
        chat_roots=tuple(ordered),
        unmapped_roots=tuple(sorted(root for root, _count in ordered if root in missing)),
        local_project_roots=tuple(sorted(root for root in project_roots if root in here)),
        remote_project_roots=tuple(sorted(root for root in project_roots if root in missing)),
    )
