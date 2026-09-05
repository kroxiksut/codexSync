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
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .config import require_guardian_identity
from .exceptions import ConfigError, ConflictError, FailSafeError
from .backup import BackupManager
from .manifest import build_manifest, load_manifest, save_manifest
from .mutation_journal import JournalState, JournalStore, MutationJournal
from .models import AppConfig, CopyAction, FileMeta, SyncPlan
from .operation_lock import OperationLock
from .path_mapping import mapping_digest
from .guardian_runner import GuardianRunner
from .guardian_store import GuardianStore
from .chat_directory import ChatDirectory, ChatEntry, ChatKind, build_chat_directory
from .chat_move import ChatMovePlan, apply_chat_moves_to_state, build_chat_move_plan
from .guardian_schema import (
    build_binding_value,
    detect_state_schema,
    replace_project_root,
    supports_project_creation,
    supports_root_remap,
    validate_global_state_references,
)
from .guardian_models import ValidationStatus
from .planner import build_sync_plan
from .preflight import (
    PreflightCheckResult,
    PreflightReport,
    print_preflight_report,
    run_preflight,
)
from .repair_plan import RepairActionKind, RepairPlan, build_repair_plan, load_repair_plan
from .restore import RestoreResult, restore_from_backup
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
)
from .safety_gate import OperationKind, ProcessState, SafetyGate
from .semantic_transfer import (
    BranchResolution,
    ResolutionChoice,
    TransferAction,
    TransferPlan,
    build_transfer_plan,
    descriptors_by_session_hash,
    load_transfer_plan,
    mirror_codec_for,
    save_transfer_plan,
)
from .semantic_store import SemanticStore
from .jsonl_codec import JSONL_READ_ERRORS, JsonlCodec, codec_of, open_jsonl
from .sqlite_audit import read_thread_placements
from .session_catalog import scan_sessions
from .session_index import (
    PROVEN_CONTRACTS,
    SESSION_INDEX_FILE,
    IndexParseResult,
    parse_session_index,
)
from .stable_reader import StableReader
from .state_locator import detect_local_state_dir, resolve_state_dirs
from .sync_engine import SyncEngine
from .version import PRODUCER_VERSION

LOG = logging.getLogger(__name__)

