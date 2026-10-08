"""Command orchestration: context building, sync, guardian, repair, recovery.

This module is the only layer allowed to combine core components into a
command.  It owns no low-level helpers: shared plumbing lives in ``runtime``,
restore in ``restore`` and diagnostics in ``preflight``.  Their public names
are re-exported here so ``codexsync.app`` stays the single import surface for
the CLI and the tests.
"""
from __future__ import annotations

import hashlib
import logging
import json
import os
import platform
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from typing import Callable, Mapping, Sequence

from .config import PATH_SUBSTITUTIONS, load_config, preview_path
from .progress import PHASES, ProgressCallback, report as report_progress
from .mapping_hints import MappingHints, build_mapping_hints
from .sync_candidates import SyncCandidate, list_sync_candidates
from .automation import AutomationView, apply_automation, automation_status, remove_automation
from .system_scheduler import BROKEN_TASK_CODES, FOREIGN_TASK, LEGACY_TASK
from .config_edit import (
    MAPPING_KEYS,
    ConfigChange,
    ConfigDocument,
    ConfigHistoryEntry,
    SavedConfig,
    change_config_value,
    change_path_mappings,
    config_diff,
    create_config,
    list_config_history,
    parse_config_value,
    read_config_document,
    remove_config_value,
    remove_key,
    replace_array_of_tables,
    save_config_text,
    set_value,
    validate_config_text,
)
from .config_migrate import (
    ConfigFinding,
    ConfigMigrationPlan,
    MigrationOutcome,
    apply_migration,
    inspect_config,
    read_config_source,
    render_migrated_text,
)
from .config import require_guardian_identity
from .exceptions import ChatDecisionsNeeded, ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .backup import BackupManager
from .manifest import build_manifest, load_manifest, save_manifest
from .mutation_journal import JournalState, JournalStore, MutationJournal
from .models import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    MAX_HANDOFF_DELIVERY_WAIT_MINUTES,
    MAX_SCHEDULER_INTERVAL_SECONDS,
    MAX_STATE_BACKUP_INTERVAL_HOURS,
    MIN_SCHEDULER_INTERVAL_SECONDS,
    AppConfig,
    CopyAction,
    DeleteAction,
    FileMeta,
    SyncPlan,
)
from .operation_lock import OperationLock
from .path_mapping import PathMappingError, apply_path_mapping, mapping_digest
from .project_sync import (
    ProjectMergePlan,
    ProjectSyncUnsupported,
    RootMappingAmbiguous,
    build_project_merge,
    publication_from_state,
    read_board as read_project_board,
    root_key,
    serialise_state,
    write_publication,
)
from .project_files import (
    WARNINGS as FILES_WARNINGS,
    FilesPublication,
    GitRunner,
    Verdict as FilesVerdict,
    compare,
    now_utc as files_now_utc,
    read_files_board,
    read_files_state,
    run_git,
    with_removed,
    write_files_publication,
)
from .guardian_runner import GuardianRunner
from .guardian_store import GuardianStore
from .chat_directory import ChatDirectory, ChatEntry, ChatKind, build_chat_directory
from .chat_move import ChatMovePlan, apply_chat_moves_to_state, build_chat_move_plan
from .guardian_schema import (
    build_binding_value,
    detect_state_schema,
    project_root_paths,
    replace_project_root,
    supports_project_creation,
    supports_root_remap,
    validate_global_state_references,
)
from .guardian_models import ValidationStatus
from .planner import build_sync_plan
from .process_knowledge import default_background_process_names, default_process_names
from .preflight import (
    PreflightCheckResult,
    PreflightReport,
    print_preflight_report,
    run_preflight,
)
from .repair_plan import RepairActionKind, RepairPlan, build_repair_plan, conflicting_bindings, load_repair_plan, save_repair_plan
from .restore import BackupSnapshotInfo, RestoreResult, list_backup_snapshots, restore_from_backup
from .recovery import (
    JournalInfo,
    RecoveryOutcome,
    list_history,
    list_journals,
    resume_operation,
    rollback_operation,
)
from .guardian_inventory import GuardianInventory, read_guardian_inventory
from .guardian_accept import GuardianAcceptPlan, ShrinkExplanation
from .guardian_restore import GuardianRestorePlan, build_guardian_restore_plan, verify_restore_still_valid
from .project_move import (
    ProjectMovePlan,
    ProjectMoveResult,
    apply_project_move,
    build_project_move_plan,
    load_project_move_plan,
    move_notes as project_move_notes,
    save_project_move_plan,
)
from .runtime import (
    ProcessSnapshot,
    _apply_session_mode,  # noqa: F401  (re-exported: imported by tests)
    _build_indexes,
    _ensure_dir,
    _hash_file,
    _make_safety_gate,
    _plan_hash,
    _require_mutation_compatible_config,
    _require_within,
    collect_codex_processes,
    collect_process_snapshot,
    initialize_runtime_paths,
    sync_machine_id,
)
from .safety_gate import OperationKind, ProcessState, SafetyGate
from .state_backup import StateBackupEntry, StateBackupResult, create_state_backup, list_state_backups
from .home_stats import (
    HomeSummary,
    StateStats,
    read_home_summary,
    state_stats_from_directory,
    write_state_stats,
)
from .session_scope import (
    SessionScope,
    build_session_scope,
    load_session_scope,
    save_session_scope,
    scope_path_for,
    _file_safe as _scope_file_safe,
)
from .semantic_transfer import (
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    TransferPlan,
    FORMAT_MIGRATION,
    MOVES_BRANCH,
    OLDER_FORMAT_HAS_LATER_RECORDS,
    NEW_CHAT_SAME_PATH,
    RESOLVED_BY_RULE,
    build_transfer_plan,
    conflict_rule_for,
    descriptors_by_session_hash,
    format_migration_resolutions,
    layout_for_new_chats,
    load_transfer_plan,
    local_folder_exists,
    mirror_codec_for,
    plan_scope,
    prefer_catalogued_copies,
    save_transfer_plan,
    transfer_direction,
)
from .semantic_store import SemanticStore
from .jsonl_codec import JSONL_READ_ERRORS, JsonlCodec, codec_of, open_jsonl
from .sqlite_audit import (
    PlacementStatus,
    ThreadActivity,
    read_backfill_state,
    read_thread_activity,
    read_thread_names,
    read_thread_placements,
)
from .fs_replace import replace_with_retry
from .config_locations import cache_dir
from .chat_names import (
    NamePlan,
    apply_names,
    build_name_plan,
    publication_from_names,
    read_names_back,
    read_names_board,
    write_names_publication,
)
from .thread_catalogue import (
    BACKFILL_PENDING,
    CatalogueRefreshPlan,
    RefreshStatus,
    build_refresh_plan,
    database_fingerprint,
    read_backfill_status,
    reset_backfill,
    unnamed_digest,
)
from .session_catalog import one_per_chat, scan_both_sides, scan_sessions
from .session_index import (
    PROVEN_CONTRACTS,
    SESSION_INDEX_FILE,
    IndexParseResult,
    parse_session_index,
)
from .stable_reader import SourceMissingError, SourceTooLargeError, SourceUnstableError, StableReader
from .state_locator import locate_local_state_dir, locate_state_dirs
from .sync_engine import SyncEngine
from .handoff import (
    Board,
    Delivery,
    Fingerprinter,
    HandoffError,
    HandoffRecord,
    STATE_WORKING,
    delivery,
    machine_key,
    mark_working,
    read_board,
    record_handoff,
)
from .notifications import Notifier
from .codex_closer import CloseOutcome, ask_codex_to_quit
from .version import PRODUCER_VERSION, __version__

LOG = logging.getLogger(__name__)

__all__ = [
    "MAPPING_KEYS",
    "CloseOutcome",
    "close_codex_for_sync",
    "SYNC_DIRECTIONS",
    "with_run_choices",
    "FullSyncPreview",
    "preview_full_sync",
    "ConfigChange",
    "change_config_value",
    "change_path_mappings",
    "parse_config_value",
    "remove_config_value",
    "__version__",
    "BROKEN_TASK_CODES",
    "MAX_HANDOFF_DELIVERY_WAIT_MINUTES",
    "HandoffNotDelivered",
    "HANDOFF_STEPS",
    "HandoffResult",
    "HandoffStatus",
    "handoff_status",
    "run_handoff",
    "refresh_thread_catalogue",
    "CatalogueRefreshResult",
    "sync_chat_names",
    "ChatNamesResult",
    "check_project_files",
    "ProjectFilesItem",
    "ProjectFilesReport",
    "watch_handoff",
    "MAX_STATE_BACKUP_INTERVAL_HOURS",
    "DEFAULT_SCHEDULER_INTERVAL_SECONDS",
    "MIN_SCHEDULER_INTERVAL_SECONDS",
    "MAX_SCHEDULER_INTERVAL_SECONDS",
    "FOREIGN_TASK",
    "LEGACY_TASK",
    "ConfigFinding",
    "ConfigMigrationPlan",
    "MigrationOutcome",
    "apply_config_migration",
    "check_config_migration",
    "preview_config_migration",
    "PRODUCER_VERSION",
    "SessionScope",
    "load_session_scope",
    "build_working_set",
    "read_working_set",
    "write_working_set",
    "MappingHints",
    "suggest_path_mappings",
    "PATH_SUBSTITUTIONS",
    "SyncCandidate",
    "list_sync_candidates",
    "PHASES",
    "ProgressCallback",
    "preview_path",
    "GuardianRestorePlan",
    "GuardianAcceptPlan",
    "ShrinkExplanation",
    "accept_guardian_baseline",
    "ProjectMovePlan",
    "ProjectMoveResult",
    "apply_project_move_plan",
    "project_move_notes",
    "restore_global_state",
    "save_project_move_plan",
    "scan_project_move",
    "AutomationRun",
    "StateBackupEntry",
    "StateBackupResult",
    "create_codex_backup",
    "HomeSummary",
    "StateStats",
    "read_home_summary",
    "recount_state",
    "remember_state_stats",
    "list_codex_backups",
    "AutomationView",
    "apply_automation",
    "automation_status",
    "remove_automation",
    "run_automation_job",
    "ConfigDocument",
    "ConfigHistoryEntry",
    "SavedConfig",
    "config_diff",
    "create_config",
    "list_config_history",
    "read_config_document",
    "remove_key",
    "replace_array_of_tables",
    "save_config_text",
    "set_value",
    "validate_config_text",
    "BackupSnapshotInfo",
    "GuardianInventory",
    "JournalInfo",
    "RecoveryOutcome",
    "list_backup_snapshots",
    "list_history",
    "list_journals",
    "read_guardian_inventory",
    "resume_operation",
    "rollback_operation",
    "AppContext",
    "PreflightCheckResult",
    "PreflightReport",
    "ProcessSnapshot",
    "RestoreResult",
    "apply_repair_projects",
    "apply_session_transfer",
    "build_context",
    "build_guardian_runner",
    "collect_codex_processes",
    "collect_process_snapshot",
    "initialize_runtime_paths",
    "inspect_recovery",
    "print_plan",
    "load_branch_resolutions",
    "load_config",
    "print_preflight_report",
    "record_branch_resolution",
    "record_format_migrations",
    "restore_from_backup",
    "commit_global_state",
    "move_chats",
    "save_transfer_plan",
    "scan_chats",
    "scan_session_transfer",
    "run_preflight",
    "run_sync",
    "save_repair_plan",
    "scan_repair_projects",
    "validate_config_only",
]


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    local_dir: Path
    cloud_dir: Path
    plan: SyncPlan
    local_index: dict[str, FileMeta]
    cloud_index: dict[str, FileMeta]
    safety_gate: SafetyGate
    volatile: bool = False



def check_config_migration(
    config_path: Path, *, include_defaults: bool = False
) -> ConfigMigrationPlan:
    """What this version would change in `config_path`. Reads, never writes."""
    text, source_sha256 = read_config_source(config_path)
    return inspect_config(
        text, include_defaults=include_defaults, source_sha256=source_sha256
    )


def preview_config_migration(
    config_path: Path,
    *,
    include_defaults: bool = False,
    skip: Sequence[str] = (),
) -> tuple[ConfigMigrationPlan, str]:
    """The plan and the diff its accepted findings would produce.

    Rendering here rather than in the caller is what lets the window and the
    command line show the same text before anything is written, and it fails
    the same way for both if an edit cannot be expressed.
    """
    text, source_sha256 = read_config_source(config_path)
    plan = inspect_config(
        text, include_defaults=include_defaults, source_sha256=source_sha256
    )
    if not plan.fixable:
        return plan, ""
    migrated = render_migrated_text(text, plan, skip=skip)
    return plan, config_diff(text, migrated, path_label=str(config_path))


def apply_config_migration(
    config_path: Path,
    *,
    confirm_plan_id: str,
    skip: Sequence[str] = (),
    include_defaults: bool = False,
) -> MigrationOutcome:
    """Apply a confirmed plan in one write. The id must still match the file."""
    return apply_migration(
        config_path,
        confirm_plan_id=confirm_plan_id,
        skip=skip,
        include_defaults=include_defaults,
    )


#: `[conflict] policy` values, which are also what `--conflict-policy` takes.
CONFLICT_POLICIES = ("manual_abort", "prefer_newer_mtime", "prefer_local", "prefer_cloud")


def with_conflict_policy(cfg: AppConfig, policy: str | None) -> AppConfig:
    """``cfg`` with `[conflict] policy` replaced for one run, or as it is.

    A run nobody watches used to force `manual_abort` here (`D-016`). Since
    D-027 the rule in the config is the person's decision made in advance, so
    it applies whoever starts the run; this override is the one-run choice a
    stopped sync offers ("keep the newer copies", "keep this machine's").
    """
    if policy is None:
        return cfg
    if policy not in CONFLICT_POLICIES:
        raise ConfigError(f"conflict policy must be one of: {', '.join(CONFLICT_POLICIES)}")
    return replace(cfg, conflict=replace(cfg.conflict, policy=policy))


#: `[sync] direction` values; ``direction`` below overrides it for one run.
SYNC_DIRECTIONS = ("bidirectional", "to_cloud", "to_local")


def with_run_choices(cfg: AppConfig, policy: str | None, direction: str | None) -> AppConfig:
    """``cfg`` with this run's conflict rule and direction, where one was chosen.

    The direction is the person's choice for one run -- "this machine wrote
    nothing, only send" -- and decides files and chats alike, as the setting
    does (D-012, D-027).
    """
    cfg = with_conflict_policy(cfg, policy)
    if direction is None:
        return cfg
    if direction not in SYNC_DIRECTIONS:
        raise ConfigError(f"direction must be one of: {', '.join(SYNC_DIRECTIONS)}")
    return replace(cfg, sync=replace(cfg.sync, direction=direction))


def build_context(
    config_path: Path,
    manual_terminate_confirmation_override: bool | None = None,
    enforce_safety: bool = True,
    conflict_policy: str | None = None,
    direction: str | None = None,
) -> AppContext:
    cfg = with_run_choices(load_config(config_path), conflict_policy, direction)
    safety_gate = _make_safety_gate(cfg)
    if enforce_safety:
        _require_mutation_compatible_config(cfg)
        safety_gate.require(OperationKind.SYNC)
    else:
        # Planning remains read-only while Codex is open, but its result is
        # volatile and must be rebuilt by a mutation command.
        plan_decision = safety_gate.check(OperationKind.PLAN)
        volatile = plan_decision.process_state is not ProcessState.STOPPED
    if enforce_safety:
        initialize_runtime_paths(cfg)
    local_dir, cloud_dir = locate_state_dirs(cfg)
    if not cfg.targets.include_roots:
        # Never "everything": that is where the credentials live (CS-289).
        raise ConfigError("targets.include_roots is not set: there is nothing to synchronise")

    local_idx, cloud_idx = _build_indexes(cfg, local_dir, cloud_dir)
    manifest = load_manifest(
        cfg.state.manifest_file, cfg.state.data_version, machine_id=sync_machine_id(cfg)
    )
    plan = build_sync_plan(
        local_index=local_idx,
        cloud_index=cloud_idx,
        local_root=local_dir,
        cloud_root=cloud_dir,
        previous_manifest=manifest,
        compare_mode=cfg.sync.compare,
        tolerance_seconds=cfg.sync.time_tolerance_seconds,
        conflict_policy=cfg.conflict.policy,
        equal_mtime_action=cfg.sync.equal_mtime_action,
        direction=cfg.sync.direction,
        delete_policy=cfg.sync.delete_policy,
        include_roots=cfg.targets.include_roots,
    )
    return AppContext(
        config=cfg,
        local_dir=local_dir,
        cloud_dir=cloud_dir,
        plan=plan,
        local_index=local_idx,
        cloud_index=cloud_idx,
        safety_gate=safety_gate,
        volatile=volatile if not enforce_safety else False,
    )


