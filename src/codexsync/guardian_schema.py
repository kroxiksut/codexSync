from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .guardian_models import ValidationReport, ValidationStatus
from .guardian_validation import DUPLICATE_JSON_KEY, validate_source_observation


UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"
BROKEN_PROJECT_REFERENCE = "BROKEN_PROJECT_REFERENCE"
BROKEN_ORDER_REFERENCE = "BROKEN_ORDER_REFERENCE"
BROKEN_BINDING_REFERENCE = "BROKEN_BINDING_REFERENCE"
DUPLICATE_ORDER_REFERENCE = "DUPLICATE_ORDER_REFERENCE"

LEGACY_V1_SCHEMA = "legacy-v1"
#: The shape written by the Electron Codex desktop app: bindings carry
#: ``projectKind``/``projectId`` and app-server ids live in a per-host map
#: instead of a flat ``project-id-migrations`` table.
ELECTRON_V2_SCHEMA = "electron-v2"
#: Binding kinds this adapter understands. An unfamiliar kind makes the whole
#: state unrecognised rather than partly understood.
_ELECTRON_BINDING_KINDS = frozenset({"local", "app-server", "remote"})
#: Where each schema keeps a project's root path. These are the only keys a
#: repair may read a root from or write one to.
_LEGACY_ROOT_KEYS = ("root", "path", "cwd")
_ELECTRON_ROOT_KEY = "rootPaths"


class _DuplicateKeyError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class StateReferences:
    schema_id: str
    project_ids: frozenset[str]
    ordered_project_ids: tuple[str, ...]
    binding_project_ids: tuple[str, ...]
    app_server_project_ids: frozenset[str]
    has_dangling_explicit_binding: bool = False


class LegacyV1Adapter:
    """Supported anonymized shape of the legacy JSON-managed project state."""

    schema_id = LEGACY_V1_SCHEMA

    def extract(self, state: dict[str, Any]) -> StateReferences | None:
        projects = state.get("local-projects")
        order = state.get("project-order")
        if not isinstance(projects, dict) or not isinstance(order, list):
            return None
        if not all(isinstance(project_id, str) and project_id for project_id in projects):
            return None
        if not all(isinstance(project, dict) for project in projects.values()):
            return None
        if not all(isinstance(project_id, str) and project_id for project_id in order):
            return None
        if any(_ELECTRON_ROOT_KEY in project for project in projects.values()):
            # Everything this adapter reads — bindings and migrations — is
            # absent in a state that has no threads assigned yet, so without
            # this it would claim a brand-new Electron state and the writer
            # would then put legacy-shaped entries into it. The key is not a
            # guess about the legacy shape; it is a fact about the Electron one.
            return None

        migrations = state.get("project-id-migrations", {})
        if not isinstance(migrations, dict) or not all(
            isinstance(old_id, str)
            and old_id
            and isinstance(new_id, str)
            and new_id
            for old_id, new_id in migrations.items()
        ):
            return None
        if not set(migrations).issubset(projects):
            return None

        assignments = state.get("thread-project-assignments", {})
        if not isinstance(assignments, dict) or not all(isinstance(thread_id, str) and thread_id for thread_id in assignments):
            return None
        legacy_ids = frozenset(projects)
        app_server_ids = frozenset(migrations.values())
        binding_ids: list[str] = []
        has_dangling_explicit_binding = False
        for assigned_id in assignments.values():
            # null is the only explicitly supported non-project sentinel in v1.
            if assigned_id is None:
                continue
            if isinstance(assigned_id, dict):
                namespace = assigned_id.get("namespace")
                project_id = assigned_id.get("project_id")
                if set(assigned_id) != {"namespace", "project_id"} or not isinstance(project_id, str) or not project_id:
                    return None
                if namespace == "legacy":
                    has_dangling_explicit_binding |= project_id not in legacy_ids
                elif namespace == "app-server":
                    has_dangling_explicit_binding |= project_id not in app_server_ids
                else:
                    return None
                binding_ids.append(project_id)
                continue
            if not isinstance(assigned_id, str) or not assigned_id:
                return None
            if assigned_id not in legacy_ids and assigned_id not in app_server_ids:
                return None
            binding_ids.append(assigned_id)
        return StateReferences(
            schema_id=self.schema_id,
            project_ids=legacy_ids,
            ordered_project_ids=tuple(order),
            binding_project_ids=tuple(binding_ids),
            app_server_project_ids=app_server_ids,
            has_dangling_explicit_binding=has_dangling_explicit_binding,
        )


