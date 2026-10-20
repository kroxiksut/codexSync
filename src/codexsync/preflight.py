"""Read-only environment diagnostics behind doctor/preflight.

Every check here must stay side-effect free: OperationKind.DOCTOR is
declared side_effect_free in safety_gate, so a check may read and
report but must never create a probe file, least of all inside the Codex state
directory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path

from .codex_compat import CHAT_LIST, CHATS, GLOBAL_STATE, REBUILD_STATUS, CompatPart, check_codex_compat
from .codex_health import Severity, diagnose_catalogue, measure_chat_files
from .config import load_config
from .guardian_models import (
    GUARDIAN_COMMITTED_NAME,
    GUARDIAN_LATEST_GOOD_DIR_NAME,
    GUARDIAN_SNAPSHOTS_DIR_NAME,
    ValidationStatus,
)
from .guardian_schema import validate_global_state_references
from .jsonl_codec import logical_relative_path
from .manifest import load_manifest
from .models import AppConfig
from .config_migrate import BLOCKER, inspect_config, read_config_source
from .exceptions import FailSafeError
from .mutation_journal import JournalStore
from .runtime import _make_safety_gate
from .process_knowledge import CLI_DAEMON_STOP_COMMAND
from .safety_gate import OperationKind, ProcessState
from .session_catalog import (
    RECORD_FORMAT_UNSUPPORTED,
    SessionCatalog,
    SessionState,
    latest_page,
    peek_record_formats,
    scan_sessions,
)
from .session_index import SESSION_INDEX_FILE, parse_session_index
from .sqlite_audit import PlacementStatus, audit_sqlite, read_backfill_state, read_thread_placements
from .project_registry import PROVEN_PROJECT_REGISTRY, registry_note
from .state_locator import locate_local_state_dir
from .sync_engine import STAGE_DIR_PREFIXES

LOG = logging.getLogger(__name__)


#: One sentence of a check's details: a code and the values it names.
Part = tuple[str, dict[str, object]]


#: Every sentence code a check may report; each is ``check.detail.<code>`` in
#: the window's language files (`tests/test_gui_i18n.py`).
DETAIL_CODES = frozenset({
    "config.loaded", "config.unreadable",
    "state_dirs.not_ready", "state_dirs.cloud_missing", "state_dirs.cloud_not_dir", "state_dirs.ok",
    "codex_compat.not_checked", "codex_compat.version", "codex_compat.version_unknown", "codex_compat.known",
    "codex_compat.writes_off", "codex_compat.chats", "codex_compat.chats_zst", "codex_compat.chat_list",
    "codex_compat.rebuild_status", "codex_compat.global_state", "codex_compat.other", "codex_compat.unchecked",
    "config_compat.not_inspected", "config_compat.current", "config_compat.blocked", "config_compat.differs",
    "sync_rules.rules", "sync_rules.propagate",
    "project_registry.proven", "project_registry.json_only",
    "path.missing", "path.not_dir", "path.ok", "path.unreadable",
    "manifest.ok", "manifest.failed",
    "codex_process.stopped", "codex_process.running", "codex_process.found", "codex_process.unknown",
    "codex_process.cli_daemon",
    "codex_startup.not_checked", "codex_startup.state", "codex_startup.finding", "codex_startup.broken",
    "codex_startup.warning",
    "session_catalog.counts", "session_catalog.unavailable",
    "session_visibility.no_catalogue", "session_visibility.unreadable", "session_visibility.hidden",
    "session_visibility.all_listed",
    "session_format.unavailable", "session_format.sides", "session_format.differing",
    "sqlite_audit.clear", "sqlite_audit.unclear", "sqlite_audit.unavailable",
    "session_index.unreadable", "session_index.missing", "session_index.empty", "session_index.counts",
    "session_index.ambiguous", "session_index.codes",
    "global_state_schema.no_file", "global_state_schema.too_big", "global_state_schema.unreadable",
    "global_state_schema.ok", "global_state_schema.codes", "global_state_schema.rejected",
    "guardian_latest_good.no_machine", "guardian_latest_good.no_pointer", "guardian_latest_good.pointer_unreadable",
    "guardian_latest_good.missing_snapshot", "guardian_latest_good.stuck", "guardian_latest_good.ok",
    "mutation_journal.unreadable_journal", "mutation_journal.not_read", "mutation_journal.pending",
    "mutation_journal.none",
    "orphan_temp_files.no_folder", "orphan_temp_files.found", "orphan_temp_files.none",
})


def _part(code: str, **params: object) -> Part:
    if code not in DETAIL_CODES:
        raise ValueError(f"unknown check detail code: {code}")
    return code, params


@dataclass(slots=True, frozen=True)
class PreflightCheckResult:
    name: str
    status: str
    details: str
    #: The same details as sentence codes with their values (CS-338), so the
    #: window says them in its own language (`check.detail.<code>`). ``details``
    #: stays the English line `doctor` prints; a reader that does not know a
    #: code shows it instead.
    parts: tuple[Part, ...] = field(default=(), compare=False)

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

def run_preflight(
    config_path: Path, operation: OperationKind = OperationKind.DOCTOR, *, facts: dict | None = None,
) -> PreflightReport:
    """``facts``, when given, receives what the checks read (`cfg`, `local_dir`,
    `cloud_dir`, `catalog`), so `doctor --report` counts from the same reading
    instead of scanning every chat file a second time (CS-393)."""
    checks: list[PreflightCheckResult] = []
    facts = facts if facts is not None else {}

    try:
        cfg = load_config(config_path)
        facts["cfg"] = cfg
        checks.append(PreflightCheckResult(
            "config", "PASS", f"Loaded config from {config_path}", (_part("config.loaded", path=str(config_path)),),
        ))
    except Exception as exc:
        checks.append(PreflightCheckResult(
            "config", "FAIL", f"Cannot load config: {exc}", (_part("config.unreadable", error=str(exc)),),
        ))
        return PreflightReport(checks=checks)

    local_dir: Path | None = None
    cloud_dir: Path | None = None
    try:
        local_dir = locate_local_state_dir(cfg)
    except Exception as exc:
        checks.append(PreflightCheckResult(
            "state_dirs", "FAIL", f"State directories are not ready: {exc}",
            (_part("state_dirs.not_ready", error=str(exc)),),
        ))
    if local_dir is not None:
        checks.append(_check_cloud_dir(local_dir, cfg.paths.cloud_root_dir.expanduser()))
        if checks[-1].status == "PASS":
            cloud_dir = cfg.paths.cloud_root_dir.expanduser()
    facts["local_dir"], facts["cloud_dir"] = local_dir, cloud_dir

    checks.append(_check_config_compat(config_path))
    checks.append(_check_sync_rules(cfg))
    checks.append(_check_project_registry())
    if local_dir is not None:
        checks.append(_check_path_available("local_state", local_dir))
    if cloud_dir is not None:
        checks.append(_check_path_available("cloud_root", cloud_dir))
    checks.append(_check_path_available("backup_dir", cfg.paths.backup_dir))
    checks.append(_check_path_available("temp_dir", cfg.paths.temp_dir))

    try:
        _ = load_manifest(
            cfg.state.manifest_file, cfg.state.data_version, machine_id=cfg.identity.machine_id
        )
        checks.append(PreflightCheckResult(
            "manifest", "PASS", "Manifest data version is compatible", (_part("manifest.ok"),),
        ))
    except Exception as exc:
        checks.append(PreflightCheckResult(
            "manifest", "FAIL", f"Manifest check failed: {exc}", (_part("manifest.failed", error=str(exc)),),
        ))

    checks.append(_check_process_state(cfg, operation))
    if local_dir is not None:
        checks.append(_check_codex_startup(cfg, local_dir))
        catalog: SessionCatalog | None = None
        try:
            catalog = scan_sessions(local_dir, volatile=True, max_line_bytes=cfg.semantic.max_jsonl_line_bytes)
            facts["catalog"] = catalog
            invalid = sum(1 for item in catalog.descriptors if item.state.value in {"INVALID", "AMBIGUOUS"})
            status = "WARN" if invalid or catalog.codes else "PASS"
            checks.append(PreflightCheckResult(
                "session_catalog", status,
                f"sessions={len(catalog.descriptors)} invalid_or_ambiguous={invalid} graph_codes={len(catalog.codes)}",
                (_part(
                    "session_catalog.counts",
                    count=len(catalog.descriptors), invalid=invalid, codes=len(catalog.codes),
                ),),
            ))
            checks.append(_check_session_visibility(local_dir, catalog))
        except Exception as exc:
            checks.append(PreflightCheckResult(
                "session_catalog", "WARN", f"Session audit unavailable: {exc}",
                (_part("session_catalog.unavailable", error=str(exc)),),
            ))
        checks.append(_check_session_format(local_dir, cloud_dir, cfg.semantic.max_jsonl_line_bytes))
        try:
            sqlite_reports = audit_sqlite(local_dir, cold=False)
            indeterminate = sum(1 for item in sqlite_reports if item.status != "PASS")
            checks.append(PreflightCheckResult(
                "sqlite_audit", "WARN" if indeterminate else "PASS",
                f"database_sets={len(sqlite_reports)} indeterminate={indeterminate}",
                (_part(
                    "sqlite_audit.unclear" if indeterminate else "sqlite_audit.clear",
                    count=len(sqlite_reports), unclear=indeterminate,
                ),),
            ))
        except Exception as exc:
            checks.append(PreflightCheckResult(
                "sqlite_audit", "WARN", f"SQLite audit unavailable: {exc}",
                (_part("sqlite_audit.unavailable", error=str(exc)),),
            ))
    if local_dir is not None:
        # The one answer to "can codexSync write to this Codex", placed right
        # after the config so it is read first (CS-394).
        checks.insert(min(2, len(checks)), _check_codex_compat(local_dir, catalog, cfg))
        checks.append(_check_session_index(local_dir))
        checks.append(_check_global_state_schema(local_dir, cfg))
        checks.append(_check_guardian_latest_good(cfg))
    checks.append(_check_orphan_temp_files(cfg.paths.temp_dir))
    checks.append(_check_mutation_journals(cfg.paths.temp_dir))

    return PreflightReport(checks=checks)

def print_preflight_report(report: PreflightReport) -> None:
    print("Preflight report:")
    for item in report.checks:
        print(f"  [{item.status}] {item.name}: {item.details}")
    print(
        "Summary: "
        f"pass={len(report.passed)} warn={len(report.warnings)} fail={len(report.failures)}"
    )


def _check_cloud_dir(local_dir: Path, cloud: Path) -> PreflightCheckResult:
    """The cloud copy folder, which on a fresh machine is simply not there yet (CS-413).

    Every operation that writes creates codexSync's own folders first
    (`initialize_runtime_paths`), so a missing one is a WARN, and the checks
    of `.codex` still run; `doctor` itself creates nothing. A path that is
    there and is not a folder stays a FAIL.
    """
    if not cloud.exists():
        return PreflightCheckResult(
            "state_dirs", "WARN",
            f"local={local_dir}; cloud={cloud} does not exist yet; the first sync creates it",
            (_part("state_dirs.cloud_missing", local=str(local_dir), cloud=str(cloud)),),
        )
    if not cloud.is_dir():
        return PreflightCheckResult(
            "state_dirs", "FAIL", f"Cloud state dir is not a directory: {cloud}",
            (_part("state_dirs.cloud_not_dir", cloud=str(cloud)),),
        )
    return PreflightCheckResult(
        "state_dirs", "PASS", f"local={local_dir}; cloud={cloud}",
        (_part("state_dirs.ok", local=str(local_dir), cloud=str(cloud)),),
    )


def _check_codex_compat(
    local_dir: Path, catalog: SessionCatalog | None, cfg: AppConfig
) -> PreflightCheckResult:
    """Codex's version, and whether every format codexSync writes into is known (CS-394).

    A WARN, not a FAIL: what is unknown is refused part by part and the rest
    of a sync still runs, so the answer names which writes are off.
    """
    try:
        report = check_codex_compat(local_dir, catalog, max_state_bytes=cfg.guardian.max_state_bytes)
    except Exception as exc:
        return PreflightCheckResult(
            "codex_compat", "WARN", f"Codex's formats not checked: {exc}",
            (_part("codex_compat.not_checked", error=str(exc)),),
        )
    version = f"Codex {report.codex_version}" if report.codex_version else "Codex version unknown"
    unchecked = f"; not checked now: {', '.join(report.unchecked)}" if report.unchecked else ""
    said = [
        _part("codex_compat.version", version=report.codex_version) if report.codex_version
        else _part("codex_compat.version_unknown")
    ]
    tail = [_part("codex_compat.unchecked", parts=", ".join(report.unchecked))] if report.unchecked else []
    if report.compatible:
        return PreflightCheckResult(
            "codex_compat", "PASS", f"{version}; every format codexSync writes into is known{unchecked}",
            (*said, _part("codex_compat.known"), *tail),
        )
    parts = "; ".join(_describe_unknown(part) for part in report.unknown)
    return PreflightCheckResult(
        "codex_compat", "WARN",
        f"{version}; writing is off for what this version does not know: {parts}{unchecked}",
        (*said, _part("codex_compat.writes_off"), *(_unknown_part(part) for part in report.unknown), *tail),
    )


def _unknown_part(part: CompatPart) -> Part:
    """`_describe_unknown` as a sentence code, for the window."""
    if part.part == CHATS:
        code = "codex_compat.chats_zst" if "zst" in part.containers else "codex_compat.chats"
        return _part(code, count=part.count, containers=", ".join(part.containers) or "?")
    if part.part == CHAT_LIST:
        return _part("codex_compat.chat_list", code=part.code)
    if part.part == REBUILD_STATUS:
        return _part("codex_compat.rebuild_status")
    if part.part == GLOBAL_STATE:
        return _part("codex_compat.global_state")
    return _part("codex_compat.other", part=part.part, code=part.code)


def _describe_unknown(part: CompatPart) -> str:
    if part.part == CHATS:
        return (
            f"{part.count} chat file(s) in a container it cannot read ({', '.join(part.containers) or '?'}): "
            "those chats are neither written into .codex nor copied to the mirror"
            + ("; codexSync on Python 3.14 or later, as in the release programs, reads zst" if "zst" in part.containers else "")
        )
    if part.part == CHAT_LIST:
        return (
            f"Codex's chat list is in an unknown shape ({part.code}): no chat is written into .codex, "
            "and chat names, archive moves and catalogue requests are not written"
        )
    if part.part == REBUILD_STATUS:
        return "the chat-list rebuild status is unknown: `codex repair` and `sessions catalogue` leave it alone"
    if part.part == GLOBAL_STATE:
        return "projects are in an unknown shape: projects, pins and chat moves are not written"
    return f"{part.part} ({part.code})"


def _check_config_compat(config_path: Path) -> PreflightCheckResult:
    """Say whether this config is one every mutating command will refuse.

    Without it `doctor` reports `config: PASS` for a file written by 0.1 and
    the user only learns otherwise when `sync` exits 4 -- with a message about
    closing Codex, which is not the problem. A blocker is a FAIL here because
    it is a FAIL in practice: nothing can be written until it is settled.
    """
    try:
        text, source_sha256 = read_config_source(config_path)
        plan = inspect_config(text, source_sha256=source_sha256)
    except Exception as exc:  # unreadable config: `config` already said so
        return PreflightCheckResult(
            "config_compat", "WARN", f"Cannot inspect config: {exc}",
            (_part("config_compat.not_inspected", error=str(exc)),),
        )
    if plan.is_current:
        return PreflightCheckResult(
            "config_compat", "PASS", "Config matches this version", (_part("config_compat.current"),),
        )
    blockers = [finding.code for finding in plan.blockers]
    if blockers:
        return PreflightCheckResult(
            "config_compat", "FAIL",
            "Every mutating command refuses this config: "
            + ", ".join(blockers)
            + "; the Settings screen offers the upgrade, or run `config check` "
            + f"(plan {plan.plan_id[:12]})",
            (_part("config_compat.blocked", codes=", ".join(blockers), plan=plan.plan_id[:12]),),
        )
    return PreflightCheckResult(
        "config_compat", "WARN",
        "This version would write some values differently: "
        + ", ".join(plan.codes())
        + "; the Settings screen lists them, or run `config check` "
        + f"(plan {plan.plan_id[:12]})",
        (_part("config_compat.differs", codes=", ".join(plan.codes()), plan=plan.plan_id[:12]),),
    )


def _check_sync_rules(cfg: AppConfig) -> PreflightCheckResult:
    """Say what this config lets a sync do, before it does it.

    A one-way direction and deletion propagation are both settings that change
    what a later run writes without changing what any plan looks like on the
    surface, so `doctor` names them (`D-012`, `D-013`).
    """
    detail = f"direction={cfg.sync.direction}; delete_policy={cfg.sync.delete_policy}"
    rules = _part("sync_rules.rules", direction=cfg.sync.direction, delete_policy=cfg.sync.delete_policy)
    if cfg.sync.delete_policy == "propagate":
        return PreflightCheckResult(
            "sync_rules", "WARN",
            f"{detail}; a proven deletion on one side removes the file here, after a verified backup",
            (rules, _part("sync_rules.propagate")),
        )
    return PreflightCheckResult("sync_rules", "PASS", detail, (rules,))


def _check_project_registry() -> PreflightCheckResult:
    """Say plainly that a project root is rewritten in the JSON only.

    Codex keeps projects in `state_*.sqlite` as well, and codexSync never
    modifies those records. Whether the runtime follows a JSON-only root change is
    unverified, so a person deciding to move a project should be told before
    they rely on it, not after.
    """
    schema = next(iter(PROVEN_PROJECT_REGISTRY), None)
    if schema is not None:
        return PreflightCheckResult(
            "project_registry", "PASS", registry_note(schema), (_part("project_registry.proven", schema=schema),),
        )
    return PreflightCheckResult("project_registry", "WARN", registry_note(None), (_part("project_registry.json_only"),))


def _check_path_available(name: str, directory: Path) -> PreflightCheckResult:
    """A diagnostic availability check that never creates a directory or probe."""
    try:
        if not directory.exists():
            # codexSync's own folders: the first operation that writes creates them.
            return PreflightCheckResult(
                name, "WARN", f"Path does not exist yet: {directory}; the first operation that writes creates it",
                (_part("path.missing", path=str(directory)),),
            )
        if not directory.is_dir():
            return PreflightCheckResult(
                name, "FAIL", f"Path is not a directory: {directory}", (_part("path.not_dir", path=str(directory)),),
            )
        _ = list(directory.iterdir())
        return PreflightCheckResult(
            name, "PASS", f"Path is readable: {directory}", (_part("path.ok", path=str(directory)),),
        )
    except Exception as exc:
        return PreflightCheckResult(
            name, "FAIL", f"Cannot access {directory}: {exc}",
            (_part("path.unreadable", path=str(directory), error=str(exc)),),
        )

def _check_session_format(local_dir: Path, cloud_dir: Path | None, max_line_bytes: int) -> PreflightCheckResult:
    """Whether a record-format rewrite reached this machine and not the mirror, or the reverse.

    The desktop build of September 2026 rewrote every session file into
    numbered records. A copy on the other side still in the old format is then
    a conflict for every session it holds, and `sessions apply` refuses until
    it is decided; saying so here is what makes that a known step rather than
    two hundred unexplained conflicts.
    """
    try:
        local = peek_record_formats(local_dir, max_line_bytes=max_line_bytes)
        cloud = (
            peek_record_formats(cloud_dir, max_line_bytes=max_line_bytes)
            if cloud_dir is not None and cloud_dir.is_dir() else {}
        )
    except Exception as exc:
        return PreflightCheckResult(
            "session_format", "WARN", f"Session format audit unavailable: {exc}",
            (_part("session_format.unavailable", error=str(exc)),),
        )

    def side(formats: dict[str, str]) -> str:
        counts: dict[str, int] = {}
        for value in formats.values():
            counts[value] = counts.get(value, 0) + 1
        return ",".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "none"

    # Per branch, not per side: a fresh session is written in the old format
    # and rewritten later, so both sides can hold both formats and agree.
    differing = sum(
        1 for path, value in local.items()
        if path in cloud and cloud[path] != value
        and not {"unreadable", RECORD_FORMAT_UNSUPPORTED} & {value, cloud[path]}
    )
    message = f"local={side(local)} cloud={side(cloud)} differing={differing}"
    sides = _part("session_format.sides", local=side(local), cloud=side(cloud))
    if differing:
        return PreflightCheckResult(
            "session_format", "WARN",
            message + "; those sessions are in a different record format on each side, which a transfer "
            "reports as FORMAT_MIGRATION conflicts: decide them with `sessions resolve --format-migrations`",
            (sides, _part("session_format.differing", count=differing)),
        )
    return PreflightCheckResult("session_format", "PASS", message, (sides,))


def _check_session_visibility(local_dir: Path, catalog: SessionCatalog) -> PreflightCheckResult:
    """How many chat files here Codex's own catalogue does not list.

    Codex shows a chat from its `threads` table, not from the folder, so a file
    it has not taken up is a chat nobody sees. A chat written by
    `[semantic] new_chats = "same_path"` lands exactly so until Codex picks it
    up (D-020); this is what turns "it did not appear" into a number instead of
    a silence. Only counts are reported: ids and titles are the user's.
    """
    placements = read_thread_placements(local_dir)
    if placements.status is PlacementStatus.ABSENT:
        return PreflightCheckResult(
            "session_visibility", "PASS",
            "No thread catalogue yet; Codex builds one from the chat files when it first starts",
            (_part("session_visibility.no_catalogue"),),
        )
    if placements.status is not PlacementStatus.AVAILABLE:
        codes = ",".join(placements.codes) or "unknown"
        return PreflightCheckResult(
            "session_visibility", "WARN",
            f"Thread catalogue unreadable ({codes}); visibility not checked",
            (_part("session_visibility.unreadable", codes=codes),),
        )
    chats, unlisted, elsewhere = visibility_counts(catalog, placements)
    detail = f"chats={chats} not_listed={unlisted} listed_at_another_path={elsewhere}"
    if unlisted or elsewhere:
        return PreflightCheckResult(
            "session_visibility", "WARN",
            detail + "; Codex does not show these chats. `sessions catalogue` asks Codex to rebuild "
            "its chat list from the files, which it does on its next start (leave it open until it finishes)",
            (_part("session_visibility.hidden", count=chats, unlisted=unlisted, elsewhere=elsewhere),),
        )
    return PreflightCheckResult(
        "session_visibility", "PASS", detail, (_part("session_visibility.all_listed", count=chats),),
    )


def visibility_counts(catalog: SessionCatalog, placements) -> tuple[int, int, int]:
    """(chats, not listed by Codex's catalogue, listed at another path). Counts only."""
    files = [
        item for item in catalog.descriptors
        if item.session_id and item.state in {SessionState.ACTIVE, SessionState.ARCHIVED}
    ]
    # Per chat, the file its history ends in: a chat continued in pages is
    # listed by its last page, and a row naming an earlier part shows the
    # chat only up to there (CS-356).
    chats = list(latest_page(files).values())
    unlisted = sum(1 for item in chats if not placements.knows(item.session_id))
    # Compared without the container: Codex 0.163 keeps a chat it has not
    # touched for a while as `x.jsonl.zst` while its row still names
    # `x.jsonl`, and reads it. Comparing the names as written reported 323
    # of 340 chats on a real machine as hidden, and advised the very rebuild
    # that once left Codex unable to start (D-032).
    elsewhere = sum(
        1 for item in chats
        if placements.knows(item.session_id)
        and placements.placement_of(item.session_id) is not None
        and logical_relative_path(placements.placement_of(item.session_id))
        != logical_relative_path(item.relative_path)
    )
    return len(chats), unlisted, elsewhere


def _check_codex_startup(cfg: AppConfig, local_dir: Path) -> PreflightCheckResult:
    """Whether Codex's chat-list rebuild will let it start (D-032).

    A rebuild marked ``running`` with no Codex process is one nobody is doing,
    and every Codex start waits for it and exits: that is a FAIL, with the
    command that repairs it. A pending rebuild is a WARN with its size, because
    the start that runs it is long and ending it part-way leads to the FAIL.
    """
    try:
        reading = read_backfill_state(local_dir)
        process = _make_safety_gate(cfg).check(OperationKind.DOCTOR).process_state
        findings = diagnose_catalogue(reading, process, measure_chat_files(local_dir))
    except Exception as exc:
        return PreflightCheckResult(
            "codex_startup", "WARN", f"Codex's chat-list state not checked: {exc}",
            (_part("codex_startup.not_checked", error=str(exc)),),
        )
    if not findings:
        return PreflightCheckResult(
            "codex_startup", "PASS", f"Chat-list rebuild: {reading.backfill or 'none'}",
            (_part("codex_startup.state", state=reading.backfill or "none"),),
        )
    item = findings[0]
    counts = " ".join(f"{key}={value}" for key, value in item.counts.items())
    # `codex.finding.<code>` is the window's sentence for the finding itself.
    finding = _part("codex_startup.finding", code=item.code, **item.counts)
    if item.severity is Severity.BROKEN:
        return PreflightCheckResult(
            "codex_startup", "FAIL",
            f"{item.code} {counts}: Codex will not start until its chat-list rebuild is complete; "
            "run `codex repair` (Codex closed)",
            (finding, _part("codex_startup.broken")),
        )
    if item.severity is Severity.WARNING:
        return PreflightCheckResult(
            "codex_startup", "WARN",
            f"{item.code} {counts}: leave Codex open on its next start until the chats appear, "
            "or `codex repair` skips the rebuild",
            (finding, _part("codex_startup.warning")),
        )
    return PreflightCheckResult("codex_startup", "PASS", f"{item.code} {counts}".strip(), (finding,))


def _check_process_state(cfg: AppConfig, operation: OperationKind) -> PreflightCheckResult:
    decision = _make_safety_gate(cfg).check(operation)
    if decision.process_state is ProcessState.STOPPED:
        return PreflightCheckResult("codex_process", "PASS", decision.reason, (_part("codex_process.stopped"),))
    status = "FAIL" if operation in {OperationKind.SYNC, OperationKind.RESTORE, OperationKind.REPAIR_APPLY} else "WARN"
    if decision.process_state is ProcessState.RUNNING:
        # The gate names what it found after a colon (`SafetyGate._detail`).
        _, _, found = decision.reason.partition(": ")
        parts = (_part("codex_process.running"),) + ((_part("codex_process.found", processes=found),) if found else ())
        details = decision.reason
        if decision.cli_daemon_only:
            # CS-429: the one thing left is Codex CLI's server, which outlives the CLI.
            parts += (_part("codex_process.cli_daemon", command=CLI_DAEMON_STOP_COMMAND),)
            details += (
                f"; only Codex CLI's background server is left (it stays after the CLI is closed): "
                f"stop it with `{CLI_DAEMON_STOP_COMMAND}`"
            )
        return PreflightCheckResult("codex_process", status, details, parts)
    return PreflightCheckResult(
        "codex_process", status, decision.reason, (_part("codex_process.unknown", reason=decision.reason),),
    )

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
        return PreflightCheckResult(
            "session_index", "WARN", f"Index unreadable: {exc}", (_part("session_index.unreadable", error=str(exc)),),
        )
    if "MISSING_INDEX" in result.codes:
        return PreflightCheckResult(
            "session_index", "PASS", f"No {SESSION_INDEX_FILE}; sessions are read from disk",
            (_part("session_index.missing", file=SESSION_INDEX_FILE),),
        )
    if "EMPTY_INDEX" in result.codes:
        return PreflightCheckResult(
            "session_index", "PASS", f"{SESSION_INDEX_FILE} is empty; sessions are read from disk",
            (_part("session_index.empty", file=SESSION_INDEX_FILE),),
        )
    codes = [code for code in result.codes if code not in {"MISSING_INDEX", "EMPTY_INDEX"}]
    status = "WARN" if codes or not result.reductions_agree else "PASS"
    parts = [_part("session_index.counts", count=len(result.records), sessions=len(result.reduced))]
    if not result.reductions_agree:
        parts.append(_part("session_index.ambiguous"))
    if codes:
        parts.append(_part("session_index.codes", codes=", ".join(codes)))
    return PreflightCheckResult(
        "session_index", status,
        f"records={len(result.records)} sessions={len(result.reduced)} "
        f"contract={result.contract.value} reductions_agree={result.reductions_agree} "
        f"codes={','.join(codes) or 'none'}",
        tuple(parts),
    )


def _check_global_state_schema(local_dir: Path, cfg: AppConfig) -> PreflightCheckResult:
    """Report whether Guardian can recognise this machine's state at all.

    An unrecognised schema means every snapshot is quarantined and `latest-good`
    never appears, so the protection is silently inert. Without this check the
    only symptom is an exit code from a command the user may never run.
    """
    source = local_dir / ".codex-global-state.json"
    if not source.is_file():
        return PreflightCheckResult(
            "global_state_schema", "WARN", f"No state file at {source}",
            (_part("global_state_schema.no_file", path=str(source)),),
        )
    try:
        if source.stat().st_size > cfg.guardian.max_state_bytes:
            return PreflightCheckResult(
                "global_state_schema", "WARN",
                f"State file exceeds guardian.max_state_bytes ({cfg.guardian.max_state_bytes})",
                (_part("global_state_schema.too_big", limit=cfg.guardian.max_state_bytes),),
            )
        report = validate_global_state_references(source.read_bytes())
    except OSError as exc:
        return PreflightCheckResult(
            "global_state_schema", "WARN", f"Cannot read state file: {exc}",
            (_part("global_state_schema.unreadable", error=str(exc)),),
        )

    codes = ", ".join(report.codes) or "none"
    if report.status in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        parts = [_part(
            "global_state_schema.ok", schema=report.schema_id,
            projects=report.project_count, bindings=report.binding_count,
        )]
        if report.codes:
            parts.append(_part("global_state_schema.codes", codes=codes))
        return PreflightCheckResult(
            "global_state_schema", "PASS" if report.status is ValidationStatus.PASS else "WARN",
            f"schema={report.schema_id} projects={report.project_count} "
            f"bindings={report.binding_count} codes={codes}",
            tuple(parts),
        )
    return PreflightCheckResult(
        "global_state_schema", "FAIL",
        f"Guardian would reject this state ({report.status.value}); codes={codes}. "
        "Snapshots go to quarantine and latest-good is never created.",
        (_part("global_state_schema.rejected", status=report.status.value, codes=codes),),
    )


def _check_guardian_latest_good(cfg: AppConfig) -> PreflightCheckResult:
    """Confirm a restorable snapshot actually exists for this machine."""
    machine = cfg.identity.machine_id
    if not machine:
        return PreflightCheckResult(
            "guardian_latest_good", "WARN", "identity.machine_id is not set", (_part("guardian_latest_good.no_machine"),),
        )
    pointer = cfg.guardian.root_dir / GUARDIAN_LATEST_GOOD_DIR_NAME / f"{machine}.json"
    if not pointer.is_file():
        return PreflightCheckResult(
            "guardian_latest_good", "WARN",
            f"No latest-good pointer for {machine}; run `guardian snapshot --once`",
            (_part("guardian_latest_good.no_pointer", machine=machine),),
        )
    try:
        snapshot_id = json.loads(pointer.read_text(encoding="utf-8"))["snapshot_id"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return PreflightCheckResult(
            "guardian_latest_good", "FAIL", f"Pointer is unreadable: {exc}",
            (_part("guardian_latest_good.pointer_unreadable", error=str(exc)),),
        )
    snapshot = cfg.guardian.root_dir / GUARDIAN_SNAPSHOTS_DIR_NAME / machine / str(snapshot_id)
    if not (snapshot / GUARDIAN_COMMITTED_NAME).is_file():
        return PreflightCheckResult(
            "guardian_latest_good", "FAIL",
            "latest-good points at a snapshot that is missing or uncommitted",
            (_part("guardian_latest_good.missing_snapshot"),),
        )
    stuck = _shrinks_quarantined_since(cfg.guardian.root_dir, machine, snapshot)
    if stuck:
        return PreflightCheckResult(
            "guardian_latest_good", "WARN",
            f"Restorable snapshot {snapshot_id}, but {stuck} newer state(s) went to quarantine for a drop in "
            "projects or bindings, so latest-good is no longer advancing; `guardian accept` shows why the "
            "counts fell and can take the current state as the new baseline",
            (_part("guardian_latest_good.stuck", snapshot=str(snapshot_id), count=stuck),),
        )
    return PreflightCheckResult(
        "guardian_latest_good", "PASS", f"Restorable snapshot {snapshot_id}",
        (_part("guardian_latest_good.ok", snapshot=str(snapshot_id)),),
    )


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


def _check_mutation_journals(temp_dir: Path) -> PreflightCheckResult:
    """Say that an unfinished mutation blocks every write, before one is tried.

    `JournalStore.begin` refuses while any journal is non-terminal, so without
    this `doctor` reported all PASS and the next `sync --apply` exited 5
    (CS-316). A FAIL, like a config blocker: nothing can be written until the
    journal is settled with `recover`. Only reads the journal files.
    """
    store = JournalStore(temp_dir)
    try:
        pending = store.non_terminal()
    except FailSafeError:
        return PreflightCheckResult(
            "mutation_journal", "FAIL",
            f"A mutation journal in {store.root} cannot be read, and it blocks every mutation; "
            "`recover inspect` on it, or the Recovery screen, shows what is left",
            (_part("mutation_journal.unreadable_journal", folder=str(store.root)),),
        )
    except OSError as exc:
        return PreflightCheckResult(
            "mutation_journal", "WARN", f"Cannot read mutation journals: {exc}",
            (_part("mutation_journal.not_read", error=str(exc)),),
        )
    if pending:
        first = pending[0]
        return PreflightCheckResult(
            "mutation_journal", "FAIL",
            f"{len(pending)} unfinished mutation(s) block every write; the first is {first.family} "
            f"{first.operation_id} ({first.state.value}). Run `recover inspect {first.operation_id}`, "
            "then `recover resume` or `recover rollback`",
            (_part(
                "mutation_journal.pending", count=len(pending), family=first.family,
                operation=first.operation_id, state=first.state.value,
            ),),
        )
    return PreflightCheckResult("mutation_journal", "PASS", "No unfinished mutation", (_part("mutation_journal.none"),))


def _check_orphan_temp_files(temp_dir: Path) -> PreflightCheckResult:
    if not temp_dir.exists():
        return PreflightCheckResult(
            "orphan_temp_files", "PASS", "Temp directory does not exist yet", (_part("orphan_temp_files.no_folder"),),
        )
    if not temp_dir.is_dir():
        # `temp_dir` already FAILs as "not a directory"; walking it would raise.
        return PreflightCheckResult(
            "orphan_temp_files", "WARN", f"Temp path is not a directory: {temp_dir}",
            (_part("path.not_dir", path=str(temp_dir)),),
        )
    orphans = [path for path in temp_dir.rglob("*.tmp") if path.is_file()]
    # A staging *directory* is an orphan too, and a heavier one: `restore`
    # extracts a whole snapshot into it. Counting only files reported "no
    # orphans" over an empty one left there by a run in September 2026.
    orphans.extend(
        path for path in temp_dir.iterdir()
        if path.is_dir() and path.name.startswith(STAGE_DIR_PREFIXES)
    )
    if orphans:
        return PreflightCheckResult(
            "orphan_temp_files",
            "WARN",
            f"Found {len(orphans)} orphan temp file(s) in {temp_dir}",
            (_part("orphan_temp_files.found", count=len(orphans), path=str(temp_dir)),),
        )
    return PreflightCheckResult(
        "orphan_temp_files", "PASS", "No orphan temp files found", (_part("orphan_temp_files.none"),),
    )
