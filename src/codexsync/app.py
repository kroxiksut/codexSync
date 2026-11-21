"""Command orchestration: context building, sync, guardian, repair, recovery.

This module is the only layer allowed to combine core components into a
command.  It owns no low-level helpers: shared plumbing lives in ``runtime``,
restore in ``restore`` and diagnostics in ``preflight``.  Their public names
are re-exported here so ``codexsync.app`` stays the single import surface for
the CLI and the tests.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import logging
import json
import os
import platform
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .config import PATH_SUBSTITUTIONS, load_config, preview_path
from .progress import PHASES, ProgressCallback, ProgressEventCallback, report as report_progress
from .progress_log import ProgressLog, progress_log_dir
from .task_items import TaskItem, filter_task_rows, load_latest_session_task, task_rows
from .mapping_hints import MappingHints, build_mapping_hints
from .sync_candidates import SyncCandidate, list_sync_candidates
from .automation import AutomationView, apply_automation, automation_status, remove_automation
from .system_scheduler import BROKEN_TASK_CODES, FOREIGN_TASK, LEGACY_TASK
from .watcher_stop import StopWatch, WatcherStopping
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
    DestinationPrecondition,
    FileMeta,
    SyncPlan,
)
from .operation_lock import OperationLock
from .path_mapping import PathMappingError, PathMappingRule, apply_path_mapping, mapping_digest, path_flavor
from .path_places import (
    MachinePlaces,
    Place,
    Skip,
    case_sensitive_on,
    forget as forget_place,
    learn_place,
    learn_skip,
    learned_rules,
    read_places,
    suggest_here,
    this_system,
    write_places,
)
from .project_sync import (
    MergeKind,
    ProjectMergePlan,
    ProjectSyncUnsupported,
    Removal as ProjectRemovalRecord,
    RootMappingAmbiguous,
    RootNotPlaced,
    build_project_merge,
    project_root_rules,
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
    SecretRule,
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
from .process_report import ProcessReport, build_process_report, os_key as process_os_key
from .issue_report import build_issue_report
from .program_file import frozen_program
from .preflight import (
    PreflightCheckResult,
    PreflightReport,
    print_preflight_report,
    run_preflight,
)
from .repair_plan import RepairActionKind, RepairPlan, build_repair_plan, conflicting_bindings, load_repair_plan, save_repair_plan
from .restore import BackupSnapshotInfo, RestoreResult, list_backup_snapshots, restore_from_backup
from .recovery import (
    rollback_applied_files,
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
    TransferItem,
    TransferPlan,
    ARCHIVE_FOLLOWS_LOCAL,
    FORMAT_MIGRATION,
    IN_PLACE_DECOMPRESSED,
    MIRROR_CONTAINER_REPLACED,
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
    read_thread_archive_rows,
    read_thread_names,
    read_thread_placements,
)
from .fs_replace import replace_with_retry, write_json_atomically
from .config_locations import cache_dir
from .chat_archive import ArchivePlan, apply_archive_changes, build_archive_plan, read_archive_back
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
    BACKFILL_COMPLETE,
    BACKFILL_PENDING,
    BackfillRow,
    CatalogueRefreshPlan,
    RefreshStatus,
    build_refresh_plan,
    database_fingerprint,
    find_unnamed_chat_files,
    read_backfill_row,
    read_backfill_status,
    read_snapshot_backfill_row,
    reset_backfill,
    settle_backfill,
    unnamed_digest,
)
from .codex_health import (
    ChatFiles,
    Finding,
    Severity,
    SettlePlan,
    build_settle_plan,
    diagnose_catalogue,
    diagnose_global_state,
    diagnose_journals,
    measure_chat_files,
)
from .session_catalog import SessionDescriptor, SessionParseCache, one_per_chat, scan_both_sides, scan_sessions
from .session_index import (
    PROVEN_CONTRACTS,
    SESSION_INDEX_FILE,
    IndexParseResult,
    parse_session_index,
)
from .shared_read import read_shared
from .stable_reader import SourceMissingError, SourceTooLargeError, SourceUnstableError, StableReader
from .state_locator import locate_local_state_dir, locate_state_dirs
from .sync_engine import SyncEngine
from .work_scheduler import WorkScheduler
from .cancellation import CancellationState, CancellationToken, OperationCancelled, OperationRolledBack, RollbackRequested
from .mutation_journal import rollback_roots_id
from .handoff import (
    Board,
    Delivery,
    Fingerprinter,
    HandoffError,
    HandoffRecord,
    STATE_WORKING,
    awaiting_delivery,
    case_twin_message as handoff_case_twin_message,
    delivery,
    machine_key,
    mark_working,
    read_board,
    record_handoff,
    record_path,
    wanted_keys as handoff_wanted_keys,
)
from .notifications import Notifier
from .codex_closer import CloseOutcome, ask_codex_to_quit
from .desktop_shortcut import ShortcutStatus, install_shortcut, remove_shortcut, shortcut_status
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
    "summarize_full_sync",
    "FullSyncSummary",
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
    "check_codex",
    "codex_process_report",
    "ShortcutStatus",
    "install_shortcut",
    "remove_shortcut",
    "shortcut_status",
    "build_issue_report",
    "frozen_program",
    "repair_codex",
    "CodexHealth",
    "CodexRepairResult",
    "sync_chat_names",
    "ChatNamesResult",
    "follow_chat_archive",
    "ChatArchiveResult",
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
    "progress_events_folder",
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
    *,
    scheduler: WorkScheduler | None = None,
    cancellation: CancellationToken | None = None,
) -> AppContext:
    if cancellation is not None:
        cancellation.checkpoint()
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
        raise ConfigError("targets.include_roots is not set: there is nothing to synchronise", code="NOTHING_TO_SYNC")

    # One owner for the whole preparation, never a pool per file. An injected
    # scheduler belongs to its caller and may also serve other read operations.
    with nullcontext(scheduler) if scheduler is not None else WorkScheduler() as work:
        local_idx, cloud_idx = _build_indexes(cfg, local_dir, cloud_dir, scheduler=work, cancellation=cancellation)
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
            scheduler=work,
            cancellation=cancellation,
        )
    if cancellation is not None:
        cancellation.checkpoint()
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
        rules=path_rules(cfg),
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
        raise ConfigError("--confirm-plan must exactly match the saved repair plan id", code="PLAN_CHANGED")
    if plan.volatile or plan.codes:
        raise FailSafeError("Volatile or unresolved repair plan cannot be applied")
    if plan.mapping_digest != mapping_digest(path_rules(cfg)):
        raise FailSafeError("Path mapping rules changed after the repair scan", code="PLAN_CHANGED")
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
        raise FailSafeError("Global state changed after the repair scan", code="PLAN_CHANGED")
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
    superseding: str | None = None,
) -> int:
    """Replace `.codex-global-state.json` with `candidate`, or leave it alone.

    Every command that edits the global state runs this exact envelope, because
    the guarantees are the file's and not any one command's: one mutation per
    state root at a time, durable evidence that a commit was entered, a verified
    full backup before the first byte moves, the process re-checked immediately
    before the replace, and a verified rollback if anything after it fails.

    A caller decides *what* to write and proves it valid; it does not get to
    decide how carefully the write happens. ``superseding`` is for `recover
    rollback` only: the open journal it undoes (``JournalStore.begin``).
    The file is this machine's alone, so another machine's open journal does
    not block it (``local_state_only``); this machine's own still does.
    """
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    machine = cfg.identity.machine_id or platform.node()
    with OperationLock(cfg.paths.temp_dir, state_root=state_root, machine_id=machine, family=family):
        journals = JournalStore(cfg.paths.temp_dir)
        # Retention follows [backup], as for every other family (CS-431).
        manager = BackupManager(
            cfg.paths.backup_dir, machine,
            retention_days=cfg.backup.retention_days,
            max_backups=cfg.backup.max_backups,
            compression="none", journal_root=cfg.paths.temp_dir,
        )
        journal = journals.begin(
            family, plan_id, action_count, backup_snapshot=manager.snapshot_name,
            counts=counts, origin=origin, machine_id=machine, superseding=superseding,
            local_state_only=True,
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
        rules=path_rules(cfg),
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
        rules=path_rules(cfg),
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
            "since then, so preview the move again and read what it now says", code="PLAN_CHANGED"
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


def places_root(cfg: AppConfig) -> Path:
    """Where each machine keeps what it was told about folders (D-033)."""
    return _coordination_dir(cfg) / "path-places"


def path_rules(cfg: AppConfig) -> list[PathMappingRule]:
    """`[[path_mappings]]` plus every place the machines learned (D-033). Reads only.

    The one list projects, chats, the working set, repair and the session
    transfer map paths through: the config's own rules, what any machine was
    told about a folder (`path_places`, both directions and through a third
    machine), and each project both machines hold under different roots. A
    part that cannot be read adds nothing; the config's rules always count.
    """
    rules = list(cfg.path_mappings)
    try:
        machine = _handoff_machine(cfg)
        coordination = _coordination_dir(cfg)
    except ConfigError:
        return rules
    places = read_places(coordination / "path-places")
    rules.extend(learned_rules(places, machine))
    try:
        source = locate_local_state_dir(cfg) / ".codex-global-state.json"
        state = json.loads(source.read_bytes().decode("utf-8-sig"))
    except (ConfigError, OSError, UnicodeError, ValueError):
        return rules
    if isinstance(state, dict):
        board = read_project_board(projects_root(cfg))
        rules.extend(project_root_rules(
            state, board.publications.values(), machine,
            case_sensitive=lambda peer: case_sensitive_on(places.system_of(peer)),
        ))
    return rules


def _native_here(path: str) -> bool:
    """Whether ``path`` is written the way this operating system writes paths."""
    flavor = path_flavor(path)
    return flavor is None or (flavor in {"windows", "unc"}) == (this_system() == "windows")


def _root_mapper(
    cfg: AppConfig, machine: str, rules: list[PathMappingRule] | None = None,
) -> Callable[[str, str], str]:
    rules = list(path_rules(cfg) if rules is None else rules)

    def map_root(peer: str, root: str) -> str:
        try:
            return apply_path_mapping(root, source_machine=peer, target_machine=machine, rules=rules).target_path
        except PathMappingError as exc:
            if str(exc) != "NO_MAPPING":
                raise RootMappingAmbiguous(str(exc)) from exc
            if not _native_here(root):
                # Another system's path names nothing here (CS-403).
                raise RootNotPlaced(root) from exc
            # Same path on both machines -- the usual case on one cloud drive.
            return root

    return map_root


def _chat_folders(
    local_dir: Path, cfg: AppConfig, parse_cache: SessionParseCache | None,
) -> list[tuple[str, str | None]]:
    """``(thread id, cwd)`` of every chat here that is not a spawned sub-thread."""
    catalog = scan_sessions(local_dir, max_line_bytes=cfg.semantic.max_jsonl_line_bytes, parse_cache=parse_cache)
    return [
        (item.session_id, item.cwd) for item in one_per_chat(catalog.descriptors)
        if item.session_id and not item.parent_id
    ]


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
    parse_cache: SessionParseCache | None = None,
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
    places = read_places(places_root(cfg))
    for name, reason in sorted(places.unreadable.items()):
        LOG.warning("folder places %s are not believed: %s", name, reason)
    pending_machines = {item.machine for item in pending}
    try:
        plan, state = build_project_merge(
            original, pending, machine=machine,
            map_root=_root_mapper(cfg, machine), folder_exists=_folder_exists,
            taken_before=[item for item in board.others(machine) if item.machine not in pending_machines],
            skipped=places.skipped_here(machine),
            chats=_chat_folders(local_dir, cfg, parse_cache), native=_native_here,
            own=_own_list(board, machine), removed_projects=cfg.sync.removed_projects,
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
            "since then, so preview again and read what it now says", code="PLAN_CHANGED"
        )
    for item in plan.ambiguous:
        LOG.warning(
            "project %s of %s left alone: %s", item.name or item.peer_project_id, item.peer_machine,
            ", ".join(item.codes),
        )
    # A project without a folder here is the usual case, not a fault (D-034).
    for item in plan.missing_folders:
        LOG.info("project %s added without a folder here: %s", item.name, ", ".join(item.roots))
    for item in plan.unplaced:
        LOG.info(
            "project %s of %s added with that machine's root as it is (%s); `projects place` gives it a folder here",
            item.name or item.peer_project_id, item.peer_machine, ", ".join(item.roots),
        )
    for item in plan.removed:
        LOG.info("project %s removed here: %s removed it on purpose", item.name or item.peer_project_id,
                 item.peer_machine)
    for removal in plan.waiting:
        LOG.warning(
            "project %s was removed in Codex here and is not taken back from the other machines; "
            "`projects removed` asks whether on purpose", removal.name or removal.project_id,
        )
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
            counts={
                "projects_added": len(plan.added), "changes": plan.action_count,
                **({"projects_removed": len(plan.removed)} if plan.removed else {}),
            },
            origin=origin,
        )
        LOG.info(
            "projects: %d added, %d placed, pins %s, order %s, %d chat binding(s) written, "
            "%d chat(s) bound by their folder",
            len(plan.added), len(plan.placed), "changed" if plan.pins_changed else "kept",
            "changed" if plan.order_changed else "kept", plan.bindings_written, plan.bound_by_folder,
        )
    published = _publish_projects(root, board, machine, source, plan)
    return replace(result, written=written, published=published)


class PlaceStatus(str, Enum):
    """Where a project another machine has stands on this one (D-033)."""

    #: Here, and its folder exists.
    HERE = "HERE"
    #: Here, without a folder: its chats can be read, its files are elsewhere.
    MISSING_HERE = "MISSING_HERE"
    #: Not here yet; the next sync adds it at ``here``.
    COMING = "COMING"
    #: Not here yet; its root is another system's path and nothing places it,
    #: so the next sync adds it with that root as it is, without a folder (D-034).
    UNPLACED = "UNPLACED"
    #: This machine was told not to carry it.
    SKIPPED = "SKIPPED"
    #: Two rules or answers disagree about where it is.
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ProjectPlace:
    project_id: str
    name: str
    #: The machines that publish it, and its root on the first of them.
    peers: tuple[str, ...]
    there: tuple[str, ...]
    status: PlaceStatus
    #: Its root here: the one Codex has, or where the next sync puts it.
    here: tuple[str, ...] = ()
    #: Folders here named like it, beside folders already placed. Offered only.
    suggestions: tuple[str, ...] = ()

    @property
    def needs_answer(self) -> bool:
        """Only a disagreement waits: a project without a folder here is fine (D-034)."""
        return self.status is PlaceStatus.AMBIGUOUS

    @property
    def without_folder(self) -> bool:
        return self.status in {PlaceStatus.UNPLACED, PlaceStatus.MISSING_HERE}


@dataclass(frozen=True, slots=True)
class PlacesReport:
    machine: str
    system: str
    projects: tuple[ProjectPlace, ...]
    #: What this machine was told, as it is stored in the shared workspace.
    learned: MachinePlaces
    root: Path
    unreadable: tuple[str, ...] = ()

    @property
    def waiting(self) -> tuple[ProjectPlace, ...]:
        return tuple(item for item in self.projects if item.needs_answer)

    @property
    def without_folder(self) -> tuple[ProjectPlace, ...]:
        return tuple(item for item in self.projects if item.without_folder)


def project_places(config_path: Path) -> PlacesReport:
    """Every project another machine has, and where it stands here. Reads only (D-033)."""
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    root = places_root(cfg)
    places = read_places(root)
    board = read_project_board(projects_root(cfg))
    rules = path_rules(cfg)
    map_root = _root_mapper(cfg, machine, rules)
    local: dict[str, Any] = {}
    schema_id = None
    try:
        state = json.loads((locate_local_state_dir(cfg) / ".codex-global-state.json").read_bytes().decode("utf-8-sig"))
        if isinstance(state, dict):
            schema_id = detect_state_schema(state)
            local = dict(state.get("local-projects") or {})
    except (ConfigError, OSError, UnicodeError, ValueError):
        pass
    here_roots = {
        project_id: project_root_paths(schema_id, entry) if schema_id and isinstance(entry, dict) else ()
        for project_id, entry in local.items()
    }
    known_here = [root for roots in here_roots.values() for root in roots if _folder_exists(root)]
    known_here += [place.here for place in places.own(machine).places]
    skipped = places.skipped_here(machine)
    # A root another machine publishes for a project already here: the merge
    # takes a root carried as it is to be that project (D-034).
    published: dict[str, set[str]] = {}
    for peer in board.others(machine):
        for project_id, entry in peer.projects.items():
            for root in project_root_paths("electron-v2", entry):
                published.setdefault(root_key(root), set()).add(project_id)

    seen: dict[str, ProjectPlace] = {}
    for peer in board.others(machine):
        for project_id, entry in sorted(peer.projects.items()):
            if project_id in seen:
                item = seen[project_id]
                seen[project_id] = replace(item, peers=item.peers + (peer.machine,))
                continue
            name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            there = project_root_paths("electron-v2", entry)
            here: tuple[str, ...] = ()
            if project_id in local:
                here = here_roots.get(project_id, ())
                status = PlaceStatus.HERE if any(_folder_exists(item) for item in here) else PlaceStatus.MISSING_HERE
            elif project_id in skipped:
                status = PlaceStatus.SKIPPED
            else:
                try:
                    here = tuple(map_root(peer.machine, item) for item in there)
                    wanted = {root_key(item) for item in here}
                    # The merge matches a project by its folder too, under another id.
                    same_folder = [roots for roots in here_roots.values() if wanted & {root_key(r) for r in roots}]
                    if same_folder:
                        here = same_folder[0]
                        status = (
                            PlaceStatus.HERE if any(_folder_exists(item) for item in here)
                            else PlaceStatus.MISSING_HERE
                        )
                    else:
                        status = PlaceStatus.COMING
                except RootNotPlaced:
                    status = PlaceStatus.UNPLACED
                    known = sorted({
                        other for root in there for other in published.get(root_key(root), ())
                        if other != project_id and other in local
                    })
                    if len(known) == 1:
                        here = here_roots.get(known[0], ())
                        status = (
                            PlaceStatus.HERE if any(_folder_exists(item) for item in here)
                            else PlaceStatus.MISSING_HERE
                        )
                except RootMappingAmbiguous:
                    status = PlaceStatus.AMBIGUOUS
            suggestions: tuple[str, ...] = ()
            if status in {PlaceStatus.UNPLACED, PlaceStatus.MISSING_HERE} and there:
                suggestions = suggest_here(there[0], known_here)
            seen[project_id] = ProjectPlace(
                project_id, name, (peer.machine,), there, status, here, suggestions,
            )
    projects = tuple(sorted(seen.values(), key=lambda item: (item.name.casefold(), item.project_id)))
    return PlacesReport(
        machine, this_system(), projects, places.own(machine), root, tuple(sorted(places.unreadable)),
    )


def place_project(
    config_path: Path,
    project: str,
    *,
    path: str | None = None,
    skip: bool = False,
    forget: bool = False,
    whole_folder: bool = True,
) -> MachinePlaces:
    """Say where another machine's project is here, not to carry it, or forget either (D-033).

    The answer goes into this machine's own file in the shared workspace, not
    into Codex: the next sync adds or moves the project through the ordinary
    envelope, and every other machine reads the answer backwards. With
    ``whole_folder`` the folder above is learned too when both ends share the
    project's folder name, so its neighbours need no question.
    """
    if sum((path is not None, skip, forget)) != 1:
        raise ConfigError("Give exactly one of a folder, skip or forget")
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    root = places_root(cfg)
    board = read_places(root)
    if f"{machine}.json" in board.unreadable:
        # Read as empty, the answer would replace every earlier one (CS-527).
        raise FailSafeError(
            f"This machine's folder places cannot be read ({board.unreadable[f'{machine}.json']}); "
            "try again once the cloud client has delivered the file",
            code="OWN_PLACES_UNREADABLE",
        )
    # This machine's file says which system wrote it; it is this one, now.
    current = replace(board.own(machine), system=this_system())
    report = project_places(config_path)
    matches = [
        item for item in report.projects
        if item.project_id == project or (item.name and item.name.casefold() == project.casefold())
    ]
    if not matches:
        raise ConfigError(f"No project of another machine matches {project!r}; `projects places` lists them")
    if len(matches) > 1:
        names = ", ".join(f"{item.name} ({item.project_id})" for item in matches)
        raise ConfigError(f"{project!r} matches more than one project: {names}; name it by its id")
    item = matches[0]
    if forget:
        updated = current
        for there in item.there:
            updated = forget_place(updated, there=there)
        updated = forget_place(updated, project_id=item.project_id)
    elif skip:
        updated = learn_skip(current, project_id=item.project_id, name=item.name, peer=item.peers[0])
    else:
        assert path is not None
        here = path.strip()
        if path_flavor(here) is None or not _native_here(here):
            raise ConfigError(f"Not an absolute path on this machine: {path!r}")
        if len(item.there) != 1:
            raise ConfigError(f"Project {item.name!r} has {len(item.there)} folders there; only one can be placed")
        updated = current
        for peer in item.peers:
            updated = learn_place(
                updated, peer=peer, there=item.there[0], here=here,
                project_id=item.project_id, name=item.name, whole_folder=whole_folder,
            )
        if not _folder_exists(here):
            LOG.warning("project %s placed at %s, which does not exist here yet", item.name, here)
    write_places(root, updated)
    LOG.info("folder places of %s written to %s", machine, root)
    return updated


@dataclass(frozen=True, slots=True)
class CatalogueRefreshResult:
    """What `refresh_thread_catalogue` found and whether it asked Codex (D-024)."""

    plan: CatalogueRefreshPlan
    #: Codex was asked to rebuild its chat list; it does so on its next start.
    refreshed: bool = False
    #: Read while Codex was open, so only an indication.
    volatile: bool = False
    #: What that rebuild walks -- every chat file, not only the missing ones --
    #: so a person asked to confirm it knows how long Codex's start may take.
    chat_files: "ChatFiles | None" = None


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
        gate.require(OperationKind.CODEX_CATALOGUE)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED
    marker = _catalogue_marker(cfg)
    plan = build_refresh_plan(
        local_dir, read_thread_placements(local_dir), read_backfill_state(local_dir),
        asked_before=_read_catalogue_marker(marker),
    )
    result = CatalogueRefreshResult(
        plan=plan, volatile=volatile, chat_files=measure_chat_files(local_dir) if plan.writes else None,
    )
    if not applying:
        return result
    if not planned_here and plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from the preview; Codex's catalogue or the chat files "
            "changed since then, so preview again", code="PLAN_CHANGED"
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
class CodexHealth:
    """What `codex check` found (D-032). Reads only."""

    findings: tuple[Finding, ...]
    #: Whether Codex was running when it was checked, as the gate saw it.
    process_state: ProcessState
    #: The repair `codex repair` would apply now; ``plan.writes`` is false
    #: when there is nothing for it to do.
    plan: SettlePlan

    @property
    def broken(self) -> bool:
        return any(item.severity is Severity.BROKEN for item in self.findings)


@dataclass(frozen=True, slots=True)
class CodexRepairResult:
    plan: SettlePlan
    repaired: bool = False
    #: The backup snapshot holding the catalogue as it was before the repair.
    snapshot: str | None = None


def _own_catalogue_backup(cfg: AppConfig, config_path: Path, database: str | None) -> tuple[str, BackfillRow] | None:
    """The row this machine's newest catalogue write found, from its verified copy.

    Only the newest committed `thread-catalogue` operation of this machine
    counts: that copy holds the row as it was right before codexSync asked for
    a rebuild. A copy that is gone or unreadable is no evidence, and an older
    one is not consulted in its place.
    """
    if database is None:
        return None
    for info in list_journals(config_path):
        if info.family != "thread-catalogue" or not info.own or info.state != JournalState.COMMITTED.value:
            continue
        if not info.backup_snapshot or not info.backup_snapshot_present:
            return None
        copy = cfg.paths.backup_dir / info.backup_snapshot / Path(database)
        # The scratch copy goes to the system's temporary folder and is removed
        # at once: `codex check` reads only and leaves nothing in codexSync's
        # folders either.
        row = read_snapshot_backfill_row(copy) if copy.is_file() else None
        return (info.backup_snapshot, row) if row is not None else None
    return None


def _settle_plan(cfg: AppConfig, config_path: Path, local_dir: Path) -> SettlePlan:
    reading = read_backfill_state(local_dir)
    own = None
    if reading.status is PlacementStatus.AVAILABLE and reading.backfill not in (None, BACKFILL_COMPLETE):
        own = _own_catalogue_backup(cfg, config_path, reading.database)
    return build_settle_plan(local_dir, reading, own_backup=own)


def codex_process_report(config_path: Path | None) -> ProcessReport:
    """What the process check sees here, safe to paste into an issue (CS-418). Reads only.

    With a config its `[process_detection]` names are used; without one, the
    names this version knows (the shipped template carries the same). Nothing
    is created: no config, no log file.
    """
    if config_path is not None:
        cfg = load_config(config_path)
        names = list(cfg.process_detection.process_names)
        markers = list(cfg.process_detection.background_process_names.get(process_os_key(), []))
        source = "config"
    else:
        names = default_process_names()
        markers = default_background_process_names().get(process_os_key(), [])
        source = "built-in"
    return build_process_report(names, markers, names_from=source)


def check_codex(config_path: Path, *, gate: SafetyGate | None = None) -> CodexHealth:
    """Why Codex may not start or show its work, and what can be repaired (D-032).

    Reads only, and creates nothing: the catalogue is opened the way every
    other reading opens it, and the process is only looked at. Each finding
    names its repair -- `repair_codex` for the chat-list rebuild, another
    codexsync command for the rest -- whoever caused the problem.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    process = gate.check(OperationKind.DOCTOR).process_state
    reading = read_backfill_state(local_dir)
    unnamed = 0
    if reading.status is PlacementStatus.AVAILABLE and reading.backfill == BACKFILL_COMPLETE:
        placements = read_thread_placements(local_dir)
        if placements.status is PlacementStatus.AVAILABLE:
            unnamed = len(find_unnamed_chat_files(local_dir, placements))
    findings = diagnose_catalogue(reading, process, measure_chat_files(local_dir), unnamed=unnamed)
    source = local_dir / ".codex-global-state.json"
    valid: bool | None = None
    recognised = True
    if source.is_file():
        try:
            report = validate_global_state_references(source.read_bytes())
            valid = report.status in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}
            recognised = report.status is not ValidationStatus.INDETERMINATE
        except OSError:
            valid = None
    findings += diagnose_global_state(present=source.is_file(), valid=valid, recognised=recognised)
    try:
        open_journals = sum(1 for item in list_journals(config_path) if not item.terminal and not item.closes_itself)
    except Exception:  # an unreadable journal folder is `recover list`'s to explain
        LOG.exception("codex check: journals not read")
        open_journals = 0
    findings += diagnose_journals(open_journals)
    return CodexHealth(tuple(findings), process, _settle_plan(cfg, config_path, local_dir))