class ElectronV2Adapter:
    """State as written by the Electron desktop build.

    Recognised by the binding shape.  Everything the reference check needs is
    read here and nothing else: project ids, their order, the ids threads are
    bound to, and the app-server ids a binding may legitimately point at.
    Values — names, paths, timestamps — are never read, so they cannot reach a
    manifest or a log.
    """

    schema_id = ELECTRON_V2_SCHEMA

    def extract(self, state: dict[str, Any]) -> StateReferences | None:
        projects = state.get("local-projects")
        order = state.get("project-order")
        if not isinstance(projects, dict) or not isinstance(order, list):
            return None
        if not all(isinstance(project_id, str) and project_id for project_id in projects):
            return None
        if not all(isinstance(project, dict) for project in projects.values()):
            return None
        if not all(isinstance(project_id, str) and project_id for project_id in order):
            return None

        app_server_ids = _electron_app_server_ids(state)
        if app_server_ids is None:
            return None

        assignments = state.get("thread-project-assignments", {})
        if not isinstance(assignments, dict) or not all(
            isinstance(thread_id, str) and thread_id for thread_id in assignments
        ):
            return None

        legacy_ids = frozenset(projects)
        binding_ids: list[str] = []
        has_dangling_explicit_binding = False
        recognised_binding = False
        for assigned in assignments.values():
            if assigned is None:
                continue
            if not isinstance(assigned, dict):
                return None
            if set(assigned) != {"projectKind", "projectId"}:
                return None
            kind = assigned["projectKind"]
            project_id = assigned["projectId"]
            if kind not in _ELECTRON_BINDING_KINDS or not isinstance(project_id, str) or not project_id:
                return None
            recognised_binding = True
            if kind == "local":
                has_dangling_explicit_binding |= project_id not in legacy_ids
            else:
                has_dangling_explicit_binding |= project_id not in app_server_ids
            binding_ids.append(project_id)

        if not recognised_binding and not app_server_ids and not _electron_project_shape(projects):
            # Nothing distinguishes this from the legacy shape, and claiming it
            # here would hide a genuinely unknown state behind a guess.
            return None

        return StateReferences(
            schema_id=self.schema_id,
            project_ids=legacy_ids,
            ordered_project_ids=tuple(order),
            binding_project_ids=tuple(binding_ids),
            app_server_project_ids=app_server_ids,
            has_dangling_explicit_binding=has_dangling_explicit_binding,
        )


def _electron_project_shape(projects: dict[str, Any]) -> bool:
    """Whether the project entries themselves name this as the Electron shape.

    A desktop state with no threads assigned yet has neither a binding nor an
    app-server id, so the two signals above are both silent and the state would
    be unrecognised — which is how Guardian came to quarantine every real file
    once already. The entry's own root key settles it, and the legacy adapter
    refuses the same key, so exactly one adapter can claim such a state.
    """
    return any(
        isinstance(project, dict) and _ELECTRON_ROOT_KEY in project
        for project in projects.values()
    )


def _electron_app_server_ids(state: dict[str, Any]) -> frozenset[str] | None:
    """Ids from ``app-server-project-id-by-legacy-project-id-by-host``."""
    by_host = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
    if not isinstance(by_host, dict):
        return None
    collected: set[str] = set()
    for host, mapping in by_host.items():
        if not isinstance(host, str) or not host or not isinstance(mapping, dict):
            return None
        for legacy_id, app_server_id in mapping.items():
            if not (isinstance(legacy_id, str) and legacy_id and isinstance(app_server_id, str) and app_server_id):
                return None
            collected.add(app_server_id)
    return frozenset(collected)


