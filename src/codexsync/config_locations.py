"""Which `config.toml` a command opens when it was not told.

This used to live in the GUI, and the two shells disagreed because of it: the
window looked in four places, while the command line's `-c` defaulted to the
literal `config.toml` and therefore only ever looked in the current directory.
A machine set up through the window -- whose config sits in the per-user
location, or beside a downloaded exe -- answered every terminal command with
"Config file not found: config.toml". The rule is one rule now, and it lives
here so that neither shell can drift from it again.

The order is deliberate. An explicit `-c` wins even when the file is absent,
because naming a path that does not exist yet is a request to create it there
rather than a typo to route around. After that only files that actually exist
are considered: the window's remembered path (a window notion -- the command
line passes `None`), the current directory, the folder of a frozen executable,
and finally the per-user location, which is also what is proposed when nothing
was found at all.

Nothing here reads a config, and nothing here is remembered: the *content* of
a config never leaves the file, and the caller decides what to do with a path
that does not exist.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile

CONFIG_NAME = "config.toml"


@dataclass(frozen=True)
class ConfigChoice:
    path: Path
    #: ``explicit`` | ``remembered`` | ``cwd`` | ``executable`` | ``user`` | ``new``
    source: str
    exists: bool
    #: Set when a remembered path was on offer and deliberately not taken.
    #: The caller shows it; nothing here decides anything from it.
    rejected_remembered: Path | None = None


def is_under_temp(path: Path) -> bool:
    """Is this path inside the user's temporary directory?

    A config there is somebody's scratch file. It may well exist and load, and
    then the window quietly runs against a throwaway state directory with
    throwaway settings -- which is exactly what happened when a smoke run left
    its own config remembered and every later start opened it (CS-266). The
    file surviving is not evidence that it was meant to be used.
    """
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        resolved = Path(path).resolve()
    except OSError:
        return False
    return resolved == temp or temp in resolved.parents


def user_config_path() -> Path:
    """The per-user config location for this platform.

    Where a config is created when the machine has none anywhere else, so an
    exe that lives in Downloads still keeps its settings somewhere sane.
    """
    if sys.platform == "win32":
        base = os.getenv("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "CodexSync" / CONFIG_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexSync" / CONFIG_NAME
    base = os.getenv("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "codexsync" / CONFIG_NAME


def choose_config_path(
    explicit: str | Path | None = None,
    remembered: str | Path | None = None,
    *,
    cwd: Path | None = None,
    executable_dir: Path | None = None,
    user_path: Path | None = None,
) -> ConfigChoice:
    """Pick the config file to open.

    ``-c`` wins even when the file is absent: naming a path that does not exist
    yet is a request to create it there, not a typo to route around. Everything
    after it is tried only if the file is actually there, and the last resort is
    the per-user path as a file still to be created.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        return ConfigChoice(path, "explicit", path.is_file())

    user = (user_path or user_config_path()).expanduser()
    ordered: list[tuple[Path, str]] = []
    rejected: Path | None = None
    if remembered is not None:
        candidate = Path(remembered).expanduser()
        # A remembered path is only a hint, and a hint pointing into the
        # temporary directory is not one worth following.
        if is_under_temp(candidate):
            rejected = candidate
        else:
            ordered.append((candidate, "remembered"))
    ordered.append(((cwd or Path.cwd()) / CONFIG_NAME, "cwd"))
    if executable_dir is not None:
        ordered.append((Path(executable_dir) / CONFIG_NAME, "executable"))
    ordered.append((user, "user"))

    for path, source in ordered:
        if path.is_file():
            return ConfigChoice(path, source, True, rejected)
    return ConfigChoice(user, "new", False, rejected)


def frozen_executable_dir() -> Path | None:
    """The folder of a PyInstaller build, or ``None`` in a source checkout."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


__all__ = [
    "CONFIG_NAME",
    "ConfigChoice",
    "choose_config_path",
    "frozen_executable_dir",
    "is_under_temp",
    "user_config_path",
]
