"""Where Codex keeps its projects -- and why codexSync will not guess.

`local-projects` in `.codex-global-state.json` is not the only place a project
exists. `state_5.sqlite` has a `projects` table (48 rows on the machine this
was examined on, against 18 in the JSON, most of them older generations of the
same names) and a `project_roots` table beside it, and the state records
`app-server-projects-migration-by-host.projectsMigrated = true`.

What *is* established, from a screenshot of the Codex sidebar taken on
2026-09-13: pinning, the selected project and the order all follow the **JSON**
ids -- `pinned-project-ids`, `selected-project` and
`electron-persisted-atom-state.unified-sidebar-project-order-v1` (16 entries,
including both duplicate copies of NetRuleRouter and CommentRake) match what
the window shows. What is *not* established is where the list's membership and
each project's root path come from. Those are exactly what a delete, a merge or
a repointing would change.

So this gate exists, and it is empty:

* **Deleting or merging a project in JSON alone is not implemented.** If the
  app-server registry is the truth, such an edit would remove a project that
  comes back on the next launch, or leave a ghost behind.
* **The moves that *are* implemented are narrower than they look.**
  `project_move` and `REMAP_ROOT` rewrite `rootPaths` in the JSON; the matching
  `project_roots.path` row in SQLite keeps the old value, because codexSync
  does not write SQLite at all. Whether Codex then starts a new chat in the new
  folder is unverified. It is reported by `doctor` rather than assumed.

`docs/dev/experiments/project-registry-contract.md` says what to run to settle it.
Fill `PROVEN_PROJECT_REGISTRY` from that run and from nothing else -- the same
rule as `PROVEN_CONTRACTS` and `PROVEN_LAYOUTS`, and for the same reason: a
guess here is invisible until someone's projects are gone.
"""
from __future__ import annotations

__all__ = [
    "PROJECT_REGISTRY_UNPROVEN",
    "PROVEN_PROJECT_REGISTRY",
    "registry_is_proven",
    "registry_note",
]

#: Codex runtime families whose project registry has been observed end to end,
#: mapping a schema id to the observation that proved it (Codex version, date,
#: and what the experiment saw). **Filled only from the experiment.**
PROVEN_PROJECT_REGISTRY: dict[str, str] = {}

#: Reported wherever a project's root is rewritten in JSON only.
PROJECT_REGISTRY_UNPROVEN = "PROJECT_REGISTRY_UNPROVEN"


def registry_is_proven(schema_id: str | None) -> bool:
    """Whether JSON-only project edits are known to be what the runtime reads."""
    return bool(schema_id) and schema_id in PROVEN_PROJECT_REGISTRY


def registry_note(schema_id: str | None) -> str:
    """One line for a report: what a JSON-only project change is known to do."""
    proof = PROVEN_PROJECT_REGISTRY.get(schema_id or "")
    if proof:
        return f"JSON project registry confirmed for {schema_id}: {proof}"
    return (
        "Projects also exist in state_*.sqlite, which codexSync never writes. "
        "Moving or remapping a project rewrites rootPaths in the JSON only; "
        "deleting or merging a project is not offered at all. See "
        "docs/dev/experiments/project-registry-contract.md"
    )
