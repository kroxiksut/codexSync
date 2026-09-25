"""Where `config.toml` is, and where this machine's workspace might already be.

Qt-free on purpose: the launcher has to answer "which config" before it knows
whether a window can be shown at all, and the boundary tests walk this module
without PySide6 installed.

Two questions live here and they are not the same one.

*Which config file.* An exe started from anywhere but the checkout saw no
`config.toml` and opened "first run", although this machine has one. So the
choice is a short ordered list (`config_locations`), and the window remembers
the answer -- **the path only**, in the pointer file the command line reads
too. When the list finds nothing there is no config: the window never invents
a location for one.

*Where the workspace is.* A machine that syncs through a cloud folder already
has one, with `guardian/`, `semantic/`, `sync/` inside it, and typing that path
again by hand is how a second workspace gets created by accident. The search is
deliberately narrow and shallow: only folders whose name reads as "codexsync",
only under a known cloud client's root, only two levels down, and only when the
folder already holds something codexSync itself creates -- otherwise the source
checkout (`.../Projects/codexSync`) would match and be offered as a workspace.

What the search must never do: open a file inside a candidate, look into
`.codex`, ask a cloud client anything (`AI_RULES` 1), or walk a network or
optical drive. A disconnected network drive blocks a directory listing for
about a minute, which on this machine is the difference between a 20 ms search
and a window that looks hung.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

# One rule for both shells: the command line resolves a missing `-c`
# through the same function, so the window cannot look in places a
# terminal command would not find (CS-258).
from ..config_locations import (
    CONFIG_NAME,
    ConfigChoice,
    choose_config_path,
    frozen_executable_dir,
    is_under_temp,
    read_config_pointer,
    write_config_pointer,
)

__all__ = [
    "CONFIG_NAME",
    "ConfigChoice",
    "WorkspaceCandidate",
    "choose_config_path",
    "find_workspaces",
    "is_under_temp",
    "machines_in_workspace",
    "read_config_pointer",
    "write_config_pointer",
]

#: Folders codexSync creates inside a workspace. One of them has to be there
#: for a folder to count as a workspace rather than a folder with the name.
WORKSPACE_MARKERS = (
    "guardian",
    "semantic",
    "sync",
    "plans",
    "backups",
    "state",
    "temp",
    "config-history",
)

#: Cloud client roots, matched against a folder name in lower case. A trailing
#: ``*`` matches a prefix: OneDrive installs as "OneDrive - <company>" too.
CLOUD_ROOT_NAMES = (
    "yandex.disk",
    "yandexdisk",
    "onedrive*",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud drive",
    "icloudrive",
    "mycloud",
    "sync.com",
    "pcloudrive",
)

#: How far below a cloud root a workspace may sit. ``Yandex.Disk/codexsync`` is
#: one level, ``Yandex.Disk/Projects/codexsync`` is two.
MAX_DEPTH = 2


@dataclass(frozen=True)
class WorkspaceCandidate:
    """A folder that looks like a codexSync workspace someone already made."""

    path: Path
    #: The marker folders found inside it, sorted. Shown, not guessed from.
    markers: tuple[str, ...]
    #: Machine names already filed in it. Offered as a warning, never selected:
    #: two machines under one name mix their Guardian snapshots together.
    machines: tuple[str, ...]


# --- finding a workspace --------------------------------------------------


def _matches(name: str, pattern: str) -> bool:
    lowered = name.lower()
    if pattern.endswith("*"):
        return lowered.startswith(pattern[:-1])
    return lowered == pattern


def _looks_like_workspace_name(name: str) -> bool:
    """``codexSync``, ``codex-sync``, ``codex_sync`` -- one name, three spellings."""
    return "".join(ch for ch in name.lower() if ch.isalnum()) == "codexsync"


def _children(path: Path) -> list[Path]:
    """Directory entries, or nothing. A listing that fails is not an error here."""
    try:
        return [entry for entry in path.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _local_drive_roots() -> list[Path]:
    """Drive roots worth walking: fixed disks only.

    A network drive that is mapped but unreachable makes the first listing take
    about a minute, and an optical drive spins up. Neither is where a cloud
    folder lives.
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, ImportError, OSError):  # pragma: no cover - not Windows
        return []
    drive_fixed = 3
    roots: list[Path] = []
    try:
        mask = kernel32.GetLogicalDrives()
    except OSError:  # pragma: no cover - defensive
        return []
    for index in range(26):
        if not mask & (1 << index):
            continue
        letter = f"{chr(ord('A') + index)}:\\"
        try:
            if kernel32.GetDriveTypeW(letter) == drive_fixed:
                roots.append(Path(letter))
        except OSError:  # pragma: no cover - defensive
            continue
    return roots


def cloud_roots(*, home: Path | None = None, drives: list[Path] | None = None) -> tuple[Path, ...]:
    """Folders a cloud client keeps its tree in, on this machine.

    The environment is asked first: OneDrive publishes its real root there, and
    it is often not under the home directory at all.
    """
    home = home or Path.home()
    found: list[Path] = []

    for name, value in os.environ.items():
        if name.lower().startswith("onedrive") and value:
            candidate = Path(value)
            if candidate.is_dir():
                found.append(candidate)

    cloud_storage = home / "Library" / "CloudStorage"
    found.extend(_children(cloud_storage))

    searched = [home, *( _local_drive_roots() if drives is None else drives)]
    for root in searched:
        for child in _children(root):
            if any(_matches(child.name, pattern) for pattern in CLOUD_ROOT_NAMES):
                found.append(child)

    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return tuple(unique)


def find_workspaces(
    *,
    home: Path | None = None,
    drives: list[Path] | None = None,
    roots: tuple[Path, ...] | None = None,
    max_depth: int = MAX_DEPTH,
) -> tuple[WorkspaceCandidate, ...]:
    """Workspaces already on this machine, searched shallowly and read-only.

    Nothing is opened and nothing is created: the answer is built from folder
    names alone, which is also why a folder has to carry a marker folder to
    count -- the source checkout is called ``codexSync`` too.
    """
    starting = cloud_roots(home=home, drives=drives) if roots is None else roots
    found: list[WorkspaceCandidate] = []
    seen: set[Path] = set()

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for child in _children(folder):
            if child in seen:
                continue
            seen.add(child)
            if _looks_like_workspace_name(child.name):
                markers = tuple(sorted(
                    entry.name for entry in _children(child) if entry.name in WORKSPACE_MARKERS
                ))
                if markers:
                    found.append(WorkspaceCandidate(child, markers, machines_in_workspace(child)))
                    continue
            walk(child, depth + 1)

    for root in starting:
        walk(root, 1)
    return tuple(sorted(found, key=lambda item: str(item.path)))


def machines_in_workspace(root: Path) -> tuple[str, ...]:
    """Machine names already filed in a workspace, from folder and file names.

    Read for the warning it makes possible, not to choose from: picking one of
    these automatically is how two machines end up writing snapshots under the
    same name. No file is opened -- a name is enough.
    """
    names: set[str] = set()
    guardian = root / "guardian"
    for area in ("snapshots", "quarantine"):
        names.update(entry.name for entry in _children(guardian / area))
    names.update(entry.name for entry in _children(root / "semantic" / "manifest"))
    try:
        names.update(
            entry.stem for entry in (guardian / "latest-good").iterdir()
            if entry.is_file() and entry.suffix == ".json"
        )
    except OSError:
        pass
    return tuple(sorted(name for name in names if name))
