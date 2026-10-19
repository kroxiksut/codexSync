"""A menu entry on Linux for a codexSync no package installed (CS-399).

A `.deb` or `.rpm` installs `codexsync.desktop` into `/usr/share/applications`;
pip, pipx, the tar.gz and the AppImage install none, so the window was in no
applications menu ("not found in the applications"). `install_shortcut` writes
the same entry for this user, under the freedesktop data folder
(`$XDG_DATA_HOME`, else `~/.local/share`): the entry in `applications/` and
the icon in `icons/hicolor/256x256/apps/`. `Exec` is the absolute path of
*this* window program -- the AppImage file itself when running from one -- so
the entry starts the copy that made it, and `TryExec` lets a menu hide the
entry once that program is gone (`pip uninstall` removes no files of ours).

Three rules keep it from touching what is not its own:

- The entry it writes carries `X-CodexSync-Shortcut=true` and the icon's
  SHA-256. An entry of that name without the mark is refused, never replaced
  or removed; the icon is removed only when its bytes are the ones recorded.
- When a package already installed the entry system-wide, nothing is written:
  a second entry of the same name would hide the package's one and outlive it.
- Nothing else is written, and no other program is run: menus notice a new
  file in `applications/` on their own.

The window keeps naming itself `codexsync` (`setDesktopFileName`), which is
how a Wayland desktop matches its window to this entry and its icon.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
from typing import Mapping

from .exceptions import ConfigError
from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)

ENTRY_NAME = "codexsync.desktop"
ICON_NAME = "codexsync.png"
WINDOW_PROGRAM = "codexsync-gui"
OWNER_KEY = "X-CodexSync-Shortcut"
ICON_HASH_KEY = "X-CodexSync-Icon-SHA256"
ICON_SUBDIR = Path("icons") / "hicolor" / "256x256" / "apps"
#: The packaged entry and icon -- the same files a `.deb`/`.rpm` installs.
#: Beside this module, not through `importlib.resources`: from a checkout the
#: `codexsync` package is the root shim, which has no `desktop/` folder.
PACKAGED = Path(__file__).resolve().parent / "desktop"
#: Where a package puts its entry when `XDG_DATA_DIRS` is not set (the spec's default).
DEFAULT_DATA_DIRS = "/usr/local/share:/usr/share"


@dataclass(frozen=True, slots=True)
class ShortcutStatus:
    """Where this user's menu entry is, and whether it is ours and works."""

    #: False away from Linux: there is nothing to install, and every write refuses.
    supported: bool
    entry: Path
    icon: Path
    #: An entry of that name exists here.
    present: bool
    #: That entry carries codexSync's mark, so `remove_shortcut` may remove it.
    ours: bool
    #: The program our entry starts, as written in it.
    program: Path | None
    #: That program still exists (a pip uninstall leaves the entry behind).
    program_exists: bool
    #: The program an install would write into the entry now, or None if none is found.
    would_start: Path | None
    #: An entry a package installed system-wide; an install writes nothing then.
    system_entry: Path | None

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "entry": str(self.entry),
            "icon": str(self.icon),
            "present": self.present,
            "ours": self.ours,
            "program": None if self.program is None else str(self.program),
            "program_exists": self.program_exists,
            "would_start": None if self.would_start is None else str(self.would_start),
            "system_entry": None if self.system_entry is None else str(self.system_entry),
        }


