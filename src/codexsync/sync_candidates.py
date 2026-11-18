"""What could be put in `targets.include_roots`, one level at a time.

A settings screen offering a tree of the Codex state directory has to read that
directory, and the GUI is not allowed to. So the reading happens here, as a
listing and nothing else: names, whether they are folders, and three facts that
decide how each one may be offered.

Three rules are the whole design.

*A secret is never offered.* `auth.json`, `cap_sid` and `.sandbox-secrets/`
hold tokens, and `AI_RULES` 1 says codexSync does not touch them. Leaving them
out of the answer means no screen can put a checkbox next to one by accident --
a filter applied in the window would be a rule kept in the wrong place.

*Semantic-owned paths are reported, not hidden.* `sessions/`, the index, the
global state and the SQLite files are excluded from plain copying by
`runtime._is_semantic_owned`; adding one to `include_roots` does nothing at
all. A screen that simply omitted them would leave a person hunting for
`sessions` in a list where it cannot be; one that shows them greyed with a
reason answers the question instead.

*Nothing is opened.* The listing calls `iterdir`, `is_dir` and nothing else --
no file inside the state directory is read, in keeping with the rule the
SQLite audit had to learn the hard way.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .exceptions import ConfigError
from .filters import PathFilter
from .models import AppConfig
from .runtime import SECRET_NAMES, _is_semantic_owned
from .state_locator import locate_local_state_dir

__all__ = ["SyncCandidate", "SECRET_NAMES", "list_sync_candidates"]


@dataclass(frozen=True, slots=True)
class SyncCandidate:
    """One entry under a node, as a settings screen may offer it."""

    #: Path relative to the state root, with forward slashes.
    relative: str
    name: str
    is_dir: bool
    #: Present in the local state directory / in the cloud mirror.
    local: bool
    cloud: bool
    #: Owned by the semantic layer: `sync` never copies it, so it cannot be
    #: chosen. Reported with its reason rather than left out.
    semantic_owned: bool
    #: Matched by one of `filters.exclude_globs`; choosing it changes nothing
    #: until that glob is changed too.
    excluded_by_glob: bool


def _children(path: Path) -> dict[str, bool]:
    """``name -> is_dir`` for one directory, or nothing if it is not readable."""
    try:
        return {entry.name: entry.is_dir() for entry in path.iterdir()}
    except OSError:
        return {}


def list_sync_candidates(cfg: AppConfig, relative: str = "") -> tuple[SyncCandidate, ...]:
    """The children of one node, merged across the local and cloud sides.

    Read-only, one level deep: a tree that walked `.codex` to the bottom would
    hash nothing but would still take seconds on 864 MiB of sessions, and the
    dialog only ever shows one level at a time anyway.
    """
    node = relative.replace("\\", "/").strip("/")
    parts = [part for part in node.split("/") if part]
    # Only inside `.codex` and its mirror, and never inside a folder whose
    # name marks a secret: `..` listed the folders above, and a secret folder
    # listed the names of what it holds (CS-540).
    if any(part in {".", ".."} or ":" in part for part in parts):
        raise ConfigError(
            f"{relative!r} is not a folder inside the Codex state directory",
            code="ROOT_OUTSIDE_STATE", params={"path": relative},
        )
    if any(part in SECRET_NAMES for part in parts):
        raise ConfigError(
            f"{relative!r} is a secret folder; codexSync does not list it",
            code="ROOT_IS_SECRET", params={"path": relative},
        )
    local_root = locate_local_state_dir(cfg)
    roots = {"local": local_root, "cloud": cfg.paths.cloud_root_dir}

    found: dict[str, dict[str, bool]] = {}
    for side, root in roots.items():
        if root is None:
            continue
        base = root / Path(*node.split("/")) if node else root
        for name, is_dir in _children(base).items():
            entry = found.setdefault(name, {"is_dir": False, "local": False, "cloud": False})
            entry["is_dir"] = entry["is_dir"] or is_dir
            entry[side] = True

    path_filter = PathFilter(cfg.filters.exclude_globs)
    candidates = []
    for name in sorted(found, key=str.casefold):
        if name in SECRET_NAMES:
            continue
        entry = found[name]
        child = f"{node}/{name}" if node else name
        candidates.append(SyncCandidate(
            relative=child,
            name=name,
            is_dir=bool(entry["is_dir"]),
            local=bool(entry["local"]),
            cloud=bool(entry["cloud"]),
            # A folder is owned when what is inside it is: `sessions` itself
            # is not a path the copier ever sees, `sessions/<file>` is.
            semantic_owned=_is_semantic_owned(child) or (
                bool(entry["is_dir"]) and _is_semantic_owned(f"{child}/any")
            ),
            excluded_by_glob=path_filter.is_excluded(child),
        ))
    return tuple(candidates)
