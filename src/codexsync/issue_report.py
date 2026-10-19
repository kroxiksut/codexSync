"""`doctor --report`: what an issue needs to know, without anything of the user's (CS-393).

SECURITY.md asks people not to attach their data, and an issue about state is
hard to answer without some. This report is the safe middle: versions, which
formats and gates this machine has, how each check came out and counts -- and
never a name, a path, a title, a chat id or a check's detail text (those carry
paths). Every value is a version, a status, a code from a fixed set, a count
or a non-identifying setting; `tests/test_issue_report.py` plants private
values and fails if any reaches the output.

It reads what `doctor` reads, once: `run_preflight` hands over its own reading.
"""
from __future__ import annotations

from collections import Counter
import platform
import sys
from pathlib import Path

from .codex_closer import PROVEN_CLOSERS
from .codex_compat import check_codex_compat, read_codex_version
from .guardian_schema import validate_global_state_references
from .preflight import run_preflight, visibility_counts
from .process_detector import PROVEN_DETECTORS
from .process_knowledge import default_background_process_names
from .process_report import build_process_report, os_key
from .project_registry import PROVEN_PROJECT_REGISTRY
from .safety_gate import OperationKind
from .semantic_transfer import PROVEN_LAYOUTS
from .session_catalog import SessionState, peek_record_formats
from .session_index import PROVEN_CONTRACTS
from .sqlite_audit import PlacementStatus, read_backfill_state, read_thread_placements
from .version import __version__

__all__ = ["REPORT_FORMAT", "build_issue_report"]

REPORT_FORMAT = "codexsync-issue-report-v1"


def build_issue_report(config_path: Path, operation: OperationKind = OperationKind.DOCTOR) -> dict:
    facts: dict = {}
    preflight = run_preflight(config_path, operation=operation, facts=facts)
    report: dict = {
        "format": REPORT_FORMAT,
        "codexsync": __version__,
        "python": platform.python_version(),
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
               "key": os_key()},
        # Name and status only: a check's detail names paths.
        "checks": {item.name: item.status for item in preflight.checks},
        "gates": {
            "detector_proven_here": sys.platform.startswith("win") or bool(PROVEN_DETECTORS.get(sys.platform)),
            "detectors": sorted(PROVEN_DETECTORS),
            "closers": sorted(PROVEN_CLOSERS),
            "layouts": len(PROVEN_LAYOUTS),
            "session_index_contracts": len(PROVEN_CONTRACTS),
            "project_registry": len(PROVEN_PROJECT_REGISTRY),
        },
    }
    cfg = facts.get("cfg")
    if cfg is not None:
        report["settings"] = {
            "sync_scope": cfg.sync.scope,
            "sync_direction": cfg.sync.direction,
            "delete_policy": cfg.sync.delete_policy,
            "close_codex": bool(cfg.sync.close_codex),
            "conflict_policy": cfg.conflict.policy,
            "new_chats": cfg.semantic.new_chats,
            "mirror_compression": cfg.semantic.mirror_compression.value,
            "path_mapping_rules": len(cfg.path_mappings),
        }
        markers = cfg.process_detection.background_process_names.get(os_key(), [])
        names, source = list(cfg.process_detection.process_names), "config"
    else:
        markers = default_background_process_names().get(os_key(), [])
        names, source = [], "built-in"
    report["process"] = _process(names, markers, source)

    local_dir = facts.get("local_dir")
    if local_dir is None:
        return report
    catalog = facts.get("catalog")
    report["codex_version"] = read_codex_version(local_dir)
    compat = check_codex_compat(local_dir, catalog, max_state_bytes=cfg.guardian.max_state_bytes)
    report["formats"] = {
        "compatible": compat.compatible,
        "unknown": [
            {"part": part.part, "code": part.code, "count": part.count, "containers": list(part.containers)}
            for part in compat.unknown
        ],
        "unchecked": list(compat.unchecked),
    }
    report["sessions"] = _sessions(local_dir, facts.get("cloud_dir"), catalog, cfg.semantic.max_jsonl_line_bytes)
    report["chat_list"] = _chat_list(local_dir, catalog)
    report["global_state"] = _global_state(local_dir, cfg.guardian.max_state_bytes)
    return report


def _process(names: list[str], markers: list[str], source: str) -> dict:
    if not names:
        from .process_knowledge import default_process_names

        names = default_process_names()
    process = build_process_report(names, markers, names_from=source)
    roles = Counter(item.role for item in process.matches)
    return {
        "supported": process.supported,
        "gate_state": process.gate_state,
        "would_read": process.would_read,
        "matches": dict(sorted(roles.items())),
        "names_from": process.names_from,
        "listing_failed": process.error is not None,
    }


def _sessions(local_dir: Path, cloud_dir: Path | None, catalog, max_line_bytes: int) -> dict:
    result: dict = {}
    if catalog is not None:
        states = Counter(item.state.value for item in catalog.descriptors)
        codes = Counter(code for item in catalog.descriptors for code in item.codes)
        result["files"] = len(catalog.descriptors)
        result["states"] = dict(sorted(states.items()))
        result["record_formats"] = dict(sorted(Counter(
            item.record_format or "none" for item in catalog.descriptors
            if item.state in {SessionState.ACTIVE, SessionState.ARCHIVED}
        ).items()))
        result["codes"] = dict(sorted(codes.items()))
        result["graph_codes"] = list(catalog.codes)
    try:
        cloud = peek_record_formats(cloud_dir, max_line_bytes=max_line_bytes) if cloud_dir is not None else {}
        result["mirror_record_formats"] = dict(sorted(Counter(cloud.values()).items()))
    except OSError:
        result["mirror_record_formats"] = None
    return result


def _chat_list(local_dir: Path, catalog) -> dict:
    placements = read_thread_placements(local_dir)
    backfill = read_backfill_state(local_dir)
    result: dict = {
        "status": placements.status.value,
        "codes": list(placements.codes),
        "rebuild_status": backfill.backfill,
    }
    if placements.status is PlacementStatus.AVAILABLE and catalog is not None:
        chats, unlisted, elsewhere = visibility_counts(catalog, placements)
        result.update({"chats": chats, "not_listed": unlisted, "listed_at_another_path": elsewhere,
                       "catalogue_rows": len(placements.by_session)})
    return result


def _global_state(local_dir: Path, max_state_bytes: int) -> dict:
    source = local_dir / ".codex-global-state.json"
    try:
        if not source.is_file():
            return {"present": False}
        if source.stat().st_size > max_state_bytes:
            return {"present": True, "too_large": True}
        validated = validate_global_state_references(source.read_bytes())
    except OSError:
        return {"present": True, "readable": False}
    return {
        "present": True,
        "status": validated.status.value,
        "schema": validated.schema_id,
        "projects": validated.project_count,
        "bindings": validated.binding_count,
        "codes": list(validated.codes),
    }
