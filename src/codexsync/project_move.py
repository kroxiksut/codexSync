"""Move a Codex project to a new folder without ever risking the old one.

A person who moves a project folder by hand loses it in Codex: the project entry
in ``.codex-global-state.json`` still names the old root, and every chat that
reached the project only because its ``cwd`` fell under that root stops
appearing, silently. ``repair_plan`` already knows how to remap a root and pin
the chats a remap would strand; what it cannot do is move the files. This does,
under one rule that outranks every convenience:

**Nothing the user owns is ever deleted or overwritten.** The project is
*copied* into a folder that does not exist yet, every byte of the copy is
re-hashed against the inventory the person previewed, and only then is the root
remapped. The old folder is never touched — not renamed, not cleaned, not
deleted — and the result says so, so the person decides when it goes. The only
thing this module may ever remove is its own staging directory, and it proves
ownership twice before doing so: by the exact name, and by a marker inside that
names the very plan being applied.

The copy lands in a staging directory beside the target and is renamed into
place in one step, so a target folder either does not exist or holds a verified
copy; there is no half-copied project for Codex or the person to open. Between
the rename and the state commit a resume marker sits beside the target. If the
commit fails, the copy and the marker stay: a rebuilt plan recognises the copy
(same inventory, re-hashed from disk) as ``copy_complete`` and the rerun only
commits, instead of refusing forever with ``TARGET_EXISTS`` or copying twice.

Why the global state is edited and session files are not: raw bytes are a
record's identity in ``semantic_merge``, so rewriting a ``cwd`` inside a session
would make one history on two machines permanently divergent. The root moves in
the project entry; the chats that reached it by path are pinned with explicit
bindings, which is correct whichever way the runtime resolves a chat.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Callable, Iterable, Sequence

from .exceptions import ConfigError, ConflictError, FailSafeError
from .guardian_models import ValidationStatus
from .guardian_schema import (
    binding_project_id,
    build_binding_value,
    detect_state_schema,
    project_root_paths,
    replace_project_root,
    supports_root_remap,
    validate_global_state_references,
)
from .repair_plan import _under


LOG = logging.getLogger(__name__)

PROJECT_MOVE_PLAN_VERSION = 1
#: Format of the two marker files this module writes beside a target.
_MARKER_FORMAT = 1

UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
MULTI_ROOT_PROJECT = "MULTI_ROOT_PROJECT"
UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
SOURCE_MISSING = "SOURCE_MISSING"
SAME_ROOT = "SAME_ROOT"
TARGET_EXISTS = "TARGET_EXISTS"
TARGET_PARENT_MISSING = "TARGET_PARENT_MISSING"
TARGET_INSIDE_SOURCE = "TARGET_INSIDE_SOURCE"
SOURCE_INSIDE_TARGET = "SOURCE_INSIDE_TARGET"
PROTECTED_TARGET = "PROTECTED_TARGET"
TARGET_BELONGS_TO_PROJECT = "TARGET_BELONGS_TO_PROJECT"
SOURCE_CONTAINS_PROJECT = "SOURCE_CONTAINS_PROJECT"
SOURCE_HAS_LINKS = "SOURCE_HAS_LINKS"
SOURCE_HAS_SPECIAL_FILES = "SOURCE_HAS_SPECIAL_FILES"
SOURCE_HAS_RESERVED_NAME = "SOURCE_HAS_RESERVED_NAME"
SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
STAGING_OCCUPIED = "STAGING_OCCUPIED"

#: Inventory size recorded for an empty directory. A directory with no files in
#: it has no entry of its own otherwise, and would silently not exist in the copy.
DIRECTORY_SIZE = -1
#: Name of the ownership marker inside a staging directory. A project that
#: carries a file of this name at its top level is refused: the copy would
#: overwrite the marker and the marker's removal would then delete the copy of
#: the person's file.
STAGING_MARKER_NAME = ".codexsync-staging.json"
_READ_CHUNK = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
#: Bit Windows sets on every reparse tag that *names another location*: symlinks,
#: junctions, WSL links. Tags without it hold the data themselves.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000
#: How many offending paths a blocked plan names. Enough to act on; a project
#: with thousands of unreadable files does not need all of them in a preview.
MAX_BLOCKED_PATHS = 50


@dataclass(frozen=True, slots=True)
class MoveInventoryEntry:
    """One file (or empty directory, ``size == DIRECTORY_SIZE``) of the source."""

    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProjectMovePlan:
    version: int
    plan_id: str
    #: Not part of ``plan_id``: a rebuild must reproduce the id it is checked against.
    created_at_utc: str
    schema_id: str
    project_id: str
    project_name: str | None
    #: The exact string the project entry stores; the remap replaces that value.
    old_root: str
    #: Absolute, as the user gave it, normalised with ``os.path.abspath``.
    new_root: str
    global_state_sha256: str
    file_count: int
    total_bytes: int
    #: Empty when the plan is blocked before hashing was worth doing.
    inventory_sha256: str
    #: Session ids to pin to the project, sorted.
    bindings: tuple[str, ...]
    #: A previous run already copied and verified the target; apply only commits.
    copy_complete: bool
    volatile: bool
    #: Any code blocks apply.
    codes: tuple[str, ...]
    #: What apply copies and verifies against. Covered by ``inventory_sha256``.
    inventory: tuple[MoveInventoryEntry, ...] = field(default=(), repr=False)
    #: ``(code, relative path)`` for the first entries that blocked the plan, so
    #: a person can act on ``SOURCE_UNREADABLE`` in a 40 000-file project.
    #: Informational only and outside ``plan_id``: the codes are what decide.
    blocked_paths: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectMoveResult:
    plan_id: str
    #: Zero when the plan was ``copy_complete``; what would be copied on a dry run.
    copied_files: int
    copied_bytes: int
    bindings_written: int
    #: The old folder, which is kept. Removing it is the person's decision.
    old_root_kept: str
    new_root: str
    dry_run: bool


# --------------------------------------------------------------------------- plan


def build_project_move_plan(
    *,
    state_bytes: bytes,
    project_id: str,
    new_root: Path,
    sessions: Iterable[tuple[str, str | None]],
    protected_roots: Sequence[Path],
    volatile: bool,
    now: datetime | None = None,
) -> ProjectMovePlan:
    """Read-only preview of moving one project's folder to ``new_root``.

    ``sessions`` are ``(session_id, cwd)`` pairs for sessions the catalogue
    judged valid. ``protected_roots`` are every directory this tool or Codex
    owns (``.codex``, the cloud mirror, backups, temp, Guardian and semantic
    roots); a target overlapping one is refused.

    Hashing a project is the expensive part, so a plan already blocked by its
    paths walks the source for counts and links only and leaves
    ``inventory_sha256`` empty — a blocked plan cannot be applied either way.
    """
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created_text = created.isoformat().replace("+00:00", "Z")
    new_root_text = os.path.abspath(os.fspath(new_root))
    state_sha = hashlib.sha256(state_bytes).hexdigest()

    def finish(
        codes: list[str],
        *,
        schema_id: str = "",
        project_name: str | None = None,
        old_root: str = "",
        inventory: tuple[MoveInventoryEntry, ...] = (),
        inventory_sha256: str = "",
        bindings: tuple[str, ...] = (),
        copy_complete: bool = False,
        blocked_paths: tuple[tuple[str, str], ...] = (),
    ) -> ProjectMovePlan:
        unique_codes = tuple(dict.fromkeys(codes))
        files = [entry for entry in inventory if entry.size != DIRECTORY_SIZE]
        plan = ProjectMovePlan(
            version=PROJECT_MOVE_PLAN_VERSION,
            plan_id="",
            created_at_utc=created_text,
            schema_id=schema_id,
            project_id=project_id,
            project_name=project_name,
            old_root=old_root,
            new_root=new_root_text,
            global_state_sha256=state_sha,
            file_count=len(files),
            total_bytes=sum(entry.size for entry in files),
            inventory_sha256=inventory_sha256,
            bindings=bindings,
            copy_complete=copy_complete,
            volatile=volatile,
            codes=unique_codes,
            inventory=inventory,
            blocked_paths=blocked_paths,
        )
        return _with_plan_id(plan)

    state = _parse_state(state_bytes)
    schema_id = detect_state_schema(state) if state is not None else None
    if state is None or schema_id is None:
        return finish([UNKNOWN_SCHEMA])
    if not supports_root_remap(schema_id):
        return finish([UNSUPPORTED_SCHEMA], schema_id=schema_id)
    projects = state["local-projects"]
    entry = projects.get(project_id)
    if not isinstance(entry, dict):
        return finish([PROJECT_NOT_FOUND], schema_id=schema_id)
    name = entry.get("name")
    project_name = name if isinstance(name, str) and name else None
    roots = project_root_paths(schema_id, entry)
    if len(roots) != 1:
        # Every project ever observed has exactly one root. Moving one of
        # several would be a guess about what the others mean.
        return finish([MULTI_ROOT_PROJECT], schema_id=schema_id, project_name=project_name)
    old_root = roots[0]

    codes: list[str] = []
    source_ok = os.path.isabs(old_root) and os.path.isdir(old_root)
    if not source_ok:
        codes.append(SOURCE_MISSING)
    norm_old = _norm(old_root)
    norm_new = _norm(new_root_text)
    if norm_old == norm_new:
        codes.append(SAME_ROOT)
    else:
        if _contains(norm_old, norm_new):
            codes.append(TARGET_INSIDE_SOURCE)
        if _contains(norm_new, norm_old):
            codes.append(SOURCE_INSIDE_TARGET)
    if not os.path.isdir(os.path.dirname(new_root_text)):
        codes.append(TARGET_PARENT_MISSING)
    for protected in protected_roots:
        norm_protected = _norm(os.fspath(protected))
        if _contains(norm_protected, norm_new) or _contains(norm_new, norm_protected):
            codes.append(PROTECTED_TARGET)
    for other_id, other in projects.items():
        if other_id == project_id or not isinstance(other, dict):
            continue
        for other_root in project_root_paths(schema_id, other):
            norm_other = _norm(other_root)
            if _contains(norm_new, norm_other):
                codes.append(TARGET_BELONGS_TO_PROJECT)
            if _contains(norm_old, norm_other):
                # Another project lives inside (or at) the folder being moved.
                # Its chats resolve to it, not to this project, and it would
                # keep pointing at the old copy: which project the moved files
                # then belong to is a decision, not something to infer.
                codes.append(SOURCE_CONTAINS_PROJECT)

    target_exists = os.path.lexists(new_root_text)
    resume_candidate = False
    marker: dict[str, Any] | None = None
    if target_exists:
        marker = _read_resume_marker(new_root_text)
        resume_candidate = (
            not codes
            and marker is not None
            and marker.get("project_id") == project_id
            and marker.get("old_root") == old_root
            and isinstance(marker.get("new_root"), str)
            and _norm(marker["new_root"]) == norm_new
        )
        if not resume_candidate:
            codes.append(TARGET_EXISTS)

    inventory: tuple[MoveInventoryEntry, ...] = ()
    inventory_sha = ""
    blocked_paths: tuple[tuple[str, str], ...] = ()
    if source_ok:
        hash_files = not codes
        walk = _walk(old_root, hash_files=hash_files)
        inventory = walk.entries
        blocked_paths = walk.problems
        codes.extend(walk.codes)
        if any(item.relative_path == STAGING_MARKER_NAME for item in walk.entries):
            codes.append(SOURCE_HAS_RESERVED_NAME)
            blocked_paths = ((SOURCE_HAS_RESERVED_NAME, STAGING_MARKER_NAME),) + blocked_paths
        if hash_files and not walk.codes:
            inventory_sha = inventory_digest(walk.entries)

    copy_complete = False
    if resume_candidate:
        if (
            inventory_sha
            and marker is not None
            and marker.get("inventory_sha256") == inventory_sha
            and marker.get("state") == "COPIED"
            and _is_plain_directory(new_root_text)
        ):
            target = _walk(new_root_text, hash_files=True)
            copy_complete = not target.codes and inventory_digest(target.entries) == inventory_sha
        if not copy_complete:
            codes.append(TARGET_EXISTS)

    assignments = state.get("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}
    bindings = tuple(sorted({
        session_id
        for session_id, cwd in sessions
        if session_id
        and cwd
        and _cwd_under(cwd, old_root, norm_old)
        # A chat that already carries a binding reaches its project through
        # that binding, not through this root: one bound here needs nothing,
        # and one bound elsewhere was put there on purpose and must stay.
        and binding_project_id(schema_id, assignments.get(session_id)) is None
    }))

    return finish(
        codes,
        schema_id=schema_id,
        project_name=project_name,
        old_root=old_root,
        inventory=inventory,
        inventory_sha256=inventory_sha,
        bindings=bindings,
        copy_complete=copy_complete,
        blocked_paths=blocked_paths[:MAX_BLOCKED_PATHS],
    )


def inventory_digest(entries: Iterable[MoveInventoryEntry]) -> str:
    """SHA-256 over one JSON line per entry, sorted by relative path.

    JSON rather than a delimiter because a file name on macOS may contain a tab
    or a newline, and two different inventories must never hash alike.
    """
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.relative_path):
        line = json.dumps([entry.relative_path, entry.size, entry.sha256], ensure_ascii=False)
        digest.update(line.encode("utf-8") + b"\n")
    return digest.hexdigest()


def save_project_move_plan(plan: ProjectMovePlan, path: Path) -> Path:
    payload = {
        "version": plan.version,
        "plan_id": plan.plan_id,
        "created_at_utc": plan.created_at_utc,
        "schema_id": plan.schema_id,
        "project_id": plan.project_id,
        "project_name": plan.project_name,
        "old_root": plan.old_root,
        "new_root": plan.new_root,
        "global_state_sha256": plan.global_state_sha256,
        "file_count": plan.file_count,
        "total_bytes": plan.total_bytes,
        "inventory_sha256": plan.inventory_sha256,
        "bindings": list(plan.bindings),
        "copy_complete": plan.copy_complete,
        "volatile": plan.volatile,
        "codes": list(plan.codes),
        "inventory": [[item.relative_path, item.size, item.sha256] for item in plan.inventory],
        "blocked_paths": [list(item) for item in plan.blocked_paths],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    # A temp file is only ever left by a save that died mid-write; it holds
    # nothing anyone confirmed, and "x" mode would otherwise refuse forever.
    temp.unlink(missing_ok=True)
    with temp.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


def load_project_move_plan(path: Path) -> ProjectMovePlan:
    """Read a saved plan, refusing one whose content no longer matches its id."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        name = raw.get("project_name")
        plan = ProjectMovePlan(
            version=int(raw["version"]),
            plan_id=str(raw["plan_id"]),
            created_at_utc=str(raw["created_at_utc"]),
            schema_id=str(raw["schema_id"]),
            project_id=str(raw["project_id"]),
            project_name=None if name is None else str(name),
            old_root=str(raw["old_root"]),
            new_root=str(raw["new_root"]),
            global_state_sha256=str(raw["global_state_sha256"]),
            file_count=int(raw["file_count"]),
            total_bytes=int(raw["total_bytes"]),
            inventory_sha256=str(raw["inventory_sha256"]),
            bindings=tuple(str(item) for item in raw["bindings"]),
            copy_complete=_strict_bool(raw["copy_complete"]),
            volatile=_strict_bool(raw["volatile"]),
            codes=tuple(str(item) for item in raw["codes"]),
            inventory=tuple(
                MoveInventoryEntry(str(item[0]), int(item[1]), str(item[2]))
                for item in raw["inventory"]
            ),
            blocked_paths=tuple((str(item[0]), str(item[1])) for item in raw.get("blocked_paths", [])),
        )
    except (OSError, KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Project move plan is invalid") from exc
    problem = _integrity_problem(plan)
    if problem is not None:
        raise ValueError(f"Project move plan does not verify: {problem}")
    return plan


# -------------------------------------------------------------------------- apply


def apply_project_move(
    plan: ProjectMovePlan,
    *,
    confirm_plan: str,
    rebuild: Callable[[], ProjectMovePlan],
    require_stopped: Callable[[], None],
    read_state: Callable[[], bytes],
    commit_state: Callable[[bytes, bytes, str, int], int],
    dry_run: bool = False,
) -> ProjectMoveResult:
    """Copy, verify, rename into place, then remap the root — or refuse.

    ``rebuild`` rebuilds the plan from the current disk and state;
    ``require_stopped`` raises ``SafetyPreconditionError`` unless Codex is
    stopped; ``commit_state`` is the caller's wrapper around
    ``app.commit_global_state`` (lock, journal, backup, final gate, rollback).

    The order is the safety. The candidate state is built and validated before
    any file is copied, so an edit that cannot be made costs nothing. The copy
    is verified against the previewed inventory before it becomes visible under
    the target name. The state is committed last, so a failure anywhere before
    it leaves Codex exactly as it was — and a failure *of* it leaves a verified
    copy a rerun picks up instead of discarding.
    """
    if confirm_plan != plan.plan_id:
        raise ConfigError("The confirmation must exactly match the project move plan id")
    if plan.codes:
        raise ConflictError("Project move plan is blocked: " + ", ".join(plan.codes))
    if plan.volatile:
        raise FailSafeError("A project move plan built while Codex was running cannot be applied")
    problem = _integrity_problem(plan)
    if problem is not None:
        raise FailSafeError(f"Project move plan does not verify: {problem}")

    fresh = rebuild()
    if fresh.plan_id != confirm_plan:
        raise FailSafeError(
            "The project, its files or the Codex state changed since the preview; preview again"
        )
    if fresh.codes:
        raise ConflictError("Project move is blocked: " + ", ".join(fresh.codes))

    require_stopped()
    original = read_state()
    if hashlib.sha256(original).hexdigest() != plan.global_state_sha256:
        raise FailSafeError("Codex state changed since the preview; preview again")
    candidate = _candidate_state(plan, original)
    action_count = 1 + len(plan.bindings)

    if dry_run:
        LOG.info(
            "project move dry-run: plan %s would copy %d file(s) (%d bytes) from %s to %s, "
            "remap the root and write %d binding(s); the old folder would be kept",
            plan.plan_id, 0 if plan.copy_complete else plan.file_count,
            0 if plan.copy_complete else plan.total_bytes,
            plan.old_root, plan.new_root, len(plan.bindings),
        )
        return _result(plan, dry_run=True)

    if plan.copy_complete:
        LOG.info("project move: %s already holds a verified copy; skipping the copy", plan.new_root)
    else:
        _copy_into_place(plan, require_stopped)
        _write_resume_marker(plan)

    require_stopped()
    try:
        commit_state(original, candidate, plan.plan_id, action_count)
    except BaseException:
        LOG.error(
            "project move: committing the state failed; the verified copy at %s and its resume "
            "marker are kept so a rerun only commits, and the old folder %s is untouched",
            plan.new_root, plan.old_root,
        )
        raise
    _remove_resume_marker(plan)
    LOG.warning(
        "project move: %s now points at %s; the old folder %s is KEPT and was not modified",
        plan.project_id, plan.new_root, plan.old_root,
    )
    return _result(plan, dry_run=False)


def _result(plan: ProjectMovePlan, *, dry_run: bool) -> ProjectMoveResult:
    return ProjectMoveResult(
        plan_id=plan.plan_id,
        copied_files=0 if plan.copy_complete else plan.file_count,
        copied_bytes=0 if plan.copy_complete else plan.total_bytes,
        bindings_written=len(plan.bindings),
        old_root_kept=plan.old_root,
        new_root=plan.new_root,
        dry_run=dry_run,
    )


def _candidate_state(plan: ProjectMovePlan, original: bytes) -> bytes:
    try:
        state = json.loads(original.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FailSafeError("Codex state is not valid JSON") from exc
    if not isinstance(state, dict) or detect_state_schema(state) != plan.schema_id:
        raise FailSafeError("Codex state schema is not the one the plan was built against")
    projects = state["local-projects"]
    entry = projects.get(plan.project_id)
    if not isinstance(entry, dict):
        raise FailSafeError("The project the plan moves is not in the Codex state")
    try:
        # The project keeps its id, name and timestamps; only the root value moves.
        projects[plan.project_id] = replace_project_root(
            plan.schema_id, entry, old_root=plan.old_root, new_root=plan.new_root
        )
    except ValueError as exc:
        raise FailSafeError(f"Project root cannot be remapped safely: {exc}") from exc
    assignments = state.setdefault("thread-project-assignments", {})
    if not isinstance(assignments, dict):
        raise FailSafeError("Codex state thread assignments are not in a supported shape")
    for session_id in plan.bindings:
        assignments[session_id] = build_binding_value(plan.schema_id, plan.project_id)
    candidate = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report = validate_global_state_references(candidate)
    if report.status not in {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING}:
        raise FailSafeError("The moved project's state failed Guardian validation")
    return candidate


def _copy_into_place(plan: ProjectMovePlan, require_stopped: Callable[[], None]) -> None:
    """Copy into an owned staging directory, verify, then rename it into place.

    Every failure before the rename removes the staging directory — it was
    created by this very call, so removing it destroys nothing but a partial
    copy — and a failure never reaches the source tree, which is only read.
    """
    staging = _staging_path(plan.new_root, plan.plan_id)
    norm_staging = _norm(staging)
    norm_old = _norm(plan.old_root)
    if _contains(norm_old, norm_staging) or _contains(norm_staging, norm_old):
        raise FailSafeError("The staging directory would overlap the project folder")
    _clear_own_staging(staging, plan.plan_id)

    LOG.info(
        "project move: copying %d file(s) (%d bytes) from %s into staging %s",
        plan.file_count, plan.total_bytes, plan.old_root, staging,
    )
    os.mkdir(staging)
    try:
        marker_path = os.path.join(staging, STAGING_MARKER_NAME)
        with open(marker_path, "x", encoding="utf-8", newline="\n") as handle:
            json.dump({"format": _MARKER_FORMAT, "plan_id": plan.plan_id}, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

        source_mtimes: dict[str, int] = {}
        for entry in plan.inventory:
            parts = entry.relative_path.split("/")
            destination = os.path.join(staging, *parts)
            if entry.size == DIRECTORY_SIZE:
                os.makedirs(destination, exist_ok=True)
                continue
            source = os.path.join(plan.old_root, *parts)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            try:
                source_mtimes[entry.relative_path] = os.stat(source).st_mtime_ns
                shutil.copy2(source, destination)
                digest, size = _sha256_file(destination)
            except OSError as exc:
                raise FailSafeError(
                    f"Copying {entry.relative_path} failed; nothing was moved: {exc}"
                ) from exc
            if digest != entry.sha256 or size != entry.size:
                raise FailSafeError(
                    f"{entry.relative_path} changed while it was being copied; nothing was moved. "
                    "Close whatever is editing the project and preview again"
                )
        _require_source_unchanged(plan, source_mtimes)

        os.remove(marker_path)
        require_stopped()
        if os.path.lexists(plan.new_root):
            # Never merge into, or replace, a folder that appeared meanwhile.
            raise FailSafeError(f"{plan.new_root} appeared during the copy; nothing was moved")
        os.replace(staging, plan.new_root)
    except BaseException:
        _remove_staging_created_here(staging)
        raise
    LOG.warning("project move: verified copy renamed into place at %s", plan.new_root)


def _require_source_unchanged(plan: ProjectMovePlan, source_mtimes: dict[str, int]) -> None:
    """Refuse a copy the source moved away from while it was being made.

    Each copy was hashed against the preview, which catches an edit made before
    a file was read. This catches the rest cheaply: a file added or removed
    anywhere, or one rewritten after its copy was taken.
    """
    walk = _walk(plan.old_root, hash_files=False)
    if walk.codes:
        raise FailSafeError("The project folder can no longer be read in full; nothing was moved")
    expected = {(item.relative_path, item.size) for item in plan.inventory}
    if {(item.relative_path, item.size) for item in walk.entries} != expected:
        raise FailSafeError("Files were added to or removed from the project during the copy; nothing was moved")
    for relative_path, mtime_ns in source_mtimes.items():
        try:
            current = os.stat(os.path.join(plan.old_root, *relative_path.split("/"))).st_mtime_ns
        except OSError as exc:
            raise FailSafeError(f"{relative_path} disappeared during the copy; nothing was moved") from exc
        if current != mtime_ns:
            raise FailSafeError(f"{relative_path} was modified during the copy; nothing was moved")


def _clear_own_staging(staging: str, plan_id: str) -> None:
    """Remove a staging directory left by an interrupted run of this same plan.

    Anything else at that path — a file, a link, a directory with no marker or
    with another plan's marker — is not provably ours and is refused.
    """
    if not os.path.lexists(staging):
        return
    occupied = FailSafeError(
        f"{STAGING_OCCUPIED}: {staging} exists and is not a staging directory of this plan; "
        "inspect it and remove it yourself if it is not needed"
    )
    if not _is_plain_directory(staging):
        raise occupied
    try:
        with open(os.path.join(staging, STAGING_MARKER_NAME), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        raise occupied from None
    if not isinstance(marker, dict) or marker.get("format") != _MARKER_FORMAT or marker.get("plan_id") != plan_id:
        raise occupied
    LOG.warning("project move: removing the interrupted staging directory %s of this plan", staging)
    shutil.rmtree(staging)


def _remove_staging_created_here(staging: str) -> None:
    if not os.path.lexists(staging):
        return
    try:
        shutil.rmtree(staging)
        LOG.warning("project move: removed the partial staging directory %s", staging)
    except OSError as exc:
        LOG.error("project move: could not remove the staging directory %s: %s", staging, exc)


def _write_resume_marker(plan: ProjectMovePlan) -> None:
    marker = _resume_marker_path(plan.new_root)
    temp = marker + ".tmp"
    payload = {
        "format": _MARKER_FORMAT,
        "project_id": plan.project_id,
        "old_root": plan.old_root,
        "new_root": plan.new_root,
        "inventory_sha256": plan.inventory_sha256,
        "state": "COPIED",
    }
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    except OSError as exc:
        raise FailSafeError(
            f"The verified copy is at {plan.new_root} but its resume marker could not be written, "
            f"so the Codex state was not changed: {exc}"
        ) from exc


def _remove_resume_marker(plan: ProjectMovePlan) -> None:
    marker = _resume_marker_path(plan.new_root)
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # The move itself is committed; a leftover marker only names a done move.
        LOG.warning("project move: could not remove the resume marker %s: %s", marker, exc)


def _read_resume_marker(new_root: str) -> dict[str, Any] | None:
    try:
        with open(_resume_marker_path(new_root), encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("format") != _MARKER_FORMAT:
        return None
    return marker


def _resume_marker_path(new_root: str) -> str:
    name_hash = hashlib.sha256(_norm(new_root).encode("utf-8")).hexdigest()[:16]
    return os.path.join(os.path.dirname(new_root), f".codexsync-move-{name_hash}.json")


def _staging_path(new_root: str, plan_id: str) -> str:
    return os.path.join(os.path.dirname(new_root), f".codexsync-move-{plan_id[:16]}.partial")


# ------------------------------------------------------------------ verification


def _plan_material(plan: ProjectMovePlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "schema_id": plan.schema_id,
        "project_id": plan.project_id,
        "old_root": plan.old_root,
        "new_root": plan.new_root,
        "global_state_sha256": plan.global_state_sha256,
        "inventory_sha256": plan.inventory_sha256,
        "bindings": list(plan.bindings),
        "copy_complete": plan.copy_complete,
        # Covered so a saved plan cannot be unblocked by deleting its codes.
        "codes": list(plan.codes),
    }


def _compute_plan_id(plan: ProjectMovePlan) -> str:
    canonical = json.dumps(_plan_material(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _with_plan_id(plan: ProjectMovePlan) -> ProjectMovePlan:
    return replace(plan, plan_id=_compute_plan_id(plan))


def _integrity_problem(plan: ProjectMovePlan) -> str | None:
    if plan.version != PROJECT_MOVE_PLAN_VERSION:
        return "unsupported plan version"
    if _compute_plan_id(plan) != plan.plan_id:
        return "plan id does not match its content"
    if list(plan.bindings) != sorted(set(plan.bindings)):
        return "bindings are not a sorted set"
    files = [item for item in plan.inventory if item.size != DIRECTORY_SIZE]
    if plan.file_count != len(files) or plan.total_bytes != sum(item.size for item in files):
        return "file counts do not match the inventory"
    if plan.codes:
        return None
    if not os.path.isabs(plan.new_root) or os.path.abspath(plan.new_root) != plan.new_root:
        return "target is not an absolute path"
    if not plan.inventory_sha256 or inventory_digest(plan.inventory) != plan.inventory_sha256:
        return "inventory does not match its digest"
    for item in plan.inventory:
        parts = item.relative_path.split("/")
        if not item.relative_path or any(part in {"", ".", ".."} for part in parts):
            return "inventory names a path outside the project"
    return None


# ------------------------------------------------------------------------- walk


@dataclass(frozen=True, slots=True)
class _Walk:
    entries: tuple[MoveInventoryEntry, ...]
    codes: tuple[str, ...]
    #: ``(code, relative path)`` per offending entry, in discovery order.
    problems: tuple[tuple[str, str], ...]


def _walk(root: str, *, hash_files: bool) -> _Walk:
    """Every regular file and empty directory under ``root``, with refusal codes.

    A link — symlink, junction, any reparse point that names another location —
    cannot be copied faithfully: following it copies someone else's tree into
    the project, and recreating it points the copy back at the old location.
    """
    entries: list[MoveInventoryEntry] = []
    problems: list[tuple[str, str]] = []
    try:
        root_stat = os.lstat(root)
    except OSError:
        return _Walk((), (SOURCE_UNREADABLE,), ((SOURCE_UNREADABLE, "."),))
    if _is_link(root, root_stat):
        return _Walk((), (SOURCE_HAS_LINKS,), ((SOURCE_HAS_LINKS, "."),))
    pending: list[tuple[str, ...]] = [()]
    while pending:
        parts = pending.pop()
        directory = os.path.join(root, *parts)
        try:
            with os.scandir(directory) as iterator:
                children = list(iterator)
        except OSError:
            problems.append((SOURCE_UNREADABLE, "/".join(parts) or "."))
            continue
        if parts and not children:
            entries.append(MoveInventoryEntry("/".join(parts), DIRECTORY_SIZE, ""))
        for child in children:
            child_parts = parts + (child.name,)
            relative = "/".join(child_parts)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError:
                problems.append((SOURCE_UNREADABLE, relative))
                continue
            if _is_link(child.path, child_stat):
                problems.append((SOURCE_HAS_LINKS, relative))
            elif stat.S_ISDIR(child_stat.st_mode):
                pending.append(child_parts)
            elif stat.S_ISREG(child_stat.st_mode):
                if hash_files:
                    try:
                        digest, size = _sha256_file(child.path)
                    except OSError:
                        problems.append((SOURCE_UNREADABLE, relative))
                        continue
                else:
                    digest, size = "", child_stat.st_size
                entries.append(MoveInventoryEntry(relative, size, digest))
            else:
                problems.append((SOURCE_HAS_SPECIAL_FILES, relative))
    entries.sort(key=lambda item: item.relative_path)
    codes = tuple(dict.fromkeys(code for code, _ in problems))
    return _Walk(tuple(entries), codes, tuple(problems))


def _sha256_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_link(path: str, info: os.stat_result) -> bool:
    """Whether an entry points somewhere else instead of holding its own data.

    Not every reparse point is a link. A cloud client's placeholder (Yandex.Disk
    and OneDrive both use ``IO_REPARSE_TAG_CLOUD_*``, ``0x9000xxxx``) is a real
    file or folder whose content reads like any other: refusing those made 13
    of 18 projects on a real machine unmovable. What separates a link is the
    name-surrogate bit Windows sets on every tag that names another location.
    Only when the tag cannot be read at all is a reparse point assumed to be a
    link, because then nothing proves it is not.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    if not getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(info, "st_reparse_tag", None)
    if not isinstance(tag, int) or tag == 0:
        return True
    return bool(tag & _REPARSE_TAG_NAME_SURROGATE)


def _is_plain_directory(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not _is_link(path, info)


# ------------------------------------------------------------------------ paths


def _norm(path: str) -> str:
    """Absolute and, on Windows, case- and separator-folded, for comparison only."""
    try:
        return os.path.normcase(os.path.abspath(path))
    except (TypeError, ValueError):
        return ""


def _contains(parent: str, child: str) -> bool:
    """Whether normalised ``child`` is ``parent`` or lies inside it."""
    if not parent or not child:
        return False
    if child == parent:
        return True
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def _cwd_under(cwd: str, old_root: str, norm_old: str) -> bool:
    """``repair_plan``'s rule, widened by host normalisation.

    A chat left unbound here stops appearing under the project after the move,
    so the two readings are united rather than one picked: ``_under`` alone
    misses ``C:/proj`` against ``C:\\proj\\sub``.
    """
    return _under(cwd, old_root) or (os.path.isabs(cwd) and _contains(norm_old, _norm(cwd)))


def _parse_state(payload: bytes) -> dict[str, Any] | None:
    try:
        state = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value
