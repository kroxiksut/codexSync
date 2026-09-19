"""An optional second shell over the same core, shipped as ``codexsync[gui]``.

The GUI is not a second implementation. It does not build a plan, does not read
the Codex state directory, does not write anything itself, and does not know
that ``SyncEngine``, ``BackupManager``, ``OperationLock`` or ``safety_gate``
exist. Everything it can do is call a function in ``app.py`` and show what came
back, which is why nothing outside this package may import it: the boundary
points one way, and a test enforces that.

The reason for that strictness is the whole of P0/P1. There is exactly one
authority on whether state may be mutated and exactly one envelope around a
mutation — lock, journal, verified backup, a final process check, atomic
replace, ``COMMITTED``. A second shell that stepped around any of them to keep
a window responsive would leave the user with two different safety stories for
the same operation, and only one of them written down.

Importing this package never imports Qt. The dependency is looked up once, at
launch, so a missing PySide6 is a sentence rather than a traceback.
"""
from __future__ import annotations

#: What the user is told when the extra was never installed. Kept here so the
#: entry point and the import guard say exactly the same thing.
MISSING_QT_MESSAGE = (
    "The codexSync GUI needs PySide6, which is an optional extra.\n"
    "Install it with:  pip install codexsync[gui]\n"
    "The command line needs nothing installed and keeps working without it."
)

#: The name the window shows. The package, the command and the CLI's own
#: messages stay ``codexsync``/``codexSync``; this is the brand as drawn in the
#: lockup, and it is not translated.
BRAND_NAME = "CodexSync"

__all__ = ["BRAND_NAME", "MISSING_QT_MESSAGE"]