#: Tried in order; the first adapter that recognises the state wins and its id
#: is recorded in the report, so a later runtime change is a new adapter rather
#: than a loosened old one.
_ADAPTERS = (LegacyV1Adapter(), ElectronV2Adapter())


def detect_state_schema(state: dict[str, Any]) -> str | None:
    """Schema id of an already-parsed state, or ``None`` when unrecognised."""
    for adapter in _ADAPTERS:
        if adapter.extract(state) is not None:
            return adapter.schema_id
    return None


def build_binding_value(schema_id: str, project_id: str) -> Any:
    """Thread-to-project binding in the shape the detected schema uses.

    A writer that assumes one schema silently produces state the runtime cannot
    read, so the shape is derived from the detected schema rather than hardcoded.
    """
    if schema_id == LEGACY_V1_SCHEMA:
        return project_id
    if schema_id == ELECTRON_V2_SCHEMA:
        return {"projectKind": "local", "projectId": project_id}
    raise ValueError(f"No binding shape is known for schema {schema_id!r}")


def project_root_paths(schema_id: str, project: dict[str, Any]) -> tuple[str, ...]:
    """Every root path one project entry claims, in the shape its schema uses.

    A legacy entry carries one path, possibly repeated under alias keys; an
    Electron entry carries a list. An entry whose root cannot be read this way
    comes back empty rather than guessed at, which is what keeps it out of a
    repair plan entirely.
    """
    if schema_id == LEGACY_V1_SCHEMA:
        values = [
            project[key] for key in _LEGACY_ROOT_KEYS
            if isinstance(project.get(key), str) and project[key]
        ]
        # The keys are aliases of one value. Disagreement means the entry is not
        # understood, and an entry with no readable root is safer than a coin toss.
        return (values[0],) if values and len(set(values)) == 1 else ()
    if schema_id == ELECTRON_V2_SCHEMA:
        roots = project.get(_ELECTRON_ROOT_KEY)
        if not isinstance(roots, list):
            return ()
        return tuple(dict.fromkeys(item for item in roots if isinstance(item, str) and item))
    return ()


def replace_project_root(
    schema_id: str, project: dict[str, Any], *, old_root: str, new_root: str
) -> dict[str, Any]:
    """One project entry with ``old_root`` rewritten to ``new_root``.

    Only the root value moves. Names, ids and timestamps stay exactly as the
    runtime wrote them, which is what separates a remap from creating a project:
    nothing is invented, so no guess reaches state the runtime treats as fact.

    Idempotent — an entry that already carries ``new_root`` and no longer
    carries ``old_root`` comes back unchanged, so a plan naming the same project
    once per session applies cleanly. Anything else raises, because an entry
    that holds neither path is not the entry the plan was built against.
    """
    updated = dict(project)
    if schema_id == LEGACY_V1_SCHEMA:
        keys = [key for key in _LEGACY_ROOT_KEYS if isinstance(project.get(key), str) and project[key]]
        if not keys:
            raise ValueError("Project entry carries no readable root path")
        if all(project[key] == new_root for key in keys):
            return updated
        if any(project[key] != old_root for key in keys):
            raise ValueError("Project entry no longer carries the root the plan recorded")
        for key in keys:
            updated[key] = new_root
        return updated
    if schema_id == ELECTRON_V2_SCHEMA:
        roots = project.get(_ELECTRON_ROOT_KEY)
        if not isinstance(roots, list):
            raise ValueError("Project entry carries no readable root path")
        if old_root not in roots:
            if new_root in roots:
                return updated
            raise ValueError("Project entry no longer carries the root the plan recorded")
        updated[_ELECTRON_ROOT_KEY] = [new_root if item == old_root else item for item in roots]
        return updated
    raise ValueError(f"No project root shape is known for schema {schema_id!r}")


def binding_project_id(schema_id: str, value: Any) -> str | None:
    """The project id a thread binding points at, or ``None`` for no project.

    Read through the schema for the same reason it is written through it: an
    Electron binding is an object, so comparing it to a bare id would report
    every already-correct binding as missing and rewrite it.
    """
    if value is None:
        return None
    if schema_id == LEGACY_V1_SCHEMA:
        if isinstance(value, str):
            return value or None
        if isinstance(value, dict):
            project_id = value.get("project_id")
            return project_id if isinstance(project_id, str) and project_id else None
        return None
    if schema_id == ELECTRON_V2_SCHEMA:
        if isinstance(value, dict):
            project_id = value.get("projectId")
            return project_id if isinstance(project_id, str) and project_id else None
        return None
    return None


