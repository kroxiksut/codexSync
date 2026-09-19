"""Launcher for the optional GUI: ``python -m codexsync.gui`` / ``codexsync-gui``.

Qt is imported here and nowhere earlier. Importing ``codexsync.gui`` or its
controller must stay possible on a machine that never installed the extra, so
that the boundary tests -- and any tooling that walks the package -- do not
need PySide6 to look at it.

The argument surface deliberately stops at ``-c``. Every other choice belongs
to a screen, where it can be shown next to what it affects; a GUI that also
took the CLI's flags would be a second place to spell the same options wrong.

Without ``-c`` the file is chosen by `locations.choose_config_path`: the path
last opened, then the working directory, then the exe's folder, then the
per-user location. Naming a missing path with ``-c`` still wins -- that is a
request to create the config there.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys

from ..exit_codes import ExitCode
from . import MISSING_QT_MESSAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexsync-gui",
        description="Optional graphical shell over the codexSync core.",
    )
    parser.add_argument(
        "-c", "--config", default=None,
        help="Path to the same config.toml the command line uses",
    )
    return parser


def _qt_is_available() -> bool:
    """Whether PySide6 can be found at all.

    ``find_spec`` does not only return ``None`` for a missing module: it raises
    ``ImportError`` when the lookup itself fails, which is a broken install
    rather than an absent one. Both mean the same thing here -- the window
    cannot be shown -- so both take the same path instead of one of them
    escaping as a traceback.
    """
    try:
        return importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not _qt_is_available():
        # The extra was never installed. That is a configuration answer, not a
        # crash, so it leaves by the same door as any other bad input.
        #
        # Asked as a question about PySide6 rather than by catching ImportError
        # around the window: a typo inside the window module raises ImportError
        # too, and reporting that as "install PySide6" would send the user to
        # fix an installation that is already fine.
        print(MISSING_QT_MESSAGE, file=sys.stderr)
        return int(ExitCode.BAD_INPUT)

    from .window import launch

    # Which config to open is decided inside `launch`: the remembered path
    # lives in QSettings, and this module may not name the toolkit -- the
    # boundary test reads its imports and a launcher that cannot be read
    # without PySide6 is the thing that test exists to prevent.
    return launch(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
