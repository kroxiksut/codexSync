"""Everything the GUI is allowed to do, with no Qt anywhere in it.

This layer exists so the window has nothing to decide. It calls the same public
functions in ``app.py`` that ``cli.py`` calls, in the same order, and turns an
exception into the same four meanings the CLI's exit codes carry. It never
opens the Codex state directory, never builds a plan, and never reaches for
``safety_gate``, ``sync_engine``, ``backup`` or the journal -- a test asserts
that by reading this package's imports.

Two consequences are worth stating plainly, because they are what make the GUI
safe rather than merely careful.

**The indicator is advisory; the refusal is real.** ``state()`` reports whether
Codex looks closed so the window can grey a button out, and that reading is
already stale when it is drawn. Nothing rests on it. The actual decision is
taken inside ``app.py`` at the moment of the mutation, against a continuously
stopped window and a final check immediately before the commit, and it refuses
regardless of what any button looked like.

**A mutation is always two steps.** First a plan, which is read-only and
carries an id computed over every decision and the state it was computed from;
then an apply that quotes that id. The window's confirm button carries the id,
not a yes -- so anything that changed in between makes the apply refuse instead
of acting on a picture the user was shown a minute ago.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..app import (
    apply_repair_projects,
    apply_session_transfer,
    audit_session_index,
    build_context,
    move_chats,
    run_preflight,
    run_sync,
    save_transfer_plan,
    scan_chats,
    scan_repair_projects,
    scan_session_transfer,
)
from ..exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from ..restore import restore_from_backup

T = TypeVar("T")


class Failure(str, Enum):
    """Why something did not happen, in the four meanings the CLI already has.

    These mirror ``cli.py``'s exception-to-exit-code chain one for one, so the
    window can never invent a fifth kind of "no" that the command line does not
    have.
    """

    #: ConfigError -> exit 4. The configuration or the request is unusable.
    CONFIGURATION = "configuration"
    #: SafetyPreconditionError -> exit 3. Codex is open, or its state is unknown.
    CODEX_NOT_STOPPED = "codex-not-stopped"
    #: ConflictError -> exit 2. Something needs a decision only the user can make.
    NEEDS_A_DECISION = "needs-a-decision"
    #: FailSafeError -> exit 5. The operation stopped; evidence may need recovery.
    STOPPED_SAFELY = "stopped-safely"
    #: Anything else. Shown as a bug rather than as a normal outcome.
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a call produced, or why it produced nothing.

    Never both: ``value`` is set exactly when ``failure`` is not.
    """

    value: Any = None
    failure: Failure | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class CheckRow:
    name: str
    status: str
    details: str


@dataclass(frozen=True, slots=True)
class StateView:
    """What the state screen shows, and nothing a decision rests on."""

    checks: tuple[CheckRow, ...]
    #: How the environment diagnostic reads the Codex process right now. False
    #: whenever it is running *or* undetermined, because both forbid a mutation.
    codex_looks_stopped: bool
    failures: int
    warnings: int