def build_guardian_runner(config_path: Path) -> GuardianRunner:
    """Construct a Guardian that reads only the global JSON state file."""
    cfg = load_config(config_path)
    machine_id = require_guardian_identity(cfg)
    local_dir = locate_local_state_dir(cfg)
    source = local_dir / ".codex-global-state.json"
    store = GuardianStore(
        cfg.guardian.root_dir,
        machine_id,
        producer_version=PRODUCER_VERSION,
        retention_days=cfg.guardian.retention_days,
        max_snapshots=cfg.guardian.max_snapshots,
        quarantine_retention_days=cfg.guardian.quarantine_retention_days,
        staging_retention_hours=cfg.guardian.staging_retention_hours,
    )
    return GuardianRunner(source, store, cfg.guardian)


def scan_repair_projects(
    config_path: Path,
    *,
    source_machine: str,
    target_machine: str,
    progress: ProgressCallback | None = None,
) -> RepairPlan:
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    decision = _make_safety_gate(cfg).check(OperationKind.REPAIR_SCAN)
    volatile = decision.process_state is not ProcessState.STOPPED
    observation = _observe_global_state(cfg, config_path, local_dir)
    catalog = scan_sessions(
        local_dir,
        volatile=volatile,
        source_machine=source_machine,
        progress=progress,
    )
    return build_repair_plan(
        catalog,
        observation.payload,
        source_machine=source_machine,
        target_machine=target_machine,
        rules=cfg.path_mappings,
        volatile=volatile,
    )


def apply_repair_projects(
    config_path: Path,
    *,
    plan_path: Path,
    confirm_plan: str,
    dry_run: bool = False,
) -> int:
    """Apply one exact repair plan, or report what applying it would do.

    ``dry_run`` runs every refusal the real apply runs — plan identity, plan
    freshness, mapping digest, unsupported actions, the process gate and the
    post-edit Guardian validation of the candidate state — and stops before the
    operation lock, so a preview is blocked by a running Codex exactly as the
    mutation is.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    try:
        plan = load_repair_plan(plan_path)
    except ValueError as exc:
        # A missing or malformed --plan file is bad input from the caller, not
        # an internal fault: it must map to exit code 4, not 1.
        raise ConfigError(f"Cannot read repair plan {plan_path}: {exc}") from exc
    if plan.plan_id != confirm_plan:
        raise ConfigError("--confirm-plan must exactly match the saved repair plan id")
    if plan.volatile or plan.codes:
        raise FailSafeError("Volatile or unresolved repair plan cannot be applied")
    if plan.mapping_digest != mapping_digest(cfg.path_mappings):
        raise FailSafeError("Path mapping rules changed after the repair scan")
    unsupported = {RepairActionKind.AMBIGUOUS_PROJECT, RepairActionKind.UNSUPPORTED_BACKEND}
    if any(action.kind in unsupported for action in plan.actions):
        raise ConflictError("Repair plan contains unresolved or unsupported actions")
    if conflicting_bindings(plan.actions):
        raise FailSafeError("Repair plan binds one chat to more than one project")
    gate = _make_safety_gate(cfg)
    gate.require(OperationKind.REPAIR_APPLY)
    local_dir = locate_local_state_dir(cfg)
    source = local_dir / ".codex-global-state.json"
    original = _read_live_state(source, config_path)
    if hashlib.sha256(original).hexdigest() != plan.global_state_sha256:
        raise FailSafeError("Global state changed after the repair scan")
    try:
        state = json.loads(original.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FailSafeError("Global state is not valid JSON") from exc
    if not isinstance(state, dict):
        raise FailSafeError("Global state schema is unsupported")
    projects = state.get("local-projects")
    order = state.get("project-order")
    assignments = state.setdefault("thread-project-assignments", {})
    if not isinstance(projects, dict) or not isinstance(order, list) or not isinstance(assignments, dict):
        raise FailSafeError("Global state schema is unsupported for JSON-only repair")
    schema_id = detect_state_schema(state)
    if schema_id is None:
        raise FailSafeError("Global state schema is not recognised; repair cannot write safely")
    for action in plan.actions:
        if action.kind is RepairActionKind.ADD_PROJECT:
            if not action.project_id or not action.target_root:
                raise FailSafeError("Repair plan action is incomplete")
            if not supports_project_creation(schema_id):
                raise ConflictError(
                    f"Creating a project entry is not supported for schema {schema_id!r}: its entry "
                    "carries fields whose meaning has not been confirmed. Create the project in "
                    "Codex, then rerun the scan to bind sessions to it."
                )
            projects.setdefault(action.project_id, {"root": action.target_root})
            if action.project_id not in order:
                order.append(action.project_id)
        elif action.kind is RepairActionKind.REMAP_ROOT:
            if not action.project_id or not action.target_root or not action.source_root:
                raise FailSafeError("Repair plan action is incomplete")
            if not supports_root_remap(schema_id):
                raise ConflictError(
                    f"Rewriting a project root is not supported for schema {schema_id!r}."
                )
            entry = projects.get(action.project_id)
            if not isinstance(entry, dict):
                raise FailSafeError("Repair plan remaps a project this state does not have")
            try:
                # The project keeps its id, so every thread bound to it moves
                # with it and no session file is touched.
                projects[action.project_id] = replace_project_root(
                    schema_id, entry, old_root=action.source_root, new_root=action.target_root
                )
            except ValueError as exc:
                raise FailSafeError(f"Project root cannot be remapped safely: {exc}") from exc
        elif action.kind is RepairActionKind.ADD_BINDING:
            if not action.session_id or not action.project_id:
                raise FailSafeError("Repair binding action is incomplete")
            assignments[action.session_id] = build_binding_value(schema_id, action.project_id)
    candidate = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report = validate_global_state_references(candidate)
    if report.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        raise FailSafeError("Repaired global state failed Guardian validation")

    if dry_run:
        approved = sum(
            1 for action in plan.actions
            if action.kind in {
                RepairActionKind.ADD_PROJECT,
                RepairActionKind.REMAP_ROOT,
                RepairActionKind.ADD_BINDING,
            }
        )
        LOG.info(
            "repair dry-run: plan %s would apply %d action(s) to %s",
            plan.plan_id, approved, source,
        )
        return approved

    return commit_global_state(
        cfg, gate, OperationKind.REPAIR_APPLY,
        family="repair", plan_id=plan.plan_id, action_count=len(plan.actions),
        state_root=local_dir, source=source, original=original, candidate=candidate,
    )


def commit_global_state(
    cfg: AppConfig,
    gate,
    operation: OperationKind,
    *,
    family: str,
    plan_id: str,
    action_count: int,
    state_root: Path,
    source: Path,
    original: bytes,
    candidate: bytes,
    counts: Mapping[str, int] | None = None,
    origin: str | None = None,
) -> int:
    """Replace `.codex-global-state.json` with `candidate`, or leave it alone.

    Every command that edits the global state runs this exact envelope, because
    the guarantees are the file's and not any one command's: one mutation per
    state root at a time, durable evidence that a commit was entered, a verified
    full backup before the first byte moves, the process re-checked immediately
    before the replace, and a verified rollback if anything after it fails.

    A caller decides *what* to write and proves it valid; it does not get to
    decide how carefully the write happens.
    """
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    machine = cfg.identity.machine_id or platform.node()
    with OperationLock(cfg.paths.temp_dir, state_root=state_root, machine_id=machine, family=family):
        journals = JournalStore(cfg.paths.temp_dir)
        manager = BackupManager(
            cfg.paths.backup_dir, machine, compression="none", journal_root=cfg.paths.temp_dir
        )
        journal = journals.begin(
            family, plan_id, action_count, backup_snapshot=manager.snapshot_name,
            counts=counts, origin=origin, machine_id=machine,
        )
        original_sha256 = hashlib.sha256(original).hexdigest()
        # Everything up to COMMITTING replaces nothing, so any failure here --
        # a backup that cannot be written, Codex starting -- closes the journal
        # as FAILED. Left open it blocked every later mutation until a manual
        # `recover resume`, for a write that never happened (CS-291).
        try:
            backup_path = manager.backup_file(source, source.name, side="local")
            if backup_path is None or _hash_file(backup_path) != original_sha256:
                raise FailSafeError("Verified full backup could not be created")
            manager.finalize()
            journal = journals.transition(journal, JournalState.BACKED_UP)
            gate.require(operation, final=True)
            journal = journals.transition(journal, JournalState.COMMITTING)
        except Exception as exc:
            _close_journal_after_failure(journals, journal, exc)
            raise
        replaced = False
        temp = source.with_name(f".{source.name}.{uuid.uuid4().hex}.tmp")

        def require_safe_to_replace() -> None:
            gate.require(operation, final=True)
            # The candidate was computed from `original`. A file that moved
            # since -- Codex, or another writer this lock did not cover --
            # would be overwritten with an edit of a state that no longer
            # exists (CS-304), so it is re-read right before the replace,
            # and again before each retry of a replace a lock refused.
            if _hash_file(source) != original_sha256:
                raise FailSafeError(
                    f"{source.name} changed after it was read; nothing was replaced. Run the command again."
                )

        try:
            with temp.open("xb") as handle:
                handle.write(candidate)
                handle.flush()
                os.fsync(handle.fileno())
            require_safe_to_replace()
            replace_with_retry(temp, source, before_retry=require_safe_to_replace)
            replaced = True
            post = validate_global_state_references(source.read_bytes())
            if post.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
                raise FailSafeError(f"{family} post-validation failed")
            journal = journals.transition(journal, JournalState.COMMITTED)
            manager.prune()
            return action_count
        except Exception as exc:
            temp.unlink(missing_ok=True)
            if not replaced:
                # The commit phase was entered and nothing was replaced. It is
                # closed through RECOVERY_REQUIRED -- COMMITTING has no edge to
                # FAILED -- so the trail keeps saying the phase was entered.
                _close_journal_after_failure(journals, journal, exc)
                raise
            journal = journals.transition(journal, JournalState.RECOVERY_REQUIRED, failure=exc)
            if replaced:
                try:
                    gate.require(operation, final=True)
                    rollback = source.with_name(f".{source.name}.{uuid.uuid4().hex}.rollback.tmp")
                    shutil.copy2(backup_path, rollback)
                    replace_with_retry(
                        rollback, source, before_retry=lambda: gate.require(operation, final=True)
                    )
                    if hashlib.sha256(source.read_bytes()).hexdigest() != hashlib.sha256(original).hexdigest():
                        raise FailSafeError("Rollback verification failed")
                    journal = journals.transition(journal, JournalState.FAILED)
                except Exception:
                    LOG.exception("%s rollback could not be completed safely", family)
            raise


def _close_journal_after_failure(journals: JournalStore, journal: MutationJournal, exc: BaseException) -> None:
    """Close a journal whose operation replaced nothing, keeping the original error.

    A journal that cannot be written here stays open, which blocks later
    mutations -- the safe side; it is logged instead of masking ``exc``.
    """
    try:
        if journal.state is JournalState.COMMITTING:
            journal = journals.transition(journal, JournalState.RECOVERY_REQUIRED, failure=exc)
        journals.transition(journal, JournalState.FAILED, failure=exc)
    except Exception:
        LOG.exception("Could not close the mutation journal %s", journal.operation_id)


def _plans_dir(cfg: AppConfig) -> Path:
    """Where working sets and conflict resolutions live, beside each other."""
    root = cfg.paths.workspace_root_dir or cfg.paths.backup_dir.parent
    return root / "plans"


def read_working_set(config_path: Path, *, source_machine: str, target_machine: str) -> SessionScope:
    """The stored working set for this pair of machines, or an empty one."""
    cfg = load_config(config_path)
    return load_session_scope(scope_path_for(_plans_dir(cfg), source_machine, target_machine))


def write_working_set(
    config_path: Path,
    scope: SessionScope,
    *,
    source_machine: str,
    target_machine: str,
) -> Path:
    """Store the chosen projects and chats beside the conflict resolutions."""
    cfg = load_config(config_path)
    return save_session_scope(
        scope, scope_path_for(_plans_dir(cfg), source_machine, target_machine)
    )


def build_working_set(
    config_path: Path,
    *,
    projects: tuple[str, ...] = (),
    chats: tuple[str, ...] = (),
    progress: ProgressCallback | None = None,
) -> SessionScope:
    """Expand chosen projects and chats into the sessions they cover. Reads only.

    The thread catalogue is read too, so the set can say which of its chats
    this machine cannot place -- they are mirrored either way, but they will
    not appear in Codex here, and that is worth knowing before the transfer
    rather than after it.
    """
    cfg = load_config(config_path)
    directory = scan_chats(config_path, progress=progress)
    return build_session_scope(
        directory, projects=projects, chats=chats,
        placements=read_thread_placements(locate_local_state_dir(cfg)),
    )


def suggest_path_mappings(
    config_path: Path,
    *,
    progress: ProgressCallback | None = None,
) -> MappingHints:
    """Candidates for a `[[path_mappings]]` rule, read from this machine.

    Built on the same chat scan the chats screen runs, so the folders offered
    are the ones chats actually name; a settings form calls this instead of
    asking the person to remember the other machine's paths.
    """
    cfg = load_config(config_path)
    directory = scan_chats(config_path, progress=progress)
    names = {cfg.identity.machine_id} if cfg.identity.machine_id else set()
    for rule in cfg.path_mappings:
        names.update((rule.source_machine, rule.target_machine))
    return build_mapping_hints(directory, machines=tuple(sorted(name for name in names if name)))


def scan_chats(
    config_path: Path,
    *,
    source_machine: str | None = None,
    target_machine: str | None = None,
    progress: ProgressCallback | None = None,
) -> ChatDirectory:
    """List every chat and the project it currently sits under. Reads only.

    Runs while Codex is open like any other scan; the result is marked volatile
    because a chat open right now is still being written.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    decision = _make_safety_gate(cfg).check(OperationKind.SESSION_SCAN)
    observation = _observe_global_state(cfg, config_path, local_dir)
    return build_chat_directory(
        local_dir,
        observation.payload,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=decision.process_state is not ProcessState.STOPPED,
        rules=cfg.path_mappings,
        source_machine=source_machine,
        target_machine=target_machine,
        progress=progress,
    )