__all__ = [
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
    "print_preflight_report",
    "record_branch_resolution",
    "restore_from_backup",
    "commit_global_state",
    "move_chats",
    "save_transfer_plan",
    "scan_chats",
    "scan_session_transfer",
    "run_preflight",
    "run_sync",
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


def build_context(
    config_path: Path,
    manual_terminate_confirmation_override: bool | None = None,
    enforce_safety: bool = True,
) -> AppContext:
    cfg = load_config(config_path)
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
    local_dir, cloud_dir = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)

    local_idx, cloud_idx = _build_indexes(cfg, local_dir, cloud_dir)
    manifest = load_manifest(cfg.state.manifest_file, cfg.state.data_version)
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
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
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
) -> RepairPlan:
    cfg = load_config(config_path)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    decision = _make_safety_gate(cfg).check(OperationKind.REPAIR_SCAN)
    volatile = decision.process_state is not ProcessState.STOPPED
    observation = StableReader(
        local_dir / ".codex-global-state.json",
        max_bytes=cfg.guardian.max_state_bytes,
    ).read_once().observation
    catalog = scan_sessions(
        local_dir,
        volatile=volatile,
        source_machine=source_machine,
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
    gate = _make_safety_gate(cfg)
    gate.require(OperationKind.REPAIR_APPLY)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    source = local_dir / ".codex-global-state.json"
    original = source.read_bytes()
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
        manager = BackupManager(cfg.paths.backup_dir, machine, compression="none")
        journal = journals.begin(
            family, plan_id, action_count, backup_snapshot=manager.snapshot_name
        )
        backup_path = manager.backup_file(source, source.name)
        if backup_path is None or _hash_file(backup_path) != hashlib.sha256(original).hexdigest():
            journals.transition(journal, JournalState.FAILED)
            raise FailSafeError("Verified full backup could not be created")
        manager.finalize()
        journal = journals.transition(journal, JournalState.BACKED_UP)
        gate.require(operation, final=True)
        journal = journals.transition(journal, JournalState.COMMITTING)
        replaced = False
        temp = source.with_name(f".{source.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(candidate)
                handle.flush()
                os.fsync(handle.fileno())
            gate.require(operation, final=True)
            os.replace(temp, source)
            replaced = True
            post = validate_global_state_references(source.read_bytes())
            if post.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
                raise FailSafeError(f"{family} post-validation failed")
            journal = journals.transition(journal, JournalState.COMMITTED)
            manager.prune()
            return action_count
        except Exception:
            temp.unlink(missing_ok=True)
            journal = journals.transition(journal, JournalState.RECOVERY_REQUIRED)
            if replaced:
                try:
                    gate.require(operation, final=True)
                    rollback = source.with_name(f".{source.name}.{uuid.uuid4().hex}.rollback.tmp")
                    shutil.copy2(backup_path, rollback)
                    os.replace(rollback, source)
                    if hashlib.sha256(source.read_bytes()).hexdigest() != hashlib.sha256(original).hexdigest():
                        raise FailSafeError("Rollback verification failed")
                    journal = journals.transition(journal, JournalState.FAILED)
                except Exception:
                    LOG.exception("%s rollback could not be completed safely", family)
            raise


def scan_chats(
    config_path: Path,
    *,
    source_machine: str | None = None,
    target_machine: str | None = None,
) -> ChatDirectory:
    """List every chat and the project it currently sits under. Reads only.

    Runs while Codex is open like any other scan; the result is marked volatile
    because a chat open right now is still being written.
    """
    cfg = load_config(config_path)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    decision = _make_safety_gate(cfg).check(OperationKind.SESSION_SCAN)
    observation = StableReader(
        local_dir / ".codex-global-state.json",
        max_bytes=cfg.guardian.max_state_bytes,
    ).read_once().observation
    return build_chat_directory(
        local_dir,
        observation.payload,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=decision.process_state is not ProcessState.STOPPED,
        rules=cfg.path_mappings,
        source_machine=source_machine,
        target_machine=target_machine,
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
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    gate = _make_safety_gate(cfg)
    applying = confirm_plan is not None
    if applying:
        _require_mutation_compatible_config(cfg)
        gate.require(OperationKind.CHAT_MOVE)
        volatile = False
    else:
        volatile = gate.check(OperationKind.SESSION_SCAN).process_state is not ProcessState.STOPPED

    source = local_dir / ".codex-global-state.json"
    original = source.read_bytes()
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
) -> TransferPlan:
    """Classify every session branch on both sides. Reads only.

    Runs while Codex is open, like `plan`, but the result is marked volatile and
    a mutation must rebuild it.
    """
    cfg = load_config(config_path)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    cloud_dir = cfg.paths.cloud_root_dir
    decision = _make_safety_gate(cfg).check(OperationKind.SESSION_SCAN)
    volatile = decision.process_state is not ProcessState.STOPPED

    local_catalog = scan_sessions(
        local_dir, volatile=volatile, source_machine=source_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
    )
    remote_catalog = scan_sessions(
        cloud_dir, volatile=volatile, source_machine=target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
    )
    resolutions = load_branch_resolutions(resolutions_path) if resolutions_path else {}
    return build_transfer_plan(
        local_catalog, remote_catalog,
        local_root=local_dir, remote_root=cloud_dir,
        source_machine=source_machine, target_machine=target_machine,
        resolutions=resolutions,
        confirmed_bases=_recorded_bases(cfg),
        placements=read_thread_placements(local_dir),
        mirror_codec=cfg.semantic.mirror_compression,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=volatile,
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
    (``docs/experiments/session-index-contract.md``), so this command exists to
    say what an index contains and where the two disagree, and nothing more.
    """
    cfg = load_config(config_path)
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
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
    existing = load_branch_resolutions(output_path) if output_path.exists() else {}
    existing[conflict_id] = resolution
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
    return resolution


#: Blocked actions that stop an apply outright, because each one names a
#: decision the user still has to make. Every other blocked action is a
#: standing limit of what codexSync may write, so it is reported and the rest
#: of the plan still applies.
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
    local_dir = detect_local_state_dir(cfg.paths.local_state_dir)
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
    archive_moves = [item for item in plan.items if item.action is TransferAction.ARCHIVE_TRANSITION]
    if archive_moves:
        # Moving a branch between sessions/ and archived_sessions/ means deleting
        # the old copy, and leaving it would activate two branches of one session
        # at once. delete_policy is never, so this stays out of 0.2 rather than
        # being approximated.
        raise ConflictError(
            f"{len(archive_moves)} archive transition(s) require moving a branch, which needs a "
            "delete; delete_policy=never. Move these by hand after taking a backup."
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
        journal = journals.begin(
            "sessions", plan.plan_id, copies.action_count, backup_snapshot=mgr.snapshot_name
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
        except Exception:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            try:
                current[0] = journals.transition(current[0], failure)
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
    local_catalog = scan_sessions(
        local_dir, volatile=False, source_machine=plan.source_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
    )
    remote_catalog = scan_sessions(
        cloud_dir, volatile=False, source_machine=plan.target_machine,
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
    )
    fresh = build_transfer_plan(
        local_catalog, remote_catalog,
        local_root=local_dir, remote_root=cloud_dir,
        source_machine=plan.source_machine, target_machine=plan.target_machine,
        resolutions=load_branch_resolutions(resolutions_path) if resolutions_path else {},
        confirmed_bases=_recorded_bases(cfg),
        placements=read_thread_placements(local_dir),
        layout_id=plan.layout_id,
        # The codec the plan was frozen under, not whatever the config says
        # now. The plan is the contract the user confirmed by its id; a config
        # edited afterwards applies to the next scan, and reading it here would
        # rename every destination underneath a confirmation already given.
        mirror_codec=_plan_mirror_codec(plan),
        max_line_bytes=cfg.semantic.max_jsonl_line_bytes,
        volatile=False,
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
    """
    to_local: list[CopyAction] = []
    to_cloud: list[CopyAction] = []
    for item in plan.items:
        if not item.action.writes or item.action is TransferAction.ARCHIVE_TRANSITION:
            continue
        if not item.target_relative_path:
            raise FailSafeError("A writable plan item has no destination path")
        if item.action is TransferAction.FAST_FORWARD_LOCAL:
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
        source_root = cloud_dir if item.action is TransferAction.FAST_FORWARD_LOCAL else local_dir
        bucket.append(
            CopyAction(
                src=source_root / Path(*source.relative_path.split("/")),
                dst=destination,
                relative_path=item.target_relative_path,
                codec=codec,
            )
        )
    return SyncPlan(to_local=to_local, to_cloud=to_cloud)


def _bundle_resolved_conflicts(
    cfg: AppConfig,
    plan: TransferPlan,
    local_dir: Path,
    cloud_dir: Path,
    local_by_hash: dict,
    remote_by_hash: dict,
) -> list[Path]:
    """Preserve both raw branches of every conflict this apply resolves."""
    resolved = [item for item in plan.items if "RESOLVED_BY_USER" in item.codes]
    if not resolved:
        return []
    store = SemanticStore(cfg.semantic.root_dir, require_guardian_identity(cfg))
    bundles: list[Path] = []
    for item in resolved:
        local = local_by_hash.get(item.session_hash)
        remote = remote_by_hash.get(item.session_hash)
        if local is None or remote is None:
            raise FailSafeError("A resolved conflict is missing one of its branches")
        bundle = store.conflict_bundle(
            local_dir / Path(*local.relative_path.split("/")),
            cloud_dir / Path(*remote.relative_path.split("/")),
            session_id=local.session_id or remote.session_id or "",
            common_records=0,
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
        if item.action is TransferAction.NOOP
        or (item.action.writes and item.action is not TransferAction.ARCHIVE_TRANSITION)
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
        towards_local = item.action is TransferAction.FAST_FORWARD_LOCAL
        # The side that was copied *from* is the one whose bytes both sides now
        # hold, so it is the one that describes the agreed branch.
        source = (remote_by_hash if towards_local else local_by_hash).get(item.session_hash)
        descriptor = source or local_by_hash.get(item.session_hash) or remote_by_hash.get(item.session_hash)
        session_id = getattr(descriptor, "session_id", None)
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
        if not item.action.writes or item.action is TransferAction.ARCHIVE_TRANSITION:
            continue
        root = local_dir if item.action is TransferAction.FAST_FORWARD_LOCAL else cloud_dir
        expected = item.remote_sha256 if item.action is TransferAction.FAST_FORWARD_LOCAL else item.local_sha256
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
        planned_records = (
            item.remote_records if item.action is TransferAction.FAST_FORWARD_LOCAL else item.local_records
        )
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


def print_plan(plan: SyncPlan, *, volatile: bool = False) -> None:
    print("Plan:")
    if volatile:
        print("  state: VOLATILE (Codex is running or process state is unknown; rebuild before apply)")
    print(f"  to_local: {len(plan.to_local)}")
    print(f"  to_cloud: {len(plan.to_cloud)}")
    print(f"  actions: {plan.action_count}")
    print(f"  conflicts: {len(plan.conflicts)}")
    for rel_path in plan.conflicts:
        print(f"    conflict: {rel_path}")
    for item in plan.to_local:
        print(f"    cloud -> local: {item.relative_path}")
    for item in plan.to_cloud:
        print(f"    local -> cloud: {item.relative_path}")


def run_sync(ctx: AppContext, dry_run: bool) -> None:
    if ctx.plan.conflicts and ctx.config.conflict.policy == "manual_abort":
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
    )
    if dry_run:
        SyncEngine(
            backup_manager=mgr,
            temp_dir=ctx.config.paths.temp_dir,
            backup_before_overwrite=True,
            fail_on_unknown=True,
        ).execute(ctx.plan, dry_run=True)
        return

    with OperationLock(
        ctx.config.paths.temp_dir,
        state_root=ctx.local_dir,
        machine_id=ctx.config.identity.machine_id or platform.node(),
        family="sync",
    ):
        journals = JournalStore(ctx.config.paths.temp_dir)
        journal = journals.begin(
            "sync",
            _plan_hash(ctx.plan),
            ctx.plan.action_count,
            backup_snapshot=mgr.snapshot_name,
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
        )
        try:
            engine.execute(ctx.plan, dry_run=False)
            local_idx, cloud_idx = _build_indexes(ctx.config, ctx.local_dir, ctx.cloud_dir)
            manifest = build_manifest(local_idx, cloud_idx, ctx.config.state.data_version)
            save_manifest(manifest, ctx.config.state.manifest_file)
            current[0] = journals.transition(current[0], JournalState.COMMITTED)
            mgr.prune()
        except Exception:
            failure = (
                JournalState.RECOVERY_REQUIRED
                if current[0].state is JournalState.COMMITTING
                else JournalState.FAILED
            )
            try:
                current[0] = journals.transition(current[0], failure)
            except Exception:
                LOG.exception("Could not persist terminal mutation journal state")
            raise


def validate_config_only(config_path: Path) -> None:
    try:
        cfg = load_config(config_path)
        _ = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)
    except ConfigError:
        raise
