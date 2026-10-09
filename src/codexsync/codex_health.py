"""Why Codex may not start or not show its work, and what can be put right (D-032).

`codex check` and the window's Codex section run these readings; each problem
is a `Finding` with a stable code, a severity and either a repair this module
plans (`fix`) or the codexsync command that deals with it (`command`). Nothing
here writes; the one repair it plans is applied by `app.repair_codex` inside
the same envelope as every other write into Codex's catalogue.

The first finding is the one that stopped Codex on 2026-10-09. Codex rebuilds
its chat list (``threads`` in ``state_5.sqlite``) from the chat files while
``backfill_state`` says anything but ``complete``, and does it while starting:
its ``app-server`` holds the rebuild and answers nobody until it is done. When
the window gives up and the process is ended part-way, the row stays
``running``, and every later start prints "state db backfill is running ...
waiting up to 30s" and exits with "timed out waiting for state db backfill" --
which the window shows as "could not load your organization's settings". A
``running`` row with no Codex process is therefore a rebuild nobody is doing,
and putting it back to ``complete`` is what lets Codex start. Codex then lists
the chats its catalogue already had; `sessions catalogue` asks for a rebuild
again, deliberately, when Codex can be left open until it finishes.

Whether codexSync caused a problem does not matter here: a repair is offered
for the state as found. Where codexSync's own catalogue write left a verified
copy of the row, that exact row is what the repair puts back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .safety_gate import ProcessState
from .sqlite_audit import BackfillReading, PlacementStatus
from .thread_catalogue import (
    BACKFILL_COMPLETE,
    CHAT_FOLDERS,
    BackfillRow,
    chat_file_id,
    database_fingerprint,
    is_link,
)

#: The status Codex writes while a process holds the rebuild.
BACKFILL_RUNNING = "running"


class Severity(str, Enum):
    #: Codex does not start, or work is out of its reach.
    BROKEN = "BROKEN"
    #: Codex starts, but something will go wrong or is not as it should be.
    WARNING = "WARNING"
    #: Worth knowing; nothing to repair.
    NOTE = "NOTE"


# Codex's chat list (its thread catalogue).
#: A rebuild is marked as running and no Codex process is doing it: Codex
#: will not start until the row says ``complete``.
CATALOGUE_REBUILD_STUCK = "CATALOGUE_REBUILD_STUCK"
#: Codex will rebuild its whole chat list on its next start. On a large set of
#: chats that start takes long, and ending it part-way leaves the rebuild stuck.
CATALOGUE_REBUILD_PENDING = "CATALOGUE_REBUILD_PENDING"
#: Codex is running and rebuilding: leave it open until the chats appear.
CATALOGUE_REBUILDING = "CATALOGUE_REBUILDING"
#: A rebuild is marked, and whether Codex is running could not be determined.
CATALOGUE_REBUILD_UNDETERMINED = "CATALOGUE_REBUILD_UNDETERMINED"
#: The catalogue exists and could not be read.
CATALOGUE_UNREADABLE = "CATALOGUE_UNREADABLE"
#: Chat files Codex's list does not lead to; `sessions catalogue` asks for a rebuild.
CATALOGUE_MISSES_CHATS = "CATALOGUE_MISSES_CHATS"
# The global state (projects, pins, which chat is in which project).
#: ``.codex-global-state.json`` is missing: the app's projects are gone unless
#: Guardian holds a snapshot (the console Codex never writes this file).
GLOBAL_STATE_MISSING = "GLOBAL_STATE_MISSING"
#: It is there and fails validation: broken JSON, an unknown shape, or a
#: project list whose references do not hold (chats torn from projects).
GLOBAL_STATE_INVALID = "GLOBAL_STATE_INVALID"
# codexSync's own operations.
#: An operation stopped part-way and blocks every later write until resolved.
OPEN_JOURNAL = "OPEN_JOURNAL"

#: Every code, for the window's labels and the language-file test.
FINDING_CODES = (
    CATALOGUE_REBUILD_STUCK,
    CATALOGUE_REBUILD_PENDING,
    CATALOGUE_REBUILDING,
    CATALOGUE_REBUILD_UNDETERMINED,
    CATALOGUE_UNREADABLE,
    CATALOGUE_MISSES_CHATS,
    GLOBAL_STATE_MISSING,
    GLOBAL_STATE_INVALID,
    OPEN_JOURNAL,
)

#: The repair `codex repair` applies: the chat-list rebuild back to ``complete``.
FIX_CATALOGUE_REBUILD = "catalogue-rebuild"

#: Where a settled row comes from, recorded in the plan.
SOURCE_OWN_BACKUP = "own-backup"
SOURCE_AS_FOUND = "as-found"

_PLAN_VERSION = 1


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    #: Numbers only -- never a name, a path or a chat title.
    counts: Mapping[str, int] = field(default_factory=dict)
    #: The repair `codex repair` plans for it, if any.
    fix: str | None = None
    #: The codexsync command that deals with it otherwise.
    command: str | None = None


@dataclass(frozen=True, slots=True)
class ChatFiles:
    count: int = 0
    size: int = 0


def measure_chat_files(state_root: Path) -> ChatFiles:
    """How many chat files Codex would walk in a rebuild, and their size."""
    count = size = 0
    root = state_root.resolve()
    for folder in CHAT_FOLDERS:
        base = root / folder
        if not base.is_dir() or is_link(base):
            continue
        for directory, subdirs, files in os.walk(base):
            current = Path(directory)
            subdirs[:] = [name for name in subdirs if not is_link(current / name)]
            for name in files:
                if chat_file_id(name) is None:
                    continue
                try:
                    size += (current / name).stat().st_size
                except OSError:
                    continue
                count += 1
    return ChatFiles(count, size)


def diagnose_catalogue(
    reading: BackfillReading, process: ProcessState, files: ChatFiles, *, unnamed: int = 0,
) -> list[Finding]:
    """What Codex's chat list says about whether Codex can start."""
    if reading.status is PlacementStatus.ABSENT:
        # No catalogue yet: Codex builds one on its first start.
        return []
    if reading.status is not PlacementStatus.AVAILABLE:
        return [Finding(CATALOGUE_UNREADABLE, Severity.WARNING)]
    sizes = {"chats": files.count, "megabytes": files.size // (1024 * 1024)}
    status = reading.backfill
    if status == BACKFILL_COMPLETE:
        if unnamed:
            return [Finding(CATALOGUE_MISSES_CHATS, Severity.NOTE, {"chats": unnamed}, command="sessions catalogue")]
        return []
    if process is ProcessState.RUNNING:
        return [Finding(CATALOGUE_REBUILDING, Severity.NOTE, sizes)]
    if process is not ProcessState.STOPPED:
        return [Finding(CATALOGUE_REBUILD_UNDETERMINED, Severity.WARNING, sizes)]
    if status == BACKFILL_RUNNING:
        return [Finding(CATALOGUE_REBUILD_STUCK, Severity.BROKEN, sizes, fix=FIX_CATALOGUE_REBUILD)]
    # `pending`, or a status this version does not know: Codex rebuilds on start.
    return [Finding(CATALOGUE_REBUILD_PENDING, Severity.WARNING, sizes, fix=FIX_CATALOGUE_REBUILD)]


def diagnose_global_state(*, present: bool, valid: bool | None) -> list[Finding]:
    """``valid`` is ``None`` when the file could not be judged at all."""
    if not present:
        # A warning, not BROKEN: the console Codex never writes this file, so
        # a machine that only uses it has none, and the app recreates it.
        return [Finding(GLOBAL_STATE_MISSING, Severity.WARNING, command="guardian restore")]
    if valid is False:
        return [Finding(GLOBAL_STATE_INVALID, Severity.BROKEN, command="guardian restore")]
    return []


def diagnose_journals(open_journals: int) -> list[Finding]:
    if not open_journals:
        return []
    return [Finding(OPEN_JOURNAL, Severity.WARNING, {"journals": open_journals}, command="recover list")]


@dataclass(frozen=True, slots=True)
class SettlePlan:
    """Putting a chat-list rebuild back to ``complete``; frozen and hashed."""

    plan_id: str
    #: The catalogue, relative to the state root; ``None`` when there is none.
    database: str | None
    current: BackfillRow | None
    target: BackfillRow | None
    #: `SOURCE_OWN_BACKUP` or `SOURCE_AS_FOUND`.
    source: str | None
    #: The backup snapshot the target row was read from.
    snapshot: str | None = None

    @property
    def writes(self) -> bool:
        return self.target is not None and self.current != self.target


def build_settle_plan(
    state_root: Path, reading: BackfillReading, *, own_backup: tuple[str, BackfillRow] | None = None,
) -> SettlePlan:
    """What `codex repair` would write. Reads only.

    ``own_backup`` is (snapshot name, row) from the copy codexSync took before
    its own last write into this catalogue; it is used only when that row says
    ``complete``. Otherwise the row as found is kept, with the status set to
    ``complete`` and the watermark cleared -- every value deterministic, so a
    preview and the apply that follows it hash the same.
    """
    current: BackfillRow | None = reading.row if reading.status is PlacementStatus.AVAILABLE else None
    target: BackfillRow | None = None
    source = snapshot = None
    if current is not None and current[0] is not None and current[0] != BACKFILL_COMPLETE:
        if own_backup is not None and own_backup[1][0] == BACKFILL_COMPLETE:
            snapshot, target, source = own_backup[0], tuple(own_backup[1]), SOURCE_OWN_BACKUP  # type: ignore[assignment]
        else:
            updated = current[3]
            target = (BACKFILL_COMPLETE, None, current[2] if current[2] is not None else updated, updated)
            source = SOURCE_AS_FOUND
    fingerprint = None
    if target is not None and reading.database is not None:
        fingerprint = database_fingerprint(state_root.resolve() / Path(reading.database))
    material = {
        "version": _PLAN_VERSION,
        "database": reading.database,
        "current": list(current) if current is not None else None,
        "target": list(target) if target is not None else None,
        "source": source,
        "snapshot": snapshot,
        "fingerprint": fingerprint,
    }
    plan_id = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return SettlePlan(plan_id, reading.database, current, target, source, snapshot)
