"""Read-only environment diagnostics behind doctor/preflight.

Every check here must stay side-effect free: OperationKind.DOCTOR is
declared side_effect_free in safety_gate, so a check may read and
report but must never create a probe file, least of all inside the Codex state
directory.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path

from .config import load_config
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_LATEST_GOOD_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    ValidationStatus,
)
from .guardian_schema import validate_global_state_references
from .manifest import load_manifest
from .models import AppConfig
from .runtime import _make_safety_gate
from .safety_gate import OperationKind, ProcessState
from .session_catalog import scan_sessions
from .session_index import SESSION_INDEX_FILE, parse_session_index
from .sqlite_audit import audit_sqlite
from .project_registry import PROVEN_PROJECT_REGISTRY, registry_note
from .state_locator import resolve_state_dirs

LOG = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class PreflightCheckResult:
    name: str
    status: str
    details: str

@dataclass(slots=True)
class PreflightReport:
    checks: list[PreflightCheckResult]

    @property
    def failures(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "FAIL"]

    @property
    def warnings(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "WARN"]

    @property
    def passed(self) -> list[PreflightCheckResult]:
        return [item for item in self.checks if item.status == "PASS"]

    @property
    def is_ok(self) -> bool:
        return not self.failures

def run_preflight(config_path: Path, operation: OperationKind = OperationKind.DOCTOR) -> PreflightReport:
    checks: list[PreflightCheckResult] = []

    try:
        cfg = load_config(config_path)
        checks.append(PreflightCheckResult("config", "PASS", f"Loaded config from {config_path}"))
    except Exception as exc:
        checks.append(PreflightCheckResult("config", "FAIL", f"Cannot load config: {exc}"))
        return PreflightReport(checks=checks)

    local_dir: Path | None = None
    cloud_dir: Path | None = None
    try:
        local_dir, cloud_dir = resolve_state_dirs(cfg.paths.local_state_dir, cfg.paths.cloud_root_dir)
        checks.append(PreflightCheckResult("state_dirs", "PASS", f"local={local_dir}; cloud={cloud_dir}"))
    except Exception as exc:
        checks.append(PreflightCheckResult("state_dirs", "FAIL", f"State directories are not ready: {exc}"))

    checks.append(_check_sync_rules(cfg))
    checks.append(_check_project_registry())
    if local_dir is not None:
        checks.append(_check_path_available("local_state", local_dir))
    if cloud_dir is not None:
        checks.append(_check_path_available("cloud_root", cloud_dir))
    checks.append(_check_path_available("backup_dir", cfg.paths.backup_dir))
    checks.append(_check_path_available("temp_dir", cfg.paths.temp_dir))

    try:
        _ = load_manifest(cfg.state.manifest_file, cfg.state.data_version)
        checks.append(PreflightCheckResult("manifest", "PASS", "Manifest data version is compatible"))
    except Exception as exc:
        checks.append(PreflightCheckResult("manifest", "FAIL", f"Manifest check failed: {exc}"))

    checks.append(_check_process_state(cfg, operation))
    if local_dir is not None:
        try:
            catalog = scan_sessions(local_dir, volatile=True, max_line_bytes=cfg.semantic.max_jsonl_line_bytes)
            invalid = sum(1 for item in catalog.descriptors if item.state.value in {"INVALID", "AMBIGUOUS"})
            status = "WARN" if invalid or catalog.codes else "PASS"
            checks.append(PreflightCheckResult(
                "session_catalog", status,
                f"sessions={len(catalog.descriptors)} invalid_or_ambiguous={invalid} graph_codes={len(catalog.codes)}",
            ))
        except Exception as exc:
            checks.append(PreflightCheckResult("session_catalog", "WARN", f"Session audit unavailable: {exc}"))
        try:
            sqlite_reports = audit_sqlite(local_dir, cold=False)
            indeterminate = sum(1 for item in sqlite_reports if item.status != "PASS")
            checks.append(PreflightCheckResult(
                "sqlite_audit", "WARN" if indeterminate else "PASS",
                f"database_sets={len(sqlite_reports)} indeterminate={indeterminate}",
            ))
        except Exception as exc:
            checks.append(PreflightCheckResult("sqlite_audit", "WARN", f"SQLite audit unavailable: {exc}"))
    if local_dir is not None:
        checks.append(_check_session_index(local_dir))
        checks.append(_check_global_state_schema(local_dir, cfg))
        checks.append(_check_guardian_latest_good(cfg))
    checks.append(_check_orphan_temp_files(cfg.paths.temp_dir))

    return PreflightReport(checks=checks)

def print_preflight_report(report: PreflightReport) -> None:
    print("Preflight report:")
    for item in report.checks:
        print(f"  [{item.status}] {item.name}: {item.details}")
    print(
        "Summary: "
        f"pass={len(report.passed)} warn={len(report.warnings)} fail={len(report.failures)}"
    )

def _check_sync_rules(cfg: AppConfig) -> PreflightCheckResult:
    """Say what this config lets a sync do, before it does it.

    A one-way direction and deletion propagation are both settings that change
    what a later run writes without changing what any plan looks like on the
    surface, so `doctor` names them (`D-012`, `D-013`).
    """
    detail = f"direction={cfg.sync.direction}; delete_policy={cfg.sync.delete_policy}"
    if cfg.sync.delete_policy == "propagate":
        return PreflightCheckResult(
            "sync_rules", "WARN",
            f"{detail}; a proven deletion on one side removes the file here, after a verified backup",
        )
    return PreflightCheckResult("sync_rules", "PASS", detail)


def _check_project_registry() -> PreflightCheckResult:
    """Say plainly that a project root is rewritten in the JSON only.

    Codex keeps projects in `state_*.sqlite` as well, and codexSync never
    writes there. Whether the runtime follows a JSON-only root change is
    unverified, so a person deciding to move a project should be told before
    they rely on it, not after.
    """
    schema = next(iter(PROVEN_PROJECT_REGISTRY), None)
    if schema is not None:
        return PreflightCheckResult("project_registry", "PASS", registry_note(schema))
    return PreflightCheckResult("project_registry", "WARN", registry_note(None))


def _check_path_available(name: str, directory: Path) -> PreflightCheckResult:
    """A diagnostic availability check that never creates a directory or probe."""
    try:
        if not directory.exists():
            return PreflightCheckResult(name, "WARN", f"Path does not exist yet: {directory}")
        if not directory.is_dir():
            return PreflightCheckResult(name, "FAIL", f"Path is not a directory: {directory}")
        _ = list(directory.iterdir())
        return PreflightCheckResult(name, "PASS", f"Path is readable: {directory}")
    except Exception as exc:
        return PreflightCheckResult(name, "FAIL", f"Cannot access {directory}: {exc}")

def _check_process_state(cfg: AppConfig, operation: OperationKind) -> PreflightCheckResult:
    decision = _make_safety_gate(cfg).check(operation)
    if decision.process_state is ProcessState.STOPPED:
        return PreflightCheckResult("codex_process", "PASS", decision.reason)
    if decision.process_state is ProcessState.RUNNING:
        status = "FAIL" if operation in {OperationKind.SYNC, OperationKind.RESTORE, OperationKind.REPAIR_APPLY} else "WARN"
        return PreflightCheckResult("codex_process", status, decision.reason)
    status = "FAIL" if operation in {OperationKind.SYNC, OperationKind.RESTORE, OperationKind.REPAIR_APPLY} else "WARN"
    return PreflightCheckResult("codex_process", status, decision.reason)

def _check_session_index(local_dir: Path) -> PreflightCheckResult:
    """Say what the index holds, and never that it is wrong for holding it.

    A repeated id and a session with no line at all are both normal in an
    append/update journal, so neither is a warning. What is worth a warning is
    a record that will not parse, a half-written tail, or the two plausible
    readings of a repeated id disagreeing -- which happens exactly when a clock
    ran backwards. An absent index is reported as absent, because it does not
    mean the sessions are absent.
    """
    try:
        result = parse_session_index(local_dir / SESSION_INDEX_FILE)
    except OSError as exc:
        return PreflightCheckResult("session_index", "WARN", f"Index unreadable: {exc}")
    if "MISSING_INDEX" in result.codes:
        return PreflightCheckResult(
            "session_index", "PASS", f"No {SESSION_INDEX_FILE}; sessions are read from disk"
        )
    if "EMPTY_INDEX" in result.codes:
        return PreflightCheckResult(
            "session_index", "PASS", f"{SESSION_INDEX_FILE} is empty; sessions are read from disk"
        )
    codes = [code for code in result.codes if code not in {"MISSING_INDEX", "EMPTY_INDEX"}]
    status = "WARN" if codes or not result.reductions_agree else "PASS"
    return PreflightCheckResult(
        "session_index", status,
        f"records={len(result.records)} sessions={len(result.reduced)} "
        f"contract={result.contract.value} reductions_agree={result.reductions_agree} "
        f"codes={','.join(codes) or 'none'}",
    )


def _check_global_state_schema(local_dir: Path, cfg: AppConfig) -> PreflightCheckResult:
    """Report whether Guardian can recognise this machine's state at all.

    An unrecognised schema means every snapshot is quarantined and `latest-good`
    never appears, so the protection is silently inert. Without this check the
    only symptom is an exit code from a command the user may never run.
    """
    source = local_dir / ".codex-global-state.json"
    if not source.is_file():
        return PreflightCheckResult("global_state_schema", "WARN", f"No state file at {source}")
    try:
        if source.stat().st_size > cfg.guardian.max_state_bytes:
            return PreflightCheckResult(
                "global_state_schema", "WARN",
                f"State file exceeds guardian.max_state_bytes ({cfg.guardian.max_state_bytes})",
            )
        report = validate_global_state_references(source.read_bytes())
    except OSError as exc:
        return PreflightCheckResult("global_state_schema", "WARN", f"Cannot read state file: {exc}")

    codes = ", ".join(report.codes) or "none"
    if report.status in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        return PreflightCheckResult(
            "global_state_schema", "PASS" if report.status is ValidationStatus.PASS else "WARN",
            f"schema={report.schema_id} projects={report.project_count} "
            f"bindings={report.binding_count} codes={codes}",
        )
    return PreflightCheckResult(
        "global_state_schema", "FAIL",
        f"Guardian would reject this state ({report.status.value}); codes={codes}. "
        "Snapshots go to quarantine and latest-good is never created.",
    )


def _check_guardian_latest_good(cfg: AppConfig) -> PreflightCheckResult:
    """Confirm a restorable snapshot actually exists for this machine."""
    machine = cfg.identity.machine_id
    if not machine:
        return PreflightCheckResult("guardian_latest_good", "WARN", "identity.machine_id is not set")
    pointer = cfg.guardian.root_dir / GUARDIAN_LATEST_GOOD_DIR_NAME / f"{machine}.json"
    if not pointer.is_file():
        return PreflightCheckResult(
            "guardian_latest_good", "WARN",
            f"No latest-good pointer for {machine}; run `guardian snapshot --once`",
        )
    try:
        snapshot_id = json.loads(pointer.read_text(encoding="utf-8"))["snapshot_id"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return PreflightCheckResult("guardian_latest_good", "FAIL", f"Pointer is unreadable: {exc}")
    snapshot = cfg.guardian.root_dir / GUARDIAN_SNAPSHOTS_DIR_NAME / machine / str(snapshot_id)
    if not (snapshot / GUARDIAN_COMMITTED_NAME).is_file():
        return PreflightCheckResult(
            "guardian_latest_good", "FAIL",
            "latest-good points at a snapshot that is missing or uncommitted",
        )
    stuck = _shrinks_quarantined_since(cfg.guardian.root_dir, machine, snapshot)
    if stuck:
        return PreflightCheckResult(
            "guardian_latest_good", "WARN",
            f"Restorable snapshot {snapshot_id}, but {stuck} newer state(s) went to quarantine for a drop in "
            "projects or bindings, so latest-good is no longer advancing; `guardian accept` shows why the "
            "counts fell and can take the current state as the new baseline",
        )
    return PreflightCheckResult("guardian_latest_good", "PASS", f"Restorable snapshot {snapshot_id}")


_SHRINK_CODES = frozenset({"PROJECT_COUNT_DROP", "BINDING_COUNT_DROP"})


def _shrinks_quarantined_since(root: Path, machine: str, snapshot: Path) -> int:
    """Quarantine events newer than latest-good that were rejected only as a shrink.

    Reads manifests only. Timestamps are compared as the store writes them
    (fixed-width UTC ISO strings), so string order is time order.
    """
    try:
        created = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))["created_at_utc"]
    except (OSError, KeyError, TypeError, ValueError):
        return 0
    base = root / "quarantine" / machine
    if not isinstance(created, str) or not base.is_dir():
        return 0
    count = 0
    for event in base.iterdir():
        try:
            raw = json.loads((event / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        codes = raw.get("reason_codes") if isinstance(raw, dict) else None
        when = raw.get("created_at_utc") if isinstance(raw, dict) else None
        if isinstance(codes, list) and isinstance(when, str) and when > created and _SHRINK_CODES & set(codes):
            count += 1
    return count


def _check_orphan_temp_files(temp_dir: Path) -> PreflightCheckResult:
    if not temp_dir.exists():
        return PreflightCheckResult("orphan_temp_files", "PASS", "Temp directory does not exist yet")
    orphan_files = [path for path in temp_dir.rglob("*.tmp") if path.is_file()]
    if orphan_files:
        return PreflightCheckResult(
            "orphan_temp_files",
            "WARN",
            f"Found {len(orphan_files)} orphan temp file(s) in {temp_dir}",
        )
    return PreflightCheckResult("orphan_temp_files", "PASS", "No orphan temp files found")
