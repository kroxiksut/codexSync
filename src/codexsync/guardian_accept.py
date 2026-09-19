"""Let a person accept a suspicious shrink as Guardian's new baseline.

Guardian compares every candidate with ``latest-good`` and quarantines one that
lost too many projects or bindings (``guardian_shrink``). That rule is right
and stays absolute for the watcher: nothing suspicious becomes ``latest-good``
on its own. But the comparison is always against the same baseline, so when
the drop is real the store is stuck for good: every later state is compared
with the one from before the drop, is suspicious again, and ``latest-good``
never moves. Observed on a real machine (2026-09-13): the desktop build
re-created all 16 projects under new ids, the 6 bindings that named the old ids
were gone, and every snapshot after that went to quarantine as
``BINDING_COUNT_DROP``.

This module plans the way out, and the plan is what a person decides on:

* **Only a shrink can be accepted.** A state that fails byte, JSON, schema or
  reference validation is refused whatever anyone confirms; acceptance
  overrides a *judgement* about counts, never an integrity check.
* **The explanation is counted, not listed.** Guardian's rule is that project
  names, roots and thread ids never leave core, and this keeps it: projects
  whose id vanished while a project with the same roots appeared under a new id
  are *replaced*, the rest *removed*; a lost binding is attributed to one of
  those or to a project that still exists. That is enough to tell "Codex
  re-created its projects" from "something deleted my bindings".
* **The plan id pins the drop, not the bytes.** It covers the baseline snapshot
  (id and hash), the counts now, the shrink codes and a digest of exactly which
  bindings and projects went and why. It does not cover the state's own hash:
  Codex rewrites that file for window geometry and sidebar state every few
  minutes, and an id that changed with it could never be confirmed while Codex
  runs, which is when Guardian works. A different drop is a different id.

The planning function is pure. ``GuardianRunner`` does the reading, takes the
runner lock and commits through ``GuardianStore``, so an accepted snapshot goes
through the same stage, verify, ``COMMITTED`` and pointer steps as any other.
The accepted manifest carries ``SHRINK_ACCEPTED`` beside the shrink codes and
names the overridden baseline as its predecessor; retention keeps both for as
long as they exist, because a baseline a person overrode is evidence nobody
else will ever recreate.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .guardian_models import GuardianManifest, ValidationReport, ValidationStatus
from .guardian_schema import binding_project_id, detect_state_schema, project_root_paths
from .guardian_shrink import BINDING_COUNT_DROP, PROJECT_COUNT_DROP


GUARDIAN_ACCEPT_PLAN_VERSION = 1

#: Recorded in the accepted snapshot's manifest next to the shrink codes.
SHRINK_ACCEPTED = "SHRINK_ACCEPTED"
ACCEPTABLE_CODES = frozenset({PROJECT_COUNT_DROP, BINDING_COUNT_DROP})

#: There is no ``latest-good`` yet, so nothing is being compared against and
#: the next snapshot commits without anyone's decision.
NO_BASELINE = "NO_BASELINE"
#: A pointer exists but does not resolve to a verified snapshot right now.
#: Guardian rebuilds it on its next run; a preview must not.
BASELINE_UNRESOLVED = "BASELINE_UNRESOLVED"
#: The state is not suspicious: the next snapshot commits it by itself.
NOTHING_TO_ACCEPT = "NOTHING_TO_ACCEPT"
#: The state fails validation outright; no confirmation can make it a baseline.
STATE_REJECTED = "STATE_REJECTED"
#: The state file does not exist.
SOURCE_MISSING = "SOURCE_MISSING"

#: Codes that describe "no decision is needed" rather than "refused".
INFORMATIONAL_CODES = frozenset({NO_BASELINE, NOTHING_TO_ACCEPT})


@dataclass(frozen=True, slots=True)
class ShrinkExplanation:
    """Why the counts fell, in counts only."""

    #: Baseline project ids that are gone while a project with the same roots
    #: now exists under another id.
    projects_replaced: int
    #: Baseline project ids that are gone with no project on the same roots.
    projects_removed: int
    #: Project ids that are new and whose roots the baseline did not have.
    projects_added: int
    #: Lost bindings that pointed at a replaced project.
    bindings_to_replaced_projects: int
    #: Lost bindings that pointed at a removed project.
    bindings_to_removed_projects: int
    #: Lost bindings whose project still exists: the binding itself was dropped.
    bindings_dropped: int


@dataclass(frozen=True, slots=True)
class GuardianAcceptPlan:
    version: int
    plan_id: str
    machine_id: str
    baseline_snapshot_id: str | None
    baseline_generation: int
    baseline_created_at_utc: str
    schema_id: str | None
    projects_before: int | None
    bindings_before: int | None
    projects_now: int | None
    bindings_now: int | None
    #: The shrink codes a confirmation accepts; empty when there is nothing to accept.
    shrink_codes: tuple[str, ...]
    #: Everything the validation of the current state reported, for display.
    state_codes: tuple[str, ...]
    explanation: ShrinkExplanation | None
    #: Anything here blocks the acceptance; see ``INFORMATIONAL_CODES``.
    codes: tuple[str, ...]

    @property
    def acceptable(self) -> bool:
        return not self.codes


def build_guardian_accept_plan(
    *,
    machine_id: str,
    baseline: GuardianManifest | None,
    baseline_payload: bytes | None,
    candidate: ValidationReport | None,
    candidate_payload: bytes | None,
    baseline_resolved: bool = True,
) -> GuardianAcceptPlan:
    """Describe accepting ``candidate`` over ``baseline``. Pure; reads nothing.

    ``candidate`` is the report *after* the shrink assessment against
    ``baseline``, exactly as the watcher computes it, so a plan can only offer
    to accept what the watcher would have quarantined. ``candidate`` is
    ``None`` when the state file is missing.
    """
    codes: list[str] = []
    shrink_codes: tuple[str, ...] = ()
    explanation: ShrinkExplanation | None = None
    digest = ""
    if candidate is None or candidate_payload is None:
        codes.append(SOURCE_MISSING)
    elif not baseline_resolved:
        codes.append(BASELINE_UNRESOLVED)
    elif baseline is None:
        codes.append(NO_BASELINE)
    elif candidate.status in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        codes.append(NOTHING_TO_ACCEPT)
    elif candidate.status is not ValidationStatus.SUSPICIOUS:
        codes.append(STATE_REJECTED)
    else:
        # SUSPICIOUS is produced only by the shrink assessment, which starts
        # from a passing report, so every other code on it is a warning.
        shrink_codes = tuple(code for code in candidate.codes if code in ACCEPTABLE_CODES)
        if not shrink_codes:
            codes.append(STATE_REJECTED)
        elif baseline_payload is None:
            codes.append(BASELINE_UNRESOLVED)
        else:
            explanation, digest = _explain(baseline_payload, candidate_payload)

    plan_material = {
        "version": GUARDIAN_ACCEPT_PLAN_VERSION,
        "machine_id": machine_id,
        "baseline_snapshot_id": baseline.snapshot_id if baseline is not None else None,
        "baseline_sha256": baseline.sha256 if baseline is not None else None,
        "schema_id": candidate.schema_id if candidate is not None else None,
        "projects_now": candidate.project_count if candidate is not None else None,
        "bindings_now": candidate.binding_count if candidate is not None else None,
        "shrink_codes": list(shrink_codes),
        "drop_digest": digest,
        "codes": codes,
    }
    return GuardianAcceptPlan(
        version=GUARDIAN_ACCEPT_PLAN_VERSION,
        plan_id=_hash(plan_material),
        machine_id=machine_id,
        baseline_snapshot_id=baseline.snapshot_id if baseline is not None else None,
        baseline_generation=baseline.generation if baseline is not None else 0,
        baseline_created_at_utc=baseline.created_at_utc if baseline is not None else "",
        schema_id=candidate.schema_id if candidate is not None else None,
        projects_before=baseline.project_count if baseline is not None else None,
        bindings_before=baseline.binding_count if baseline is not None else None,
        projects_now=candidate.project_count if candidate is not None else None,
        bindings_now=candidate.binding_count if candidate is not None else None,
        shrink_codes=shrink_codes,
        state_codes=candidate.codes if candidate is not None else (),
        explanation=explanation,
        codes=tuple(codes),
    )


def accepted_validation(candidate: ValidationReport) -> ValidationReport:
    """The report an accepted state is committed under.

    ``PASS_WITH_WARNING`` because the state did pass every integrity check; the
    shrink codes stay on it so the manifest still says what was overridden.
    """
    return ValidationReport(
        ValidationStatus.PASS_WITH_WARNING,
        tuple(candidate.codes) + (SHRINK_ACCEPTED,),
        project_count=candidate.project_count,
        binding_count=candidate.binding_count,
        schema_id=candidate.schema_id,
    )


def _explain(baseline_payload: bytes, candidate_payload: bytes) -> tuple[ShrinkExplanation | None, str]:
    before = _read_state(baseline_payload)
    after = _read_state(candidate_payload)
    if before is None or after is None:
        return None, ""
    schema_before = detect_state_schema(before)
    schema_after = detect_state_schema(after)
    if schema_before is None or schema_after is None:
        return None, ""

    roots_before = _roots_by_project(schema_before, before)
    roots_after = _roots_by_project(schema_after, after)
    known_roots_after = set(roots_after.values())
    known_roots_before = set(roots_before.values())
    gone = {project for project in roots_before if project not in roots_after}
    replaced = {project for project in gone if roots_before[project] and roots_before[project] in known_roots_after}
    removed = gone - replaced
    added = {
        project for project in roots_after
        if project not in roots_before and not (roots_after[project] and roots_after[project] in known_roots_before)
    }

    to_local_before = _app_server_to_local(before)
    bindings_before = _bindings(schema_before, before, to_local_before)
    bindings_after = _bindings(schema_after, after, _app_server_to_local(after))
    lost: dict[str, str] = {}
    for thread, project in bindings_before.items():
        if thread in bindings_after:
            continue
        if project in replaced:
            lost[thread] = "replaced"
        elif project in removed or project not in roots_before:
            lost[thread] = "removed"
        else:
            lost[thread] = "dropped"

    reasons = list(lost.values())
    explanation = ShrinkExplanation(
        projects_replaced=len(replaced),
        projects_removed=len(removed),
        projects_added=len(added),
        bindings_to_replaced_projects=reasons.count("replaced"),
        bindings_to_removed_projects=reasons.count("removed"),
        bindings_dropped=reasons.count("dropped"),
    )
    digest = _hash({
        "lost_bindings": sorted(lost.items()),
        "replaced": sorted(replaced),
        "removed": sorted(removed),
        "added": sorted(added),
    })
    return explanation, digest


def _read_state(payload: bytes) -> dict[str, Any] | None:
    try:
        state = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _roots_by_project(schema_id: str, state: dict[str, Any]) -> dict[str, frozenset[str]]:
    projects = state.get("local-projects")
    if not isinstance(projects, dict):
        return {}
    return {
        project_id: frozenset(project_root_paths(schema_id, entry))
        for project_id, entry in projects.items()
        if isinstance(project_id, str) and isinstance(entry, dict)
    }


def _app_server_to_local(state: dict[str, Any]) -> dict[str, str]:
    by_host = state.get("app-server-project-id-by-legacy-project-id-by-host")
    mapping: dict[str, str] = {}
    if isinstance(by_host, dict):
        for per_host in by_host.values():
            if isinstance(per_host, dict):
                for local_id, app_server_id in per_host.items():
                    if isinstance(local_id, str) and isinstance(app_server_id, str):
                        mapping[app_server_id] = local_id
    return mapping


def _bindings(schema_id: str, state: dict[str, Any], to_local: dict[str, str]) -> dict[str, str]:
    """Thread id -> the *local* project id it is bound to; unbound threads omitted."""
    assignments = state.get("thread-project-assignments")
    if not isinstance(assignments, dict):
        return {}
    bound: dict[str, str] = {}
    for thread, value in assignments.items():
        project = binding_project_id(schema_id, value)
        if isinstance(thread, str) and project is not None:
            bound[thread] = to_local.get(project, project)
    return bound


def _hash(material: dict[str, Any]) -> str:
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
