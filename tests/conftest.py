"""Shared test-suite policy for the on-disk sandbox.

Why tests build their sandboxes under ``<repo>/test-sandbox`` instead of
pytest's ``tmp_path``: codexSync commits every mutation with ``os.replace``
from a staging directory, which is only atomic when staging and target sit on
the same volume.  ``tmp_path`` may resolve to a different volume (a separate
``TEMP`` drive on Windows, ``/private/var`` on macOS), which would silently
exercise a copy-then-delete path that production never takes.  Keeping the
whole sandbox under the repository guarantees one volume for local state,
cloud root, backups and temp.

The cost of that choice is that leaked directories accumulate in the working
tree and mask real leaks — orphan temp files are exactly what ``doctor``
reports on.  So the sandbox is cleared before the session and checked after it.

Removal retries before it complains.  A checkout can live inside a
cloud-synced or indexed folder (OneDrive, Yandex.Disk, an antivirus scanner),
and those hold brief handles on files a test just wrote; failing the run on
someone else's transient handle would be a guard that cries wolf.  A handle
codexSync itself failed to close outlives the retries and is still reported.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import time

import pytest


SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "test-sandbox"
_REMOVE_ATTEMPTS = 6
_REMOVE_BACKOFF_SECONDS = 0.25


def remove_stubbornly(path: Path) -> bool:
    """Remove a tree, retrying while some other process still holds it."""
    for attempt in range(_REMOVE_ATTEMPTS):
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(_REMOVE_BACKOFF_SECONDS * (attempt + 1))
    return not path.exists()


@pytest.fixture(scope="session", autouse=True)
def sandbox_root() -> None:
    remove_stubbornly(SANDBOX_ROOT)
    yield
    if not SANDBOX_ROOT.is_dir():
        return
    leftovers = sorted(p.name for p in SANDBOX_ROOT.iterdir())
    if not leftovers:
        remove_stubbornly(SANDBOX_ROOT)
        return
    if remove_stubbornly(SANDBOX_ROOT):
        # Removable after a retry: someone else's transient handle, not a leak.
        return
    raise AssertionError(
        "test-sandbox could not be emptied; a test leaked a directory or an open handle: "
        + ", ".join(leftovers)
    )