def data_home(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_DATA_HOME` when it is an absolute path, else `~/.local/share` (the spec's rule)."""
    env = os.environ if env is None else env
    configured = env.get("XDG_DATA_HOME", "")
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".local" / "share"


def window_program(
    *, executable: str | None = None, frozen: bool | None = None, env: Mapping[str, str] | None = None,
) -> Path | None:
    """The window program a menu entry should start: the one this process belongs to.

    From an AppImage, the AppImage file (`$APPIMAGE`): the folder it runs from
    is a mount that is gone after it exits. From a frozen build, the program
    beside this one. From pip or pipx, the script in the same `bin` folder as
    this interpreter -- not resolved, since a venv's `python` is a link out of
    that folder -- and only then `codexsync-gui` on `PATH`.
    """
    env = os.environ if env is None else env
    executable = sys.executable if executable is None else executable
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    appimage = env.get("APPIMAGE", "")
    if appimage and Path(appimage).is_absolute() and Path(appimage).is_file():
        return Path(appimage)
    if executable:
        beside = Path(executable).parent / WINDOW_PROGRAM
        if beside.is_file():
            return beside
    if frozen:
        return None
    found = shutil.which(WINDOW_PROGRAM, path=env.get("PATH"))
    return Path(found) if found else None


def shortcut_status(
    *, env: Mapping[str, str] | None = None, platform: str | None = None,
    executable: str | None = None, frozen: bool | None = None,
) -> ShortcutStatus:
    """Read where the entry is and what it starts. Writes nothing."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = data_home(env)
    entry = home / "applications" / ENTRY_NAME
    icon = home / ICON_SUBDIR / ICON_NAME
    fields = _read_entry(entry)
    present = fields is not None
    ours = present and fields.get(OWNER_KEY) == "true"
    program = _program_of(fields.get("TryExec", "")) if ours else None
    return ShortcutStatus(
        supported=platform.startswith("linux"),
        entry=entry,
        icon=icon,
        present=present,
        ours=ours,
        program=program,
        program_exists=program is not None and program.is_file(),
        would_start=window_program(executable=executable, frozen=frozen, env=env),
        system_entry=_system_entry(env, home),
    )


def install_shortcut(
    *, env: Mapping[str, str] | None = None, platform: str | None = None,
    executable: str | None = None, frozen: bool | None = None,
) -> ShortcutStatus:
    """Write this user's menu entry and icon, or bring ours up to date. Returns the new status."""
    status = shortcut_status(env=env, platform=platform, executable=executable, frozen=frozen)
    if not status.supported:
        raise ConfigError("A menu entry is made on Linux only", code="SHORTCUT_LINUX_ONLY")
    if status.system_entry is not None:
        raise ConfigError(
            f"The package that installed codexSync already put its menu entry at {status.system_entry}; "
            "nothing was written",
            code="SHORTCUT_FROM_PACKAGE", params={"path": str(status.system_entry)},
        )
    if status.present and not status.ours:
        raise ConfigError(
            f"{status.entry} exists and was not written by codexSync; nothing was written",
            code="SHORTCUT_NOT_OURS", params={"path": str(status.entry)},
        )
    program = status.would_start
    if program is None:
        raise ConfigError(
            f"No {WINDOW_PROGRAM} program was found to start; install codexsync[gui] first",
            code="SHORTCUT_NO_PROGRAM",
        )
    icon_bytes = _packaged("codexsync.png")
    _write(status.icon, icon_bytes)
    LOG.warning("menu entry: icon written to %s", status.icon)
    _write(status.entry, render_entry(program, hashlib.sha256(icon_bytes).hexdigest()).encode("utf-8"))
    LOG.warning("menu entry: %s written, it starts %s", status.entry, program)
    return shortcut_status(env=env, platform=platform, executable=executable, frozen=frozen)


def remove_shortcut(
    *, env: Mapping[str, str] | None = None, platform: str | None = None,
) -> ShortcutStatus:
    """Remove the entry and icon this module wrote; anything else is left alone."""
    status = shortcut_status(env=env, platform=platform)
    if not status.supported:
        raise ConfigError("A menu entry is made on Linux only", code="SHORTCUT_LINUX_ONLY")
    if not status.present:
        return status
    if not status.ours:
        raise ConfigError(
            f"{status.entry} was not written by codexSync; it was left alone",
            code="SHORTCUT_NOT_OURS", params={"path": str(status.entry)},
        )
    fields = _read_entry(status.entry) or {}
    recorded = fields.get(ICON_HASH_KEY, "")
    if recorded and _sha256_of(status.icon) == recorded:
        status.icon.unlink()
        LOG.warning("menu entry: icon %s removed", status.icon)
    status.entry.unlink()
    LOG.warning("menu entry: %s removed", status.entry)
    return shortcut_status(env=env, platform=platform)


def render_entry(program: Path, icon_sha256: str) -> str:
    """The packaged entry with this program in `Exec`/`TryExec` and codexSync's mark."""
    lines = []
    for line in _packaged("codexsync.desktop").decode("utf-8").splitlines():
        if line.startswith("Exec="):
            line = f"Exec={exec_value(program)}"
            lines.append(line)
            lines.append(f"TryExec={_string_value(program.as_posix())}")
            continue
        if line.startswith("TryExec="):
            continue
        lines.append(line)
    lines.append(f"{OWNER_KEY}=true")
    lines.append(f"{ICON_HASH_KEY}={icon_sha256}")
    return "\n".join(lines) + "\n"


_PLAIN_ARGUMENT = re.compile(r"^[A-Za-z0-9/._+,:@=-]+$")


def exec_value(program: Path) -> str:
    """`program` as the `Exec` key's value, quoted the way the Desktop Entry spec asks.

    A path with a space or a reserved character is put in double quotes, with
    `"`, `` ` ``, `$` and `\\` escaped by a backslash; `%` is doubled, since a
    single one starts a field code. The result is then a string value, whose
    own escape doubles every backslash.
    """
    text = program.as_posix()
    if _PLAIN_ARGUMENT.match(text):
        quoted = text
    else:
        quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", text) + '"'
    return _string_value(quoted.replace("%", "%%"))


def _string_value(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def _unescape_string(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            out.append({"\\": "\\", "n": "\n", "t": "\t", "r": "\r", "s": " "}.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _program_of(try_exec: str) -> Path | None:
    value = _unescape_string(try_exec)
    return Path(value) if value and Path(value).is_absolute() else None


def _read_entry(path: Path) -> dict[str, str] | None:
    """The `[Desktop Entry]` keys of ``path``, or None when there is no such file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    fields: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Desktop Entry" and "=" in line:
            key, value = line.split("=", 1)
            fields.setdefault(key.strip(), value.strip())
    return fields


def _system_entry(env: Mapping[str, str], home: Path) -> Path | None:
    dirs = env.get("XDG_DATA_DIRS", "") or DEFAULT_DATA_DIRS.replace(":", os.pathsep)
    # The spec's separator is ':', which is `os.pathsep` on Linux; on another
    # system this only ever runs in tests, where ':' is part of a drive name.
    for folder in dirs.split(os.pathsep):
        if not folder or not Path(folder).is_absolute() or Path(folder) == home:
            continue
        candidate = Path(folder) / "applications" / ENTRY_NAME
        if candidate.is_file():
            return candidate
    return None


def _packaged(name: str) -> bytes:
    return (PACKAGED / name).read_bytes()


def _sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.codexsync.tmp")
    try:
        with staged.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(staged, path)
    finally:
        staged.unlink(missing_ok=True)
