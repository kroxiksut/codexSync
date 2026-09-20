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

from dataclasses import dataclass, field
import hashlib
from enum import Enum
import os
from pathlib import Path
import platform
import re
import sys
import tomllib
from typing import Any, Callable, TypeVar

from ..app import (
    __version__,
    accept_guardian_baseline,
    apply_project_move_plan,
    restore_global_state,
    save_project_move_plan,
    scan_project_move,
    apply_automation,
    apply_repair_projects,
    automation_status,
    remove_automation,
    run_automation_job,
    config_diff,
    create_config,
    list_config_history,
    read_config_document,
    remove_key,
    replace_array_of_tables,
    save_config_text,
    set_value,
    validate_config_text,
    apply_session_transfer,
    audit_session_index,
    build_context,
    build_guardian_runner,
    inspect_recovery,
    list_backup_snapshots,
    list_journals,
    load_config,
    preview_path,
    PATH_SUBSTITUTIONS,
    read_guardian_inventory,
    move_chats,
    record_branch_resolution,
    record_format_migrations,
    resume_operation,
    rollback_operation,
    run_preflight,
    run_sync,
    save_repair_plan,
    save_transfer_plan,
    PHASES,
    ProgressCallback,
    SessionScope,
    build_working_set,
    list_sync_candidates,
    read_working_set,
    scan_chats,
    suggest_path_mappings,
    write_working_set,
    scan_repair_projects,
    scan_session_transfer,
    restore_from_backup,
)
from ..exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .locations import find_workspaces as find_workspace_candidates

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


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a sync did, counted from the plan the gated context built."""

    #: False for a dry run: the plan was built and checked, nothing was written.
    applied: bool
    actions: int
    conflicts: int


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


@dataclass(frozen=True, slots=True)
class ConfigInfo:
    """The parts of the loaded config that screens show or default from."""

    machine_id: str | None
    #: Every machine name the config mentions: this one, and both ends of
    #: every `[[path_mappings]]` rule. Offered as choices, never enforced.
    machines: tuple[str, ...]
    workspace_root_dir: Path | None
    local_state_dir: Path | None
    cloud_root_dir: Path
    backup_dir: Path
    temp_dir: Path
    plans_dir: Path


@dataclass(frozen=True, slots=True)
class SyncPreview:
    to_local: tuple[str, ...]
    to_cloud: tuple[str, ...]
    conflicts: tuple[str, ...]
    #: Built while Codex may be running; a sync rebuilds its own plan anyway.
    volatile: bool


@dataclass(frozen=True, slots=True)
class SessionScan:
    plan: Any
    plan_path: Path
    #: The decisions file this scan read, or where the next decision goes.
    resolutions_path: Path
    used_resolutions: bool
    #: session hash -> opening line of the chat, for the branches this machine
    #: holds. Local display only: the plan and every report stay id-free.
    titles: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConfigEdits:
    """What the settings screen wants changed, and nothing it merely displayed.

    ``values`` maps ``(section, key)`` to a new value, or to ``REMOVE``;
    ``mappings`` replaces every ``[[path_mappings]]`` block when not ``None``.
    """

    values: dict[tuple[str, str], Any]
    mappings: list[dict[str, Any]] | None = None


#: Marks a key the edit removes rather than sets.
REMOVE = object()


@dataclass(frozen=True, slots=True)
class OpenedConfig:
    document: Any
    #: The file parsed as TOML, before any path is resolved: what the user wrote.
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoveScan:
    plan: Any
    plan_path: Path


@dataclass(frozen=True, slots=True)
class RepairScan:
    plan: Any
    plan_path: Path


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """What this particular build is, for the about screen and a bug report.

    Facts about the program, never about the state: the version, how it was
    started and where its configuration is. Nothing here opens `.codex`, so the
    screen that shows it needs no job and cannot be stale in a way that matters.
    """

    version: str
    #: Started from a packaged executable rather than from a Python checkout.
    frozen: bool
    executable: str
    python: str
    system: str
    architecture: str
    config_path: str
    config_exists: bool


def _file_safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.") or "machine"


def suggested_machine_id() -> str:
    """A readable default for a new machine name: the host name, file-safe."""
    return _file_safe(platform.node().lower()) or "this-machine"


def resolve_path_preview(
    value: str, *, base_dir: Path, workspace_root: str | None = None
) -> Outcome:
    """What a path field would become, recomputed as it is typed.

    The same resolution the loader performs (`config.preview_path`), so the
    line under the box cannot drift from what the config actually means. A
    value the loader would refuse comes back as a refusal, which is the answer
    worth showing.
    """

    def build() -> Path | None:
        root = preview_path(workspace_root, base_dir=base_dir) if workspace_root else None
        return preview_path(value, base_dir=base_dir, workspace_root=root)

    return run(build)


def find_workspaces() -> Outcome:
    """Workspaces this machine already has, as the first-run screen offers them.

    Read-only and shallow (`locations.find_workspaces`): folder names under a
    cloud client's root, nothing opened, `.codex` not touched. Wrapped in an
    ``Outcome`` because it runs as a job like every other reading -- a
    disconnected drive is slow, not fatal.
    """
    return run(lambda: find_workspace_candidates())


def suggested_codex_dir() -> Path:
    home = os.getenv("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


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

    def config_exists(self) -> bool:
        return self._config_path.is_file()

    def about(self) -> BuildInfo:
        """What this build is. Reads no configuration and cannot fail.

        ``sys.frozen`` is what PyInstaller sets, and it is the difference that
        matters to a bug report: a packaged `CodexSync.exe` carries its own
        Python and its own copy of the package, so "which version" cannot be
        answered from the interpreter the user happens to have.
        """
        frozen = bool(getattr(sys, "frozen", False))
        return BuildInfo(
            version=__version__,
            frozen=frozen,
            executable=sys.executable if frozen else str(Path(sys.argv[0]).resolve()),
            python=platform.python_version(),
            system=f"{platform.system()} {platform.release()}".strip(),
            architecture=platform.machine() or "unknown",
            config_path=str(self._config_path),
            config_exists=self.config_exists(),
        )

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

    def config_info(self) -> Outcome:
        def build() -> ConfigInfo:
            cfg = load_config(self._config_path)
            names = {cfg.identity.machine_id} if cfg.identity.machine_id else set()
            for rule in cfg.path_mappings:
                names.update((rule.source_machine, rule.target_machine))
            return ConfigInfo(
                machine_id=cfg.identity.machine_id,
                machines=tuple(sorted(name for name in names if name)),
                workspace_root_dir=cfg.paths.workspace_root_dir,
                local_state_dir=cfg.paths.local_state_dir,
                cloud_root_dir=cfg.paths.cloud_root_dir,
                backup_dir=cfg.paths.backup_dir,
                temp_dir=cfg.paths.temp_dir,
                plans_dir=self._plans_dir(cfg),
            )

        return run(build)

    def _plans_dir(self, cfg) -> Path:
        """Where the window keeps the plan files an apply has to quote.

        Beside the workspace, never in the backup directory (pruned by age) or
        the temp directory (swept for orphans), and never in `.codex`.
        """
        root = cfg.paths.workspace_root_dir or self._config_path.resolve().parent
        return root / "plans"

    def chats(
        self,
        *,
        source_machine: str | None = None,
        target_machine: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> Outcome:
        return run(lambda: scan_chats(
            self._config_path, source_machine=source_machine,
            target_machine=target_machine, progress=progress,
        ))

    def mapping_hints(self, *, progress: ProgressCallback | None = None) -> Outcome:
        """Folders and machine names a `[[path_mappings]]` rule can be built from."""
        return run(lambda: suggest_path_mappings(self._config_path, progress=progress))

    def sync_candidates(self, relative: str = "") -> Outcome:
        """The children of one node of the two state directories. Reads only.

        The window never opens `.codex` itself; this is the whole of what a
        path picker is allowed to know, and secrets are already absent from it.
        """
        return run(lambda: list_sync_candidates(load_config(self._config_path), relative))

    def save_working_set(
        self,
        *,
        projects: tuple[str, ...],
        chats: tuple[str, ...],
        source_machine: str,
        target_machine: str,
        progress: ProgressCallback | None = None,
    ) -> Outcome:
        """Expand the chosen projects into sessions and remember the choice.

        The stored file holds the names, not the expansion: a set that froze
        session ids would stop covering a chat started on the other machine
        tomorrow, and a standing working set is exactly for that case.
        """

        def go() -> SessionScope:
            scope = build_working_set(
                self._config_path, projects=projects, chats=chats, progress=progress,
            )
            write_working_set(
                self._config_path, scope,
                source_machine=source_machine, target_machine=target_machine,
            )
            return scope

        return run(go)

    def working_set(self, *, source_machine: str, target_machine: str) -> Outcome:
        """The stored working set for this pair of machines."""
        return run(lambda: read_working_set(
            self._config_path, source_machine=source_machine, target_machine=target_machine,
        ))

    def session_index(self) -> Outcome:
        return run(lambda: audit_session_index(self._config_path))

    def scan_sessions(
        self, *, source_machine: str, target_machine: str,
        progress: ProgressCallback | None = None,
    ) -> Outcome:
        """Scan both sides and keep the plan on disk so it can be applied by id.

        Recorded decisions for this pair of machines are read when they exist,
        which is what makes a resolved conflict disappear from the next scan.
        """

        def go() -> SessionScan:
            cfg = load_config(self._config_path)
            plans = self._plans_dir(cfg)
            pair = f"{_file_safe(source_machine)}-{_file_safe(target_machine)}"
            resolutions = plans / f"sessions-resolutions-{pair}.json"
            used = resolutions.is_file()
            scope = read_working_set(
                self._config_path, source_machine=source_machine, target_machine=target_machine,
            )
            plan = scan_session_transfer(
                self._config_path,
                source_machine=source_machine,
                target_machine=target_machine,
                resolutions_path=resolutions if used else None,
                progress=progress,
                scope=None if scope.is_empty else build_working_set(
                    self._config_path, projects=scope.projects, chats=scope.chats,
                ),
            )
            plan_path = plans / f"sessions-plan-{pair}.json"
            save_transfer_plan(plan, plan_path)
            return SessionScan(plan, plan_path, resolutions, used, self._session_titles())

        return run(go)

    def _session_titles(self) -> dict[str, str]:
        """Titles are a courtesy: a failure to read them never fails the scan."""
        try:
            directory = scan_chats(self._config_path)
        except Exception:  # noqa: BLE001 - the scan's own result stands without titles
            return {}
        return {
            hashlib.sha256(chat.session_id.encode("utf-8")).hexdigest(): chat.title
            for chat in directory.chats
            if chat.title
        }

    def resolve_session_conflict(self, scan: SessionScan, *, conflict_id: str, choice: str) -> Outcome:
        return run(lambda: record_branch_resolution(
            scan.plan_path, conflict_id=conflict_id, choice=choice, output_path=scan.resolutions_path
        ))

    def resolve_format_migrations(self, scan: SessionScan) -> Outcome:
        """Keep the newer record format for every conflict that is only a rewrite."""
        return run(lambda: record_format_migrations(scan.plan_path, output_path=scan.resolutions_path))

    def scan_repair(
        self, *, source_machine: str, target_machine: str,
        progress: ProgressCallback | None = None,
    ) -> Outcome:
        def go() -> RepairScan:
            cfg = load_config(self._config_path)
            plan = scan_repair_projects(
                self._config_path, source_machine=source_machine,
                target_machine=target_machine, progress=progress,
            )
            pair = f"{_file_safe(source_machine)}-{_file_safe(target_machine)}"
            plan_path = self._plans_dir(cfg) / f"repair-plan-{pair}.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            save_repair_plan(plan, plan_path)
            return RepairScan(plan, plan_path)

        return run(go)

    def preview_sync(self) -> Outcome:
        """Build a sync plan without enforcing the gate, exactly like `plan`.

        The result is a picture taken while Codex may be running, so it is not
        reusable by a mutation: ``sync`` builds its own context with the gate
        enforced. Nothing here shortens that.
        """

        def go() -> SyncPreview:
            context = build_context(self._config_path, enforce_safety=False)
            return SyncPreview(
                to_local=tuple(action.relative_path for action in context.plan.to_local),
                to_cloud=tuple(action.relative_path for action in context.plan.to_cloud),
                conflicts=tuple(context.plan.conflicts),
                volatile=context.volatile,
            )

        return run(go)

    def save_session_plan(self, plan: Any, path: Path) -> Outcome:
        return run(lambda: save_transfer_plan(plan, Path(path)))

    # --- mutating: always a plan first, then its id ----------------------

    def sync(self, *, dry_run: bool) -> Outcome:
        """Run a sync through the same context the CLI builds.

        ``enforce_safety`` stays on. A GUI that turned it off to show a nicer
        error would be the one place in the project where a mutation is decided
        outside ``safety_gate``.
        """

        def go() -> SyncResult:
            context = build_context(self._config_path, enforce_safety=True)
            run_sync(context, dry_run=dry_run)
            return SyncResult(
                applied=not dry_run,
                actions=context.plan.action_count,
                conflicts=len(context.plan.conflicts),
            )

        return run(go)

    def apply_sessions(self, scan: SessionScan, *, confirm_plan: str, dry_run: bool = False) -> Outcome:
        return run(lambda: apply_session_transfer(
            self._config_path,
            plan_path=scan.plan_path,
            confirm_plan=confirm_plan,
            resolutions_path=scan.resolutions_path if scan.used_resolutions else None,
            dry_run=dry_run,
        ))

    def apply_repair(self, scan: RepairScan, *, confirm_plan: str, dry_run: bool = False) -> Outcome:
        return run(lambda: apply_repair_projects(
            self._config_path,
            plan_path=scan.plan_path,
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
        source_machine: str | None = None,
        target_machine: str | None = None,
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
            source_machine=source_machine,
            target_machine=target_machine,
        ))

    def restore(self, *, snapshot_name: str | None, target: str, dry_run: bool) -> Outcome:
        return run(lambda: restore_from_backup(
            self._config_path, snapshot_name, target, dry_run
        ))

    # --- guardian, backups, recovery --------------------------------------

    def guardian_inventory(self) -> Outcome:
        return run(lambda: read_guardian_inventory(self._config_path))

    def guardian_snapshot(self) -> Outcome:
        """One Guardian pass. Allowed while Codex runs: it only reads the state."""
        return run(lambda: build_guardian_runner(self._config_path).once())

    def backups(self) -> Outcome:
        return run(lambda: list_backup_snapshots(self._config_path))

    def journals(self) -> Outcome:
        return run(lambda: list_journals(self._config_path))

    def inspect_journal(self, operation_id: str) -> Outcome:
        return run(lambda: inspect_recovery(self._config_path, operation_id))

    def resume_journal(self, operation_id: str, *, dry_run: bool) -> Outcome:
        return run(lambda: resume_operation(self._config_path, operation_id, dry_run=dry_run))

    def rollback_journal(self, operation_id: str, *, target: str, dry_run: bool) -> Outcome:
        return run(lambda: rollback_operation(
            self._config_path, operation_id, target=target, dry_run=dry_run
        ))

    # --- config.toml -------------------------------------------------------

    def open_config(self) -> Outcome:
        def go() -> OpenedConfig:
            document = read_config_document(self._config_path)
            try:
                raw = tomllib.loads(document.text)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{self._config_path}: {exc}") from exc
            return OpenedConfig(document, raw)

        return run(go)

    def render_config(self, base_text: str, edits: ConfigEdits) -> Outcome:
        """Apply edits to the opened text, keeping every comment and unrelated byte."""

        def go() -> str:
            text = base_text
            try:
                for (section, key), value in edits.values.items():
                    if value is REMOVE:
                        text = remove_key(text, section, key)
                    else:
                        text = set_value(text, section, key, value)
                if edits.mappings is not None:
                    text = replace_array_of_tables(text, "path_mappings", edits.mappings)
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"The edit cannot be written safely: {exc}") from exc
            return text

        return run(go)

    def validate_config(self, text: str) -> Outcome:
        return run(lambda: validate_config_text(text, path=self._config_path))

    def config_diff(self, old: str, new: str) -> str:
        return config_diff(old, new, path_label=self._config_path.name)

    def save_config(self, text: str, *, expected_sha256: str) -> Outcome:
        return run(lambda: save_config_text(self._config_path, text, expected_sha256=expected_sha256))

    def config_history(self) -> Outcome:
        return run(lambda: list_config_history(load_config(self._config_path), self._config_path))

    def create_config(
        self,
        path: Path,
        *,
        machine_id: str,
        local_state_dir: str,
        workspace_root_dir: str,
        cloud_root_dir: str | None,
    ) -> Outcome:
        return run(lambda: create_config(
            Path(path),
            machine_id=machine_id,
            local_state_dir=local_state_dir,
            workspace_root_dir=workspace_root_dir,
            cloud_root_dir=cloud_root_dir or None,
        ))

    # --- automation --------------------------------------------------------

    def automation(self) -> Outcome:
        return run(lambda: automation_status(self._config_path))

    def apply_automation(self) -> Outcome:
        """Install or remove the OS task so it matches `[scheduler]` as saved."""
        return run(lambda: apply_automation(self._config_path))

    def remove_automation(self) -> Outcome:
        return run(lambda: remove_automation(self._config_path))

    def run_automation_now(self) -> Outcome:
        return run(lambda: run_automation_job(self._config_path))

    # --- guardian restore and project move ------------------------------------

    def preview_guardian_restore(self, snapshot_id: str) -> Outcome:
        """What restoring this snapshot would change. Reads only; works while Codex runs."""
        return run(lambda: restore_global_state(self._config_path, snapshot_id=snapshot_id)[0])

    def apply_guardian_restore(self, snapshot_id: str, *, confirm_plan: str, dry_run: bool) -> Outcome:
        return run(lambda: restore_global_state(
            self._config_path, snapshot_id=snapshot_id, confirm_plan=confirm_plan, dry_run=dry_run
        )[1])

    def preview_guardian_accept(self) -> Outcome:
        """Why Guardian keeps quarantining, and the plan that would accept it. Reads only."""
        return run(lambda: accept_guardian_baseline(self._config_path)[0])

    def apply_guardian_accept(self, *, confirm_plan: str) -> Outcome:
        """Make the current state latest-good; value is the accepted snapshot id."""
        return run(lambda: accept_guardian_baseline(self._config_path, confirm_plan=confirm_plan)[1])

    def scan_project_move(self, *, project_id: str, new_root: Path) -> Outcome:
        """Hash the project and keep the plan on disk so it can be applied by id."""

        def go() -> MoveScan:
            cfg = load_config(self._config_path)
            plan = scan_project_move(self._config_path, project_id=project_id, new_root=Path(new_root))
            path = self._plans_dir(cfg) / f"project-move-{_file_safe(plan.project_id)}.json"
            save_project_move_plan(plan, path)
            return MoveScan(plan, path)

        return run(go)

    def apply_project_move(self, scan: MoveScan, *, confirm_plan: str, dry_run: bool) -> Outcome:
        return run(lambda: apply_project_move_plan(
            self._config_path, plan_path=scan.plan_path, confirm_plan=confirm_plan, dry_run=dry_run
        ))
