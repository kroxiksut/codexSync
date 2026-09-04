"""Single source of the codexSync version.

The version is read from installed package metadata so that it can never drift
from ``pyproject.toml``.  It lives in its own module because the repo-root
``codexsync`` shim shadows ``src/codexsync/__init__.py`` when the package is
used from a source checkout.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _package_version

try:
    __version__ = _package_version("codexsync")
except PackageNotFoundError:  # source checkout without an install
    __version__ = "0.0.0+unknown"

#: Producing build recorded in Guardian snapshot manifests.
PRODUCER_VERSION = f"codexsync-{__version__}"

__all__ = ["PRODUCER_VERSION", "__version__"]
