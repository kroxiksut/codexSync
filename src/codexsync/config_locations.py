"""Which `config.toml` a command opens when it was not told.

This used to live in the GUI, and the two shells disagreed because of it: the
window looked in several places, while the command line's `-c` defaulted to the
literal `config.toml` and therefore only ever looked in the current directory.
The rule is one rule now, and it lives here so that neither shell can drift
from it again.

The order: an explicit `-c` wins even when the file is absent, because naming a
path that does not exist yet is a request to create it there rather than a typo
to route around. After that only files that actually exist are considered: the
config the window last opened (the *pointer*), the current directory, and the
folder of a frozen executable. When none of them holds a file the answer is
"no config" -- never a location made up on the user's behalf. An earlier
version proposed a per-user file under `%APPDATA%` and every screen of the
window then worked against that file although nobody had created it or asked
for it there (CS-268). Where a config lives is the user's decision.

The pointer is how a terminal finds the file chosen in the window: one line
holding the absolute path, in the per-user *local* application directory. It
carries the path and never the content, it is written only by the window after
a file was opened or created, and it is local rather than roaming because a
path on this machine's `D:` means nothing on another one.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile

from .config import codex_folder_holding
from .fs_replace import replace_with_retry
from .program_file import frozen_program

CONFIG_NAME = "config.toml"
#: The file holding the path of the config the window last opened.
POINTER_NAME = "config-path.txt"


@dataclass(frozen=True)
class ConfigChoice:
    #: ``None`` when nothing was found and nothing was named: there is no
    #: config, and none is invented.
    path: Path | None
    #: ``explicit`` | ``remembered`` | ``cwd`` | ``executable`` | ``none``
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


def pointer_path() -> Path:
    """Where the window records the path of the config it last opened.

    Local, not roaming: the recorded path belongs to this machine.
    """
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "CodexSync" / POINTER_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexSync" / POINTER_NAME
    base = os.getenv("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "codexsync" / POINTER_NAME


def standard_config_path() -> Path:
    """The standard place for this machine's CodexSync config (CS-410, U-11 option C).

    Beside the pointer, local and never roaming: `%LOCALAPPDATA%\\CodexSync`
    on Windows, `~/Library/Application Support/CodexSync` on macOS,
    `$XDG_CONFIG_HOME/codexsync` (else `~/.config/codexsync`) on Linux. It is
    only ever *offered*: the window writes it into the field on a button, the
    file is created by *Create*, and the search looks there last. Nothing
    creates a config here on its own (CS-258, CS-268).
    """
    return pointer_path().with_name(CONFIG_NAME)


def cache_dir() -> Path:
    """The application's own cache folder: numbers that are slow to compute.

    Beside the pointer on Windows (`%LOCALAPPDATA%\\CodexSync\\cache`), and in
    each platform's cache location elsewhere. Local, like the pointer, and
    nothing in it is needed: a missing or unreadable cache is recomputed.
    """
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "CodexSync" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "CodexSync"
    base = os.getenv("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "codexsync"


def read_config_pointer(pointer: Path | None = None) -> Path | None:
    """The config the window last opened, or ``None``.

    A pointer that is missing, unreadable, empty or relative is simply no
    pointer: it is a hint, and a broken hint is not an error for the command
    that happened to look at it.
    """
    target = pointer if pointer is not None else pointer_path()
    try:
        text = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text or "\n" in text:
        return None
    path = Path(text)
    return path if path.is_absolute() else None


def write_config_pointer(config: Path, pointer: Path | None = None) -> bool:
    """Record ``config`` as the one to open next time; ``False`` if it was not.

    Only an existing file outside the temporary directory is recorded -- a
    scratch config left there by a test or a smoke run is exactly what must
    not become the default (CS-266). The write replaces the pointer in one
    step, so a reader never sees half a path.
    """
    config = Path(config)
    if not config.is_file() or is_under_temp(config) or codex_folder_holding(config) is not None:
        return False
    target = pointer if pointer is not None else pointer_path()
    try:
        value = str(config.resolve())
        if read_config_pointer(target) == Path(value):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        staged.write_text(value + "\n", encoding="utf-8")
        replace_with_retry(staged, target)
    except OSError:
        return False
    return True


def choose_config_path(
    explicit: str | Path | None = None,
    remembered: str | Path | None = None,
    *,
    cwd: Path | None = None,
    executable_dir: Path | None = None,
    standard: Path | None = None,
) -> ConfigChoice:
    """Pick the config file to open.

    ``-c`` wins even when the file is absent: naming a path that does not exist
    yet is a request to create it there, not a typo to route around. Everything
    after it is tried only if the file is actually there: the remembered path,
    the current folder, the program's folder, then ``standard`` (the standard
    place, `standard_config_path`, which callers pass). When nothing is, the
    answer has no path at all.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        return ConfigChoice(path, "explicit", path.is_file())

    ordered: list[tuple[Path, str]] = []
    rejected: Path | None = None
    if remembered is not None:
        candidate = Path(remembered).expanduser()
        # A remembered path is only a hint, and a hint pointing into the
        # temporary directory -- or into Codex's own folder -- is not one
        # worth following.
        if is_under_temp(candidate) or codex_folder_holding(candidate) is not None:
            rejected = candidate
        else:
            ordered.append((candidate, "remembered"))
    ordered.append(((cwd or Path.cwd()) / CONFIG_NAME, "cwd"))
    if executable_dir is not None:
        ordered.append((Path(executable_dir) / CONFIG_NAME, "executable"))
    if standard is not None:
        ordered.append((Path(standard), "standard"))

    for path, source in ordered:
        # A start from inside `.codex` finds Codex's own `config.toml` here
        # (CS-412); only `-c` may name such a file, and loading refuses it.
        if path.is_file() and codex_folder_holding(path) is None:
            return ConfigChoice(path, source, True, rejected)
    return ConfigChoice(None, "none", False, rejected)


def frozen_executable_dir() -> Path | None:
    """Where a frozen build's own file lies, for the "beside the program" search.

    ``None`` in a source checkout, and also for a folder this user cannot write
    (a package installs into one only root may change): a config there could
    never be saved, so it is not looked for (CS-420). Inside an AppImage this
    is the AppImage's folder, not its temporary mount (`program_file`).
    """
    program = frozen_program()
    if program is None:
        return None
    folder = program.parent
    if not sys.platform.startswith("win") and not os.access(folder, os.W_OK):
        return None
    return folder


__all__ = [
    "CONFIG_NAME",
    "cache_dir",
    "ConfigChoice",
    "choose_config_path",
    "codex_folder_holding",
    "frozen_executable_dir",
    "is_under_temp",
    "pointer_path",
    "read_config_pointer",
    "standard_config_path",
    "write_config_pointer",
]