def move_chats(
    config_path: Path,
    *,
    chat_refs: list[str],
    to_project: str,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    include_sub_threads: bool = False,
    source_machine: str | None = None,
    target_machine: str | None = None,
) -> tuple[ChatMovePlan, int]:
    """Preview or perform a move of chosen chats under one project.

    Without ``confirm_plan`` this reads only and returns the plan to show. With
    it, Codex must be closed and the plan is rebuilt from the state as it is
    now; the id has to still match, which is what makes a preview safe to act
    on later without a plan file existing anywhere.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    gate = _make_safety_gate(cfg)
    applying = confirm_plan is not None
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.CHAT_MOVE)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED

    source = local_dir / ".codex-global-state.json"
    original = _read_live_state(source, config_path)
    directory = build_chat_directory(
        local_dir, original,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=volatile,
        rules=cfg.path_mappings,
        source_machine=source_machine,
        target_machine=target_machine,
    )
    target = _one_project(directory, to_project)
    selected = _selected_chats(directory, chat_refs, include_sub_threads=include_sub_threads)
    plan = build_chat_move_plan(
        directory, original, chats=selected, to_project_id=target.project_id
    )
    if not applying:
        return plan, 0
    if plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm must match the plan id from the preview; the state has changed "
            "since then, so preview the move again and read what it now says"
        )
    if plan.codes:
        raise ConflictError("Chat move plan is unresolved: " + ", ".join(plan.codes))

    state = json.loads(original.decode("utf-8-sig"))
    if not isinstance(state, dict):
        raise FailSafeError("Global state schema is unsupported")
    apply_chat_moves_to_state(state, plan)
    candidate = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report = validate_global_state_references(candidate)
    if report.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        raise FailSafeError("Moved chats would leave the global state invalid")

    written = len(plan.writing_actions)
    if dry_run:
        LOG.info("chat move dry-run: plan %s would write %d binding(s)", plan.plan_id, written)
        return plan, written
    return plan, commit_global_state(
        cfg, gate, OperationKind.CHAT_MOVE,
        family="chats", plan_id=plan.plan_id, action_count=written,
        state_root=local_dir, source=source, original=original, candidate=candidate,
    )


@dataclass(frozen=True, slots=True)
class ProjectSyncResult:
    """What carrying the project list did, or would do (CS-333)."""

    machine: str
    root: Path
    plan: ProjectMergePlan
    #: Peer machines whose publication the plan takes.
    pending: tuple[str, ...]
    #: Publication files that were not believed, by name.
    unreadable: tuple[str, ...]
    #: Codex was running while this was read: a preview only.
    volatile: bool = False
    #: Changes written into the global state; 0 for a preview.
    written: int = 0
    #: Whether this machine's own publication was (re)written.
    published: bool = False


class ProjectsNotCarried(FailSafeError):
    """This machine's project list cannot be merged: no global state, or an unknown shape."""


def _coordination_dir(cfg: AppConfig) -> Path:
    """The folder of files machines coordinate through: beside the sync manifest.

    It already sits in the synced workspace and already may not overlap
    `.codex`, the cloud copy, backups or temp, so nothing new is chosen.
    """
    manifest = cfg.state.manifest_file
    if manifest is None:
        raise ConfigError(
            "state.manifest_file is empty: it names the shared folder machines coordinate through"
        )
    return manifest.parent


def projects_root(cfg: AppConfig) -> Path:
    return _coordination_dir(cfg) / "projects"


def _root_mapper(cfg: AppConfig, machine: str) -> Callable[[str, str], str]:
    rules = list(cfg.path_mappings)

    def map_root(peer: str, root: str) -> str:
        if not rules:
            return root
        try:
            return apply_path_mapping(root, source_machine=peer, target_machine=machine, rules=rules).target_path
        except PathMappingError as exc:
            if str(exc) == "NO_MAPPING":
                # Same path on both machines -- the usual case on one cloud drive.
                return root
            raise RootMappingAmbiguous(str(exc)) from exc

    return map_root


def _folder_exists(root: str) -> bool:
    try:
        return Path(root).is_dir()
    except (OSError, ValueError):
        return False


def sync_projects(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    gate: SafetyGate | None = None,
    origin: str | None = None,
    planned_here: bool = False,
) -> ProjectSyncResult:
    """Preview or carry other machines' project lists into this one (CS-333).

    Without ``confirm_plan`` this reads only. With it, Codex must be closed, the
    plan is rebuilt from the state as it is now and its id must still match;
    the merge is written through `commit_global_state` and this machine's own
    publication is rewritten from the result, so the other machines take this
    one's projects on their next sync. ``planned_here`` applies the plan this
    call builds, as `refresh_thread_catalogue` does; the envelope still refuses
    a state that moved since it was read.
    """
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    applying = confirm_plan is not None or planned_here
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.PROJECT_SYNC)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED

    root = projects_root(cfg)
    board = read_project_board(root)
    for name, reason in sorted(board.unreadable.items()):
        LOG.warning("project list %s is not believed: %s", name, reason)
    pending = board.pending(machine)
    source = local_dir / ".codex-global-state.json"
    if not source.is_file():
        raise ProjectsNotCarried(f"No Codex global state at {source}; there is no project list to merge into")
    original = _read_live_state(source, config_path)
    try:
        plan, state = build_project_merge(
            original, pending, machine=machine,
            map_root=_root_mapper(cfg, machine), folder_exists=_folder_exists,
        )
    except ProjectSyncUnsupported as exc:
        raise ProjectsNotCarried(f"Projects cannot be carried: {exc}") from exc
    result = ProjectSyncResult(
        machine=machine, root=root, plan=plan,
        pending=tuple(item.machine for item in pending),
        unreadable=tuple(sorted(board.unreadable)), volatile=volatile,
    )
    if not applying:
        return result
    if not planned_here and plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from the preview; the state has changed "
            "since then, so preview again and read what it now says"
        )
    for item in plan.ambiguous:
        LOG.warning(
            "project %s of %s left alone: %s", item.name or item.peer_project_id, item.peer_machine,
            ", ".join(item.codes),
        )
    for item in plan.missing_folders:
        LOG.warning("project %s added, but its folder does not exist here: %s", item.name, ", ".join(item.roots))
    if dry_run:
        LOG.info("project sync dry-run: plan %s would make %d change(s)", plan.plan_id, plan.action_count)
        return result

    written = 0
    if plan.writes:
        candidate = serialise_state(state)
        report = validate_global_state_references(candidate)
        if report.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
            raise FailSafeError("The merged project list would leave the global state invalid; nothing was written")
        written = commit_global_state(
            cfg, gate, OperationKind.PROJECT_SYNC,
            family="project-sync", plan_id=plan.plan_id, action_count=plan.action_count,
            state_root=local_dir, source=source, original=original, candidate=candidate,
            # What the history shows for this run: numbers only, no names.
            counts={"projects_added": len(plan.added), "changes": plan.action_count},
            origin=origin,
        )
        LOG.info(
            "projects: %d added, pins %s, order %s, %d chat binding(s) written",
            len(plan.added), "changed" if plan.pins_changed else "kept",
            "changed" if plan.order_changed else "kept", plan.bindings_written,
        )
    published = _publish_projects(root, board, machine, source, plan)
    return replace(result, written=written, published=published)


@dataclass(frozen=True, slots=True)
class CatalogueRefreshResult:
    """What `refresh_thread_catalogue` found and whether it asked Codex (D-024)."""

    plan: CatalogueRefreshPlan
    #: Codex was asked to rebuild its chat list; it does so on its next start.
    refreshed: bool = False
    #: Read while Codex was open, so only an indication.
    volatile: bool = False


def _catalogue_marker(cfg: AppConfig) -> Path:
    """Which files this machine last asked Codex to take up, beside the journals."""
    machine = cfg.identity.machine_id or platform.node()
    return cfg.paths.temp_dir / "thread-catalogue" / f"{machine_key(machine)}.json"


