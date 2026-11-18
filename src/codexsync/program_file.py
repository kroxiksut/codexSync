"""Which file starts this build of codexSync again (CS-420).

A scheduled task, the "config beside the program" search and the About page
all need the program's own file, and `sys.executable` is not always it. Inside
an AppImage it lies under `/tmp/.mount_*`, a folder that is gone once the
program exits -- a task pointing there fails at its first run, and a config
"beside the program" would be looked for in a folder nobody can keep a file
in. The AppImage runtime names the real file in `$APPIMAGE`, and the folder
it mounted in `$APPDIR`. Both variables are inherited by every program an
AppImage starts, so a `codexsync` run from the terminal of an editor that is
itself an AppImage sees the editor's (CS-505); `$APPIMAGE` is believed only
for a frozen build whose own executable lies inside `$APPDIR`. This module is
the one place that knows that; it imports nothing of codexSync, so the
scheduler can use it without pulling the config loader in.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

from typing import Mapping

__all__ = ["appimage_file", "frozen_program", "is_frozen"]


def is_frozen() -> bool:
    """A PyInstaller build, which is its own command line."""
    return bool(getattr(sys, "frozen", False))


def appimage_file(
    *, env: Mapping[str, str] | None = None, executable: str | None = None, frozen: bool | None = None,
) -> Path | None:
    """The AppImage this very process runs from, or ``None``.

    `$APPIMAGE` must name an existing file, and this must be a frozen build
    whose executable lies inside `$APPDIR` -- the mount the AppImage runtime
    made for this program. A variable inherited from another AppImage (an
    editor's terminal) fails the second test: a pip install is not frozen, and
    a tar.gz build lies outside that editor's mount.
    """
    env = os.environ if env is None else env
    executable = sys.executable if executable is None else executable
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return None
    appimage, appdir = env.get("APPIMAGE", ""), env.get("APPDIR", "")
    if not appimage or not appdir or not executable:
        return None
    image, mount = Path(appimage), Path(appdir)
    if not (image.is_absolute() and mount.is_absolute() and image.is_file()):
        return None
    try:
        inside = Path(executable).resolve().is_relative_to(mount.resolve())
    except OSError:
        return None
    return image if inside else None


def frozen_program() -> Path | None:
    """The file that starts this build again, or ``None`` when run from source.

    The AppImage file when this process runs from one (`appimage_file`), else
    the executable itself, resolved: a package's `/usr/bin/codexsync` link
    leads to the folder the package installs into, which keeps its name
    across updates (the packages are built so -- CS-421).
    """
    if not is_frozen():
        return None
    appimage = None if sys.platform.startswith("win") else appimage_file()
    return appimage or Path(sys.executable).resolve()
