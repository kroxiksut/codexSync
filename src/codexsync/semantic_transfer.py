"""Frozen, read-only plan for moving session branches between two machines.

The plan is the whole decision: which sessions may fast-forward, which are in
conflict, and which are blocked and why. Building it writes nothing. Applying it
is a cold mutation that must quote the exact plan id, exactly like
``repair-projects apply``.

Two gates keep this honest:

* ``PROVEN_LAYOUTS`` is empty, so nothing may be written into a directory a
  Codex runtime reads. Where a transferred branch has to land inside
  ``sessions/`` is a property of that runtime, not of the source filename, and
  the source layout is metadata rather than an instruction.

  The gate covers exactly that destination and no more. The cloud mirror is
  codexSync's own copy — no Codex reads it — so its layout is not a guess about
  a runtime, and a branch written towards the mirror keeps the relative path it
  has in the state directory it came from (``MIRROR_LAYOUT_ID``). Gating the
  mirror too would leave the mirror with no way to be rebuilt at all, because
  sessions are excluded from generic mtime copying as semantic-owned.
* A write into the Codex state directory is refused unless the runtime's own
  thread catalogue already places that branch at exactly that path. On an
  observed machine every session on disk has a catalogue row naming its rollout
  file, so the file system is not the whole truth about where a session lives:
  a branch put somewhere the catalogue does not name is invisible, with no
  error. Making the runtime see a *new* session would mean writing that
  database, which codexSync only ever reads — so it is
  ``UNSUPPORTED_STATE_BACKEND``, not a half-transfer. Overwriting a branch the
  catalogue already points at is exactly the case that stays allowed.

Conflicts are never resolved here. A divergence produces a conflict id and the
plan blocks until a versioned resolution is recorded, so the branch that loses
is still on disk, byte for byte.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Iterable

from .exceptions import FailSafeError
from .jsonl_codec import JsonlCodec, codec_of, logical_name, with_codec
from .semantic_merge import (
    CANONICAL_DIGEST_VERSION,
    BranchComparison,
    BranchRelation,
    BranchState,
    compare_session_branches,
)
from .session_catalog import SessionCatalog, SessionDescriptor, SessionState
from .sqlite_audit import PlacementStatus, ThreadPlacements


TRANSFER_PLAN_VERSION = 1
TRANSFER_PLAN_FORMAT = "codexsync-session-transfer-v1"


class TransferAction(str, Enum):
    #: Both sides already agree; nothing to do.
    NOOP = "NOOP"
    #: Local adopts the remote branch wholesale.
    FAST_FORWARD_LOCAL = "FAST_FORWARD_LOCAL"
    #: Remote adopts the local branch wholesale.
    FAST_FORWARD_REMOTE = "FAST_FORWARD_REMOTE"
    #: Ancestry proven; only the active/archived placement changes.
    ARCHIVE_TRANSITION = "ARCHIVE_TRANSITION"
    #: Divergence: needs a recorded resolution before anything may move.
    BLOCKED_CONFLICT = "BLOCKED_CONFLICT"
    #: The target layout has not been proven by a controlled run.
    BLOCKED_UNPROVEN_LAYOUT = "BLOCKED_UNPROVEN_LAYOUT"
    #: Two different session ids want the same destination path.
    BLOCKED_TARGET_COLLISION = "BLOCKED_TARGET_COLLISION"
    #: The runtime binding lives in a store codexSync will not write.
    BLOCKED_UNSUPPORTED_BACKEND = "BLOCKED_UNSUPPORTED_BACKEND"
    #: Outside the working set: nothing is written into `.codex` for this
    #: session, and it blocks nothing. The cloud mirror is written regardless,
    #: so the backup stays complete whatever the working set says.
    OUT_OF_SCOPE = "OUT_OF_SCOPE"

    @property
    def is_blocked(self) -> bool:
        return self.name.startswith("BLOCKED_")

    @property
    def writes(self) -> bool:
        return self in {
            TransferAction.FAST_FORWARD_LOCAL,
            TransferAction.FAST_FORWARD_REMOTE,
            TransferAction.ARCHIVE_TRANSITION,
        }


class ResolutionChoice(str, Enum):
    KEEP_LOCAL = "KEEP_LOCAL"
    KEEP_REMOTE = "KEEP_REMOTE"
    #: Decide later. The conflict bundle keeps both branches meanwhile.
    DEFER = "DEFER"


#: Target layouts proven by a controlled run on disposable state, keyed by an
#: id recorded together with the Codex version it was observed on.
#:
#: Deliberately empty: see docs/dev/experiments/session-layout-adapter.md. While it
#: is empty a plan can be built and read, and nothing can be written into a
#: directory the Codex runtime reads. Writes towards the cloud mirror are a
#: separate destination and are not gated on this dict.
PROVEN_LAYOUTS: dict[str, str] = {}

#: State directories a branch can live under, and therefore the first segment
#: of every source relative path the catalogue produces.
_STATE_DIRS = ("sessions", "archived_sessions")

#: Placeholders a layout template may use.
#:
#: ``source_dir`` is the directory a branch sat in *below* its state folder on
#: the machine it came from, and it exists because a real machine does not have
#: one layout. On the state this was measured against, 228 active branches live
#: in ``sessions/<year>/<month>/<day>/`` while 23 archived ones lie flat in
#: ``archived_sessions/``. A template that could only name the state folder and
#: the file describes the second and puts every branch of the first in the
#: wrong directory -- where the thread catalogue does not name it, which is a
#: session the runtime never shows and no error anywhere.
#:
#: An empty segment disappears from the rendered path, so the single template
#: ``{state}/{source_dir}/{file_name}`` describes both shapes at once.
_LAYOUT_FIELDS = ("state", "source_dir", "file_name", "session_id")

#: Layout of the codexSync cloud mirror. Recorded as an id so a plan still
#: names the layout every destination was chosen under, but it is not a
#: PROVEN_LAYOUTS entry: it describes codexSync's own directory rather than a
#: runtime whose behaviour would have to be observed.
MIRROR_LAYOUT_ID = "codexsync-mirror-v1"

#: Mirror layout per container. The codec changes the file name of every
#: destination, so it is part of the layout rather than a detail beneath it,
#: and since the id is hashed into the plan a plan frozen for one container can
#: never be applied under another.
#:
#: It names the container a branch the mirror does not yet hold is written in.
#: A branch already there keeps the container it is already stored in, because
#: changing it would leave the old file behind under its old name; each item's
#: `target_relative_path` is what actually decides, and the plan id covers all
#: of them.
_MIRROR_LAYOUT_IDS: dict[JsonlCodec, str] = {
    JsonlCodec.NONE: MIRROR_LAYOUT_ID,
    JsonlCodec.GZIP: "codexsync-mirror-gzip-v1",
    JsonlCodec.XZ: "codexsync-mirror-xz-v1",
}


def mirror_layout_id(codec: JsonlCodec) -> str:
    return _MIRROR_LAYOUT_IDS[codec]


def mirror_codec_for(layout_id: str) -> JsonlCodec:
    """The container a plan's mirror destinations were named under.

    A plan carries its layout, not the config, so an apply writes what the id
    the user confirmed describes. An unknown id is refused rather than guessed:
    a wrong container here would write a compressed body under a plain name.
    """
    for codec, known in _MIRROR_LAYOUT_IDS.items():
        if known == layout_id:
            return codec
    raise FailSafeError(
        f"Transfer plan names an unknown mirror layout {layout_id!r}; rebuild the plan"
    )


@dataclass(frozen=True, slots=True)
class BranchResolution:
    """A user's versioned choice between two divergent branches.

    The choice is pinned to the exact bytes it was made about. If either branch
    changes afterwards the confirmation no longer matches and the resolution is
    refused as stale, rather than being applied to a history the user never saw.
    """
    conflict_id: str
    session_hash: str
    local_sha256: str
    remote_sha256: str
    choice: ResolutionChoice

    @property
    def confirmation(self) -> str:
        material = "\0".join(
            (self.conflict_id, self.session_hash, self.local_sha256, self.remote_sha256, self.choice.value)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def matches(self, comparison: BranchComparison) -> bool:
        return (
            self.local_sha256 == comparison.local_sha256
            and self.remote_sha256 == comparison.remote_sha256
        )


@dataclass(frozen=True, slots=True)
class TransferItem:
    session_hash: str
    relation: BranchRelation
    action: TransferAction
    local_sha256: str
    remote_sha256: str
    local_records: int
    remote_records: int
    target_relative_path: str | None = None
    conflict_id: str | None = None
    codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransferPlan:
    version: int
    plan_id: str
    created_at_utc: str
    source_machine: str
    target_machine: str
    layout_id: str
    canonical_version: str
    volatile: bool
    items: tuple[TransferItem, ...] = ()
    codes: tuple[str, ...] = ()
    #: Layout used for every destination in the cloud mirror. Recorded next to
    #: ``layout_id`` because a plan writes towards two different destinations
    #: and each has its own layout; a later mirror layout is a new id here.
    mirror_layout_id: str = MIRROR_LAYOUT_ID
    #: The working set: session hashes this plan may write into `.codex`,
    #: sorted. Empty means "everything", which is what every plan built before
    #: working sets existed meant -- and why an empty scope is left out of the
    #: id material entirely, so those plans keep the id they had.
    scope: tuple[str, ...] = ()

    @property
    def out_of_scope_items(self) -> tuple["TransferItem", ...]:
        return tuple(item for item in self.items if item.action is TransferAction.OUT_OF_SCOPE)

    @property
    def writable_items(self) -> tuple[TransferItem, ...]:
        return tuple(item for item in self.items if item.action.writes)

    @property
    def blocked_items(self) -> tuple[TransferItem, ...]:
        return tuple(item for item in self.items if item.action.is_blocked)


def conflict_id_for(session_hash: str, local_sha256: str, remote_sha256: str) -> str:
    """Stable id for one exact pair of divergent branches."""
    material = "\0".join(sorted((local_sha256, remote_sha256)))
    return hashlib.sha256(f"{session_hash}\0{material}".encode("utf-8")).hexdigest()


def build_transfer_plan(
    local_catalog: SessionCatalog,
    remote_catalog: SessionCatalog,
    *,
    local_root: Path,
    remote_root: Path,
    source_machine: str,
    target_machine: str,
    resolutions: dict[str, BranchResolution] | None = None,
    confirmed_bases: set[str] | None = None,
    placements: ThreadPlacements | None = None,
    layout_id: str = "unproven",
    mirror_codec: JsonlCodec = JsonlCodec.NONE,
    max_line_bytes: int = 64 * 1024 * 1024,
    volatile: bool = False,
    scope: Iterable[str] | None = None,
) -> TransferPlan:
    """Classify every session present on either side and freeze the decisions.

    ``resolutions`` are keyed by conflict id. ``confirmed_bases`` holds the
    session hashes for which the semantic store has a recorded common ancestor.
    ``placements`` is the runtime's own thread catalogue; without it a write
    into the Codex state directory is unconstrained, which is only correct when
    no such catalogue exists.

    ``scope`` is the working set: session hashes that may be written into
    `.codex`. ``None`` means everything, which is what every plan meant before
    working sets existed. The mirror is written in full either way.
    """
    resolutions = resolutions or {}
    # A changed branch changes the conflict id, so an earlier choice would
    # simply not be found. Index by session too, to say "your decision is
    # stale" instead of silently presenting a brand new conflict.
    by_session: dict[str, BranchResolution] = {
        resolution.session_hash: resolution for resolution in resolutions.values()
    }
    confirmed_bases = confirmed_bases or set()
    codes: list[str] = []

    local_by_id = _by_session_id(local_catalog)
    remote_by_id = _by_session_id(remote_catalog)
    if local_catalog.volatile or remote_catalog.volatile:
        volatile = True

    items: list[TransferItem] = []
    claimed_targets: dict[str, str] = {}

    for session_id in sorted(set(local_by_id) | set(remote_by_id)):
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        local = local_by_id.get(session_id)
        remote = remote_by_id.get(session_id)

        if local is None or remote is None:
            # One side simply does not have this session yet. That is a plain
            # copy, but where it lands is still a placement decision, so it goes
            # through the same gate a fast-forward does.
            items.append(
                _one_sided_item(
                    session_hash, session_id, local, remote,
                    placements=placements, layout_id=layout_id,
                    mirror_codec=mirror_codec,
                    claimed_targets=claimed_targets,
                )
            )
            continue

        comparison = compare_session_branches(
            local_root / _os_path(local.relative_path),
            remote_root / _os_path(remote.relative_path),
            local_state=_branch_state(local.state),
            remote_state=_branch_state(remote.state),
            has_confirmed_base=session_hash in confirmed_bases,
            max_line_bytes=max_line_bytes,
        )
        item = _decide(
            session_hash, session_id, comparison, local, remote,
            resolutions=resolutions,
            resolutions_by_session=by_session,
            placements=placements,
            layout_id=layout_id,
            mirror_codec=mirror_codec,
            claimed_targets=claimed_targets,
        )
        items.append(item)
        codes.extend(item.codes)

    if scope is not None:
        items = [_apply_scope(item, set(scope)) for item in items]
        codes.extend(
            code for item in items if item.action is TransferAction.OUT_OF_SCOPE
            for code in item.codes
        )
    if any(item.action.is_blocked for item in items):
        codes.append("PLAN_HAS_BLOCKED_ITEMS")
    plan = TransferPlan(
        TRANSFER_PLAN_VERSION, "", _now(), source_machine, target_machine,
        layout_id, CANONICAL_DIGEST_VERSION, volatile,
        tuple(items), tuple(dict.fromkeys(codes)), mirror_layout_id(mirror_codec),
        tuple(sorted(scope)) if scope else (),
    )
    return _with_plan_id(plan)


def _apply_scope(item: TransferItem, scope: set[str]) -> TransferItem:
    """Narrow one item to the working set.

    Only writes *into `.codex`* are narrowed. A branch the mirror is missing is
    still copied there, because the cloud copy is the backup and a partial
    backup is the one thing a working set must not produce. A conflict outside
    the set stops being a question anyone has to answer now, so it stops
    blocking the apply -- the branch is still kept in full in the mirror.
    """
    if item.session_hash in scope:
        return item
    if item.action in {TransferAction.NOOP, TransferAction.FAST_FORWARD_REMOTE}:
        return item
    return TransferItem(
        item.session_hash, item.relation, TransferAction.OUT_OF_SCOPE,
        item.local_sha256, item.remote_sha256, item.local_records, item.remote_records,
        item.target_relative_path, item.conflict_id,
        # What it would have been is kept, so a summary can say what the
        # working set is holding back rather than simply omitting it.
        tuple(dict.fromkeys((*item.codes, f"WOULD_BE_{item.action.value}"))),
    )


def _decide(
    session_hash: str,
    session_id: str,
    comparison: BranchComparison,
    local: SessionDescriptor,
    remote: SessionDescriptor,
    *,
    resolutions: dict[str, BranchResolution],
    resolutions_by_session: dict[str, BranchResolution],
    placements: ThreadPlacements | None,
    layout_id: str,
    mirror_codec: JsonlCodec,
    claimed_targets: dict[str, str],
) -> TransferItem:
    def make(action: TransferAction, *, target: str | None = None, conflict: str | None = None,
             extra: tuple[str, ...] = ()) -> TransferItem:
        return TransferItem(
            session_hash, comparison.relation, action,
            comparison.local_sha256, comparison.remote_sha256,
            comparison.local_records, comparison.remote_records,
            target, conflict, extra,
        )

    if comparison.relation is BranchRelation.IDENTICAL:
        return make(TransferAction.NOOP)

    if comparison.is_conflict:
        conflict = conflict_id_for(session_hash, comparison.local_sha256, comparison.remote_sha256)
        resolution = resolutions.get(conflict)
        if resolution is None:
            previous = resolutions_by_session.get(session_hash)
            if previous is not None and not previous.matches(comparison):
                # A decision exists for this session but was made about other
                # bytes: report it as stale rather than as an unseen conflict.
                return make(
                    TransferAction.BLOCKED_CONFLICT, conflict=conflict, extra=("STALE_RESOLUTION",)
                )
            return make(TransferAction.BLOCKED_CONFLICT, conflict=conflict)
        if not resolution.matches(comparison):
            return make(TransferAction.BLOCKED_CONFLICT, conflict=conflict, extra=("STALE_RESOLUTION",))
        if resolution.choice is ResolutionChoice.DEFER:
            return make(TransferAction.BLOCKED_CONFLICT, conflict=conflict, extra=("DEFERRED",))
        resolved = (
            TransferAction.FAST_FORWARD_REMOTE if resolution.choice is ResolutionChoice.KEEP_LOCAL
            else TransferAction.FAST_FORWARD_LOCAL
        )
        return _gate_write(
            make, resolved, session_id, local, remote,
            placements=placements, layout_id=layout_id, mirror_codec=mirror_codec,
            claimed_targets=claimed_targets,
            conflict=conflict, extra=("RESOLVED_BY_USER",),
        )

    action = {
        BranchRelation.FAST_FORWARD_LOCAL: TransferAction.FAST_FORWARD_LOCAL,
        BranchRelation.FAST_FORWARD_REMOTE: TransferAction.FAST_FORWARD_REMOTE,
        BranchRelation.ARCHIVE_TRANSITION: TransferAction.ARCHIVE_TRANSITION,
    }[comparison.relation]
    return _gate_write(
        make, action, session_id, local, remote,
        placements=placements, layout_id=layout_id, mirror_codec=mirror_codec,
        claimed_targets=claimed_targets,
    )


def _one_sided_item(
    session_hash: str,
    session_id: str,
    local: SessionDescriptor | None,
    remote: SessionDescriptor | None,
    *,
    placements: ThreadPlacements | None,
    layout_id: str,
    mirror_codec: JsonlCodec,
    claimed_targets: dict[str, str],
) -> TransferItem:
    """Decide a session that exists on one side only.

    There is nothing to compare, so there is no divergence to fear; the only
    open question is where the copy may land, which is what the gate answers.
    """
    action = (
        TransferAction.FAST_FORWARD_REMOTE if remote is None else TransferAction.FAST_FORWARD_LOCAL
    )
    relation = (
        BranchRelation.FAST_FORWARD_REMOTE if remote is None else BranchRelation.FAST_FORWARD_LOCAL
    )

    def make(chosen: TransferAction, *, target: str | None = None, conflict: str | None = None,
             extra: tuple[str, ...] = ()) -> TransferItem:
        return TransferItem(
            session_hash, relation, chosen,
            local.sha256 if local else "",
            remote.sha256 if remote else "",
            local.line_count if local else 0,
            remote.line_count if remote else 0,
            target, conflict, extra,
        )

    return _gate_write(
        make, action, session_id, local, remote,
        placements=placements, layout_id=layout_id, mirror_codec=mirror_codec,
        claimed_targets=claimed_targets,
        extra=("SESSION_ON_ONE_SIDE_ONLY",),
    )


def _gate_write(
    make,
    action: TransferAction,
    session_id: str,
    local: SessionDescriptor | None,
    remote: SessionDescriptor | None,
    *,
    placements: ThreadPlacements | None,
    layout_id: str,
    mirror_codec: JsonlCodec,
    claimed_targets: dict[str, str],
    conflict: str | None = None,
    extra: tuple[str, ...] = (),
) -> TransferItem:
    source = remote if action is TransferAction.FAST_FORWARD_LOCAL else local
    if source is None:
        raise FailSafeError("A transfer decision has no source branch to copy")
    if action is TransferAction.FAST_FORWARD_REMOTE:
        # Destination is codexSync's own mirror, which no Codex reads, so the
        # layout is ours and the source path is the answer rather than a guess.
        # Nothing has to find this file afterwards, so the catalogue is not
        # consulted either.
        #
        # The container is the one the mirror already holds this branch in, and
        # only a branch the mirror does not have yet gets the configured one.
        # Changing the container renames the destination, and nothing deletes
        # the old name because `delete_policy` is never -- so writing the
        # configured container over a mirror that stores the branch plainly
        # leaves two files for one session id, which the catalogue reads as
        # `DUPLICATE_SESSION_ID`. Both copies then drop out of `valid` and the
        # session is never compared, mirrored or fast-forwarded again, with no
        # error at all. Converting an existing mirror needs a delete, so it is
        # refused here in the same way an archive transition is.
        stored = codec_of(remote.relative_path) if remote is not None else None
        codec = mirror_codec if stored is None else stored
        side, target = "mirror", mirror_relative_path(source, codec)
        extra = extra + ("MIRROR_DESTINATION",)
        if stored is not None and stored is not mirror_codec:
            extra = extra + ("MIRROR_CONTAINER_KEPT",)
    elif layout_id not in PROVEN_LAYOUTS:
        return make(TransferAction.BLOCKED_UNPROVEN_LAYOUT, conflict=conflict, extra=extra)
    else:
        side, target = "local", target_relative_path(layout_id, source)
        objection = _catalogue_objection(placements, session_id, target)
        if objection is not None:
            return make(
                TransferAction.BLOCKED_UNSUPPORTED_BACKEND,
                target=target, conflict=conflict, extra=extra + objection,
            )

    # Two destinations only collide when they are the same file, and the two
    # sides are different roots, so the side is part of the claim.
    claim = f"{side}:{target}"
    owner = claimed_targets.get(claim)
    if owner is not None and owner != session_id:
        return make(TransferAction.BLOCKED_TARGET_COLLISION, target=target, conflict=conflict, extra=extra)
    claimed_targets[claim] = session_id
    return make(action, target=target, conflict=conflict, extra=extra)


def _catalogue_objection(
    placements: ThreadPlacements | None, session_id: str, target: str
) -> tuple[str, ...] | None:
    """Why the runtime would not see a branch written at ``target``, if it would not.

    The catalogue is treated as advisory about *other* branches — a second file
    it does not mention is never an orphan to delete — but as authoritative
    about whether our own write will be found. A session it does not list at
    all would need a new row, and codexSync does not write these databases.
    """
    if placements is None or placements.status is PlacementStatus.ABSENT:
        return None
    if placements.status is not PlacementStatus.AVAILABLE:
        return ("CATALOG_UNREADABLE",)
    if not placements.knows(session_id):
        return ("SESSION_NOT_IN_CATALOG",)
    recorded = placements.placement_of(session_id)
    if recorded is None:
        return ("CATALOG_PLACEMENT_UNKNOWN",)
    if recorded != target:
        return ("CATALOG_PLACES_ELSEWHERE",)
    return None


def mirror_relative_path(
    source: SessionDescriptor, codec: JsonlCodec = JsonlCodec.NONE
) -> str:
    """Destination path for a branch written into the codexSync cloud mirror.

    The mirror is not a Codex state directory: nothing but codexSync reads it,
    so a branch keeps the relative path it has where it came from, and may keep
    it in a compressed container. That makes the mirror a faithful copy and
    keeps the way back an ordinary transfer, which is still gated on a proven
    layout because the way back lands in a directory the runtime does read.

    The logical path is taken first, so a branch already read out of the mirror
    is restored to the same name rather than gaining a second suffix.
    """
    return with_codec(source.relative_path, codec)


def target_relative_path(layout_id: str, source: SessionDescriptor) -> str:
    """Destination path for a transferred branch under a proven layout.

    Refuses on an unproven layout: the source filename records where the branch
    used to live on another machine, which is evidence about that machine and
    not an instruction for this one.

    The logical name is used, never the stored one. A branch coming back out of
    the cloud mirror is stored in a container there, and the Codex runtime
    reads plain JSONL: a destination still carrying `.xz` would be a session
    the runtime never sees, with no error anywhere.

    The template is rendered with ``state``, ``source_dir``, ``file_name`` and
    ``session_id``; empty segments collapse, so one template can describe a
    date-partitioned ``sessions/`` tree and a flat ``archived_sessions/`` one.
    """
    if layout_id not in PROVEN_LAYOUTS:
        raise FailSafeError(
            f"Session layout {layout_id!r} is not proven, so a destination path cannot be chosen. "
            "Run the controlled experiment in docs/dev/experiments/session-layout-adapter.md."
        )
    template = PROVEN_LAYOUTS[layout_id]
    if "{session_id}" in template and not source.session_id:
        raise FailSafeError(
            f"Session layout {layout_id!r} names the session id, which this branch does not carry"
        )
    try:
        rendered = template.format(
            state="archived_sessions" if source.state is SessionState.ARCHIVED else "sessions",
            source_dir=_source_dir(source.relative_path),
            file_name=logical_name(source.relative_path.rsplit("/", 1)[-1]),
            session_id=source.session_id or "",
        )
    except (KeyError, IndexError, ValueError) as exc:
        # ValueError is an unbalanced brace in the template: malformed rather
        # than unknown, but the same answer -- the layout cannot be rendered.
        raise FailSafeError(
            f"Session layout {layout_id!r} cannot be rendered ({exc}); "
            f"known placeholders are {', '.join(_LAYOUT_FIELDS)}"
        ) from exc
    # An empty `source_dir` collapses here rather than leaving `sessions//file`,
    # which is what lets one template cover a partitioned and a flat state
    # directory at the same time.
    segments = [segment for segment in rendered.split("/") if segment]
    if not segments or any(segment in {".", ".."} for segment in segments):
        raise FailSafeError(
            f"Session layout {layout_id!r} rendered {rendered!r}, which is not a usable path"
        )
    return "/".join(segments)


def _source_dir(relative_path: str) -> str:
    """The directory a branch sat in below its state folder, on its own machine.

    The state folder itself is dropped because ``{state}`` names it: the source
    may have held the branch as active while it is archived here, and the
    template decides which folder it lands in.
    """
    directory, _, _ = relative_path.rpartition("/")
    head, _, tail = directory.partition("/")
    return tail if head in _STATE_DIRS else directory


class _PlanRejected(ValueError):
    """A plan refused for a reason worth telling the user."""


def save_transfer_plan(plan: TransferPlan, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_serialise(plan), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return path


def load_transfer_plan(path: Path) -> TransferPlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("format") != TRANSFER_PLAN_FORMAT:
            raise _PlanRejected("unsupported transfer plan format")
        if "mirror_layout_id" not in raw:
            # The field joined the hashed plan material, so such a plan could
            # not match its own id even if the value were guessed. Say which
            # plan it is rather than letting it read as a corrupt one.
            raise _PlanRejected(
                "transfer plan predates the mirror layout id; rescan to build a current plan"
            )
        items = tuple(
            TransferItem(
                str(entry["session_hash"]),
                BranchRelation(entry["relation"]),
                TransferAction(entry["action"]),
                str(entry["local_sha256"]),
                str(entry["remote_sha256"]),
                int(entry["local_records"]),
                int(entry["remote_records"]),
                entry.get("target_relative_path"),
                entry.get("conflict_id"),
                tuple(str(code) for code in entry.get("codes", ())),
            )
            for entry in raw["items"]
        )
        plan = TransferPlan(
            int(raw["version"]), str(raw["plan_id"]), str(raw["created_at_utc"]),
            str(raw["source_machine"]), str(raw["target_machine"]), str(raw["layout_id"]),
            str(raw["canonical_version"]), bool(raw["volatile"]), items,
            tuple(str(code) for code in raw.get("codes", ())),
            str(raw["mirror_layout_id"]),
            tuple(str(item) for item in raw.get("scope", ())),
        )
    except _PlanRejected:
        # A refusal that already knows why: the generic guard below would
        # replace it with "invalid", which is exactly the message that leaves a
        # user unable to tell an old plan from a corrupt one.
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Transfer plan is invalid") from exc
    if _with_plan_id(plan).plan_id != plan.plan_id:
        raise ValueError("Transfer plan id does not match its contents")
    return plan


def _serialise(plan: TransferPlan) -> dict:
    return {
        "format": TRANSFER_PLAN_FORMAT,
        "version": plan.version,
        "plan_id": plan.plan_id,
        "created_at_utc": plan.created_at_utc,
        "source_machine": plan.source_machine,
        "target_machine": plan.target_machine,
        "layout_id": plan.layout_id,
        "mirror_layout_id": plan.mirror_layout_id,
        "canonical_version": plan.canonical_version,
        "volatile": plan.volatile,
        "codes": list(plan.codes),
        # Left out entirely when empty: a plan without a working set has to
        # hash to exactly what it hashed to before working sets existed.
        **({"scope": list(plan.scope)} if plan.scope else {}),
        "items": [
            {
                "session_hash": item.session_hash,
                "relation": item.relation.value,
                "action": item.action.value,
                "local_sha256": item.local_sha256,
                "remote_sha256": item.remote_sha256,
                "local_records": item.local_records,
                "remote_records": item.remote_records,
                "target_relative_path": item.target_relative_path,
                "conflict_id": item.conflict_id,
                "codes": list(item.codes),
            }
            for item in plan.items
        ],
    }


def _with_plan_id(plan: TransferPlan) -> TransferPlan:
    payload = _serialise(plan)
    payload["plan_id"] = ""
    # The id must be a function of the decisions alone. Including the creation
    # time would make every rebuild differ from the plan it is checking, so the
    # freshness check before an apply could never pass.
    payload["created_at_utc"] = ""
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return TransferPlan(
        plan.version, digest, plan.created_at_utc, plan.source_machine, plan.target_machine,
        plan.layout_id, plan.canonical_version, plan.volatile, plan.items, plan.codes,
        plan.mirror_layout_id, plan.scope,
    )


def descriptors_by_session_hash(catalog: SessionCatalog) -> dict[str, SessionDescriptor]:
    """Map session hash to descriptor, so a plan need not carry file names."""
    return {
        hashlib.sha256(session_id.encode("utf-8")).hexdigest(): descriptor
        for session_id, descriptor in _by_session_id(catalog).items()
    }


def _by_session_id(catalog: SessionCatalog) -> dict[str, SessionDescriptor]:
    out: dict[str, SessionDescriptor] = {}
    for descriptor in catalog.valid:
        if descriptor.session_id:
            # A duplicate id inside one catalog is a branch the catalog already
            # flagged; the first descriptor wins here and the rest surface as
            # catalog codes rather than being silently merged.
            out.setdefault(descriptor.session_id, descriptor)
    return out


def _branch_state(state: SessionState) -> BranchState:
    return BranchState.ARCHIVED if state is SessionState.ARCHIVED else BranchState.ACTIVE


def _os_path(relative: str) -> Path:
    return Path(*relative.split("/"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