def _read_catalogue_marker(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = payload.get("unnamed_digest") if isinstance(payload, dict) else None
    return digest if isinstance(digest, str) else None


def _write_catalogue_marker(path: Path, plan: CatalogueRefreshPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "unnamed_digest": unnamed_digest(plan.unnamed),
        "files": len(plan.unnamed),
        "asked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    replace_with_retry(temp, path)


def refresh_thread_catalogue(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    gate: SafetyGate | None = None,
    origin: str | None = None,
    planned_here: bool = False,
) -> CatalogueRefreshResult:
    """Preview, or ask Codex to list the chat files its catalogue misses (D-024).

    Without ``confirm_plan`` this reads only. With it, Codex must be closed, the
    plan is rebuilt and its id must still match, and the one write --
    `backfill_state` back to the row Codex's own migration creates -- runs in
    the usual envelope: operation lock, journal, a verified backup of the
    database and its sidecars, the process re-checked and the database re-hashed
    right before the transaction. Codex then writes every thread row itself on
    its next start; codexSync never writes one.

    ``planned_here`` applies the plan this call builds, for a caller in the
    same run that showed nobody a preview (`run_handoff`): building it twice
    only to compare the ids hashed the whole database twice more. The envelope
    still rebuilds it right before the write.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    applying = confirm_plan is not None or planned_here
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.SESSION_APPLY)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED
    marker = _catalogue_marker(cfg)
    plan = build_refresh_plan(
        local_dir, read_thread_placements(local_dir), read_backfill_state(local_dir),
        asked_before=_read_catalogue_marker(marker),
    )
    result = CatalogueRefreshResult(plan=plan, volatile=volatile)
    if not applying:
        return result
    if not planned_here and plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from the preview; Codex's catalogue or the chat files "
            "changed since then, so preview again"
        )
    if not plan.writes:
        LOG.info("thread catalogue: %s, nothing asked of Codex", plan.status.value)
        return result
    if dry_run:
        LOG.info("thread catalogue dry-run: %d chat file(s) Codex does not list", len(plan.unnamed))
        return result
    assert plan.database is not None
    database = local_dir / Path(plan.database)
    snapshot = _write_codex_catalogue(
        cfg, gate, local_dir, database,
        family="thread-catalogue", plan_id=plan.plan_id, action_count=1,
        counts={"chats": len(plan.unnamed)}, origin=origin,
        # The plan was built from these exact bytes; a database that moved
        # since is one whose status may no longer be what was planned.
        still_planned=lambda: build_refresh_plan(
            local_dir, read_thread_placements(local_dir), read_backfill_state(local_dir),
            asked_before=_read_catalogue_marker(marker),
        ).plan_id == plan.plan_id,
        write=lambda: reset_backfill(database, now=int(time.time())),
        verify=lambda: read_backfill_status(database) == BACKFILL_PENDING,
    )
    _write_catalogue_marker(marker, plan)
    LOG.info(
        "thread catalogue: asked Codex to take up %d chat file(s) on its next start (backup %s)",
        len(plan.unnamed), snapshot,
    )
    return replace(result, refreshed=True)


@dataclass(frozen=True, slots=True)
class ChatNamesResult:
    """What `sync_chat_names` found, set and published (D-025)."""

    plan: NamePlan
    #: Names set on this machine's chats.
    written: int = 0
    #: This machine's own names were (re)published for the others.
    published: bool = False
    volatile: bool = False


def chat_names_root(cfg: AppConfig) -> Path:
    return _coordination_dir(cfg) / "chat-names"


def sync_chat_names(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    gate: SafetyGate | None = None,
    origin: str | None = None,
    planned_here: bool = False,
) -> ChatNamesResult:
    """Preview, or set other machines' chat names here and publish this one's.

    Without ``confirm_plan`` this reads only. With it, Codex must be closed and
    the plan id must still match; names are set only on chats whose name here
    is unset (`chat_names.is_unset`), in one transaction inside the catalogue
    envelope, and this machine's names are then published for the others.
    ``planned_here`` applies the plan this call builds, as
    `refresh_thread_catalogue` does.
    """
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    applying = confirm_plan is not None or planned_here
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.SESSION_APPLY)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED
    root = chat_names_root(cfg)

    def fingerprint(relative: str) -> str:
        return database_fingerprint(local_dir / Path(relative))

    def plan_now() -> NamePlan:
        board = read_names_board(root)
        for name, reason in sorted(board.unreadable.items()):
            LOG.warning("chat names %s are not believed: %s", name, reason)
        return build_name_plan(read_thread_names(local_dir), board.others(machine), fingerprint=fingerprint)

    plan = plan_now()
    result = ChatNamesResult(plan=plan, volatile=volatile)
    if not applying:
        return result
    if not planned_here and plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from the preview; chat names changed since then, "
            "so preview again"
        )
    if dry_run:
        LOG.info("chat names dry-run: %d name(s) would be set", len(plan.changes))
        return result
    written = 0
    if plan.writes:
        assert plan.database is not None
        database = local_dir / Path(plan.database)
        expected = {item.thread_id: item.name for item in plan.changes}
        _write_codex_catalogue(
            cfg, gate, local_dir, database,
            family="chat-names", plan_id=plan.plan_id, action_count=len(plan.changes),
            counts={"names": len(plan.changes)}, origin=origin,
            still_planned=lambda: plan_now().plan_id == plan.plan_id,
            write=lambda: apply_names(database, plan.changes),
            verify=lambda: read_names_back(database, sorted(expected)) == expected,
        )
        written = len(plan.changes)
        LOG.info("chat names: %d set from other machines, %d kept as named here", written, plan.kept)
    return replace(result, written=written, published=_publish_chat_names(root, machine, local_dir))


def _publish_chat_names(root: Path, machine: str, local_dir: Path) -> bool:
    """Rewrite this machine's names file when what it shows differs from it."""
    names = read_thread_names(local_dir)
    if names.status is not PlacementStatus.AVAILABLE:
        if names.status is not PlacementStatus.ABSENT:
            LOG.warning("chat names not published: catalogue %s %s", names.status.value, ",".join(names.codes))
        return False
    publication = publication_from_names(names, machine)
    own = read_names_board(root).publications.get(machine)
    if own is not None and own.content_id == publication.content_id:
        return False
    write_names_publication(root, publication)
    return True


@dataclass(frozen=True, slots=True)
class ProjectFilesItem:
    """One project of this machine compared with one other machine (D-026)."""

    project_id: str
    name: str
    root: str
    peer: str
    verdict: FilesVerdict
    #: When the other machine published what it held.
    peer_published_at_utc: str = ""
    #: Plain folders: files changed later there, files only there, and files
    #: deleted there that are still here.
    newer_there: tuple[str, ...] = ()
    missing_here: tuple[str, ...] = ()
    removed_there: tuple[str, ...] = ()
    #: The last time (unix seconds) a chat of this project changed on the
    #: other machine, when that is later than any here; 0 otherwise. Work in
    #: chats is the likeliest reason files moved, so it is said first.
    chats_there_at: int = 0

    @property
    def warns(self) -> bool:
        return self.verdict in FILES_WARNINGS


@dataclass(frozen=True, slots=True)
class ProjectFilesReport:
    machine: str
    items: tuple[ProjectFilesItem, ...] = ()
    published: bool = False
    unreadable: tuple[str, ...] = ()

    @property
    def warnings(self) -> tuple[ProjectFilesItem, ...]:
        return tuple(item for item in self.items if item.warns)


def project_files_root(cfg: AppConfig) -> Path:
    return _coordination_dir(cfg) / "project-files"


#: Project folders read at once: git and the walk mostly wait on the disk.
PROJECT_FILES_WORKERS = 4


def check_project_files(
    config_path: Path,
    *,
    publish: bool = False,
    git: GitRunner = run_git,
) -> ProjectFilesReport:
    """Whether this machine's project folders hold what the others last had.

    Reads only, except that ``publish`` rewrites this machine's own file in the
    workspace (never anything in a project or in `.codex`), and only when what
    it says changed. Folders and files come and go at any time, so every check
    reads them afresh and publishes: a full sync, `projects files`, and the
    window on every start -- which is how a machine that never ran a full sync
    with this version still tells the others what it holds.
    """
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    local_dir = locate_local_state_dir(cfg)
    source = local_dir / ".codex-global-state.json"
    try:
        state = json.loads(source.read_bytes().decode("utf-8-sig"))
    except FileNotFoundError:
        return ProjectFilesReport(machine)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"Cannot read the project list in {source}: {exc}") from exc
    schema_id = detect_state_schema(state)
    projects: dict[str, tuple[str, str]] = {}
    for project_id, entry in dict(state.get("local-projects") or {}).items():
        if not isinstance(entry, dict):
            continue
        roots = project_root_paths(schema_id, entry) if schema_id else ()
        if roots:
            # Codex's sidebar shows the folder's name.
            projects[str(project_id)] = (roots[0], Path(roots[0]).name or str(entry.get("name") or ""))

    from concurrent.futures import ThreadPoolExecutor

    cache_root = _project_hash_cache_root()
    with ThreadPoolExecutor(max_workers=PROJECT_FILES_WORKERS) as pool:
        states = dict(zip(
            projects,
            pool.map(
                lambda item: read_files_state(Path(item[0]), git=git, cache_root=cache_root),
                projects.values(),
            ),
        ))
    last_chat = _last_chat_by_project(
        {key: value[0] for key, value in projects.items()}, read_thread_activity(local_dir),
    )

    root = project_files_root(cfg)
    board = read_files_board(root)
    for name, reason in sorted(board.unreadable.items()):
        LOG.warning("project files %s are not believed: %s", name, reason)
    map_root = _root_mapper(cfg, machine)
    by_root = {root_key(local_root): project_id for project_id, (local_root, _) in projects.items()}
    items: list[ProjectFilesItem] = []
    for peer in board.others(machine):
        for peer_id, published in sorted(peer.projects.items()):
            local_id = peer_id if peer_id in projects else None
            if local_id is None:
                try:
                    local_id = by_root.get(root_key(map_root(peer.machine, published["root"])))
                except RootMappingAmbiguous:
                    local_id = None
            if local_id is None:
                continue
            local_root, name = projects[local_id]
            comparison = compare(Path(local_root), states[local_id], published["state"], git=git)
            there_at = int(published.get("last_chat_at") or 0)
            items.append(ProjectFilesItem(
                local_id, name, local_root, peer.machine, comparison.verdict, peer.published_at_utc,
                comparison.newer_there, comparison.missing_here, comparison.removed_there,
                there_at if there_at > last_chat.get(local_id, 0) else 0,
            ))
    # Projects whose chats moved on elsewhere first: that is where work happened.
    items.sort(key=lambda item: (not item.chats_there_at, item.name.casefold(), item.peer))
    for item in items:
        if item.warns:
            LOG.warning(
                "project %s: files %s against %s (%d newer there, %d missing here, %d deleted there%s)",
                item.name, item.verdict.value, item.peer, len(item.newer_there), len(item.missing_here),
                len(item.removed_there),
                ", chats continued there" if item.chats_there_at else "",
            )
    published = False
    if publish:
        own = board.publications.get(machine)
        now = int(time.time())
        publication = FilesPublication(
            machine,
            {
                key: {
                    "root": value[0], "name": value[1], "last_chat_at": last_chat.get(key, 0),
                    # Deletions are known only against what this machine said before.
                    "state": with_removed(
                        states[key],
                        own.projects[key]["state"] if own is not None and key in own.projects else None,
                        now,
                    ),
                }
                for key, value in projects.items()
            },
            files_now_utc(),
        )
        # Rewritten only when what it says changed: it sits in the cloud
        # folder and lists every file of every plain project.
        if own is None or own.body()["projects"] != publication.body()["projects"]:
            write_files_publication(root, publication)
            published = True
    return ProjectFilesReport(machine, tuple(items), published, tuple(sorted(board.unreadable)))


def _project_hash_cache_root() -> Path:
    """This machine's own cache of project file hashes; local, never synced."""
    return cache_dir() / "project-hashes"


def _last_chat_by_project(roots: Mapping[str, str], activity: ThreadActivity) -> dict[str, int]:
    """Per project, the last time a chat whose folder lies inside it changed."""
    keyed = {project_id: root_key(root) for project_id, root in roots.items()}
    result: dict[str, int] = {}
    for cwd, updated_at in activity.rows:
        where = root_key(cwd)
        for project_id, key in keyed.items():
            separator = "\\" if "\\" in key else "/"
            if where == key or where.startswith(key.rstrip(separator) + separator):
                result[project_id] = max(result.get(project_id, 0), updated_at)
    return result


def _write_codex_catalogue(
    cfg: AppConfig,
    gate,
    local_dir: Path,
    database: Path,
    *,
    family: str,
    plan_id: str,
    action_count: int,
    counts: Mapping[str, int],
    origin: str | None,
    still_planned: Callable[[], bool],
    write: Callable[[], object],
    verify: Callable[[], bool],
) -> str:
    """The one envelope for a write into Codex's thread catalogue (D-024, D-025).

    Operation lock, journal, a verified backup of the database and its
    sidecars, the plan rebuilt and the process re-checked right before the
    write, which is a single SQLite transaction: it commits whole or rolls
    back, so a failure inside it closes the journal as replacing nothing. A
    write that committed but does not read back as intended leaves the
    journal in RECOVERY_REQUIRED; the database is in the snapshot.
    Returns the snapshot name.
    """
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    machine = cfg.identity.machine_id or platform.node()
    relative = database.relative_to(local_dir).as_posix()
    with OperationLock(cfg.paths.temp_dir, state_root=local_dir, machine_id=machine, family=family):
        journals = JournalStore(cfg.paths.temp_dir)
        manager = BackupManager(
            cfg.paths.backup_dir, machine, compression="none", journal_root=cfg.paths.temp_dir
        )
        journal = journals.begin(
            family, plan_id, action_count, backup_snapshot=manager.snapshot_name,
            counts=counts, origin=origin, machine_id=machine,
        )
        try:
            for suffix in ("", "-wal", "-shm"):
                part = database.with_name(database.name + suffix)
                if not part.is_file():
                    continue
                expected = _hash_file(part)
                copy = manager.backup_file(part, relative + suffix, side="local")
                if copy is None or _hash_file(copy) != expected:
                    raise FailSafeError("Verified backup of Codex's catalogue could not be created")
            manager.finalize()
            journal = journals.transition(journal, JournalState.BACKED_UP)
            gate.require(OperationKind.SESSION_APPLY, final=True)
            if not still_planned():
                raise FailSafeError("Codex's catalogue changed after it was read; nothing was written. Run again.")
            journal = journals.transition(journal, JournalState.COMMITTING)
        except Exception as exc:
            _close_journal_after_failure(journals, journal, exc)
            raise
        try:
            # Again right before the transaction: the journal write above may
            # have waited seconds on a cloud client, and Codex may have started
            # in them -- the same final check `commit_global_state` makes.
            gate.require(OperationKind.SESSION_APPLY, final=True)
            write()
        except Exception as exc:
            _close_journal_after_failure(journals, journal, exc)
            raise
        if not verify():
            failure = FailSafeError(f"{family}: the catalogue did not read back as written")
            journals.transition(journal, JournalState.RECOVERY_REQUIRED, failure=failure)
            raise failure
        journals.transition(journal, JournalState.COMMITTED)
        manager.prune()
        return manager.snapshot_name


def _publish_projects(root: Path, board, machine: str, source: Path, plan: ProjectMergePlan) -> bool:
    """Rewrite this machine's project list from the state as it now is.

    Written only when something in it differs, so an unchanged machine does
    not make every other one see a "new" list.
    """
    state = json.loads(source.read_bytes().decode("utf-8-sig"))
    own = board.own(machine)
    accepted = {**(own.accepted if own is not None else {}), **plan.taken}
    publication = publication_from_state(state, machine, accepted=accepted)
    if (
        own is not None and own.publication_id == publication.publication_id
        and own.accepted == publication.accepted
    ):
        return False
    write_publication(root, publication)
    LOG.info("published this machine's project list (%d project(s)) to %s", len(publication.projects), root)
    return True


def _one_project(directory: ChatDirectory, reference: str):
    matches = directory.project_named(reference)
    if not matches:
        raise ConfigError(f"No project matches {reference!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(item.name or item.project_id for item in matches))
        raise ConfigError(f"{reference!r} matches more than one project: {names}")
    return matches[0]


def _selected_chats(
    directory: ChatDirectory, refs: list[str], *, include_sub_threads: bool
) -> tuple[ChatEntry, ...]:
    """Resolve what a person typed into exactly the chats they meant.

    An ambiguous prefix is refused rather than resolved to the first match: the
    whole point of naming a chat is to move that one.
    """
    chosen: dict[str, ChatEntry] = {}
    for ref in refs:
        matches = directory.find(ref)
        if not include_sub_threads:
            matches = tuple(chat for chat in matches if chat.kind is ChatKind.TOP_LEVEL)
        if not matches:
            raise ConfigError(f"No chat matches {ref!r}")
        if len(matches) > 1:
            raise ConfigError(
                f"{ref!r} matches {len(matches)} chats; use more of the id"
            )
        chosen[matches[0].session_id] = matches[0]
    return tuple(chosen.values())


def scan_session_transfer(
    config_path: Path,
    *,
    source_machine: str,
    target_machine: str,
    resolutions_path: Path | None = None,
    progress: ProgressCallback | None = None,
    scope: SessionScope | None = None,
    conflict_policy: str | None = None,
    direction: str | None = None,
) -> TransferPlan:
    """Classify every session branch on both sides. Reads only.

    Runs while Codex is open, like `plan`, but the result is marked volatile and
    a mutation must rebuild it.

    ``scope`` narrows what may be written into `.codex` to one working set
    (`session_scope`). The cloud mirror is written in full regardless, so the
    backup never becomes partial, and a plan without a scope behaves -- and
    hashes -- exactly as it did before working sets existed.

    A divergence is decided by `[conflict] policy` and `sync.direction`
    (D-027), or by ``conflict_policy`` for this one plan; the rule is frozen
    into the plan, so its apply decides the same way. ``direction`` replaces
    `sync.direction` for this one plan the same way.
    """
    cfg = with_run_choices(load_config(config_path), conflict_policy, direction)
    local_dir = locate_local_state_dir(cfg)
    cloud_dir = cfg.paths.cloud_root_dir
    decision = _make_safety_gate(cfg).check(OperationKind.SESSION_SCAN)
    volatile = decision.process_state is not ProcessState.STOPPED

    local_catalog, remote_catalog = scan_both_sides(
        local_dir, cloud_dir, local_machine=source_machine, remote_machine=target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes, volatile=volatile, progress=progress,
    )
    resolutions = load_branch_resolutions(resolutions_path) if resolutions_path else {}
    placements = read_thread_placements(local_dir)
    return build_transfer_plan(
        prefer_catalogued_copies(local_catalog, placements), remote_catalog,
        local_root=local_dir, remote_root=cloud_dir,
        source_machine=source_machine, target_machine=target_machine,
        resolutions=resolutions,
        confirmed_bases=_recorded_bases(cfg),
        agreed_states=_agreed_states(cfg),
        placements=placements,
        layout_id=layout_for_new_chats(cfg.semantic.new_chats),
        mirror_codec=cfg.semantic.mirror_compression,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=volatile,
        scope=scope.session_hashes if scope is not None and not scope.is_empty else None,
        path_rules=cfg.path_mappings,
        folder_exists=local_folder_exists,
        conflict_rule=conflict_rule_for(cfg.conflict.policy, cfg.sync.direction),
        direction=cfg.sync.direction,
    )


def audit_session_index(config_path: Path) -> dict:
    """Report what each side's ``session_index.jsonl`` says. Reads only.

    The index is an append/update journal, so a repeated id is normal and a
    session with no line at all is normal: this never decides that a session
    exists or stopped existing, and nothing here removes or rewrites a line.
    On the machine this was measured against the local index holds 191 records
    for 151 distinct sessions, which is what that journal shape looks like.

    Two things are worth knowing about before an index is ever rewritten, and
    both are reported rather than acted on. A repeated id has two plausible
    readings — last line wins, or greatest ``updated_at`` wins — and they differ
    exactly when a clock ran backwards; disagreement shows up as
    ``REDUCTION_AMBIGUOUS``. And the two sides may hold a different record for
    one session, which is a rename divergence: a decision, not a merge.

    Rendering a new index stays refused until the consumer contract is proven
    (``docs/dev/experiments/session-index-contract.md``), so this command exists to
    say what an index contains and where the two disagree, and nothing more.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    local = parse_session_index(local_dir / SESSION_INDEX_FILE)
    cloud = parse_session_index(cfg.paths.cloud_root_dir / SESSION_INDEX_FILE)

    only_local = sorted(set(local.reduced) - set(cloud.reduced))
    only_cloud = sorted(set(cloud.reduced) - set(local.reduced))
    differing = sorted(
        session_id for session_id in set(local.reduced) & set(cloud.reduced)
        if local.reduced[session_id].digest != cloud.reduced[session_id].digest
    )

    codes: list[str] = []
    if differing:
        codes.append("INDEX_CONFLICT")
    for side in (local, cloud):
        codes.extend(
            code for code in side.codes if code not in {"MISSING_INDEX", "EMPTY_INDEX"}
        )
    if local.contract not in PROVEN_CONTRACTS:
        # Not a fault of this state: it is the standing reason no index is
        # rewritten, reported so the user is never left guessing why.
        codes.append("UNPROVEN_CONSUMER_CONTRACT")

    return {
        "local": _index_side(local),
        "cloud": _index_side(cloud),
        "only_local": len(only_local),
        "only_cloud": len(only_cloud),
        "differing": len(differing),
        # Session ids and thread names are user content and never printed; a
        # divergence is addressed by the same hashed id `merge_session_indexes`
        # uses for a conflict.
        "differing_ids": [
            hashlib.sha256(session_id.encode("utf-8")).hexdigest() for session_id in differing
        ],
        "contract_proven": local.contract in PROVEN_CONTRACTS,
        "codes": list(dict.fromkeys(codes)),
    }


def _index_side(result: IndexParseResult) -> dict:
    return {
        "present": "MISSING_INDEX" not in result.codes,
        "empty": "EMPTY_INDEX" in result.codes,
        "records": len(result.records),
        "sessions": len(result.reduced),
        "contract": result.contract.value,
        "reductions_agree": result.reductions_agree,
        "codes": list(result.codes),
    }


def _recorded_bases(cfg: AppConfig) -> set[str]:
    """Sessions both sides were once known to share, or nothing.

    A base only matters for a decision that rests on ancestry, and the store is
    the only place one is recorded. Failing to read it is not an error: no base
    means `MISSING_BASE`, which is the conservative answer either way.
    """
    try:
        return SemanticStore(cfg.semantic.root_dir, require_guardian_identity(cfg)).confirmed_bases()
    except (OSError, ValueError):
        LOG.debug("no semantic bases are readable; ancestry decisions stay unproven")
        return set()


def _agreed_states(cfg: AppConfig) -> dict[str, str]:
    """The archive state this machine last agreed on, per session, or nothing.

    It tells which side of an archive move moved. Unreadable means this machine
    follows the mirror, whose state another machine vouched for (D-023).
    """
    try:
        return SemanticStore(cfg.semantic.root_dir, require_guardian_identity(cfg)).own_states()
    except (OSError, ValueError):
        LOG.debug("no semantic manifest of this machine is readable; archive moves follow the mirror")
        return {}


def load_branch_resolutions(path: Path) -> dict[str, BranchResolution]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw["resolutions"] if isinstance(raw, dict) else raw
        out: dict[str, BranchResolution] = {}
        for entry in entries:
            resolution = BranchResolution(
                str(entry["conflict_id"]), str(entry["session_hash"]),
                str(entry["local_sha256"]), str(entry["remote_sha256"]),
                ResolutionChoice(entry["choice"]),
            )
            if entry.get("confirmation") != resolution.confirmation:
                raise ValueError("resolution confirmation does not match its contents")
            out[resolution.conflict_id] = resolution
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read branch resolutions {path}: {exc}") from exc
    return out


def record_branch_resolution(
    plan_path: Path,
    *,
    conflict_id: str,
    choice: str,
    output_path: Path,
) -> BranchResolution:
    """Append one versioned choice for a conflict named by a saved plan."""
    try:
        plan = load_transfer_plan(plan_path)
    except ValueError as exc:
        raise ConfigError(f"Cannot read transfer plan {plan_path}: {exc}") from exc
    item = next((entry for entry in plan.items if entry.conflict_id == conflict_id), None)
    if item is None:
        raise ConfigError(f"Plan {plan.plan_id} has no conflict {conflict_id}")
    resolution = BranchResolution(
        conflict_id, item.session_hash, item.local_sha256, item.remote_sha256,
        ResolutionChoice(choice),
    )
    _write_branch_resolutions(output_path, [resolution])
    return resolution


def record_format_migrations(
    plan_path: Path, *, output_path: Path
) -> tuple[list[BranchResolution], list[str]]:
    """Record one decision for every conflict that is only a format rewrite.

    Each keeps the copy in the newer record format, and each is an ordinary
    pinned resolution -- the person makes this decision once, for all of them,
    by running it. A conflict whose older copy holds a later record is left for
    a separate decision. Returns what was recorded and the conflict ids left,
    which name no session and are what `--conflict` takes.
    """
    try:
        plan = load_transfer_plan(plan_path)
    except ValueError as exc:
        raise ConfigError(f"Cannot read transfer plan {plan_path}: {exc}") from exc
    decided, held = format_migration_resolutions(plan)
    if decided:
        _write_branch_resolutions(output_path, decided)
    return decided, sorted(item.conflict_id for item in held if item.conflict_id)


def _write_branch_resolutions(output_path: Path, resolutions: list[BranchResolution]) -> None:
    existing = load_branch_resolutions(output_path) if output_path.exists() else {}
    for resolution in resolutions:
        existing[resolution.conflict_id] = resolution
    payload = {
        "format": "codexsync-branch-resolutions-v1",
        "resolutions": [
            {
                "conflict_id": entry.conflict_id,
                "session_hash": entry.session_hash,
                "local_sha256": entry.local_sha256,
                "remote_sha256": entry.remote_sha256,
                "choice": entry.choice.value,
                "confirmation": entry.confirmation,
            }
            for entry in sorted(existing.values(), key=lambda value: value.conflict_id)
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


#: Blocked actions that stop an apply outright, because each one names a
#: decision the user still has to make. Every other blocked action is a
#: standing limit of what codexSync may write, so it is reported and the rest
#: of the plan still applies. `BLOCKED_INVALID_BRANCH` is one of those: a copy
#: that cannot be read, or one id in two files, is left untouched on both
#: sides, and no recorded choice could fix it -- stopping every apply for it
#: would leave the mirror unwritable while one file on either side is broken.
_UNRESOLVED_TRANSFER_BLOCKS = frozenset({
    TransferAction.BLOCKED_CONFLICT,
    TransferAction.BLOCKED_TARGET_COLLISION,
})


def apply_session_transfer(
    config_path: Path,
    *,
    plan_path: Path,
    confirm_plan: str,
    resolutions_path: Path | None = None,
    dry_run: bool = False,
    origin: str | None = None,
) -> int:
    """Apply one exact session transfer plan with Codex closed.

    A branch is transferred whole. Nothing is appended to a destination file and
    no history is interleaved: the descendant branch replaces the ancestor in
    one atomic step, after the complete backup set exists.

    The plan is rebuilt from the current state and its id must still match. That
    single check covers every way the world could have moved since the scan — a
    branch that grew, a conflict that appeared, a resolution that went stale —
    because the id is computed over all of it.

    An apply is partial by design. A conflict or a target collision stops it,
    because both name a decision the user has to make; a session blocked on an
    unproven layout or a SQLite-held binding does not, because neither is a
    decision anyone can take today, and treating them as one would mean the
    cloud mirror can never be rebuilt while a single such session exists.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    try:
        plan = load_transfer_plan(plan_path)
    except ValueError as exc:
        raise ConfigError(f"Cannot read transfer plan {plan_path}: {exc}") from exc
    if plan.plan_id != confirm_plan:
        raise ConfigError("--confirm-plan must exactly match the saved transfer plan id")
    if plan.volatile:
        raise FailSafeError("A volatile transfer plan cannot be applied; rescan with Codex closed")

    gate = _make_safety_gate(cfg)
    gate.require(OperationKind.SESSION_APPLY)
    initialize_runtime_paths(cfg)
    local_dir = locate_local_state_dir(cfg)
    cloud_dir = cfg.paths.cloud_root_dir

    fresh, local_by_hash, remote_by_hash = _rebuild_transfer_plan(
        cfg, local_dir, cloud_dir, plan, resolutions_path
    )
    if fresh.plan_id != plan.plan_id:
        raise FailSafeError(
            "Session state changed after the scan; rebuild the plan and confirm the new id"
        )

    unresolved = [item for item in plan.blocked_items if item.action in _UNRESOLVED_TRANSFER_BLOCKS]
    if unresolved:
        raise ConflictError(
            f"{len(unresolved)} plan item(s) are blocked on a decision only you can make: "
            + ", ".join(sorted({item.action.value for item in unresolved}))
        )
    deferred = [item for item in plan.blocked_items if item.action not in _UNRESOLVED_TRANSFER_BLOCKS]
    if deferred:
        # These are capability limits, not open questions: no proven layout for
        # a Codex-read directory, or a binding kept in SQLite. Neither can be
        # resolved by the user, and refusing the whole plan for them would mean
        # the mirror could never be written while a single session is blocked.
        LOG.info(
            "session transfer plan %s leaves %d item(s) in place: %s",
            plan.plan_id, len(deferred),
            ", ".join(sorted({item.action.value for item in deferred})),
        )
    copies = _transfer_copy_actions(
        plan, local_dir, cloud_dir, local_by_hash, remote_by_hash
    )
    if not copies.action_count:
        LOG.info("session transfer plan %s has nothing to write", plan.plan_id)
        if not dry_run:
            _record_semantic_manifest(cfg, plan, local_by_hash, remote_by_hash)
        return 0

    if not dry_run:
        # A branch that loses a resolution is about to be overwritten. Its only
        # other copy would be the backup snapshot, and backups are pruned by
        # retention, so the sole surviving record of a divergent history would
        # quietly disappear later. The bundle lives in the semantic root, which
        # retention never touches.
        _bundle_resolved_conflicts(cfg, plan, local_dir, cloud_dir, local_by_hash, remote_by_hash)

    mgr = BackupManager(
        backup_root=cfg.paths.backup_dir,
        machine_id=cfg.identity.machine_id or platform.node(),
        retention_days=cfg.backup.retention_days,
        max_backups=cfg.backup.max_backups,
        compression=cfg.backup.compression,
        journal_root=cfg.paths.temp_dir,
    )
    if dry_run:
        SyncEngine(
            backup_manager=mgr,
            temp_dir=cfg.paths.temp_dir,
            backup_before_overwrite=True,
            fail_on_unknown=True,
        ).execute(copies, dry_run=True)
        return copies.action_count

    machine = cfg.identity.machine_id or platform.node()
    with OperationLock(cfg.paths.temp_dir, state_root=local_dir, machine_id=machine, family="sessions"):
        journals = JournalStore(cfg.paths.temp_dir)
        # The same numbers a settings sync records, so a history row can say
        # how many chats went each way (`to_cloud` is the mirror).
        journal = journals.begin(
            "sessions", plan.plan_id, copies.action_count, backup_snapshot=mgr.snapshot_name,
            counts=sync_plan_counts(copies), origin=origin, machine_id=machine,
        )
        current = [journal]

        def after_backup() -> None:
            current[0] = journals.transition(current[0], JournalState.BACKED_UP)

        def pre_commit() -> None:
            gate.require(OperationKind.SESSION_APPLY, final=True)
            current[0] = journals.transition(current[0], JournalState.COMMITTING)

        engine = SyncEngine(
            backup_manager=mgr,
            temp_dir=cfg.paths.temp_dir,
            backup_before_overwrite=True,
            fail_on_unknown=True,
            pre_commit_check=pre_commit,
            before_replace_check=lambda: gate.require(OperationKind.SESSION_APPLY, final=True),
            after_backup=after_backup,
        )
        try:
            engine.execute(copies, dry_run=False)
            _verify_transferred_branches(cfg, plan, local_dir, cloud_dir)
            current[0] = journals.transition(current[0], JournalState.COMMITTED)
            _record_semantic_manifest(cfg, plan, local_by_hash, remote_by_hash)
            mgr.prune()
        except Exception as exc:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            try:
                current[0] = journals.transition(current[0], failure, failure=exc)
            except Exception:
                LOG.exception("Could not persist terminal session transfer journal state")
            raise
    return copies.action_count


def _rebuild_transfer_plan(
    cfg: AppConfig,
    local_dir: Path,
    cloud_dir: Path,
    plan: TransferPlan,
    resolutions_path: Path | None,
) -> tuple[TransferPlan, dict[str, object], dict[str, object]]:
    local_catalog, remote_catalog = scan_both_sides(
        local_dir, cloud_dir, local_machine=plan.source_machine, remote_machine=plan.target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
    )
    placements = read_thread_placements(local_dir)
    local_catalog = prefer_catalogued_copies(local_catalog, placements)
    fresh = build_transfer_plan(
        local_catalog, remote_catalog,
        local_root=local_dir, remote_root=cloud_dir,
        source_machine=plan.source_machine, target_machine=plan.target_machine,
        resolutions=load_branch_resolutions(resolutions_path) if resolutions_path else {},
        confirmed_bases=_recorded_bases(cfg),
        agreed_states=_agreed_states(cfg),
        placements=placements,
        layout_id=plan.layout_id,
        # The codec the plan was frozen under, not whatever the config says
        # now. The plan is the contract the user confirmed by its id; a config
        # edited afterwards applies to the next scan, and reading it here would
        # rename every destination underneath a confirmation already given.
        mirror_codec=_plan_mirror_codec(plan),
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=False,
        # The working set the plan was confirmed under, never the one stored
        # now: the id covers the scope, so a set edited after the scan applies
        # to the next scan, not to a confirmation already given.
        scope=plan_scope(plan),
        # The rules as they are now: the codes they produce are in the id, so a
        # rule edited after the scan is a changed plan, not a silent one.
        path_rules=cfg.path_mappings,
        folder_exists=local_folder_exists,
        # The rule and direction the plan was confirmed under, like the codec.
        conflict_rule=plan.conflict_rule,
        direction=plan.direction,
    )
    return (
        fresh,
        descriptors_by_session_hash(local_catalog),
        descriptors_by_session_hash(remote_catalog),
    )


def _plan_mirror_codec(plan: TransferPlan) -> JsonlCodec:
    return mirror_codec_for(plan.mirror_layout_id)


def _transfer_copy_actions(
    plan: TransferPlan,
    local_dir: Path,
    cloud_dir: Path,
    local_by_hash: dict,
    remote_by_hash: dict,
) -> SyncPlan:
    """Turn writable plan items into whole-file copies.

    The source is always the descendant branch and is only ever read, so a
    failure at any point leaves both branches intact.

    The container comes from the destination the item names, not from the
    plan-wide mirror layout: a branch the mirror already stores keeps the
    container it is stored in, so within one plan the two can differ.

    A move (`MOVES_BRANCH`, D-023) is a copy to the new path plus the removal of
    the destination side's old file. The engine backs that file up and verifies
    the backup before replacing or removing anything, and removes it only after
    every copy is in place.
    """
    to_local: list[CopyAction] = []
    to_cloud: list[CopyAction] = []
    deletions: list[DeleteAction] = []
    for item in plan.items:
        direction = transfer_direction(item)
        if direction is None:
            continue
        if not item.target_relative_path:
            raise FailSafeError("A writable plan item has no destination path")
        towards_local = direction == "local"
        if towards_local:
            source = remote_by_hash.get(item.session_hash)
            root, bucket = local_dir, to_local
            # A destination the Codex runtime reads is never transformed.
            codec = JsonlCodec.NONE
        else:
            source = local_by_hash.get(item.session_hash)
            root, bucket = cloud_dir, to_cloud
            codec = codec_of(item.target_relative_path) or JsonlCodec.NONE
        if source is None:
            raise FailSafeError("A planned branch is no longer present on its source side")
        destination = root / Path(*item.target_relative_path.split("/"))
        _require_within(destination, root, "session transfer destination")
        source_root = cloud_dir if towards_local else local_dir
        bucket.append(
            CopyAction(
                src=source_root / Path(*source.relative_path.split("/")),
                dst=destination,
                relative_path=item.target_relative_path,
                codec=codec,
            )
        )
        if MOVES_BRANCH in item.codes:
            old = (local_by_hash if towards_local else remote_by_hash).get(item.session_hash)
            if old is None:
                raise FailSafeError("A planned move no longer finds the file it moves")
            if old.relative_path == item.target_relative_path:
                raise FailSafeError("A planned move names the file it moves as its destination")
            old_path = root / Path(*old.relative_path.split("/"))
            _require_within(old_path, root, "session transfer move source")
            deletions.append(DeleteAction(
                path=old_path, relative_path=old.relative_path,
                side="local" if towards_local else "cloud",
            ))
    return SyncPlan(to_local=to_local, to_cloud=to_cloud, deletions=deletions)


def _bundle_resolved_conflicts(
    cfg: AppConfig,
    plan: TransferPlan,
    local_dir: Path,
    cloud_dir: Path,
    local_by_hash: dict,
    remote_by_hash: dict,
) -> list[Path]:
    """Preserve the branches of every conflict this apply resolves.

    A divergence keeps both raw branches. A format rewrite keeps only the branch
    being overwritten, compressed: the other one is what the destination is
    about to hold, and copying all of them was a gigabyte into the cloud.
    """
    resolved = [
        item for item in plan.items
        if "RESOLVED_BY_USER" in item.codes or RESOLVED_BY_RULE in item.codes
    ]
    if not resolved:
        return []
    store = SemanticStore(cfg.semantic.root_dir, require_guardian_identity(cfg))
    bundles: list[Path] = []
    for item in resolved:
        local = local_by_hash.get(item.session_hash)
        remote = remote_by_hash.get(item.session_hash)
        if local is None or remote is None:
            raise FailSafeError("A resolved conflict is missing one of its branches")
        if FORMAT_MIGRATION in item.codes:
            if not item.action.writes:
                # Held back by the layout gate or the working set: nothing is
                # overwritten, so there is nothing to keep yet.
                continue
            losing = (
                cloud_dir / Path(*remote.relative_path.split("/"))
                if item.action is TransferAction.FAST_FORWARD_REMOTE
                else local_dir / Path(*local.relative_path.split("/"))
            )
            kept = store.archive_superseded(
                losing,
                session_id=local.branch_key or remote.branch_key or "",
                reason=FORMAT_MIGRATION,
                codec=cfg.semantic.mirror_compression,
            )
            LOG.info("superseded branch archived for plan %s: %s", plan.plan_id, kept.name)
            bundles.append(kept)
            continue
        bundle = store.conflict_bundle(
            local_dir / Path(*local.relative_path.split("/")),
            cloud_dir / Path(*remote.relative_path.split("/")),
            # The branch key, so the bundle's conflict id is the item's own --
            # for a page of a chat as for the file it began in (CS-356).
            session_id=local.branch_key or remote.branch_key or "",
            common_records=0,
            # The pair the resolution was pinned to, so the bundle holds -- and
            # is named by -- exactly the conflict that was decided.
            expected=(item.local_sha256, item.remote_sha256),
        )
        LOG.info("conflict bundle written for plan %s: %s", plan.plan_id, bundle.name)
        bundles.append(bundle)
    return bundles


def _record_semantic_manifest(
    cfg: AppConfig, plan: TransferPlan, local_by_hash: dict, remote_by_hash: dict
) -> int:
    """Record what each reconciled session now is, and that both sides held it.

    Written after the commit and never allowed to undo one. The manifest is
    bookkeeping that makes a later ancestry decision possible; losing an entry
    costs a `MISSING_BASE` next time, which is exactly where that decision
    already starts, so a failure here is logged rather than raised.
    """
    agreed = [
        item for item in plan.items
        if item.action is TransferAction.NOOP or transfer_direction(item) is not None
    ]
    if not agreed:
        return 0
    try:
        store = SemanticStore(cfg.semantic.root_dir, require_guardian_identity(cfg))
    except (OSError, ValueError):
        LOG.warning("semantic store is unavailable; nothing was recorded")
        return 0
    recorded = 0
    for item in agreed:
        towards_local = transfer_direction(item) == "local"
        # The side that was copied *from* is the one whose bytes both sides now
        # hold, so it is the one that describes the agreed branch.
        source = (remote_by_hash if towards_local else local_by_hash).get(item.session_hash)
        descriptor = source or local_by_hash.get(item.session_hash) or remote_by_hash.get(item.session_hash)
        # Keyed like the item: a page of a chat is an entry of its own.
        session_id = getattr(descriptor, "branch_key", None)
        if not session_id:
            continue
        try:
            store.record(
                session_id,
                state=getattr(getattr(descriptor, "state", None), "value", "ACTIVE"),
                sha256=item.remote_sha256 if towards_local else item.local_sha256,
                record_count=item.remote_records if towards_local else item.local_records,
                byte_count=getattr(descriptor, "byte_count", 0),
                parent_id=getattr(descriptor, "parent_id", None),
                agreed=True,
            )
            recorded += 1
        except (*JSONL_READ_ERRORS, FailSafeError):
            LOG.exception("Could not record a semantic manifest entry for one session")
    LOG.info("recorded %d manifest entr(y/ies) for plan %s", recorded, plan.plan_id)
    return recorded


def _verify_transferred_branches(
    cfg: AppConfig, plan: TransferPlan, local_dir: Path, cloud_dir: Path
) -> None:
    """Re-read what was written and confirm it is the branch that was planned."""
    for item in plan.items:
        direction = transfer_direction(item)
        if direction is None:
            continue
        towards_local = direction == "local"
        root = local_dir if towards_local else cloud_dir
        expected = item.remote_sha256 if towards_local else item.local_sha256
        written = root / Path(*(item.target_relative_path or "").split("/"))
        digest = hashlib.sha256()
        records = 0
        try:
            with open_jsonl(written) as handle:
                for line in handle:
                    records += 1
                    digest.update(line)
        except JSONL_READ_ERRORS as exc:
            # A branch that cannot be read back is a failed commit, not an
            # internal error: say so with the exception the recovery path knows.
            raise FailSafeError(
                f"Transferred branch could not be read back after writing: {exc}"
            ) from exc
        planned_records = item.remote_records if towards_local else item.local_records
        if digest.hexdigest() != expected or records != planned_records:
            raise FailSafeError(
                "Transferred branch does not match the plan after writing; recovery is required"
            )


def inspect_recovery(config_path: Path, operation_id: str) -> MutationJournal:
    cfg = load_config(config_path)
    journal = JournalStore(cfg.paths.temp_dir).load(operation_id)
    if journal.operation_id != operation_id:
        raise FailSafeError("Mutation journal identity does not match the requested operation")
    return journal


def print_plan(plan: SyncPlan, *, volatile: bool = False, direction: str = "bidirectional") -> None:
    print("Plan:")
    if volatile:
        print("  state: VOLATILE (Codex is running or process state is unknown; rebuild before apply)")
    print(f"  direction: {direction}")
    print(f"  to_local: {len(plan.to_local)}")
    print(f"  to_cloud: {len(plan.to_cloud)}")
    if plan.deletions:
        print(f"  deletions: {len(plan.deletions)}")
    if plan.skipped:
        # Named, because a one-way run that copied nothing has to say why, and
        # because these paths stay unsynchronised in the manifest (`D-012`).
        print(f"  skipped by direction: {len(plan.skipped)}")
    print(f"  actions: {plan.action_count}")
    print(f"  conflicts: {len(plan.conflicts)}")
    for rel_path in plan.conflicts:
        print(f"    conflict: {rel_path}")
    for item in plan.to_local:
        print(f"    cloud -> local: {item.relative_path}")
    for item in plan.to_cloud:
        print(f"    local -> cloud: {item.relative_path}")
    for item in plan.deletions:
        print(f"    delete on {item.side}: {item.relative_path}")
    for rel_path in plan.skipped:
        print(f"    skipped: {rel_path}")


def sync_plan_counts(plan: SyncPlan) -> dict[str, int]:
    """What a sync journal records about its plan: numbers per direction, no paths."""
    return {
        "to_cloud": len(plan.to_cloud),
        "to_local": len(plan.to_local),
        "deletions": len(plan.deletions),
    }


def run_sync(ctx: AppContext, dry_run: bool, *, origin: str | None = None) -> None:
    """Apply (or dry-run) the plan in `ctx`.

    `origin` -- ``window``, ``cli`` or ``unattended`` -- is written into the
    journal so a history can say who started the run; it decides nothing.
    """
    # Any conflict left in the plan stops the run, whatever `conflict.policy`
    # says: a policy that resolves one never leaves it here, and what is left
    # (equal times under `manual_abort`, a disputed or mass deletion, a case
    # collision) would otherwise be recorded as agreement (CS-295).
    if ctx.plan.conflicts:
        details = ", ".join(ctx.plan.conflicts)
        if not ctx.config.conflict.report_conflicts:
            details = "hidden by configuration"
        raise ConflictError(f"Conflict detected for files: {details}. Resolve manually and rerun.")

    mgr = BackupManager(
        backup_root=ctx.config.paths.backup_dir,
        machine_id=ctx.config.identity.machine_id or platform.node(),
        retention_days=ctx.config.backup.retention_days,
        max_backups=ctx.config.backup.max_backups,
        compression=ctx.config.backup.compression,
        journal_root=ctx.config.paths.temp_dir,
    )
    if dry_run:
        SyncEngine(
            backup_manager=mgr,
            temp_dir=ctx.config.paths.temp_dir,
            backup_before_overwrite=True,
            fail_on_unknown=True,
        ).execute(ctx.plan, dry_run=True)
        return

    machine = ctx.config.identity.machine_id or platform.node()
    with OperationLock(
        ctx.config.paths.temp_dir,
        state_root=ctx.local_dir,
        machine_id=machine,
        family="sync",
    ):
        journals = JournalStore(ctx.config.paths.temp_dir)
        journal = journals.begin(
            "sync",
            _plan_hash(ctx.plan),
            ctx.plan.action_count,
            backup_snapshot=mgr.snapshot_name,
            counts=sync_plan_counts(ctx.plan),
            origin=origin,
            machine_id=machine,
        )
        current = [journal]

        def after_backup() -> None:
            current[0] = journals.transition(current[0], JournalState.BACKED_UP)

        def pre_commit() -> None:
            ctx.safety_gate.require(OperationKind.SYNC, final=True)
            current[0] = journals.transition(current[0], JournalState.COMMITTING)

        engine = SyncEngine(
            backup_manager=mgr,
            temp_dir=ctx.config.paths.temp_dir,
            backup_before_overwrite=True,
            fail_on_unknown=True,
            pre_commit_check=pre_commit,
            before_replace_check=lambda: ctx.safety_gate.require(OperationKind.SYNC, final=True),
            after_backup=after_backup,
            before_stage_check=lambda: ctx.safety_gate.require(OperationKind.SYNC),
        )
        try:
            engine.execute(ctx.plan, dry_run=False)
            local_idx, cloud_idx = _build_indexes(ctx.config, ctx.local_dir, ctx.cloud_dir)
            # The paths this direction did not act on keep their old entry:
            # recording them now would say the two sides agreed (`D-012`).
            previous = load_manifest(
                ctx.config.state.manifest_file, ctx.config.state.data_version,
                machine_id=sync_machine_id(ctx.config),
            )
            manifest = build_manifest(
                local_idx, cloud_idx, ctx.config.state.data_version,
                previous=previous, skipped=ctx.plan.skipped,
            )
            save_manifest(manifest, ctx.config.state.manifest_file)
            current[0] = journals.transition(current[0], JournalState.COMMITTED)
            mgr.prune()
        except Exception as exc:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            try:
                current[0] = journals.transition(current[0], failure, failure=exc)
            except Exception:
                LOG.exception("Could not persist terminal mutation journal state")
            raise


# --- handing work between machines (CS-328, `D-018`) -------------------------

#: How often a load looks again while the cloud is still delivering.
HANDOFF_DELIVERY_POLL_SECONDS = 15.0
#: How often the watcher asks whether Codex is running.
HANDOFF_WATCH_POLL_SECONDS = 10.0


#: Blocked actions that leave a newer chat in the mirror and not in `.codex`.
#: Each is a standing limit (`PROVEN_LAYOUTS` unless `[semantic] new_chats`
#: says otherwise, the SQLite thread catalogue), not a decision, so it does not
#: stop a handoff -- but it is counted.
_CHATS_LEFT_IN_THE_MIRROR = frozenset({
    TransferAction.BLOCKED_UNPROVEN_LAYOUT,
    TransferAction.BLOCKED_UNSUPPORTED_BACKEND,
    TransferAction.BLOCKED_INVALID_BRANCH,
})


class HandoffNotDelivered(FailSafeError):
    """Another machine's handoff has not fully arrived in the cloud copy."""

    def __init__(self, deliveries: Sequence[Delivery]) -> None:
        self.deliveries = tuple(deliveries)
        waiting = ", ".join(
            f"{item.machine} ({item.arrived} of {item.total} files)" for item in self.deliveries
        )
        super().__init__(
            f"The handoff from {waiting} has not fully arrived in the cloud copy; nothing was "
            "loaded. Wait for the cloud client, or pass --accept-undelivered if you know it "
            "never will."
        )


#: The steps of a full sync that may fail without failing it, in run order.
HANDOFF_STEPS = ("projects", "chat_names", "catalogue")


@dataclass(frozen=True, slots=True)
class HandoffResult:
    machine: str
    #: Machines whose handoff this run took.
    taken: tuple[str, ...]
    #: Files the settings sync wrote (both directions).
    sync_actions: int
    #: Session branches written (both directions).
    session_actions: int
    #: Whether this run wrote to the cloud copy, i.e. handed something off.
    handed_off: bool
    record: HandoffRecord
    waited_seconds: float = 0.0
    #: Chats the cloud copy holds newer than `.codex` that could not be put
    #: there: no proven layout, or a thread the catalogue does not place.
    #: Reported, because "loaded" must not be said of work that stayed behind.
    chats_not_loaded: int = 0
    #: Of those, chats this machine never held that stayed in the cloud copy
    #: only because `[semantic] new_chats` is `keep_in_cloud` -- the one part
    #: of `chats_not_loaded` a setting changes (CS-347).
    new_chats_kept_in_cloud: int = 0
    #: Chats this machine never held that were written into `.codex` because
    #: `[semantic] new_chats = "same_path"` (D-020). Codex shows them only once
    #: it takes the files up, which `doctor` reports as `session_visibility`.
    new_chats_written: int = 0
    #: Chats changed on both machines that the conflict rule decided (D-027);
    #: the copy not kept of each is in the conflict bundle.
    chats_decided_by_rule: int = 0
    #: Projects another machine had that were added here (CS-333).
    projects_added: int = 0
    #: Changes written into the project list: added projects, pins, order, bindings.
    project_changes: int = 0
    #: Added projects whose folder does not exist on this machine.
    projects_missing_folders: int = 0
    #: Chat files Codex's catalogue did not list, which it was asked to take
    #: up on its next start (D-024).
    chats_codex_will_list: int = 0
    #: Chat files Codex still does not list although it was asked once before.
    chats_codex_ignores: int = 0
    #: Chat names taken from other machines (D-025).
    chat_names_set: int = 0
    #: Chats named here that another machine names differently; local kept.
    chat_names_kept: int = 0
    #: Names waiting for Codex to list their chat here; set on a later sync.
    chat_names_waiting: int = 0
    #: Projects whose folder here holds less than another machine had (D-026).
    project_files_behind: tuple["ProjectFilesItem", ...] = ()
    #: Steps after the settings and chats that failed this time and were
    #: logged: ``projects``, ``chat_names``, ``catalogue`` (`HANDOFF_STEPS`).
    #: The handoff itself happened; the next sync does them again.
    steps_not_done: tuple[str, ...] = ()
    #: Codex was open and quit when asked (`[sync] close_codex`, D-029).
    codex_closed: bool = False
    #: The other machine the chats were paired with, so a page can open
    #: Sessions for exactly this pair.
    source: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffStatus:
    """What `handoff status` and the window say. Reads only."""

    machine: str
    root: Path
    enabled: bool
    own: HandoffRecord | None
    others: tuple[HandoffRecord, ...]
    #: Machines whose last handoff this machine has not taken.
    pending: tuple[str, ...]
    #: Machines marked working that have not handed off since.
    working_elsewhere: tuple[str, ...]
    #: Per pending machine, how much has arrived; empty unless asked for.
    deliveries: tuple[Delivery, ...] = ()
    unreadable: tuple[str, ...] = ()


def _handoff_root(cfg: AppConfig) -> Path:
    root = cfg.handoff.root_dir
    if root is None:
        raise ConfigError("handoff.root_dir is empty: choose a folder inside the synced workspace")
    return root


def _handoff_machine(cfg: AppConfig) -> str:
    try:
        return machine_key(require_guardian_identity(cfg))
    except HandoffError as exc:
        raise ConfigError(str(exc)) from exc


def handoff_status(config_path: Path, *, check_delivery: bool = False) -> HandoffStatus:
    """Every machine's handoff as this machine sees it. Reads only.

    ``check_delivery`` hashes the cloud copy to say how much of each pending
    handoff has arrived; it reads every mirrored file, so it is asked for.
    """
    cfg = load_config(config_path)
    root = _handoff_root(cfg)
    machine = _handoff_machine(cfg)
    board = read_board(root)
    pending = board.pending(machine)
    deliveries: tuple[Delivery, ...] = ()
    if check_delivery and pending:
        current = Fingerprinter().files(cfg)
        deliveries = tuple(delivery(record, current) for record in pending)
    return HandoffStatus(
        machine=machine,
        root=root,
        enabled=bool(cfg.handoff.enabled),
        own=board.own(machine),
        others=tuple(board.others(machine)),
        pending=tuple(record.machine for record in pending),
        working_elsewhere=tuple(record.machine for record in board.working_elsewhere(machine)),
        deliveries=deliveries,
        unreadable=tuple(sorted(board.unreadable)),
    )


def _wait_for_delivery(
    cfg: AppConfig,
    pending: Sequence[HandoffRecord],
    fingerprints: Fingerprinter,
    *,
    wait_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    on_wait: Callable[[Sequence[Delivery]], None] | None,
) -> float:
    """Return once every pending handoff is in the cloud copy; the seconds waited."""
    started = monotonic()
    while True:
        current = fingerprints.files(cfg)
        late = [item for item in (delivery(record, current) for record in pending) if not item.delivered]
        waited = monotonic() - started
        if not late:
            return waited
        if waited >= wait_seconds:
            raise HandoffNotDelivered(late)
        LOG.info(
            "waiting for the cloud to deliver: %s",
            ", ".join(f"{item.machine} {item.arrived}/{item.total}" for item in late),
        )
        if on_wait is not None:
            on_wait(late)
        sleep(min(poll_seconds, wait_seconds - waited))


def _handoff_source(board: Board, machine: str, pending: Sequence[HandoffRecord]) -> str:
    """The other machine of the chat transfer's pair, as the window names it.

    The pair keys the working set and the recorded decisions, so a handoff
    uses the ones the person chose for that machine. Before any other machine
    has written a handoff there is no pair; this machine stands in for it,
    which maps no paths and matches no stored choice.
    """
    if pending:
        return pending[-1].machine
    others = sorted(board.others(machine), key=lambda record: record.generation)
    return others[-1].machine if others else machine


@dataclass(frozen=True, slots=True)
class KnownMachines:
    """Machine names a person may pick, and the one a full sync would pair with."""

    #: This machine, both ends of every `[[path_mappings]]` rule, and every
    #: machine that left a trace in the shared workspace: a handoff record, a
    #: project publication or a semantic manifest directory.
    names: tuple[str, ...]
    #: The other machine of the chat transfer's pair, as `run_handoff` picks
    #: it; ``None`` before any other machine has handed off (CS-345).
    usual_source: str | None = None


def known_machines(config_path: Path) -> KnownMachines:
    """Every machine name this config or the shared workspace knows. Reads only.

    A config without `[[path_mappings]]` names only this machine, which left
    the Sessions page with no other machine to compare with unless it was
    opened from a stopped sync (CS-345). The traces in the workspace are what
    the full sync itself uses, so both name the same pair. A trace that cannot
    be read is skipped: this is a list of choices, never a decision.
    """
    cfg = load_config(config_path)
    own = cfg.identity.machine_id
    names: set[str] = {own} if own else set()
    for rule in cfg.path_mappings:
        names.update((rule.source_machine, rule.target_machine))
    usual: str | None = None
    try:
        machine = _handoff_machine(cfg)
        board = read_board(_handoff_root(cfg))
        names.update(board.records)
        source = _handoff_source(board, machine, board.pending(machine))
        usual = source if source != machine else None
    except (ConfigError, OSError):
        LOG.debug("no handoff board is readable; machines come from the config only")
    traces: list[Path] = []
    try:
        traces.extend(path for path in projects_root(cfg).glob("*.json") if path.is_file())
    except (ConfigError, OSError):
        pass
    try:
        manifest = cfg.semantic.root_dir / "manifest"
        traces.extend(path for path in manifest.iterdir() if path.is_dir())
    except OSError:
        pass
    names.update(path.stem for path in traces if not path.name.startswith("."))
    return KnownMachines(tuple(sorted(name for name in names if name)), usual)


def session_pair_name(source_machine: str, target_machine: str) -> str:
    """How files kept per pair of machines are named (plans, recorded decisions).

    One function for the window and the full sync: decisions recorded on the
    Sessions page are only found by the sync if both spell the pair alike.
    """
    return f"{_scope_file_safe(source_machine)}-{_scope_file_safe(target_machine)}"


def run_handoff(
    config_path: Path,
    *,
    origin: str = "handoff",
    wait_seconds: float | None = None,
    accept_undelivered: bool = False,
    poll_seconds: float = HANDOFF_DELIVERY_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[Sequence[Delivery]], None] | None = None,
    progress: ProgressCallback | None = None,
    conflict_policy: str | None = None,
    direction: str | None = None,
    close_codex: bool = True,
) -> HandoffResult:
    """One full sync with nobody deciding anything, then the handoff record.

    Conflicts of both kinds -- settings files and chats -- are decided by
    `[conflict] policy` and `sync.direction` (D-027), or by
    ``conflict_policy`` for this run, and chats are applied by the plan's own
    id in the same process (`D-018`). What the rule leaves open -- `manual_abort`,
    two copies `newer` cannot order, a target collision -- stops the run before
    the first write, and the handoff record is written only when both halves
    finished: a record that claimed a handoff which did not happen is exactly
    the lie the other machine would act on.

    Other machines' handoffs are waited for first, up to ``wait_seconds``
    (``[handoff] delivery_wait_minutes``); loading half of one is how a file
    the cloud client was still writing would be copied into `.codex`.
    """
    cfg = load_config(config_path)
    root = _handoff_root(cfg)
    machine = _handoff_machine(cfg)
    _require_mutation_compatible_config(cfg)
    # `[sync] close_codex` (D-029): asked once, never forced. The watcher
    # passes ``close_codex=False`` -- it reacts to Codex closing and must
    # never close it under someone who just opened it.
    closed = close_codex_for_sync(config_path) if close_codex else None
    # Waiting for the cloud is pointless while Codex is open: every write that
    # follows would be refused anyway.
    _make_safety_gate(cfg).require(OperationKind.SYNC)

    board = read_board(root)
    for name, reason in sorted(board.unreadable.items()):
        LOG.warning("handoff file %s is not believed: %s", name, reason)
    pending = board.pending(machine)
    fingerprints = Fingerprinter()
    if wait_seconds is None:
        wait_seconds = float(cfg.handoff.delivery_wait_minutes) * 60.0
    waited = 0.0
    if pending:
        try:
            waited = _wait_for_delivery(
                cfg, pending, fingerprints, wait_seconds=wait_seconds, poll_seconds=poll_seconds,
                monotonic=monotonic, sleep=sleep, on_wait=on_wait,
            )
        except HandoffNotDelivered as exc:
            if not accept_undelivered:
                raise
            LOG.warning("loading anyway, as asked: %s", exc)

    # Chats first, and read-only: a decision found here stops the run before
    # the settings sync has written anything.
    source, plan, resolutions_path = _full_sync_chat_plan(
        config_path, cfg, board, machine, pending, progress=progress, conflict_policy=conflict_policy,
        direction=direction,
    )
    plans = _plans_dir(cfg)
    pair = session_pair_name(source, machine)
    if plan.volatile:
        raise SafetyPreconditionError("Codex started while the chats were being read; nothing was written")
    # Exactly what the Sessions page lists under "needs a decision", and what
    # the apply below would refuse: an archive move is applied (D-023), so it
    # stops nothing (CS-349).
    unresolved = [item for item in plan.blocked_items if item.action in _UNRESOLVED_TRANSFER_BLOCKS]
    if unresolved:
        rewrites = [
            item for item in unresolved
            if item.action is TransferAction.BLOCKED_CONFLICT and FORMAT_MIGRATION in item.codes
        ]
        held = sum(1 for item in rewrites if OLDER_FORMAT_HAS_LATER_RECORDS in item.codes)
        raise ChatDecisionsNeeded(
            source=source, target=machine,
            format_migrations=len(rewrites) - held,
            held_migrations=held,
            divergences=sum(
                1 for item in unresolved
                if item.action is TransferAction.BLOCKED_CONFLICT and FORMAT_MIGRATION not in item.codes
            ),
            collisions=sum(1 for item in unresolved if item.action is TransferAction.BLOCKED_TARGET_COLLISION),
        )
    plan_path = plans / f"handoff-sessions-plan-{pair}.json"
    save_transfer_plan(plan, plan_path)

    report_progress(progress, "sync_settings", 0, 0)
    ctx = build_context(config_path, enforce_safety=True, conflict_policy=conflict_policy, direction=direction)
    run_sync(ctx, dry_run=False, origin=origin)
    report_progress(progress, "sync_chats", 0, 0)
    session_actions = apply_session_transfer(
        config_path, plan_path=plan_path, confirm_plan=plan.plan_id, resolutions_path=resolutions_path,
        origin=origin,
    )
    # Projects, names and the catalogue come after both halves and are each
    # applied by the plan they build. Each is re-done by the next sync, so one
    # that fails -- a peer's file arriving mid-run, Codex starting, a locked
    # database -- is logged and reported, and never costs the handoff record
    # of the settings and chats already written.
    skipped: list[str] = []

    def step(name: str, call: Callable[[], object]):
        try:
            return call()
        except ProjectsNotCarried as exc:
            LOG.warning("projects were not carried: %s", exc)
        except Exception:
            LOG.exception("full sync: %s was not done; the next sync tries again", name)
            skipped.append(name)
        return None

    # Projects last: a chat binding may name a chat the transfer just wrote.
    # A merge never needs a person (an ambiguous project is left alone).
    report_progress(progress, "sync_projects", 0, 0)
    projects = step("projects", lambda: sync_projects(config_path, origin=origin, planned_here=True))
    # Names live only in Codex's catalogue, never in the chat file (D-025):
    # set the other machines' names on chats Codex already lists here, and
    # publish this machine's.
    report_progress(progress, "sync_chat_names", 0, 0)
    names = step("chat_names", lambda: sync_chat_names(config_path, origin=origin, planned_here=True))
    # Chat files Codex's catalogue does not list -- written now or by an
    # earlier run -- are invisible until Codex takes them up (D-024).
    report_progress(progress, "refresh_catalogue", 0, 0)
    catalogue = step(
        "catalogue", lambda: refresh_thread_catalogue(config_path, origin=origin, planned_here=True),
    )
    # Chats are carried, project folders are not (D-026): say which projects
    # here hold less than another machine had, and publish this one's. A
    # check that fails says so in the log and never undoes a finished sync.
    report_progress(progress, "check_project_files", 0, 0)
    try:
        files_report = check_project_files(config_path, publish=True)
    except Exception:
        LOG.exception("project files were not checked")
        files_report = None
    to_cloud = len(ctx.plan.to_cloud) + sum(
        1 for item in plan.items if transfer_direction(item) == "mirror"
    )
    # A run that wrote nothing to the cloud hands nothing off: a new id would
    # make every other machine wait for a "delivery" of what it already has.
    handed_off = to_cloud > 0
    record = record_handoff(
        root, machine, files=fingerprints.files(cfg), taken=pending, new_handoff=handed_off,
    )
    LOG.info(
        "handoff finished: %d file(s) and %d chat(s) written; took %s; %s",
        ctx.plan.action_count, session_actions,
        ", ".join(item.machine for item in pending) or "nothing",
        f"handed off {record.handoff_id}" if handed_off else "nothing new to hand off",
    )
    left_behind = sum(1 for item in plan.blocked_items if item.action in _CHATS_LEFT_IN_THE_MIRROR)
    new_chats = sum(
        1 for item in plan.items
        if item.action is TransferAction.FAST_FORWARD_LOCAL and NEW_CHAT_SAME_PATH in item.codes
    )
    return HandoffResult(
        machine=machine,
        source=source,
        projects_added=len(projects.plan.added) if projects else 0,
        project_changes=projects.written if projects else 0,
        projects_missing_folders=len(projects.plan.missing_folders) if projects else 0,
        # Asked now, or Codex has not run its own backfill since: either way
        # it walks the files on its next start.
        chats_codex_will_list=(
            len(catalogue.plan.unnamed)
            if catalogue and (catalogue.refreshed or catalogue.plan.status is RefreshStatus.ALREADY_PENDING)
            else 0
        ),
        chats_codex_ignores=(
            len(catalogue.plan.unnamed)
            if catalogue and catalogue.plan.status is RefreshStatus.ALREADY_ASKED else 0
        ),
        chat_names_set=names.written if names else 0,
        chat_names_kept=names.plan.kept if names else 0,
        chat_names_waiting=names.plan.waiting if names else 0,
        steps_not_done=tuple(skipped),
        codex_closed=bool(closed and closed.accepted),
        project_files_behind=files_report.warnings if files_report else (),
        chats_not_loaded=left_behind,
        new_chats_kept_in_cloud=sum(
            1 for item in plan.items if item.action is TransferAction.BLOCKED_UNPROVEN_LAYOUT
            and "SESSION_ON_ONE_SIDE_ONLY" in item.codes
        ),
        new_chats_written=new_chats,
        chats_decided_by_rule=sum(
            1 for item in plan.items if item.action.writes and RESOLVED_BY_RULE in item.codes
        ),
        taken=tuple(item.machine for item in pending),
        sync_actions=ctx.plan.action_count,
        session_actions=session_actions,
        handed_off=handed_off,
        record=record,
        waited_seconds=waited,
    )


def _full_sync_chat_plan(
    config_path: Path,
    cfg: AppConfig,
    board: Board,
    machine: str,
    pending: Sequence[HandoffRecord],
    *,
    progress: ProgressCallback | None,
    conflict_policy: str | None,
    direction: str | None = None,
):
    """The chat plan a full sync applies: the pair, resolutions and working set it uses.

    One place, so a preview (`preview_full_sync`) shows the very plan
    `run_handoff` would build. Returns ``(source, plan, resolutions_path)``.
    """
    source = _handoff_source(board, machine, pending)
    pair = session_pair_name(source, machine)
    resolutions = _plans_dir(cfg) / f"sessions-resolutions-{pair}.json"
    resolutions_path = resolutions if resolutions.is_file() else None
    stored_scope = read_working_set(config_path, source_machine=source, target_machine=machine)
    scope = None if stored_scope.is_empty else build_working_set(
        config_path, projects=stored_scope.projects, chats=stored_scope.chats,
    )
    plan = scan_session_transfer(
        config_path, source_machine=source, target_machine=machine,
        resolutions_path=resolutions_path, progress=progress, scope=scope,
        conflict_policy=conflict_policy, direction=direction,
    )
    return source, plan, resolutions_path


@dataclass(frozen=True, slots=True)
class FullSyncPreview:
    """What a full sync would do now, built read-only (`sync --dry-run`, D-028)."""

    machine: str
    #: The machine whose chats are paired with this one's.
    source: str
    files: AppContext
    chats: TransferPlan
    #: ``None`` when this state's project list cannot be carried.
    projects: "ProjectSyncResult | None"
    #: Other machines' handoffs not fully in the cloud copy yet; a real run
    #: waits for them first.
    waiting_for: tuple[str, ...] = ()


def preview_full_sync(
    config_path: Path,
    *,
    conflict_policy: str | None = None,
    direction: str | None = None,
    progress: ProgressCallback | None = None,
) -> FullSyncPreview:
    """Everything `run_handoff` would plan, and nothing written.

    Codex must be closed, as for the run itself: a preview taken while it
    writes would describe a state the run will not find.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    machine = _handoff_machine(cfg)
    board = read_board(_handoff_root(cfg))
    pending = board.pending(machine)
    files = build_context(config_path, enforce_safety=True, conflict_policy=conflict_policy, direction=direction)
    source, chats, _ = _full_sync_chat_plan(
        config_path, cfg, board, machine, pending, progress=progress, conflict_policy=conflict_policy,
        direction=direction,
    )
    try:
        projects = sync_projects(config_path)
    except ProjectsNotCarried as exc:
        LOG.warning("projects would not be carried: %s", exc)
        projects = None
    current = Fingerprinter().files(cfg) if pending else {}
    waiting = tuple(
        item.machine for item in (delivery(record, current) for record in pending) if not item.delivered
    )
    return FullSyncPreview(machine, source, files, chats, projects, waiting)


#: How long a sync waits for Codex to be gone after asking it to quit.
CLOSE_CODEX_WAIT_SECONDS = 60.0


def close_codex_for_sync(
    config_path: Path,
    *,
    ask: Callable[[], CloseOutcome] = ask_codex_to_quit,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_seconds: float = CLOSE_CODEX_WAIT_SECONDS,
) -> CloseOutcome | None:
    """When `[sync] close_codex` is on and Codex is open, ask it to quit (D-029).

    ``None`` when nothing was asked: the setting is off, or Codex is not
    running. Otherwise the app is asked once -- never forced -- and this waits
    until the process check sees it stopped. If it declines, or is still
    running after ``wait_seconds``, the sync is refused here with why, and
    nothing has been written. The gate inside every write checks again anyway.
    """
    cfg = load_config(config_path)
    if not cfg.sync.close_codex:
        return None
    gate = _make_safety_gate(cfg)
    if gate.check(OperationKind.SYNC).process_state is not ProcessState.RUNNING:
        return None
    outcome = ask()
    LOG.warning("Codex is open and [sync] close_codex is on; asked it to quit: %s", outcome.detail)
    if not outcome.accepted:
        raise SafetyPreconditionError(f"Codex is open and was not closed: {outcome.detail}. Nothing was written.")
    deadline = monotonic() + wait_seconds
    while gate.check(OperationKind.SYNC).process_state is not ProcessState.STOPPED:
        if monotonic() >= deadline:
            raise SafetyPreconditionError(
                f"Codex was asked to quit but is still running after {int(wait_seconds)} s; "
                "close it from the tray. Nothing was written."
            )
        sleep(1.0)
    LOG.warning("Codex quit when asked; the sync goes on")
    return outcome


def _codex_state(config_path: Path) -> ProcessState:
    return _make_safety_gate(load_config(config_path)).check(OperationKind.PLAN).process_state


def watch_handoff(
    config_path: Path,
    *,
    probe: Callable[[], ProcessState] | None = None,
    notifier: Callable[..., object] | None = None,
    handoff: Callable[..., HandoffResult] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = HANDOFF_WATCH_POLL_SECONDS,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run from sign-in: load while Codex is closed, hand off when it closes.

    Only a state the process check is sure of counts. ``UNKNOWN`` is neither
    a start nor a close, so a failed process listing can never trigger a
    sync -- and the gate inside every write checks again anyway.

    - At start, with Codex closed: load (waiting for the cloud to deliver).
    - Codex starts: mark this machine as working, and warn when another
      machine is still working or its handoff has not been taken here.
    - Codex closes: hand off.

    Nothing here ends the loop: an error is logged and reported, and the
    watcher goes on waiting for the next change.
    """
    cfg = load_config(config_path)
    root = _handoff_root(cfg)
    machine = _handoff_machine(cfg)
    notify = notifier if notifier is not None else Notifier(enabled=bool(cfg.handoff.notify))
    sample = probe if probe is not None else (lambda: _codex_state(config_path))
    # The watcher never asks Codex to quit: it acts when Codex closes, and at
    # start a Codex that is open is the person's, not a sync's (D-029).
    act = handoff if handoff is not None else (lambda path: run_handoff(path, close_codex=False))

    def attempt(closing: bool) -> None:
        try:
            result = act(config_path)
        except HandoffNotDelivered as exc:
            LOG.warning("%s", exc)
            late = exc.deliveries[0]
            notify("notify.not_delivered", machine=late.machine, arrived=late.arrived, total=late.total)
        except SafetyPreconditionError:
            LOG.info("Codex is open; the handoff waits for it to close")
            if not closing:
                notify("notify.codex_open")
        except ConflictError as exc:
            LOG.warning("handoff stopped: %s", exc)
            notify("notify.conflict")
        except Exception as exc:  # noqa: BLE001 - the watcher outlives any one run
            LOG.exception("handoff failed")
            notify("notify.failed", reason=type(exc).__name__)
        else:
            if result.taken and result.chats_not_loaded:
                notify(
                    "notify.loaded_partial", machine=", ".join(result.taken), count=result.chats_not_loaded,
                )
            elif result.taken:
                notify("notify.loaded", machine=", ".join(result.taken))
            elif closing and result.handed_off:
                notify("notify.handed_off")

    def codex_started() -> None:
        try:
            mark_working(root, machine)
            board = read_board(root)
        except (OSError, HandoffError) as exc:
            LOG.warning("could not record that Codex is running here: %s", exc)
            return
        for record in board.working_elsewhere(machine):
            notify("notify.working_elsewhere", machine=record.machine, since=record.state_since_utc)
        for record in board.pending(machine):
            if record.state != STATE_WORKING:
                notify("notify.not_taken", machine=record.machine, since=record.handed_off_at_utc)

    last = sample()
    if last is ProcessState.STOPPED:
        attempt(closing=False)
    elif last is ProcessState.RUNNING:
        codex_started()
    else:
        last = None
    while not should_stop():
        sleep(poll_seconds)
        state = sample()
        if state is ProcessState.UNKNOWN:
            continue
        if state is ProcessState.RUNNING and last is not ProcessState.RUNNING:
            codex_started()
        elif state is ProcessState.STOPPED and last is ProcessState.RUNNING:
            attempt(closing=True)
        last = state


def validate_config_only(config_path: Path) -> tuple[str, ...]:
    """Refuse a config that loads but that every mutating command would refuse.

    `validate` used to stop at loading, so it said "valid" for a 0.1 config
    on which sync, restore, repair and recover all exit 4 (CS-325). What is
    valid but limits a command is returned as notes: a config without
    `targets.include_roots` serves Guardian and diagnostics, and `sync`
    refuses it (CS-319).
    """
    cfg = load_config(config_path)
    _ = locate_state_dirs(cfg)
    _require_mutation_compatible_config(cfg)
    notes: list[str] = []
    if not cfg.targets.listed:
        notes.append(
            "targets.include_roots is not set: sync has nothing to synchronise and refuses to run"
        )
    return tuple(notes)


@dataclass(frozen=True, slots=True)
class AutomationRun:
    """One run of the configured safe job, performed now, in this process."""

    mode: str
    #: guardian_snapshot: the Guardian outcome status; preflight: PASSED or
    #: FAILED; sync_dry_run: DRY_RUN_FINISHED.
    status: str
    detail: str = ""
    actions: int | None = None


def run_automation_job(config_path: Path) -> AutomationRun:
    """Run the job `[scheduler]` names, exactly as the scheduled command would.

    Each branch calls what the corresponding CLI command calls, so a "run now"
    button and the scheduled task cannot disagree about what the job does or
    about when it is refused -- a dry-run sync still needs Codex closed.
    """
    cfg = load_config(config_path)
    mode = cfg.scheduler.mode
    if mode == "guardian_snapshot":
        outcome = build_guardian_runner(config_path).once()
        return AutomationRun(mode, outcome.status.value, outcome.detail or "")
    if mode == "preflight":
        report = run_preflight(config_path, operation=OperationKind.SYNC)
        failed = ", ".join(item.name for item in report.failures)
        return AutomationRun(mode, "PASSED" if report.is_ok else "FAILED", failed)
    if mode == "sync_dry_run":
        ctx = build_context(config_path, enforce_safety=True)
        run_sync(ctx, dry_run=True)
        return AutomationRun(mode, "DRY_RUN_FINISHED", actions=ctx.plan.action_count)
    raise ConfigError(f"scheduler.mode {mode!r} cannot be run")


def create_codex_backup(
    config_path: Path,
    *,
    wait: bool = False,
    progress: ProgressCallback | None = None,
) -> StateBackupResult:
    """Take one copy of the valuable part of `.codex` (`[state_backup]`, CS-276).

    Gated like a mutation because a copy of files that are being written is a
    copy of no moment; with ``wait`` it waits for Codex to close instead of
    refusing, which is what the scheduled task passes.
    """
    cfg = load_config(config_path)
    return create_state_backup(cfg, gate=_make_safety_gate(cfg), wait=wait, progress=progress)


def remember_state_stats(config_path: Path, directory: ChatDirectory) -> StateStats:
    """Keep a chat scan's counts for the Home page (CS-275). Best effort, counts only."""
    stats = state_stats_from_directory(directory)
    write_state_stats(config_path, stats)
    return stats


def recount_state(config_path: Path, *, progress: ProgressCallback | None = None) -> StateStats:
    """Scan the chats now and keep the counts. Reads `.codex` only."""
    return remember_state_stats(config_path, scan_chats(config_path, progress=progress))


def list_codex_backups(config_path: Path) -> list[StateBackupEntry]:
    """Copies in `[state_backup] root_dir`, newest first. Reads names and sizes."""
    return list_state_backups(load_config(config_path))


def accept_guardian_baseline(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
) -> tuple[GuardianAcceptPlan, str | None]:
    """Preview, or perform, accepting the current state as Guardian's new baseline.

    Returns the plan and, once accepted, the id of the snapshot that became
    latest-good. Both halves run while Codex is open: the state is only read
    and the only writes land in the Guardian root, exactly as for a snapshot.
    What makes it a decision rather than a snapshot is the plan id, which pins
    the drop being accepted (``guardian_accept``).
    """
    runner = build_guardian_runner(config_path)
    if confirm_plan is None:
        return runner.preview_accept(), None
    plan, snapshot = runner.accept(confirm_plan=confirm_plan)
    return plan, snapshot.snapshot_id


def _observe_global_state(cfg: AppConfig, config_path: Path, local_dir: Path):
    """The live global state for a read-only scan, or a message worth reading.

    `StableReader` can only say "this path holds no file". Which config named
    that path is the other half of the sentence, and without it a scan that
    opened the wrong config is indistinguishable from a machine where Codex was
    never installed -- the two need opposite fixes (CS-261).
    """
    source = local_dir / ".codex-global-state.json"
    try:
        return StableReader(source, max_bytes=cfg.guardian.max_state_bytes).read_once().observation
    except SourceMissingError as exc:
        raise ConfigError(
            f"No Codex global state at {source}. "
            f"paths.local_state_dir in {config_path} points at {local_dir}. "
            "Either Codex is not installed for this user, or this is not the config you meant."
        ) from exc
    except SourceTooLargeError as exc:
        raise ConfigError(
            f"{source} is larger than guardian.max_state_bytes ({cfg.guardian.max_state_bytes}) in {config_path}"
        ) from exc
    except SourceUnstableError as exc:
        raise FailSafeError(f"{source} kept changing while it was read; close Codex or try again") from exc


def _read_live_state(source: Path, config_path: Path) -> bytes:
    """The live global state for a mutation, with its failures mapped to exit codes.

    A bare `read_bytes` let a missing or locked file escape as a traceback and
    exit 1 (CS-325). Missing is a configuration answer -- which config named
    this path is the useful half of the message -- and anything else is a
    fail-safe stop, never a guess.
    """
    try:
        return source.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"No Codex global state at {source}, the folder {config_path} names. "
            "Either Codex is not installed for this user, or this is not the config you meant."
        ) from exc
    except OSError as exc:
        raise FailSafeError(f"Cannot read {source}: {exc}") from exc


