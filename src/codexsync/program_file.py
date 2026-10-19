"""Which file starts this build of codexSync again (CS-420).

A scheduled task, the "config beside the program" search and the About page
all need the program's own file, and `sys.executable` is not always it. Inside
an AppImage it lies under `/tmp/.mount_*`, a folder that is gone once the
program exits -- a task pointing there fails at its first run, and a config
"beside the program" would be looked for in a folder nobody can keep a file
in. The AppImage runtime names the real file in `$APPIMAGE`. This module is
the one place that knows that; it imports nothing of codexSync, so the
scheduler can use it without pulling the config loader in.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

__all__ = ["frozen_program", "is_frozen"]


def is_frozen() -> bool:
    """A PyInstaller build, which is its own command line."""
    return bool(getattr(sys, "frozen", False))


def frozen_program() -> Path | None:
    """The file that starts this build again, or ``None`` when run from source.

    `$APPIMAGE` when the AppImage runtime set it to an existing file, else the
    executable itself, resolved: a package's `/usr/bin/codexsync` link leads to
    the folder the package installs into, which keeps its name across updates
    (the packages are built so -- CS-421).
    """
    if not is_frozen():
        return None
    appimage = os.environ.get("APPIMAGE")
    if appimage and not sys.platform.startswith("win"):
        path = Path(appimage)
        if path.is_absolute() and path.is_file():
            return path
    return Path(sys.executable).resolve()