def supports_root_remap(schema_id: str) -> bool:
    """Whether an existing project's root may be rewritten for this schema.

    Available wherever the root shape is known, including the Electron shape
    where creating a project is not: a remap edits one value the runtime itself
    wrote, while a creation would have to invent the fields around it.
    """
    return schema_id in {LEGACY_V1_SCHEMA, ELECTRON_V2_SCHEMA}


def supports_project_creation(schema_id: str) -> bool:
    """Whether a *new* project entry can be written for this schema.

    Only the legacy shape qualifies. An Electron project entry also carries a
    name and creation timestamps whose semantics have not been confirmed, and
    inventing them would put a guess into state that the runtime then treats as
    fact. Remapping or binding an existing project stays available.
    """
    return schema_id == LEGACY_V1_SCHEMA


def validate_global_state_references(payload: bytes) -> ValidationReport:
    """Validate supported project references without exposing identifiers or values."""
    byte_report = validate_source_observation(_stable_observation(payload))
    if byte_report.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        return byte_report
    try:
        state = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError:
        return ValidationReport(ValidationStatus.INVALID, (DUPLICATE_JSON_KEY,))
    except (UnicodeError, json.JSONDecodeError, MemoryError, OverflowError, RecursionError):
        return ValidationReport(ValidationStatus.INDETERMINATE, (UNKNOWN_SCHEMA,))
    if not isinstance(state, dict):  # Kept defensive when the byte validator changes.
        return ValidationReport(ValidationStatus.INDETERMINATE, (UNKNOWN_SCHEMA,))

    for adapter in _ADAPTERS:
        references = adapter.extract(state)
        if references is not None:
            return _validate_references(references, byte_report)
    return ValidationReport(ValidationStatus.INDETERMINATE, (UNKNOWN_SCHEMA,))


def _validate_references(references: StateReferences, byte_report: ValidationReport) -> ValidationReport:
    project_ids = references.project_ids
    order = references.ordered_project_ids
    if len(set(order)) != len(order):
        return _report(ValidationStatus.INVALID, DUPLICATE_ORDER_REFERENCE, references, byte_report)
    if not set(order).issubset(project_ids):
        return _report(ValidationStatus.INVALID, BROKEN_ORDER_REFERENCE, references, byte_report)
    # The v1 fixture contract requires a complete order, including for an empty state.
    if set(order) != set(project_ids):
        return _report(ValidationStatus.INVALID, BROKEN_PROJECT_REFERENCE, references, byte_report)
    known_binding_ids = project_ids | references.app_server_project_ids
    if references.has_dangling_explicit_binding or not set(references.binding_project_ids).issubset(known_binding_ids):
        return _report(ValidationStatus.INVALID, BROKEN_BINDING_REFERENCE, references, byte_report)
    return _report(ValidationStatus.PASS, None, references, byte_report)


def _report(
    status: ValidationStatus,
    code: str | None,
    references: StateReferences,
    byte_report: ValidationReport,
) -> ValidationReport:
    codes = byte_report.codes + ((code,) if code else ())
    final_status = ValidationStatus.PASS_WITH_WARNING if status is ValidationStatus.PASS and codes else status
    return ValidationReport(
        final_status,
        codes,
        project_count=len(references.project_ids),
        binding_count=len(references.binding_project_ids),
        schema_id=references.schema_id,
    )


def _stable_observation(payload: bytes):
    from .guardian_models import SourceObservation

    return SourceObservation(
        payload=payload,
        source_name=".codex-global-state.json",
        source_size_before=len(payload),
        source_size_after=len(payload),
        source_mtime_ns_before=0,
        source_mtime_ns_after=0,
        source_file_id_before=None,
        source_file_id_after=None,
        is_stable=True,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKeyError
        value[key] = item
    return value