def _read_state_for_preview(cfg: AppConfig, source: Path) -> bytes | None:
    """The live global state as a preview sees it: a stable read, or nothing."""
    if not source.is_file():
        return None
    return StableReader(source, max_bytes=cfg.guardian.max_state_bytes).read_once().observation.payload


def restore_global_state(
    config_path: Path,
    *,
    snapshot_id: str,
    confirm_plan: str | None = None,
    dry_run: bool = False,
) -> tuple[GuardianRestorePlan, int]:
    """Preview, or perform, putting a verified Guardian snapshot back.

    Without ``confirm_plan`` this reads only and works while Codex runs. With
    it, Codex must be closed, the plan is rebuilt from the state as it is now
    and must still carry the confirmed id, and the write goes through
    ``commit_global_state`` -- the replaced file is backed up and verified
    first, and restored if the result does not validate.

    A missing state file is refused rather than created: the envelope's first
    step is a verified backup of what it replaces, and there is nothing to back
    up. Starting Codex once recreates the file; restore over that.
    """
    cfg = load_config(config_path)
    machine = require_guardian_identity(cfg)
    local_dir = locate_local_state_dir(cfg)
    source = local_dir / ".codex-global-state.json"
    root = cfg.guardian.root_dir
    if confirm_plan is None:
        current = _read_state_for_preview(cfg, source)
        plan, _ = build_guardian_restore_plan(
            root_dir=root, machine_id=machine, snapshot_id=snapshot_id, current_state=current
        )
        return plan, 0

    _require_mutation_compatible_config(cfg)
    gate = _make_safety_gate(cfg)
    gate.require(OperationKind.GUARDIAN_RESTORE)
    if not source.is_file():
        raise FailSafeError(
            f"There is no {source.name} to replace, so no verified backup of it can be made. "
            "Start Codex once so it writes the file, close it, and restore again."
        )
    current = source.read_bytes()
    plan, _ = build_guardian_restore_plan(
        root_dir=root, machine_id=machine, snapshot_id=snapshot_id, current_state=current
    )
    payload = verify_restore_still_valid(plan, root_dir=root, current_state=current, confirm_plan=confirm_plan)
    if dry_run:
        LOG.info("guardian restore dry-run: plan %s would replace %s with snapshot %s", plan.plan_id, source, snapshot_id)
        return plan, 1
    written = commit_global_state(
        cfg, gate, OperationKind.GUARDIAN_RESTORE,
        family="guardian-restore", plan_id=plan.plan_id, action_count=1,
        state_root=local_dir, source=source, original=current, candidate=payload,
    )
    return plan, written


