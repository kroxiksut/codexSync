from __future__ import annotations

from pathlib import Path

# Allow running `python -m codexsync` from a source checkout without installing
# the package by extending package search path with src/codexsync.
_src_pkg = Path(__file__).resolve().parent.parent / "src" / "codexsync"
if _src_pkg.is_dir():
    __path__.append(str(_src_pkg))

# Mirror the real package surface so `from codexsync import ...` behaves the
# same in a source checkout as it does from an installed distribution.
from .exit_codes import ExitCode  # noqa: E402
from .version import PRODUCER_VERSION, __version__  # noqa: E402

__all__ = ["ExitCode", "PRODUCER_VERSION", "__version__"]