def run(call: Callable[[], T]) -> Outcome:
    """Call into the core and translate the refusal, if there is one.

    The order matters: ``SafetyPreconditionError`` and the ``FailSafeError``
    family both descend from the base error hierarchy, and matching them in the
    same order ``cli.py`` does is what keeps the two shells telling the user the
    same thing about the same event.
    """
    try:
        return Outcome(value=call())
    except ConfigError as exc:
        return Outcome(failure=Failure.CONFIGURATION, message=str(exc))
    except SafetyPreconditionError as exc:
        return Outcome(failure=Failure.CODEX_NOT_STOPPED, message=str(exc))
    except ConflictError as exc:
        return Outcome(failure=Failure.NEEDS_A_DECISION, message=str(exc))
    except FailSafeError as exc:
        return Outcome(failure=Failure.STOPPED_SAFELY, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported as a bug, never swallowed
        return Outcome(failure=Failure.UNEXPECTED, message=f"{type(exc).__name__}: {exc}")


class Controller:
    """One config file's worth of the core, exposed to a window.

    Holds the path and nothing else. Configuration is re-read on every call, so
    a file edited while the window is open takes effect on the next action
    rather than at the next restart.
    """

    def __init__(self, config_path: Path) -> None:
        self._config_path = Path(config_path)

    @property
    def config_path(self) -> Path:
        return self._config_path

    # --- read-only -------------------------------------------------------

    def state(self) -> Outcome:
        """The environment diagnostic, as the state screen shows it."""

        def build() -> StateView:
            report = run_preflight(self._config_path)
            checks = tuple(
                CheckRow(item.name, item.status, item.details) for item in report.checks
            )
            process = next((item for item in checks if item.name == "codex_process"), None)
            return StateView(
                checks=checks,
                # Only an explicit PASS means stopped. A WARN covers both "it is
                # running" and "it could not be determined", and an undetermined
                # process is never optimistically read as a stopped one.
                codex_looks_stopped=process is not None and process.status == "PASS",
                failures=len(report.failures),
                warnings=len(report.warnings),
            )

        return run(build)

    def chats(self, *, source_machine: str | None = None, target_machine: str | None = None) -> Outcome:
        return run(lambda: scan_chats(
            self._config_path, source_machine=source_machine, target_machine=target_machine
        ))

    def session_index(self) -> Outcome:
        return run(lambda: audit_session_index(self._config_path))

    def scan_sessions(
        self,
        *,
        source_machine: str,
        target_machine: str,
        resolutions_path: Path | None = None,
    ) -> Outcome:
        return run(lambda: scan_session_transfer(
            self._config_path,
            source_machine=source_machine,
            target_machine=target_machine,
            resolutions_path=resolutions_path,
        ))

    def scan_repair(self, *, source_machine: str, target_machine: str) -> Outcome:
        return run(lambda: scan_repair_projects(
            self._config_path, source_machine=source_machine, target_machine=target_machine
        ))

    def preview_sync(self) -> Outcome:
        """Build a sync plan without enforcing the gate, exactly like `plan`.

        The result is a picture taken while Codex may be running, so it is not
        reusable by a mutation: ``sync`` builds its own context with the gate
        enforced. Nothing here shortens that.
        """
        return run(lambda: build_context(self._config_path, enforce_safety=False).plan)

    def save_session_plan(self, plan: Any, path: Path) -> Outcome:
        return run(lambda: save_transfer_plan(plan, Path(path)))

    # --- mutating: always a plan first, then its id ----------------------

    def sync(self, *, dry_run: bool) -> Outcome:
        """Run a sync through the same context the CLI builds.

        ``enforce_safety`` stays on. A GUI that turned it off to show a nicer
        error would be the one place in the project where a mutation is decided
        outside ``safety_gate``.
        """

        def go() -> bool:
            context = build_context(self._config_path, enforce_safety=True)
            run_sync(context, dry_run=dry_run)
            return not dry_run

        return run(go)

    def apply_sessions(
        self,
        *,
        plan_path: Path,
        confirm_plan: str,
        resolutions_path: Path | None = None,
        dry_run: bool = False,
    ) -> Outcome:
        return run(lambda: apply_session_transfer(
            self._config_path,
            plan_path=Path(plan_path),
            confirm_plan=confirm_plan,
            resolutions_path=resolutions_path,
            dry_run=dry_run,
        ))

    def apply_repair(self, *, plan_path: Path, confirm_plan: str, dry_run: bool = False) -> Outcome:
        return run(lambda: apply_repair_projects(
            self._config_path,
            plan_path=Path(plan_path),
            confirm_plan=confirm_plan,
            dry_run=dry_run,
        ))

    def move_chats(
        self,
        *,
        chat_refs: list[str],
        to_project: str,
        confirm_plan: str | None = None,
        dry_run: bool = False,
        include_sub_threads: bool = False,
    ) -> Outcome:
        """Preview a chat move, or perform the one a preview id names.

        ``confirm_plan=None`` is the preview and writes nothing; the id it
        returns covers the decisions *and* the exact state bytes, so it stops
        matching the moment anything changes.
        """
        return run(lambda: move_chats(
            self._config_path,
            chat_refs=chat_refs,
            to_project=to_project,
            confirm_plan=confirm_plan,
            dry_run=dry_run,
            include_sub_threads=include_sub_threads,
        ))

    def restore(self, *, snapshot_name: str | None, target: str, dry_run: bool) -> Outcome:
        return run(lambda: restore_from_backup(
            self._config_path, snapshot_name, target, dry_run
        ))