def _project_move_protected_roots(cfg: AppConfig, local_dir: Path) -> tuple[Path, ...]:
    """Everything a moved project must not land in or around."""
    return (
        local_dir,
        cfg.paths.cloud_root_dir,
        cfg.paths.backup_dir,
        cfg.paths.temp_dir,
        cfg.guardian.root_dir,
        cfg.semantic.root_dir,
    )


def _build_project_move(
    cfg: AppConfig, local_dir: Path, *, project_id: str, new_root: Path, volatile: bool, state: bytes
) -> ProjectMovePlan:
    catalog = scan_sessions(local_dir, volatile=volatile, max_line_bytes=cfg.semantic.max_jsonl_line_bytes)
    sessions = [(item.session_id, item.cwd) for item in one_per_chat(catalog.valid) if item.session_id]
    return build_project_move_plan(
        state_bytes=state,
        project_id=project_id,
        new_root=new_root,
        sessions=sessions,
        protected_roots=_project_move_protected_roots(cfg, local_dir),
        volatile=volatile,
    )


def scan_project_move(config_path: Path, *, project_id: str, new_root: Path) -> ProjectMovePlan:
    """Plan copying a project to ``new_root`` and remapping it there. Reads only.

    Hashes every file of the project, so it takes as long as reading the
    project does. Runs while Codex is open, but such a plan is volatile and an
    apply refuses it.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    volatile = _make_safety_gate(cfg).check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED
    source = local_dir / ".codex-global-state.json"
    state = _read_state_for_preview(cfg, source)
    if state is None:
        raise ConfigError(f"There is no {source.name}; open Codex once so it creates its projects")
    return _build_project_move(
        cfg, local_dir, project_id=_resolve_project_reference(state, project_id),
        new_root=Path(new_root), volatile=volatile, state=state,
    )


def _resolve_project_reference(state: bytes, reference: str) -> str:
    """An exact project id, or a name that names exactly one project.

    An ambiguous name is refused rather than resolved to the first match: two
    projects called the same thing are a real case (a moved project re-added),
    and copying the wrong one is not a mistake a preview should make for you.
    """
    try:
        projects = json.loads(state.decode("utf-8-sig")).get("local-projects", {})
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        projects = {}
    if not isinstance(projects, dict) or reference in projects:
        return reference
    wanted = reference.strip().casefold()
    matches = [
        project_id for project_id, entry in projects.items()
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].casefold() == wanted
    ]
    if len(matches) > 1:
        raise ConfigError(f"{reference!r} names {len(matches)} projects; use the project id")
    return matches[0] if matches else reference


def apply_project_move_plan(
    config_path: Path,
    *,
    plan_path: Path,
    confirm_plan: str,
    dry_run: bool = False,
) -> ProjectMoveResult:
    """Copy the project, verify it, then remap its root -- or refuse.

    The old folder is never modified or deleted. The state write runs through
    ``commit_global_state`` under its own operation kind, so a crash leaves a
    journal that ``recover`` understands, and a verified copy a rerun picks up.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    try:
        plan = load_project_move_plan(Path(plan_path))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read project move plan {plan_path}: {exc}") from exc
    local_dir = locate_local_state_dir(cfg)
    source = local_dir / ".codex-global-state.json"
    gate = _make_safety_gate(cfg)

    def rebuild() -> ProjectMovePlan:
        return _build_project_move(
            cfg, local_dir, project_id=plan.project_id, new_root=Path(plan.new_root),
            volatile=False, state=source.read_bytes(),
        )

    def commit(original: bytes, candidate: bytes, plan_id: str, action_count: int) -> int:
        return commit_global_state(
            cfg, gate, OperationKind.PROJECT_MOVE,
            family="project-move", plan_id=plan_id, action_count=action_count,
            state_root=local_dir, source=source, original=original, candidate=candidate,
        )

    def apply() -> ProjectMoveResult:
        return apply_project_move(
            plan,
            confirm_plan=confirm_plan,
            rebuild=rebuild,
            require_stopped=lambda: gate.require(OperationKind.PROJECT_MOVE),
            read_state=source.read_bytes,
            commit_state=commit,
            dry_run=dry_run,
        )

    if dry_run:
        return apply()
    # The copy is held under the state root's lock too, not only the commit:
    # a second move to the same folder (the window and the console, or a plan
    # rebuilt after a file changed) clears an earlier attempt's staging
    # directory, and must never clear one that is still being copied into.
    # The commit inside re-enters the lock in this thread.
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    machine = cfg.identity.machine_id or platform.node()
    with OperationLock(
        cfg.paths.temp_dir, state_root=local_dir, machine_id=machine, family="project-move", reentrant=True,
    ):
        return apply()
