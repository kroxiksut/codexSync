"""What the window knows about the jobs its pages start. Qt-free.

A job is one controller call run off the UI thread (`MainWindow.run`). This
module holds the bookkeeping around it: which kind of work it is, which page
asked, when it started, and -- once it is over -- how it ended. Two things
depend on that bookkeeping.

The same work offered on two pages is one job (CS-336). *Dry run* is on
Overview and on Synchronisation, *Synchronise now* on Synchronisation and on
Automation, the chat list on Projects and on Sessions. Before, each page knew
only what it had started itself, so a dry run begun on one page left the
other's button live and its line silent, and a second click ran the same
work twice. A shared kind (`SHARED_KINDS`) is busy for every page that offers
it, and its outcome is delivered to each of their models.

The task list (CS-337) shows what runs, from which page, since when, how far it
has got, and the last few that ended. Nothing here decides anything: a job's
own outcome is still what its page draws, and core still refuses what it
refuses at the moment of the write.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable

from .controller import Failure, Outcome

#: Every kind a page may name. Each has a ``job.<kind>`` sentence in every
#: language file; a job started without a kind is described by its page.
JOB_KINDS = (
    "environment",
    "dry_run",
    "full_sync",
    "sync_preview",
    "sync_history",
    "chat_list",
    "chats_scan",
    "chat_move_preview",
    "chat_move",
    "sessions_scan",
    "sessions_apply",
    "sessions_resolve",
    "working_set",
    "session_index",
    "repair_scan",
    "repair_apply",
    "project_move_scan",
    "project_move",
    "project_files",
    "project_places",
    "project_place",
    "guardian_inventory",
    "guardian_snapshot",
    "guardian_restore_preview",
    "guardian_restore",
    "guardian_accept_preview",
    "guardian_accept",
    "backups_list",
    "restore",
    "state_backup",
    "state_backups_list",
    "journals",
    "recovery_action",
    "codex_check",
    "codex_repair",
    "catalogue_preview",
    "catalogue_rebuild",
    "automation_status",
    "automation_apply",
    "handoff_board",
    "home",
    "recount",
    "config_check",
    "config_upgrade",
    "config_save",
    "config_create",
    "mapping_hints",
    "workspace_search",
    "menu_entry",
)

#: Kinds offered on more than one page, and the pages that offer them. Busy on
#: one is busy on all; an outcome lands in every one of their models.
SHARED_KINDS: dict[str, tuple[str, ...]] = {
    "dry_run": ("overview", "sync"),
    "full_sync": ("sync", "automation"),
    "chat_list": ("projects", "sessions"),
}

#: How many ended jobs the task list keeps.
RECENT_LIMIT = 12


@dataclass
class RunningJob:
    """A job started and not yet delivered."""

    page: str
    kind: str | None
    apply: Callable[[Any, Outcome], None]
    model: Any
    cancellable: bool
    dry_run: bool
    #: ``time.monotonic()`` at the start: what "running for 12 s" is measured from.
    started: float
    #: ``time.time()`` at the start: what "started at 14:02" shows.
    started_at: float
    #: The last ``(phase, done, total)`` the job reported, if it reports.
    progress: tuple[str, int, int] | None = None

    @property
    def shared(self) -> bool:
        return self.kind in SHARED_KINDS


@dataclass(frozen=True)
class FinishedJob:
    """How a job ended, for the task list. Holds no result, only its shape."""

    page: str
    kind: str | None
    dry_run: bool
    cancellable: bool
    started_at: float
    seconds: float
    #: ``None`` for a job that finished well.
    failure: Failure | None
    #: The person stopped waiting; the work itself may have run on.
    abandoned: bool = False


class JobBook:
    """The running jobs by token, and the ones that ended most recently."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._wall = wall
        self.running: dict[Any, RunningJob] = {}
        self.recent: deque[FinishedJob] = deque(maxlen=RECENT_LIMIT)

    def start(
        self,
        token: Any,
        *,
        page: str,
        kind: str | None,
        apply: Callable[[Any, Outcome], None],
        model: Any,
        cancellable: bool,
        dry_run: bool = False,
    ) -> RunningJob:
        if kind is not None and kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind: {kind!r}")
        job = RunningJob(
            page=page, kind=kind, apply=apply, model=model, cancellable=cancellable,
            dry_run=dry_run, started=self._clock(), started_at=self._wall(),
        )
        self.running[token] = job
        return job

    def retoken(self, old: Any, new: Any) -> None:
        """The runner named the job only after it was registered."""
        job = self.running.pop(old, None)
        if job is not None:
            self.running[new] = job

    def finish(self, token: Any, outcome: Outcome | None, *, abandoned: bool = False) -> RunningJob | None:
        """Forget a running job and remember how it ended."""
        job = self.running.pop(token, None)
        if job is None:
            return None
        self.recent.appendleft(FinishedJob(
            page=job.page, kind=job.kind, dry_run=job.dry_run, cancellable=job.cancellable,
            started_at=job.started_at,
            seconds=max(0.0, self._clock() - job.started),
            failure=None if outcome is None else outcome.failure,
            abandoned=abandoned,
        ))
        return job

    def seconds(self, job: RunningJob) -> int:
        return int(max(0.0, self._clock() - job.started))

    def busy(self, kind: str, models: Iterable[Any] | None = None) -> bool:
        """Is a job of ``kind`` running -- for the pages as they are now?

        ``models`` are the pages' current models. A job started against a
        config that has since been swapped for another file belongs to models
        no page holds any more, and keeps no page of the new file busy.
        """
        current = None if models is None else {id(model) for model in models}
        return any(
            job.kind == kind and (current is None or id(job.model) in current)
            for job in self.running.values()
        )

    def report(self, token: Any, phase: str, done: int, total: int) -> bool:
        job = self.running.get(token)
        if job is None:
            return False
        job.progress = (phase, done, total)
        return True


def sharing_pages(kind: str | None, page: str) -> tuple[str, ...]:
    """The other pages that show a job of ``kind`` started on ``page``."""
    if kind is None:
        return ()
    return tuple(other for other in SHARED_KINDS.get(kind, ()) if other != page)