def repair_codex(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    gate: SafetyGate | None = None,
    origin: str | None = None,
) -> CodexRepairResult:
    """Preview, or put Codex's chat-list rebuild back to ``complete`` (D-032).

    Without ``confirm_plan`` this reads only. With it, Codex must be closed,
    the plan is rebuilt and must still carry that id, and the write -- one row
    of ``backfill_state``, re-read under the write lock and refused if Codex
    moved it -- runs in the envelope every catalogue write uses: operation
    lock, journal (family ``codex-repair``), a verified backup of the database
    and its sidecars, the process checked again right before the transaction.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    plan = _settle_plan(cfg, config_path, local_dir)
    result = CodexRepairResult(plan=plan)
    if confirm_plan is None:
        return result
    _require_mutation_compatible_config(cfg)
    gate.require(OperationKind.CODEX_CATALOGUE)
    if plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from `codex repair`; Codex's catalogue changed since "
            "then, so check again", code="PLAN_CHANGED"
        )
    if not plan.writes:
        LOG.info("codex repair: nothing to repair")
        return result
    if dry_run:
        LOG.info("codex repair dry-run: the chat-list rebuild would be set back to complete")
        return result
    assert plan.database is not None and plan.current is not None and plan.target is not None
    database = local_dir / Path(plan.database)
    expected, target = plan.current, plan.target
    snapshot = _write_codex_catalogue(
        cfg, gate, local_dir, database,
        family="codex-repair", plan_id=plan.plan_id, action_count=1, counts={}, origin=origin,
        still_planned=lambda: _settle_plan(cfg, config_path, local_dir).plan_id == plan.plan_id,
        write=lambda: settle_backfill(database, expected=expected, target=target),
        verify=lambda: read_backfill_row(database) == tuple(target),
    )
    LOG.info(
        "codex repair: Codex's chat-list rebuild set back to complete (%s; backup %s)",
        f"row from {plan.snapshot}" if plan.snapshot else "row as found", snapshot,
    )
    return replace(result, repaired=True, snapshot=snapshot)


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
        gate.require(OperationKind.CODEX_CATALOGUE)
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
            "so preview again", code="PLAN_CHANGED"
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


@dataclass(frozen=True, slots=True)
class ChatArchiveResult:
    """What `follow_chat_archive` found and rewrote (D-035)."""

    plan: ArchivePlan
    #: Catalogue rows pointed at the file an archive move left them.
    written: int = 0
    volatile: bool = False


def follow_chat_archive(
    config_path: Path,
    *,
    confirm_plan: str | None = None,
    dry_run: bool = False,
    gate: SafetyGate | None = None,
    origin: str | None = None,
    planned_here: bool = False,
) -> ChatArchiveResult:
    """Preview, or make Codex's catalogue follow chats moved into or out of the archive.

    A sync moves an archived chat's file between ``sessions/`` and
    ``archived_sessions/`` (D-023); this points the row at the file it moved
    to, with the archive state that folder means (`chat_archive`). Without
    ``confirm_plan`` it reads only; with it, Codex must be closed and the plan
    id must still match, and the rows are rewritten in one transaction inside
    the catalogue envelope. ``planned_here`` applies the plan this call builds.
    """
    cfg = load_config(config_path)
    local_dir = locate_local_state_dir(cfg)
    gate = gate if gate is not None else _make_safety_gate(cfg)
    applying = confirm_plan is not None or planned_here
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.CODEX_CATALOGUE)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED

    def fingerprint(relative: str) -> str:
        return database_fingerprint(local_dir / Path(relative))

    def plan_now() -> ArchivePlan:
        return build_archive_plan(read_thread_archive_rows(local_dir), local_dir, fingerprint=fingerprint)

    plan = plan_now()
    result = ChatArchiveResult(plan=plan, volatile=volatile)
    if not applying:
        return result
    if not planned_here and plan.plan_id != confirm_plan:
        raise ConfigError(
            "--confirm-plan must match the plan id from the preview; the catalogue or the chat files "
            "changed since then, so preview again", code="PLAN_CHANGED"
        )
    if dry_run or not plan.writes:
        if dry_run:
            LOG.info("chat archive dry-run: %d catalogue row(s) would follow their file", len(plan.changes))
        return result
    assert plan.database is not None
    database = local_dir / Path(plan.database)
    expected = {
        item.thread_id: (item.path_after, item.archived_after, item.archived_at_after) for item in plan.changes
    }
    _write_codex_catalogue(
        cfg, gate, local_dir, database,
        family="chat-archive", plan_id=plan.plan_id, action_count=len(plan.changes),
        counts={"archived": plan.archived, "restored": plan.restored}, origin=origin,
        still_planned=lambda: plan_now().plan_id == plan.plan_id,
        write=lambda: apply_archive_changes(database, plan.changes),
        verify=lambda: read_archive_back(database, sorted(expected)) == expected,
    )
    LOG.info(
        "chat archive: %d chat(s) archived and %d brought back in Codex's catalogue, as their files moved",
        plan.archived, plan.restored,
    )
    return replace(result, written=len(plan.changes))


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
    secrets = SecretRule(cfg.project_files.secret_files, tuple(cfg.project_files.secret_patterns))
    with ThreadPoolExecutor(max_workers=PROJECT_FILES_WORKERS) as pool:
        states = dict(zip(
            projects,
            pool.map(
                lambda item: read_files_state(Path(item[0]), git=git, cache_root=cache_root, secrets=secrets),
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
                except RootNotPlaced:
                    # Carried with that machine's root as it is (D-034, CS-498).
                    local_id = by_root.get(root_key(published["root"]))
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
    journal in RECOVERY_REQUIRED; the database is in the snapshot. Like
    `commit_global_state`, it is not blocked by another machine's open
    journal (``local_state_only``). Returns the snapshot name.
    """
    _ensure_dir(cfg.paths.backup_dir, "paths.backup_dir")
    _ensure_dir(cfg.paths.temp_dir, "paths.temp_dir")
    machine = cfg.identity.machine_id or platform.node()
    relative = database.relative_to(local_dir).as_posix()
    with OperationLock(cfg.paths.temp_dir, state_root=local_dir, machine_id=machine, family=family):
        journals = JournalStore(cfg.paths.temp_dir)
        # Retention follows [backup], as for every other family (CS-431).
        manager = BackupManager(
            cfg.paths.backup_dir, machine,
            retention_days=cfg.backup.retention_days,
            max_backups=cfg.backup.max_backups,
            compression="none", journal_root=cfg.paths.temp_dir,
        )
        journal = journals.begin(
            family, plan_id, action_count, backup_snapshot=manager.snapshot_name,
            counts=counts, origin=origin, machine_id=machine, local_state_only=True,
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
            gate.require(OperationKind.CODEX_CATALOGUE, final=True)
            if not still_planned():
                raise FailSafeError("Codex's catalogue changed after it was read; nothing was written. Run again.", code="STATE_CHANGED_RETRY")
            journal = journals.transition(journal, JournalState.COMMITTING)
        except Exception as exc:
            _close_journal_after_failure(journals, journal, exc)
            raise
        try:
            # Again right before the transaction: the journal write above may
            # have waited seconds on a cloud client, and Codex may have started
            # in them -- the same final check `commit_global_state` makes.
            gate.require(OperationKind.CODEX_CATALOGUE, final=True)
            write()
        except Exception as exc:
            _close_journal_after_failure(journals, journal, exc)
            raise
        try:
            matched = verify()
        except Exception as exc:
            # The transaction committed; a read-back that fails proves nothing
            # either way, so the journal says recovery is needed (CS-523).
            journals.transition(journal, JournalState.RECOVERY_REQUIRED, failure=exc)
            raise
        if not matched:
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
    publication = publication_from_state(
        state, machine, accepted=accepted, removed=plan.removals, followed=plan.followed,
        section_waiting=plan.section_waiting,
    )
    if (
        own is not None and own.publication_id == publication.publication_id
        and own.accepted == publication.accepted and own.projectless == publication.projectless
        and _removal_state(own) == _removal_state(publication)
    ):
        return False
    write_publication(root, publication)
    LOG.info("published this machine's project list (%d project(s)) to %s", len(publication.projects), root)
    return True


def _removal_state(publication) -> tuple:
    return (
        tuple((item.removal_id, item.confirmed) for item in publication.removed),
        tuple(publication.followed),
        tuple(publication.section_waiting),
    )


def _own_list(board, machine: str):
    """This machine's last project list; refuses when it exists but cannot be read.

    It is what tells a project removed in Codex here from one never carried
    (D-039). Read as absent, every removal here would be taken back from the
    other machines without a question.
    """
    own = board.own(machine)
    if own is None and f"{machine}.json" in board.unreadable:
        raise FailSafeError(
            f"This machine's own project list cannot be read ({board.unreadable[f'{machine}.json']}); "
            "try again once the cloud client has delivered it",
            code="OWN_PROJECT_LIST_UNREADABLE",
        )
    return own


@dataclass(frozen=True, slots=True)
class ProjectRemoval:
    """A project removed in Codex on some machine, as `projects removed` shows it (D-039)."""

    machine: str
    project_id: str
    name: str
    roots: tuple[str, ...]
    removed_at_utc: str
    #: Removed on purpose: the other machines remove it too.
    confirmed: bool
    #: Removed on this machine (the person here answers for it).
    here: bool


@dataclass(frozen=True, slots=True)
class ProjectRemovals:
    machine: str
    #: What this machine does with a project removed here (`[sync] removed_projects`).
    policy: str
    items: tuple[ProjectRemoval, ...]

    @property
    def waiting(self) -> tuple[ProjectRemoval, ...]:
        """Removed here, not answered yet."""
        return tuple(item for item in self.items if item.here and not item.confirmed)


def project_removals(config_path: Path) -> ProjectRemovals:
    """Projects removed in Codex here and, on purpose, elsewhere. Reads only (D-039).

    A removal here appears once a sync has seen it: the sync compares the
    state with this machine's last published list.
    """
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    board = read_project_board(projects_root(cfg))
    own = _own_list(board, machine)
    items: list[ProjectRemoval] = [
        ProjectRemoval(machine, item.project_id, item.name, item.roots, item.removed_at_utc, item.confirmed, True)
        for item in (own.removed if own is not None else ())
    ]
    for peer in board.others(machine):
        items += [
            ProjectRemoval(peer.machine, item.project_id, item.name, item.roots, item.removed_at_utc, True, False)
            for item in peer.removed if item.confirmed
        ]
    items.sort(key=lambda item: (not item.here, item.confirmed, item.name.casefold(), item.machine))
    return ProjectRemovals(machine, cfg.sync.removed_projects, tuple(items))


def decide_project_removal(config_path: Path, project: str, *, everywhere: bool) -> ProjectRemoval:
    """Answer for a project removed here: on purpose (``everywhere``) or by mistake (D-039).

    The answer goes into this machine's own project list, not into Codex: on
    purpose, every other machine removes the project on its next sync; by
    mistake, this machine's next sync takes it back from them. Either way the
    question is not asked again.
    """
    cfg = load_config(config_path)
    machine = _handoff_machine(cfg)
    root = projects_root(cfg)
    board = read_project_board(root)
    own = _own_list(board, machine)
    removed = own.removed if own is not None else ()
    matches = [
        item for item in removed
        if item.project_id == project or (item.name and item.name.casefold() == project.casefold())
    ]
    if not matches:
        raise ConfigError(
            f"No project removed here matches {project!r}; `projects removed` lists them",
            code="NO_SUCH_REMOVAL", params={"project": project},
        )
    if len(matches) > 1:
        names = ", ".join(f"{item.name} ({item.project_id})" for item in matches)
        raise ConfigError(f"{project!r} matches more than one removed project: {names}; name it by its id")
    chosen = matches[0]
    if everywhere:
        kept = tuple(
            ProjectRemovalRecord(item.removal_id, item.project_id, item.name, item.roots, item.removed_at_utc,
                                 True if item is chosen else item.confirmed)
            for item in removed
        )
    else:
        kept = tuple(item for item in removed if item is not chosen)
    assert own is not None
    write_publication(root, replace(own, removed=kept, published_at_utc=_utc_now_text()))
    LOG.info(
        "project %s: removed here %s", chosen.name or chosen.project_id,
        "on purpose; the other machines remove it on their next sync" if everywhere
        else "by mistake; the next sync takes it back from the other machines",
    )
    return ProjectRemoval(machine, chosen.project_id, chosen.name, chosen.roots, chosen.removed_at_utc,
                          everywhere, True)


def _utc_now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def _session_parse_cache(config_path: Path, cfg: AppConfig, local_dir: Path) -> SessionParseCache | None:
    try:
        directory = (cache_dir() / "session-catalogues").resolve()
        if any(directory.is_relative_to(root.resolve()) for root in (local_dir, cfg.paths.cloud_root_dir)):
            return None
        key = hashlib.sha256(os.fsencode(config_path.resolve())).hexdigest()
        path = directory / f"{key}.json"
        if path.is_symlink():
            return None
        return SessionParseCache(path)
    except (OSError, RuntimeError):
        return None


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
    cancellation: CancellationToken | None = None,
    parse_cache: SessionParseCache | None = None,
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
    if parse_cache is None:
        parse_cache = _session_parse_cache(config_path, cfg, local_dir)

    local_catalog, remote_catalog = scan_both_sides(
        local_dir, cloud_dir, local_machine=source_machine, remote_machine=target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes, volatile=volatile, progress=progress,
        cancellation=cancellation,
        parse_cache=parse_cache,
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
        scope=_hashed_scope(scope),
        path_rules=path_rules(cfg),
        folder_exists=local_folder_exists,
        conflict_rule=conflict_rule_for(cfg.conflict.policy, cfg.sync.direction),
        direction=cfg.sync.direction,
    )


def _hashed_scope(scope: SessionScope | None) -> tuple[str, ...] | None:
    """What a working set hands the plan: ``None`` only when nothing was chosen.

    Emptiness is the choice's, never its expansion's. A set whose projects all
    vanished (renamed, re-created by Codex under new ids, a typo) expands to
    no session, and that is ``()`` -- nothing may be written into `.codex` --
    not ``None``, which writes every chat (CS-495).
    """
    if scope is None or scope.is_empty:
        return None
    return scope.session_hashes


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
    # Atomic: a truncated resolutions file is a ConfigError on every later
    # full sync, and the recorded decisions are lost with it (CS-517).
    write_json_atomically(output_path, payload)


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
    progress: Callable[[int, int], None] | None = None,
    phase_progress: ProgressCallback | None = None,
    event_progress: ProgressEventCallback | None = None,
    parse_cache: SessionParseCache | None = None,
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

    Returns the number of branches written. The removal of a file a branch
    left behind (an archive move, a `.zst` continued as plain JSONL) is part
    of writing that branch, not one more chat.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    try:
        plan = load_transfer_plan(plan_path)
    except ValueError as exc:
        raise ConfigError(f"Cannot read transfer plan {plan_path}: {exc}") from exc
    if plan.plan_id != confirm_plan:
        raise ConfigError("--confirm-plan must exactly match the saved transfer plan id", code="PLAN_CHANGED")
    if plan.volatile:
        raise FailSafeError("A volatile transfer plan cannot be applied; rescan with Codex closed", code="PLAN_VOLATILE")

    gate = _make_safety_gate(cfg)
    gate.require(OperationKind.SESSION_APPLY)
    initialize_runtime_paths(cfg)
    local_dir = locate_local_state_dir(cfg)
    cloud_dir = cfg.paths.cloud_root_dir
    owns_cache = parse_cache is None
    if owns_cache:
        parse_cache = _session_parse_cache(config_path, cfg, local_dir)

    fresh, local_by_hash, remote_by_hash = _rebuild_transfer_plan(
        cfg, local_dir, cloud_dir, plan, resolutions_path, parse_cache=parse_cache,
    )
    if fresh.plan_id != plan.plan_id:
        raise FailSafeError(
            "Session state changed after the scan; rebuild the plan and confirm the new id", code="PLAN_CHANGED"
        )

    unresolved = [item for item in plan.blocked_items if item.action in _UNRESOLVED_TRANSFER_BLOCKS]
    if unresolved:
        raise ConflictError(
            f"{len(unresolved)} plan item(s) are blocked on a decision only you can make: "
            + ", ".join(sorted({item.action.value for item in unresolved})), code="TRANSFER_DECISIONS_NEEDED", params={"count": len(unresolved)}
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
            if owns_cache and parse_cache is not None:
                parse_cache.publish()
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
        return _branches_written(copies)

    machine = cfg.identity.machine_id or platform.node()
    with (
        OperationLock(cfg.paths.temp_dir, state_root=local_dir, machine_id=machine, family="sessions"),
        ProgressLog(progress_log_dir(cfg.paths.temp_dir, local_dir), "sessions", event_progress) as events,
        WorkScheduler() as scheduler,
    ):
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
            # Staging writes siblings into .codex: re-check first (CS-460).
            before_stage_check=lambda: gate.require(OperationKind.SESSION_APPLY),
            progress=progress,
            phase_progress=phase_progress,
            progress_events=events,
            scheduler=scheduler,
            stage_operation_id=journal.operation_id,
            # Session transfer performs a stronger record-level read-back below.
            # Deferring that read avoids opening every cloud file twice in the
            # serial commit loop while the journal remains COMMITTING.
            verify_destination_after_replace=False,
        )
        try:
            engine.execute(copies, dry_run=False)
            _verify_transferred_branches(
                cfg, plan, local_dir, cloud_dir,
                scheduler=scheduler, phase_progress=phase_progress,
            )
            current[0] = journals.transition(current[0], JournalState.COMMITTED)
        except Exception as exc:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            try:
                item = engine.failure_item
                if getattr(exc, "code", None) == "TRANSFER_READBACK_MISMATCH":
                    item = {"phase": "verify_chats", "relative_path": exc.params.get("relative_path", ""),
                            "session_hash": getattr(exc, "transfer_session_hash", None)}
                relative = str(item.get("relative_path", "")).replace("\\", "/")
                parts = relative.split("/")
                safe_path = relative if relative and all(
                    part not in {"", ".", ".."} and ":" not in part for part in parts
                ) else None
                params = {"staged": engine.staged_count, "applied": engine.applied_count, "deleted": engine.deleted_count,
                          "copies": len(copies.to_local) + len(copies.to_cloud),
                          "deletions": len(copies.deletions)}
                if safe_path is not None:
                    params["relative_path"] = safe_path
                if item.get("phase") in {"stage_chats", "backup_chats", "apply_chats", "delete_chat_files", "verify_chats"}:
                    params["phase"] = item["phase"]
                if isinstance(item.get("index"), int):
                    params["index"] = item["index"]
                phase, index = item.get("phase"), item.get("index")
                copy_items = [entry for side in ("local", "mirror") for entry in plan.items
                              if transfer_direction(entry) == side]
                delete_items = [entry for entry in plan.items if transfer_direction(entry) is not None and
                                any(code in entry.codes for code in (MOVES_BRANCH, IN_PLACE_DECOMPRESSED,
                                                                     MIRROR_CONTAINER_REPLACED))]
                candidates = copy_items if phase in {"stage_chats", "apply_chats"} else (
                    delete_items if phase == "delete_chat_files" else []
                )
                if isinstance(index, int) and 1 <= index <= len(candidates):
                    params["session_hash"] = candidates[index - 1].session_hash
                elif phase == "verify_chats" and any(entry.session_hash == item.get("session_hash") for entry in plan.items):
                    params["session_hash"] = item["session_hash"]
                code = getattr(exc, "code", None)
                io_error = exc if isinstance(exc, OSError) else exc.__cause__
                if code is None and isinstance(io_error, OSError):
                    code = "FILE_IO_ERROR"
                    if isinstance(io_error.errno, int):
                        params["errno"] = io_error.errno
                    if isinstance(getattr(io_error, "winerror", None), int):
                        params["winerror"] = io_error.winerror
                if code == "STATE_CHANGED_RETRY" and item.get("phase") in {"apply_chats", "delete_chat_files"}:
                    code = "DESTINATION_CHANGED"
                messages = {
                    "CODEX_STATE_UNKNOWN": "Could not determine whether Codex is running.",
                    "DESTINATION_APPEARED": "Destination appeared after backup.",
                    "DESTINATION_CHANGED": "Destination changed after backup.",
                    "DESTINATION_CHANGED_SINCE_PLAN": "Destination changed after the confirmed plan was built.",
                    "STATE_CHANGED_RETRY": "A file changed while the operation was running.",
                    "DESTINATION_VERIFY_FAILED": "Destination verification failed after replacement.",
                    "TRANSFER_READBACK_MISMATCH": "The written chat did not match the plan on read-back.",
                    "FILE_IO_ERROR": "The operating system could not complete a file operation.",
                    "PLAN_CHANGED": "The source no longer matches the plan.",
                }
                code = code if code in messages else "SESSION_TRANSFER_FAILED"
                current[0] = journals.transition(
                    current[0], failure, failure=exc, failure_code=code, failure_params=params,
                    failure_message=messages.get(code, "Session transfer stopped; recovery may be required."),
                )
                if failure is JournalState.RECOVERY_REQUIRED:
                    exc.recovery_operation_id = current[0].operation_id
                    exc.recovery_message = messages.get(code, "Session transfer stopped; recovery may be required.")
            except Exception:
                LOG.exception("Could not persist terminal session transfer journal state")
            raise
        # After COMMITTED nothing fails the run (CS-513); an error here used to
        # attempt COMMITTED -> FAILED and report a finished transfer as failed.
        try:
            _record_semantic_manifest(cfg, plan, local_by_hash, remote_by_hash)
        except Exception:
            LOG.exception("semantic manifest not recorded; the next transfer records it")
        try:
            mgr.prune()
        except Exception:
            LOG.exception("old backups not pruned; the next run prunes them")
    if owns_cache and parse_cache is not None:
        parse_cache.publish()
    return _branches_written(copies)


def _branches_written(copies: SyncPlan) -> int:
    # A session plan deletes only a file its own branch replaces, so every
    # deletion belongs to a copy already counted (laptop, 2026-10-23: one chat
    # continued from `.zst` was reported as two).
    return len(copies.to_local) + len(copies.to_cloud)


def _rebuild_transfer_plan(
    cfg: AppConfig,
    local_dir: Path,
    cloud_dir: Path,
    plan: TransferPlan,
    resolutions_path: Path | None,
    *,
    parse_cache: SessionParseCache | None = None,
) -> tuple[TransferPlan, dict[str, object], dict[str, object]]:
    local_catalog, remote_catalog = scan_both_sides(
        local_dir, cloud_dir, local_machine=plan.source_machine, remote_machine=plan.target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        parse_cache=parse_cache,
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
        path_rules=path_rules(cfg),
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
    every copy is in place. A chat Codex compressed and the other machine
    continued (`IN_PLACE_DECOMPRESSED`, CS-430) goes the same way: the plain
    file is written and the `.zst` removed. So does a mirror copy kept in a
    container codexSync never writes (`MIRROR_CONTAINER_REPLACED`, CS-519).
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
            raise FailSafeError("A planned branch is no longer present on its source side", code="PLAN_CHANGED")
        destination = root / Path(*item.target_relative_path.split("/"))
        _require_within(destination, root, "session transfer destination")
        source_root = cloud_dir if towards_local else local_dir
        old = (local_by_hash if towards_local else remote_by_hash).get(item.session_hash)
        destination_hash = item.local_sha256 if towards_local else item.remote_sha256
        replaced = old is not None and Path(old.relative_path) == Path(item.target_relative_path)
        bucket.append(
            CopyAction(
                src=source_root / Path(*source.relative_path.split("/")),
                dst=destination,
                relative_path=item.target_relative_path,
                codec=codec,
                # What the confirmed plan saw on the source side; staging
                # refuses anything else (CS-515).
                expected_sha256=(item.remote_sha256 if towards_local else item.local_sha256) or None,
                destination_precondition=(
                    _transfer_destination(old, destination_hash) if replaced else DestinationPrecondition(exists=False)
                ),
                session_hash=item.session_hash,
            )
        )
        if (
            MOVES_BRANCH in item.codes or IN_PLACE_DECOMPRESSED in item.codes
            or MIRROR_CONTAINER_REPLACED in item.codes
        ):
            if old is None:
                raise FailSafeError("A planned move no longer finds the file it moves", code="PLAN_CHANGED")
            if old.relative_path == item.target_relative_path:
                raise FailSafeError("A planned move names the file it moves as its destination")
            old_path = root / Path(*old.relative_path.split("/"))
            _require_within(old_path, root, "session transfer move source")
            deletions.append(DeleteAction(
                path=old_path, relative_path=old.relative_path,
                side="local" if towards_local else "cloud",
                destination_precondition=_transfer_destination(old, destination_hash),
                session_hash=item.session_hash,
            ))
    return SyncPlan(to_local=to_local, to_cloud=to_cloud, deletions=deletions)


def _transfer_destination(branch: SessionDescriptor, expected_sha256: str) -> DestinationPrecondition:
    if not expected_sha256 or branch.sha256 != expected_sha256:
        raise FailSafeError("A destination branch no longer matches the plan", code="PLAN_CHANGED")
    if branch.scan_signature is None:
        return DestinationPrecondition(exists=True, mtime_ns=branch.mtime_ns, content_sha256=expected_sha256)
    size, mtime_ns, file_id = branch.scan_signature
    device, inode = (int(part, 16) for part in file_id.split(":")) if file_id else (None, None)
    return DestinationPrecondition(True, mtime_ns, size, device, inode, expected_sha256)


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
    # Only a resolution this apply carries out. One held back -- by the layout
    # gate, a projected chat, the working set or the sync direction --
    # overwrites nothing, so there is nothing to keep yet; bundling it anyway
    # put a new full copy into the cloud on every sync while the item stayed
    # blocked, and a bundle that could not be written failed the whole apply
    # (CS-496).
    resolved = [
        item for item in plan.items
        if item.action.writes
        and ("RESOLVED_BY_USER" in item.codes or RESOLVED_BY_RULE in item.codes)
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


def _transfer_readback_error(message: str, *, code: str, params: dict, session_hash: str) -> FailSafeError:
    error = FailSafeError(message, code=code, params=params)
    error.transfer_session_hash = session_hash
    return error


def _verify_transferred_branch(
    work: tuple[TransferItem, Path, str, int],
) -> None:
    """Read one committed branch; callers may safely run these checks in parallel."""
    item, written, expected, planned_records = work
    digest = hashlib.sha256()
    records = 0
    try:
        with open_jsonl(written) as handle:
            for line in handle:
                records += 1
                digest.update(line)
    except JSONL_READ_ERRORS as exc:
        raise _transfer_readback_error(
            f"Transferred branch could not be read back after writing: {exc}",
            code="TRANSFER_READBACK_MISMATCH", params={"relative_path": item.target_relative_path or ""},
            session_hash=item.session_hash,
        ) from exc
    if digest.hexdigest() != expected or records != planned_records:
        raise _transfer_readback_error(
            "Transferred branch does not match the plan after writing; recovery is required",
            code="TRANSFER_READBACK_MISMATCH", params={"relative_path": item.target_relative_path or ""},
            session_hash=item.session_hash,
        )


def _verify_transferred_branches(
    cfg: AppConfig, plan: TransferPlan, local_dir: Path, cloud_dir: Path, *,
    scheduler: WorkScheduler | None = None,
    phase_progress: ProgressCallback | None = None,
) -> None:
    """Re-read every write while the journal still makes failure recoverable."""
    work = []
    for item in plan.items:
        direction = transfer_direction(item)
        if direction is None:
            continue
        towards_local = direction == "local"
        root = local_dir if towards_local else cloud_dir
        work.append((
            item,
            root / Path(*(item.target_relative_path or "").split("/")),
            item.remote_sha256 if towards_local else item.local_sha256,
            item.remote_records if towards_local else item.local_records,
        ))
    if phase_progress is not None:
        phase_progress("verify_chats", 0, len(work))
    if scheduler is None:
        for index, entry in enumerate(work, start=1):
            _verify_transferred_branch(entry)
            if phase_progress is not None:
                phase_progress("verify_chats", index, len(work))
        return

    def verified(index: int, _entry, _result: None) -> None:
        if phase_progress is not None:
            phase_progress("verify_chats", index + 1, len(work))

    scheduler.map_read(
        _verify_transferred_branch, work, pool="io", large_file=lambda _entry: True,
        on_result=verified,
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


def progress_events_folder(config_path: Path) -> Path | None:
    """Locate the progress log folder without creating it."""
    cfg = load_config(config_path)
    return progress_log_dir(cfg.paths.temp_dir, locate_local_state_dir(cfg))


def run_sync(
    ctx: AppContext, dry_run: bool, *, origin: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    scheduler: WorkScheduler | None = None,
    event_progress: ProgressEventCallback | None = None,
    cancellation: CancellationToken | None = None,
    finish_cancellation: bool = True,
    on_commit: Callable[[], None] | None = None,
) -> None:
    """Apply (or dry-run) the plan in `ctx`.

    `origin` -- ``window``, ``cli`` or ``unattended`` -- is written into the
    journal so a history can say who started the run; it decides nothing.
    """
    # Any conflict left in the plan stops the run, whatever `conflict.policy`
    # says: a policy that resolves one never leaves it here, and what is left
    # (equal times under `manual_abort`, a disputed or mass deletion, a case
    # collision) would otherwise be recorded as agreement (CS-295).
    if cancellation is not None:
        cancellation.checkpoint()
    if ctx.plan.conflicts:
        details = ", ".join(ctx.plan.conflicts)
        if not ctx.config.conflict.report_conflicts:
            details = "hidden by configuration"
        raise ConflictError(f"Conflict detected for files: {details}. Resolve manually and rerun.", code="FILE_CONFLICTS", params={"count": len(ctx.plan.conflicts)})

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
        if cancellation is not None:
            cancellation.checkpoint()
            cancellation.finish()
        return

    machine = ctx.config.identity.machine_id or platform.node()
    with (
        OperationLock(ctx.config.paths.temp_dir, state_root=ctx.local_dir, machine_id=machine, family="sync") as lock,
        ProgressLog(progress_log_dir(ctx.config.paths.temp_dir, ctx.local_dir), "sync", event_progress) as events,
    ):
        journals = JournalStore(ctx.config.paths.temp_dir)
        roots_id = rollback_roots_id(ctx.local_dir, ctx.cloud_dir) if cancellation is not None else None
        journal = journals.begin(
            "sync",
            _plan_hash(ctx.plan, cancellation=cancellation),
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
            if cancellation is not None:
                cancellation.begin_commit()
            if on_commit is not None:
                on_commit()
            current[0] = journals.transition(current[0], JournalState.COMMITTING)

        def before_replace() -> None:
            if current[0].state == JournalState.BACKED_UP:
                pre_commit()
            ctx.safety_gate.require(OperationKind.SYNC, final=True)

        try:
            with nullcontext(scheduler) if scheduler is not None else WorkScheduler() as work:
                engine = SyncEngine(
                    backup_manager=mgr,
                    temp_dir=ctx.config.paths.temp_dir,
                    backup_before_overwrite=True,
                    fail_on_unknown=True,
                    pre_commit_check=pre_commit if cancellation is None else None,
                    before_replace_check=before_replace,
                    after_backup=after_backup,
                    before_stage_check=lambda: ctx.safety_gate.require(OperationKind.SYNC),
                    progress=progress,
                    scheduler=work,
                    stage_operation_id=journal.operation_id,
                    progress_events=events,
                    cancellation=cancellation,
                )
                engine.execute(ctx.plan, dry_run=False)
            if current[0].state == JournalState.BACKED_UP:
                pre_commit()  # no actions, but the handoff may have later phases
            if cancellation is not None:
                cancellation.complete_commit(finish=finish_cancellation)
            current[0] = journals.transition(current[0], JournalState.COMMITTED)
        except RollbackRequested as exc:
            events.recovery_required = True
            current[0] = journals.transition(
                replace(current[0], rollback_files=tuple(engine.applied_files), rollback_roots=roots_id),
                JournalState.RECOVERY_REQUIRED, failure=exc,
            )
            with lock.allow_nested_recovery(), scheduler.exclusive() if scheduler is not None else nullcontext():
                rollback_applied_files(ctx.config, current[0], ctx.safety_gate, target=None, dry_run=False)
            events.recovery_required = False
            raise OperationRolledBack("Stopped by the user. The changed files were rolled back.") from None
        except Exception as exc:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            events.recovery_required = failure == JournalState.RECOVERY_REQUIRED
            if cancellation is not None and cancellation.finish() == CancellationState.ROLLING_BACK and events.recovery_required:
                current[0] = replace(current[0], rollback_files=tuple(engine.applied_files), rollback_roots=roots_id)
            try:
                kwargs = {"failure_code": exc.code} if isinstance(exc, OperationCancelled) else {}
                current[0] = journals.transition(current[0], failure, failure=exc, **kwargs)
            except Exception:
                LOG.exception("Could not persist terminal mutation journal state")
            raise
        finally:
            if cancellation is not None and finish_cancellation:
                cancellation.finish()
        # Every file is in place and the journal says so. Nothing after this
        # line may turn the run into a failure (CS-513): a rescan that hit a
        # file a cloud client was replacing made a finished sync
        # RECOVERY_REQUIRED, and a prune error tried COMMITTED -> FAILED.
        _save_sync_manifest(ctx)
        try:
            mgr.prune()
        except Exception:
            LOG.exception("old backups not pruned; the next run prunes them")


def _save_sync_manifest(ctx: AppContext) -> None:
    """Record the baseline after a committed sync; a failure is logged, never raised.

    Saved only after COMMITTED (CS-440): a rollback of an unfinished run
    restores the files, not the manifest, and a baseline naming the
    rolled-back content would push old files over new ones. A stale baseline
    is safe: equal sides are skipped and a one-sided change against it reads
    as a conflict.
    """
    try:
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
    except Exception:
        LOG.exception("sync manifest not saved; the next run records it")


# --- handing work between machines (CS-328, `D-018`) -------------------------

#: How often a load looks again while the cloud is still delivering.
HANDOFF_DELIVERY_POLL_SECONDS = 15.0
#: How often the watcher asks whether Codex is running.
HANDOFF_WATCH_POLL_SECONDS = 10.0
# A retried load waits 1, 2, 4 ... polls, never more than this (CS-449).
HANDOFF_WATCH_MAX_BACKOFF_POLLS = 60


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


def _held_from_codex(item) -> bool:
    """A chat the cloud copy would have written into `.codex` that a working
    set or a one-way `sync.direction` held back -- not loaded, by choice."""
    if item.action not in {TransferAction.OUT_OF_SCOPE, TransferAction.HELD_BY_DIRECTION}:
        return False
    if f"WOULD_BE_{TransferAction.FAST_FORWARD_LOCAL.value}" in item.codes:
        return True
    return (
        f"WOULD_BE_{TransferAction.ARCHIVE_TRANSITION.value}" in item.codes
        and ARCHIVE_FOLLOWS_LOCAL not in item.codes
    )


#: The steps of a full sync that may fail without failing it, in run order.
HANDOFF_STEPS = ("projects", "chat_archive", "chat_names", "catalogue")


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
    #: Chats newer in the cloud copy that the working set or a one-way
    #: `sync.direction` kept out of `.codex` (CS-503). Not loaded, by choice.
    chats_held_back: int = 0
    #: Settings files newer in the cloud copy that `sync.direction = to_cloud`
    #: did not load (CS-503).
    files_held_back: int = 0
    #: Chats changed on both machines that the conflict rule decided (D-027);
    #: the copy not kept of each is in the conflict bundle.
    chats_decided_by_rule: int = 0
    #: Projects another machine had that were added here (CS-333).
    projects_added: int = 0
    #: Changes written into the project list: added projects, pins, order, bindings.
    project_changes: int = 0
    #: Added projects without a folder on this machine -- reported, not a fault (D-034).
    projects_missing_folders: int = 0
    #: Projects removed in Codex here waiting for the person's answer (D-039).
    projects_waiting: int = 0
    #: Projects removed here because another machine removed them on purpose (D-039).
    projects_removed: int = 0
    #: Chat files Codex's catalogue does not list that Codex takes up on its
    #: next start, because a rebuild of its own is pending (D-024).
    chats_codex_will_list: int = 0
    #: Chat files Codex's catalogue does not list. A sync no longer asks Codex
    #: to rebuild its chat list (D-032): `sessions catalogue` does, on request.
    chats_codex_does_not_list: int = 0
    #: Chat files Codex still does not list although it was asked once before.
    chats_codex_ignores: int = 0
    #: Chats archived / brought back in Codex's catalogue as their files moved (D-035).
    chats_archived: int = 0
    chats_restored: int = 0
    #: Chat names taken from other machines (D-025).
    chat_names_set: int = 0
    #: Chats named here that another machine names differently; local kept.
    chat_names_kept: int = 0
    #: Names waiting for Codex to list their chat here; set on a later sync.
    chat_names_waiting: int = 0
    #: Projects whose folder here holds less than another machine had (D-026).
    project_files_behind: tuple["ProjectFilesItem", ...] = ()
    #: Steps after the settings and chats that failed this time and were
    #: logged: ``projects``, ``chat_archive``, ``chat_names``, ``catalogue``
    #: (`HANDOFF_STEPS`).
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
        raise ConfigError(
            "the handoff folder is kept beside state.manifest_file, which is empty: set "
            "state.manifest_file to a file inside the synced workspace"
        )
    return root


def _handoff_machine(cfg: AppConfig) -> str:
    try:
        return machine_key(require_guardian_identity(cfg))
    except HandoffError as exc:
        raise ConfigError(str(exc)) from exc


def _handoff_owed_marker(cfg: AppConfig, machine: str) -> Path:
    """Where this machine notes cloud writes it has not announced yet (CS-464).

    In `temp_dir`, which is this machine's, named by machine all the same in
    case a config shares it.
    """
    return cfg.paths.temp_dir / "handoff" / f"owed-{machine}"


def _remember_handoff_owed(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(files_now_utc() + "\n", encoding="utf-8")
    except OSError:
        LOG.exception("could not note the handoff still owed; the next sync that writes to the cloud makes it")


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
        checked = awaiting_delivery(pending)
        current = Fingerprinter().files(cfg, wanted=handoff_wanted_keys(checked))
        deliveries = tuple(delivery(record, current) for record in checked)
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
    cancellation: CancellationToken | None = None,
) -> float:
    """Return once every pending handoff is in the cloud copy; the seconds waited.

    A handoff a later pending one already carries is not waited for (CS-438).
    """
    waiting = awaiting_delivery(pending)
    started = monotonic()
    wanted = handoff_wanted_keys(waiting)
    while True:
        if cancellation is not None:
            cancellation.checkpoint()
        current = fingerprints.files(cfg, wanted=wanted)
        late = [item for item in (delivery(record, current) for record in waiting) if not item.delivered]
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


@contextmanager
def _log_handoff_preparation_cancel(
    cfg: AppConfig, progress: ProgressEventCallback | None,
) -> Iterator[None]:
    """Use the regular progress log when no settings operation was started."""
    try:
        yield
    except OperationCancelled:
        try:
            folder = progress_log_dir(cfg.paths.temp_dir, locate_local_state_dir(cfg))
        except (ConfigError, OSError):
            folder = None
        with ProgressLog(folder, "sync", progress):
            raise


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
    event_progress: ProgressEventCallback | None = None,
    conflict_policy: str | None = None,
    direction: str | None = None,
    close_codex: bool = True,
    cancellation: CancellationToken | None = None,
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

    The caller owns the lifetime of a supplied token. Use a token with
    rollback disabled because settings and chats commit separately.
    """
    cfg = load_config(config_path)
    with _log_handoff_preparation_cancel(cfg, event_progress):
        if cancellation is not None:
            cancellation.checkpoint()
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
        twin = board.case_twin(machine)
        if twin is not None:
            raise ConfigError(
                handoff_case_twin_message(machine, twin),
                code="MACHINE_ID_CASE_TWIN", params={"machine": machine, "other": twin},
            )
        pending = board.pending(machine)
        fingerprints = Fingerprinter()
        parse_cache = _session_parse_cache(config_path, cfg, locate_local_state_dir(cfg))
        if wait_seconds is None:
            wait_seconds = float(cfg.handoff.delivery_wait_minutes) * 60.0
        waited = 0.0
        if pending:
            try:
                waited = _wait_for_delivery(
                    cfg, pending, fingerprints, wait_seconds=wait_seconds, poll_seconds=poll_seconds,
                    monotonic=monotonic, sleep=sleep, on_wait=on_wait, cancellation=cancellation,
                )
            except HandoffNotDelivered as exc:
                if not accept_undelivered:
                    raise
                LOG.warning("loading anyway, as asked: %s", exc)

        # Chats first, and read-only: a decision found here stops the run before
        # the settings sync has written anything.
        if cancellation is not None:
            cancellation.checkpoint()
        source, plan, resolutions_path = _full_sync_chat_plan(
            config_path, cfg, board, machine, pending, progress=progress, conflict_policy=conflict_policy,
            direction=direction, cancellation=cancellation, parse_cache=parse_cache,
        )
        if cancellation is not None:
            cancellation.checkpoint()
        plans = _plans_dir(cfg)
        pair = session_pair_name(source, machine)
        if plan.volatile:
            raise SafetyPreconditionError("Codex started while the chats were being read; nothing was written", code="CODEX_STARTED_DURING_READ")
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
        ctx = build_context(
            config_path, enforce_safety=True, conflict_policy=conflict_policy, direction=direction,
            cancellation=cancellation,
        )
        # Remember the handoff is owed before the first write to the cloud copy,
        # and forget it only once the record says so: a run that fails after
        # writing -- in the chats, a rescan, the record itself -- would otherwise
        # leave work in the cloud that no handoff announces (CS-504).
        owed = _handoff_owed_marker(cfg, machine)
        will_write_cloud = bool(ctx.plan.to_cloud) or any(
            transfer_direction(item) == "mirror" for item in plan.items
        )
    # "File 3 of 10" while each half writes (CS-339).
    run_sync(
        ctx, dry_run=False, origin=origin,
        progress=lambda done, total: report_progress(progress, "sync_settings", done, total),
        event_progress=event_progress, cancellation=cancellation, finish_cancellation=False,
        on_commit=(lambda: _remember_handoff_owed(owed)) if will_write_cloud else None,
    )
    report_progress(progress, "sync_chats", 0, 0)
    session_actions = apply_session_transfer(
        config_path, plan_path=plan_path, confirm_plan=plan.plan_id, resolutions_path=resolutions_path,
        origin=origin, progress=lambda done, total: report_progress(progress, "sync_chats", done, total),
        phase_progress=progress,
        event_progress=event_progress,
        parse_cache=parse_cache,
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
    projects = step("projects", lambda: sync_projects(
        config_path, origin=origin, planned_here=True, parse_cache=parse_cache,
    ))
    # A chat archived or brought back on the other machine arrived as a file
    # move; Codex's catalogue row still names the old path and state until it
    # is pointed at the file (D-035).
    report_progress(progress, "follow_chat_archive", 0, 0)
    archive = step("chat_archive", lambda: follow_chat_archive(config_path, origin=origin, planned_here=True))
    # Names live only in Codex's catalogue, never in the chat file (D-025):
    # set the other machines' names on chats Codex already lists here, and
    # publish this machine's.
    report_progress(progress, "sync_chat_names", 0, 0)
    names = step("chat_names", lambda: sync_chat_names(config_path, origin=origin, planned_here=True))
    # Chat files Codex's catalogue does not list -- written now or by an
    # earlier run -- are invisible until Codex takes them up (D-024). Only
    # counted: asking Codex to rebuild its chat list makes its next start walk
    # every chat file, and a start ended part-way left Codex unable to start
    # at all (2026-10-09). That request is `sessions catalogue`, made by a
    # person who can leave Codex open until it finishes (D-032).
    report_progress(progress, "refresh_catalogue", 0, 0)
    catalogue = step("catalogue", lambda: refresh_thread_catalogue(config_path))
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
    # make every other machine wait for a "delivery" of what it already has --
    # unless an earlier run wrote and could not announce it (CS-464).
    handed_off = to_cloud > 0 or owed.is_file()
    own_files: dict = {}
    if handed_off:
        try:
            own_files = fingerprints.files(cfg, strict=True)
        except HandoffError as exc:
            # A fingerprint missing a file would let another machine count this
            # handoff delivered without it: keep the previous id, and remember
            # that what this run wrote is still to be announced (CS-464).
            LOG.error("nothing handed off: %s; the next sync hands it off", exc)
            handed_off = False
            _remember_handoff_owed(owed)
    # A run that only sends (`sync.direction = to_cloud`) loaded nothing from
    # anyone: marking their handoffs taken would tell the watcher "you can
    # start Codex" and stop "not loaded here" for work that never came (CS-503).
    one_way_out = (direction or cfg.sync.direction) == "to_cloud"
    taken = () if one_way_out else tuple(pending)
    try:
        record = record_handoff(
            root, machine, files=own_files, taken=taken, new_handoff=handed_off,
        )
    except HandoffError as exc:
        # Settings and chats are written; the owed mark stays, so the next
        # run announces them once this machine's file reads again (CS-502).
        _remember_handoff_owed(owed)
        raise FailSafeError(
            f"The sync finished, but the handoff was not recorded: {exc}",
            code="HANDOFF_FILE_UNREADABLE", params={"path": str(record_path(root, machine))},
        ) from exc
    if handed_off:
        owed.unlink(missing_ok=True)
    if parse_cache is not None:
        parse_cache.publish()
    LOG.info(
        "handoff finished: %d file(s) and %d chat(s) written; took %s; %s",
        ctx.plan.action_count, session_actions,
        ", ".join(item.machine for item in taken) or "nothing",
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
        projects_missing_folders=(
            len(projects.plan.missing_folders) + len(projects.plan.unplaced) if projects else 0
        ),
        projects_waiting=len(projects.plan.waiting) if projects else 0,
        projects_removed=len(projects.plan.removed) if projects else 0,
        # Codex has a rebuild of its own pending: it walks the files on its
        # next start. Nothing of this run asked for it (D-032).
        chats_codex_will_list=(
            len(catalogue.plan.unnamed)
            if catalogue and catalogue.plan.status is RefreshStatus.ALREADY_PENDING else 0
        ),
        chats_codex_does_not_list=(
            len(catalogue.plan.unnamed) if catalogue and catalogue.plan.status is RefreshStatus.NEEDED else 0
        ),
        chats_codex_ignores=(
            len(catalogue.plan.unnamed)
            if catalogue and catalogue.plan.status is RefreshStatus.ALREADY_ASKED else 0
        ),
        chats_archived=archive.plan.archived if archive and archive.written else 0,
        chats_restored=archive.plan.restored if archive and archive.written else 0,
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
        chats_held_back=sum(1 for item in plan.items if _held_from_codex(item)),
        files_held_back=len(ctx.plan.skipped) if one_way_out else 0,
        taken=tuple(item.machine for item in taken),
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
    cancellation: CancellationToken | None = None,
    parse_cache: SessionParseCache | None = None,
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
        conflict_policy=conflict_policy, direction=direction, cancellation=cancellation,
        parse_cache=parse_cache,
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

    @property
    def volatile(self) -> bool:
        """Read while Codex may be running: a picture, never a plan a write may use."""
        return bool(
            self.files.volatile or self.chats.volatile
            or (self.projects is not None and self.projects.volatile)
        )


@dataclass(frozen=True, slots=True)
class FullSyncSummary:
    """A full sync preview counted the one way the console and the window both show it."""

    machine: str
    source: str
    files: int
    file_conflicts: int
    chats_to_cloud: int
    chats_to_codex: int
    #: Chats the run would stop on before writing anything (a conflict the
    #: rule leaves open, a target collision).
    chats_to_decide: int
    #: ``None`` when this state's project list cannot be carried.
    projects_to_add: int | None
    project_changes: int | None
    waiting_for: tuple[str, ...]
    volatile: bool


def summarize_full_sync(preview: FullSyncPreview) -> FullSyncSummary:
    plan = preview.chats
    projects = preview.projects
    return FullSyncSummary(
        machine=preview.machine,
        source=preview.source,
        files=preview.files.plan.action_count,
        file_conflicts=len(preview.files.plan.conflicts),
        chats_to_cloud=sum(1 for item in plan.items if transfer_direction(item) == "mirror"),
        chats_to_codex=sum(1 for item in plan.items if transfer_direction(item) == "local"),
        chats_to_decide=sum(1 for item in plan.blocked_items if item.action in _UNRESOLVED_TRANSFER_BLOCKS),
        projects_to_add=len(projects.plan.added) if projects is not None else None,
        project_changes=projects.plan.action_count if projects is not None else None,
        waiting_for=preview.waiting_for,
        volatile=preview.volatile,
    )


def preview_full_sync(
    config_path: Path,
    *,
    conflict_policy: str | None = None,
    direction: str | None = None,
    progress: ProgressCallback | None = None,
    enforce_safety: bool = True,
) -> FullSyncPreview:
    """Everything `run_handoff` would plan, and nothing written.

    By default Codex must be closed, as for the run itself (`sync --dry-run`):
    the preview then describes the state the run will find. With
    ``enforce_safety`` off it is built while Codex runs (`plan`, the window's
    *Dry run*, CS-411) and is `volatile`: a picture of a state Codex is still
    writing, which no write may use -- every apply builds its own plans behind
    the gate.
    """
    cfg = load_config(config_path)
    _require_mutation_compatible_config(cfg)
    machine = _handoff_machine(cfg)
    board = read_board(_handoff_root(cfg))
    pending = board.pending(machine)
    parse_cache = _session_parse_cache(config_path, cfg, locate_local_state_dir(cfg))
    files = build_context(
        config_path, enforce_safety=enforce_safety, conflict_policy=conflict_policy, direction=direction,
    )
    source, chats, _ = _full_sync_chat_plan(
        config_path, cfg, board, machine, pending, progress=progress, conflict_policy=conflict_policy,
        direction=direction,
        parse_cache=parse_cache,
    )
    try:
        projects = sync_projects(config_path, parse_cache=parse_cache)
    except ProjectsNotCarried as exc:
        LOG.warning("projects would not be carried: %s", exc)
        projects = None
    checked = awaiting_delivery(pending)
    current = Fingerprinter().files(cfg, wanted=handoff_wanted_keys(checked)) if pending else {}
    waiting = tuple(
        item.machine for item in (delivery(record, current) for record in checked)
        if not item.delivered
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
        raise SafetyPreconditionError(f"Codex is open and was not closed: {outcome.detail}. Nothing was written.", code="CODEX_NOT_CLOSED")
    deadline = monotonic() + wait_seconds
    while gate.check(OperationKind.SYNC).process_state is not ProcessState.STOPPED:
        if monotonic() >= deadline:
            raise SafetyPreconditionError(
                f"Codex was asked to quit but is still running after {int(wait_seconds)} s; "
                "close it from the tray. Nothing was written.", code="CODEX_STILL_RUNNING", params={"seconds": int(wait_seconds)}
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
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Run from sign-in: load while Codex is closed, hand off when it closes.

    Only a state the process check is sure of counts. ``UNKNOWN`` is neither
    a start nor a close, so a failed process listing can never trigger a
    sync -- and the gate inside every write checks again anyway.

    - At start, with Codex closed: load (waiting for the cloud to deliver).
    - Codex starts: mark this machine as working, and warn when another
      machine is still working or its handoff has not been taken here.
    - Codex closes: hand off.

    No error ends the loop: it is logged and reported, and the watcher goes
    on waiting for the next change. What ends it is a stop request
    (`watcher_stop`: its task was removed or switched off) or, on POSIX, a
    SIGTERM -- both acted on only between runs, so a write that has begun
    always finishes (CS-506). A wait for the cloud is not a write: a stop ends
    it at once.
    """
    cfg = load_config(config_path)
    if should_stop is None:
        stop_watch = StopWatch(cfg.paths.temp_dir)
        deferred = stop_watch.defer_sigterm()
        try:
            return watch_handoff(
                config_path, probe=probe, notifier=notifier, handoff=handoff, sleep=sleep,
                poll_seconds=poll_seconds, should_stop=stop_watch,
            )
        finally:
            if deferred:
                stop_watch.restore_sigterm()
    root = _handoff_root(cfg)
    machine = _handoff_machine(cfg)
    notify = notifier if notifier is not None else Notifier(enabled=bool(cfg.handoff.notify))
    sample = probe if probe is not None else (lambda: _codex_state(config_path))

    def stop_waiting(_late) -> None:
        if should_stop():
            raise WatcherStopping()

    # The watcher never asks Codex to quit: it acts when Codex closes, and at
    # start a Codex that is open is the person's, not a sync's (D-029).
    act = handoff if handoff is not None else (
        lambda path: run_handoff(path, close_codex=False, on_wait=stop_waiting)
    )

    def attempt(closing: bool, retry: bool = False) -> bool:
        """Run once; True when nothing is left to retry while Codex stays closed.

        A retry that fails again only logs: the person was told the first time.
        """
        say = (lambda *a, **k: None) if retry else notify
        try:
            result = act(config_path)
        except WatcherStopping:
            LOG.info("handoff watcher: the wait for the cloud ended, the watcher is stopping")
            return True
        except HandoffNotDelivered as exc:
            LOG.warning("%s", exc)
            late = exc.deliveries[0]
            say("notify.not_delivered", machine=late.machine, arrived=late.arrived, total=late.total)
            return False
        except SafetyPreconditionError:
            LOG.info("Codex is open; the handoff waits for it to close")
            if not closing:
                say("notify.codex_open")
            return False
        except ConflictError as exc:
            # A conflict needs a person; retrying would only repeat it.
            LOG.warning("handoff stopped: %s", exc)
            say("notify.conflict")
            return True
        except Exception as exc:  # noqa: BLE001 - the watcher outlives any one run
            LOG.exception("handoff failed")
            say("notify.failed", reason=type(exc).__name__)
            return False
        else:
            if result.taken and result.chats_not_loaded:
                notify(
                    "notify.loaded_partial", machine=", ".join(result.taken), count=result.chats_not_loaded,
                )
            elif result.taken and result.chats_held_back:
                notify(
                    "notify.loaded_held", machine=", ".join(result.taken), count=result.chats_held_back,
                )
            elif result.taken:
                notify("notify.loaded", machine=", ".join(result.taken))
            elif closing and result.handed_off:
                notify("notify.handed_off")
            return True

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

    def observe() -> ProcessState:
        # A config the cloud client holds for a moment, or a half-saved edit,
        # must not end the watcher: such a sample is simply undetermined (CS-435).
        try:
            return sample()
        except Exception:  # noqa: BLE001 - the watcher outlives any one sample
            LOG.warning("could not tell whether Codex is running; trying again", exc_info=True)
            return ProcessState.UNKNOWN

    # A load that has not happened or did not finish is retried while Codex
    # stays closed, with a growing pause counted in polls (CS-449); waiting
    # for Codex to be started and closed would ask for the very thing the
    # "nothing loaded" notification forbids.
    pending: bool | None = True  # None: nothing to retry; else the `closing` flag
    wait_polls = 0
    backoff = 1

    def run(closing: bool, retry: bool) -> None:
        nonlocal pending, wait_polls, backoff
        if attempt(closing=closing, retry=retry):
            pending, backoff = None, 1
        else:
            pending, wait_polls = closing, backoff
            backoff = min(backoff * 2, HANDOFF_WATCH_MAX_BACKOFF_POLLS)

    last: ProcessState | None = observe()
    if last is ProcessState.STOPPED:
        run(closing=False, retry=False)
    elif last is ProcessState.RUNNING:
        pending = None
        codex_started()
    else:
        last = None
    while not should_stop():
        sleep(poll_seconds)
        state = observe()
        if state is ProcessState.UNKNOWN:
            continue
        if state is ProcessState.RUNNING and last is not ProcessState.RUNNING:
            pending, backoff = None, 1
            codex_started()
        elif state is ProcessState.STOPPED and last is ProcessState.RUNNING:
            run(closing=True, retry=False)
        elif state is ProcessState.STOPPED and last is None:
            # The first sure sample: the load the start would have made.
            run(closing=False, retry=False)
        elif state is ProcessState.STOPPED and pending is not None:
            wait_polls -= 1
            if wait_polls <= 0:
                run(closing=pending, retry=True)
        last = state
    LOG.warning("handoff watcher: stopped as asked")


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
        # The task runs `sync --dry-run`, which follows `[sync] scope`: at
        # "full" it previews the whole sync, chats included (CS-544).
        if cfg.sync.scope == "full":
            preview = preview_full_sync(config_path)
            run_sync(preview.files, dry_run=True)
            return AutomationRun(mode, "DRY_RUN_FINISHED", actions=preview.files.plan.action_count)
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
        raise FailSafeError(f"{source} kept changing while it was read; close Codex or try again", code="STATE_CHANGED_RETRY") from exc


def _read_live_state(source: Path, config_path: Path) -> bytes:
    """The live global state for a mutation, with its failures mapped to exit codes.

    A bare `read_bytes` let a missing or locked file escape as a traceback and
    exit 1 (CS-325). Missing is a configuration answer -- which config named
    this path is the useful half of the message -- and anything else is a
    fail-safe stop, never a guess.
    """
    try:
        return read_shared(source)
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
    try:
        return StableReader(source, max_bytes=cfg.guardian.max_state_bytes).read_once().observation.payload
    except SourceMissingError:
        return None
    except SourceTooLargeError as exc:
        raise ConfigError(f"{source} is larger than guardian.max_state_bytes ({cfg.guardian.max_state_bytes})") from exc
    except SourceUnstableError as exc:
        raise FailSafeError(f"{source} kept changing while it was read; close Codex or try again", code="STATE_CHANGED_RETRY") from exc


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
    # A file a cloud client or antivirus holds is a fail-safe stop, not a
    # traceback (CS-535, as CS-325 did for the other writers).
    current = _read_live_state(source, config_path)
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
        raise ConfigError(f"There is no {source.name}; open Codex once so it creates its projects", code="NO_GLOBAL_STATE")
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
