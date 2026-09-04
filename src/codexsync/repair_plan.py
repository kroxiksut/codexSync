"""Immutable read-only project repair planning from session cwd evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from .guardian_schema import binding_project_id, detect_state_schema, project_root_paths
from .path_mapping import PathMappingError, PathMappingRule, apply_path_mapping, mapping_digest
from .session_catalog import SessionCatalog, SessionState


class RepairActionKind(str, Enum):
    KEEP_PROJECT = "KEEP_PROJECT"
    ADD_PROJECT = "ADD_PROJECT"
    REMAP_ROOT = "REMAP_ROOT"
    ADD_BINDING = "ADD_BINDING"
    KEEP_BINDING = "KEEP_BINDING"
    SKIP_UNMAPPED = "SKIP_UNMAPPED"
    AMBIGUOUS_PROJECT = "AMBIGUOUS_PROJECT"
    UNSUPPORTED_BACKEND = "UNSUPPORTED_BACKEND"


@dataclass(frozen=True, slots=True)
class RepairAction:
    kind: RepairActionKind
    session_hash: str
    root_hash: str | None
    project_id: str | None = field(repr=False)
    rule_id: str | None = None
    target_root: str | None = field(default=None, repr=False)
    session_id: str | None = field(default=None, repr=False)
    #: For REMAP_ROOT: the exact root string the project entry carries today.
    #: A remap replaces that value and nothing else, so the plan has to name it.
    source_root: str | None = field(default=None, repr=False)
    source_root_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RepairPlan:
    version: int
    plan_id: str
    created_at_utc: str
    source_machine: str
    target_machine: str
    global_state_sha256: str
    mapping_digest: str
    schema_id: str
    volatile: bool
    actions: tuple[RepairAction, ...]
    codes: tuple[str, ...] = ()


def build_repair_plan(
    catalog: SessionCatalog,
    global_state: bytes,
    *,
    source_machine: str,
    target_machine: str,
    rules: list[PathMappingRule],
    volatile: bool,
) -> RepairPlan:
    state = _parse_state(global_state)
    schema_id = detect_state_schema(state)
    if schema_id is None:
        # An unrecognised state is not a state whose project entries may be
        # read, let alone written: the shape is what says where a root lives.
        raise ValueError("UNKNOWN_SCHEMA")
    projects = _existing_project_roots(state, schema_id)
    assignments = state.get("thread-project-assignments", {})
    actions: list[RepairAction] = []
    codes: list[str] = []
    for session in catalog.descriptors:
        if session.state in {SessionState.INVALID, SessionState.AMBIGUOUS} or not session.session_id:
            continue
        session_hash = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
        if not session.cwd:
            actions.append(RepairAction(RepairActionKind.SKIP_UNMAPPED, session_hash, None, None, session_id=session.session_id))
            continue
        try:
            mapped = apply_path_mapping(
                session.cwd, source_machine=source_machine, target_machine=target_machine, rules=rules
            )
            target_root = _candidate_root(Path(mapped.target_path))
        except (PathMappingError, OSError):
            actions.append(RepairAction(RepairActionKind.SKIP_UNMAPPED, session_hash, None, None, session_id=session.session_id))
            continue
        root_text = str(target_root)
        root_hash = hashlib.sha256(root_text.encode("utf-8")).hexdigest()
        matching = [
            project_id for project_id, roots in projects.items()
            if any(_same_host_path(root, root_text) for root in roots)
        ]
        if len(matching) > 1:
            actions.append(RepairAction(RepairActionKind.AMBIGUOUS_PROJECT, session_hash, root_hash, None, mapped.rule_id, root_text, session.session_id))
            codes.append("AMBIGUOUS_PROJECT")
            continue
        if matching:
            project_id = matching[0]
            actions.append(RepairAction(RepairActionKind.KEEP_PROJECT, session_hash, root_hash, project_id, mapped.rule_id, root_text, session.session_id))
        else:
            moved = _moved_projects(
                projects, root_text,
                source_machine=source_machine, target_machine=target_machine, rules=rules,
            )
            if len({project_id for project_id, _ in moved}) > 1:
                actions.append(RepairAction(RepairActionKind.AMBIGUOUS_PROJECT, session_hash, root_hash, None, mapped.rule_id, root_text, session.session_id))
                codes.append("AMBIGUOUS_PROJECT")
                continue
            if moved:
                project_id, source_root = moved[0]
                actions.append(RepairAction(
                    RepairActionKind.REMAP_ROOT, session_hash, root_hash, project_id, mapped.rule_id,
                    root_text, session.session_id, source_root,
                    hashlib.sha256(source_root.encode("utf-8")).hexdigest(),
                ))
            else:
                project_id = str(uuid5(NAMESPACE_URL, f"codexsync:{target_machine}:{root_hash}"))
                actions.append(RepairAction(RepairActionKind.ADD_PROJECT, session_hash, root_hash, project_id, mapped.rule_id, root_text, session.session_id))
        assigned = binding_project_id(schema_id, assignments.get(session.session_id))
        binding_kind = RepairActionKind.KEEP_BINDING if assigned == project_id else RepairActionKind.ADD_BINDING
        actions.append(RepairAction(binding_kind, session_hash, root_hash, project_id, mapped.rule_id, root_text, session.session_id))
    actions.extend(_bind_sessions_left_behind_by_a_remap(catalog, actions, assignments, schema_id))
    codes.extend(_remap_orphan_codes(catalog, actions))
    plan_material = {
        "version": 1,
        "source_machine": source_machine,
        "target_machine": target_machine,
        "global_state_sha256": hashlib.sha256(global_state).hexdigest(),
        "mapping_digest": mapping_digest(rules),
        "volatile": volatile,
        "actions": [
            {"kind": item.kind.value, "session_hash": item.session_hash, "root_hash": item.root_hash, "project_id": item.project_id, "rule_id": item.rule_id, "source_root_hash": item.source_root_hash}
            for item in actions
        ],
    }
    plan_id = hashlib.sha256(json.dumps(plan_material, sort_keys=True).encode("utf-8")).hexdigest()
    return RepairPlan(
        1, plan_id, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        source_machine, target_machine, plan_material["global_state_sha256"], plan_material["mapping_digest"],
        schema_id, volatile, tuple(actions), tuple(dict.fromkeys(codes)),
    )


def save_repair_plan(plan: RepairPlan, path: Path) -> Path:
    payload = {
        "version": plan.version,
        "plan_id": plan.plan_id,
        "created_at_utc": plan.created_at_utc,
        "source_machine": plan.source_machine,
        "target_machine": plan.target_machine,
        "global_state_sha256": plan.global_state_sha256,
        "mapping_digest": plan.mapping_digest,
        "schema_id": plan.schema_id,
        "volatile": plan.volatile,
        "codes": list(plan.codes),
        "actions": [
            {
                "kind": action.kind.value,
                "session_hash": action.session_hash,
                "root_hash": action.root_hash,
                "project_id": action.project_id,
                "rule_id": action.rule_id,
                "target_root": action.target_root,
                "session_id": action.session_id,
                "source_root": action.source_root,
                "source_root_hash": action.source_root_hash,
            }
            for action in plan.actions
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def load_repair_plan(path: Path) -> RepairPlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        actions = tuple(
            RepairAction(
                RepairActionKind(item["kind"]), item["session_hash"], item.get("root_hash"),
                item.get("project_id"), item.get("rule_id"), item.get("target_root"), item.get("session_id"),
                item.get("source_root"), item.get("source_root_hash"),
            )
            for item in raw["actions"]
        )
        plan = RepairPlan(
            int(raw["version"]), str(raw["plan_id"]), str(raw["created_at_utc"]),
            str(raw["source_machine"]), str(raw["target_machine"]), str(raw["global_state_sha256"]),
            str(raw["mapping_digest"]), str(raw["schema_id"]), bool(raw["volatile"]), actions,
            tuple(str(item) for item in raw.get("codes", [])),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Repair plan is invalid") from exc
    for action in plan.actions:
        if action.target_root is not None and hashlib.sha256(action.target_root.encode("utf-8")).hexdigest() != action.root_hash:
            raise ValueError("Repair plan target root hash does not match")
        if action.session_id is not None and hashlib.sha256(action.session_id.encode("utf-8")).hexdigest() != action.session_hash:
            raise ValueError("Repair plan session hash does not match")
        if action.source_root is not None and hashlib.sha256(action.source_root.encode("utf-8")).hexdigest() != action.source_root_hash:
            raise ValueError("Repair plan source root hash does not match")
        if action.kind is RepairActionKind.REMAP_ROOT and not (action.source_root and action.target_root and action.project_id):
            raise ValueError("Repair plan remap action is incomplete")
    expected = _recompute_plan_id(plan)
    if plan.version != 1 or expected != plan.plan_id:
        raise ValueError("Repair plan hash does not match its content")
    return plan


def _recompute_plan_id(plan: RepairPlan) -> str:
    material = {
        "version": plan.version,
        "source_machine": plan.source_machine,
        "target_machine": plan.target_machine,
        "global_state_sha256": plan.global_state_sha256,
        "mapping_digest": plan.mapping_digest,
        "volatile": plan.volatile,
        "actions": [
            {"kind": item.kind.value, "session_hash": item.session_hash, "root_hash": item.root_hash, "project_id": item.project_id, "rule_id": item.rule_id, "source_root_hash": item.source_root_hash}
            for item in plan.actions
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_state(payload: bytes) -> dict:
    try:
        state = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("UNKNOWN_SCHEMA") from exc
    if not isinstance(state, dict) or not isinstance(state.get("local-projects"), dict) or not isinstance(state.get("project-order"), list):
        raise ValueError("UNKNOWN_SCHEMA")
    return state


def _existing_project_roots(state: dict, schema_id: str) -> dict[str, tuple[str, ...]]:
    """Root paths every project entry claims, read through its schema."""
    result: dict[str, tuple[str, ...]] = {}
    for project_id, value in state["local-projects"].items():
        if not isinstance(project_id, str) or not isinstance(value, dict):
            continue
        roots = project_root_paths(schema_id, value)
        if roots:
            result[project_id] = roots
    return result


def _bind_sessions_left_behind_by_a_remap(
    catalog: SessionCatalog,
    actions: list[RepairAction],
    assignments: dict,
    schema_id: str,
) -> list[RepairAction]:
    """Bind every session that reaches a remapped project through its old root.

    A remap points the project at where its *newer* sessions live, which by
    construction is not where the older ones point. Those older chats reach the
    project only through the root being replaced, so a remap on its own detaches
    them — the exact failure this command exists to repair, and one that is
    silent: the chat simply stops appearing under the project.

    So a remap is never emitted alone. Every session under the old root also
    gets an explicit ``thread-project-assignments`` entry, which is the shape
    Codex itself writes when a thread's directory no longer matches its
    project. That is deliberately belt and braces: if the runtime resolves a
    chat by its directory, the binding is redundant; if it resolves by the
    binding, the binding is the whole repair. Neither reading has to be settled
    before this is safe, and settling it wrongly would cost the user their
    history.
    """
    remaps = {
        action.project_id: action
        for action in actions
        if action.kind is RepairActionKind.REMAP_ROOT and action.project_id
    }
    if not remaps:
        return []
    bound = {
        (action.session_id, action.project_id)
        for action in actions
        if action.kind in {RepairActionKind.ADD_BINDING, RepairActionKind.KEEP_BINDING}
    }
    extra: list[RepairAction] = []
    for project_id, remap in sorted(remaps.items()):
        for session in catalog.valid:
            if not session.session_id or not _under(session.cwd, remap.source_root):
                continue
            if (session.session_id, project_id) in bound:
                continue
            bound.add((session.session_id, project_id))
            assigned = binding_project_id(schema_id, assignments.get(session.session_id))
            extra.append(RepairAction(
                RepairActionKind.KEEP_BINDING if assigned == project_id else RepairActionKind.ADD_BINDING,
                hashlib.sha256(session.session_id.encode("utf-8")).hexdigest(),
                remap.root_hash, project_id, remap.rule_id, remap.target_root, session.session_id,
            ))
    return extra


def _remap_orphan_codes(catalog: SessionCatalog, actions: list[RepairAction]) -> list[str]:
    """Refuse a remap that would still leave a session behind.

    The pass above is meant to make this impossible, which is exactly why it is
    checked rather than assumed: a plan is the whole decision, and a refactor
    that quietly stopped covering a session would otherwise ship as a silent
    loss of history. Sessions the catalog already rejected are out of scope
    here, as they are everywhere else in this planner: they carry no usable id,
    so no binding could name them.
    """
    codes: list[str] = []
    bound = {
        (action.session_id, action.project_id)
        for action in actions
        if action.kind in {RepairActionKind.ADD_BINDING, RepairActionKind.KEEP_BINDING}
    }
    for action in actions:
        if action.kind is not RepairActionKind.REMAP_ROOT:
            continue
        for session in catalog.valid:
            if not session.session_id or not _under(session.cwd, action.source_root):
                continue
            if (session.session_id, action.project_id) not in bound:
                codes.append("REMAP_ORPHANS_SESSIONS")
                return codes
    return codes


def _under(path: str | None, root: str | None) -> bool:
    """Whether ``path`` is ``root`` or lives inside it, compared as host paths."""
    if not path or not root:
        return False
    if _same_host_path(path, root):
        return True
    prefix = root.rstrip("/\\").casefold()
    lowered = path.casefold()
    return lowered.startswith(prefix + "/") or lowered.startswith(prefix + "\\")


def _moved_projects(
    projects: dict[str, tuple[str, ...]],
    target_root: str,
    *,
    source_machine: str,
    target_machine: str,
    rules: list[PathMappingRule],
) -> list[tuple[str, str]]:
    """Projects whose recorded root is this same directory, named the old way.

    A project entry written on the source machine still holds that machine's
    path, and a project that simply moved still holds the old one. Running a
    recorded root through the very mapping the sessions use is the only
    evidence that an existing project *is* this project under a new path.

    Remapping it is what makes the whole migration safe: the project keeps its
    id, so every thread already bound to it follows automatically, and not one
    byte inside a session file has to change — rewriting a ``cwd`` there would
    make the same history on two machines permanently divergent.
    """
    moved: list[tuple[str, str]] = []
    for project_id, roots in sorted(projects.items()):
        for root in roots:
            try:
                mapped = apply_path_mapping(
                    root, source_machine=source_machine, target_machine=target_machine, rules=rules
                )
            except PathMappingError:
                continue
            if _same_host_path(mapped.target_path, target_root):
                moved.append((project_id, root))
                break
    return moved


def _candidate_root(path: Path) -> Path:
    candidate = path
    if not candidate.exists():
        raise OSError("mapped path is unavailable")
    candidate = candidate.resolve()
    for current in (candidate, *candidate.parents):
        if (current / ".git").exists():
            return current
    return candidate


def _same_host_path(left: str, right: str) -> bool:
    return left.rstrip("/\\").casefold() == right.rstrip("/\\").casefold()
