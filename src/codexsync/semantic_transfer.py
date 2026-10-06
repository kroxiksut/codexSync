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

  Nor does it cover a branch this machine already holds where the runtime's
  catalogue names it (`IN_PLACE`, CS-330a). Writing over that file chooses no
  path at all, so there is no layout to prove: it is how a chat continued on
  the other machine reaches this one. A session this machine has never held
  still needs a proven layout -- or `[semantic] new_chats = "same_path"`, which
  places it at the path it had on its own machine, as 0.1 did (D-020).

  An archive move is the one write that changes where a branch lives (D-023):
  a chat archived (or taken out of the archive) on one machine is moved into
  the same state folder on the other, at the path the other machine keeps it
  under, and the old file is removed after its backup. It too requires the
  catalogue to name the file being moved.
* A write into the Codex state directory is refused unless the runtime's own
  thread catalogue already places that branch at exactly that path. On an
  observed machine every session on disk has a catalogue row naming its rollout
  file, so the file system is not the whole truth about where a session lives:
  a branch put somewhere the catalogue does not name is invisible, with no
  error. Making the runtime see a *new* session would mean writing that
  database, which codexSync only ever reads — so it is
  ``UNSUPPORTED_STATE_BACKEND``, not a half-transfer. Overwriting a branch the
  catalogue already points at is exactly the case that stays allowed.

A divergence produces a conflict id. It is decided by a versioned resolution a
person recorded, or else by the rule `[conflict] policy` names (D-027): the
side with the later last record, this machine, or the mirror. Under the rule
`ask` (`manual_abort`), or where the rule cannot tell -- two equal or unknown
times under `newer` -- the plan blocks. Whoever decides, the branch that loses
is kept whole in a conflict bundle before anything is overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .exceptions import FailSafeError
from .jsonl_codec import JsonlCodec, codec_of, logical_name, logical_relative_path, with_codec
from .path_mapping import PathMappingError, PathMappingRule, apply_path_mapping, path_flavor
from .semantic_merge import (
    CANONICAL_DIGEST_VERSION,
    BranchComparison,
    BranchRelation,
    BranchState,
    compare_session_branches,
)
from .session_catalog import (
    DUPLICATE_SESSION_ID,
    HISTORY_PAGE,
    INVALID_CODES,
    RECORD_FORMAT_RANK,
    SessionCatalog,
    SessionDescriptor,
    SessionState,
)
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
    #: A copy of this session cannot be read as one branch -- unreadable,
    #: truncated, or one id in two files -- or the destination already holds a
    #: file that is not this session's. Nothing is written for it on either
    #: side: a copy that cannot be compared is not a copy that is absent, and
    #: overwriting it would replace a history nobody looked at.
    BLOCKED_INVALID_BRANCH = "BLOCKED_INVALID_BRANCH"
    #: Outside the working set: nothing is written into `.codex` for this
    #: session, and it blocks nothing. The cloud mirror is written regardless,
    #: so the backup stays complete whatever the working set says.
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    #: `sync.direction` is one-way and this write goes the other way. Nothing is
    #: written for it and it blocks nothing, exactly as a settings file the
    #: direction skips.
    HELD_BY_DIRECTION = "HELD_BY_DIRECTION"

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


#: How a divergence nobody decided by hand is decided (D-027). ``ask`` blocks,
#: which is all there was before; ``newer`` keeps the copy whose last record is
#: later; ``local`` keeps this machine's; ``remote`` keeps the mirror's.
RULE_ASK = "ask"
RULE_NEWER = "newer"
RULE_LOCAL = "local"
RULE_REMOTE = "remote"
CONFLICT_RULES = (RULE_ASK, RULE_NEWER, RULE_LOCAL, RULE_REMOTE)

#: `[conflict] policy` -> the rule for chats. One setting for settings files
#: and chats; for a chat "newer" is the time of its last record, never the
#: file's mtime, which Codex has been seen to keep through a rewrite.
_POLICY_RULES = {
    "manual_abort": RULE_ASK,
    "prefer_newer_mtime": RULE_NEWER,
    "prefer_local": RULE_LOCAL,
    "prefer_cloud": RULE_REMOTE,
}
#: `sync.direction` values; a one-way direction is the decision too.
DIRECTIONS = ("bidirectional", "to_cloud", "to_local")


def conflict_rule_for(policy: str, direction: str = "bidirectional") -> str:
    """The rule a plan decides divergences by, from the two settings that say so.

    A one-way `sync.direction` names the machine that wins: `to_cloud` sends
    this machine's work and never takes the other's, so this machine's copy
    is kept; `to_local` the reverse. Otherwise `[conflict] policy` decides.
    """
    if direction == "to_cloud":
        return RULE_LOCAL
    if direction == "to_local":
        return RULE_REMOTE
    try:
        return _POLICY_RULES[policy]
    except KeyError:
        raise FailSafeError(f"Unknown conflict.policy value {policy!r}") from None


#: A divergence decided by the rule rather than by a recorded resolution. The
#: rule itself follows as `RULE_<NAME>`.
RESOLVED_BY_RULE = "RESOLVED_BY_RULE"
#: Under `newer`, the two copies end at the same moment or one has no time, so
#: the rule cannot tell which is newer; the conflict waits for a person.
RULE_CANNOT_DECIDE = "RULE_CANNOT_DECIDE"


#: Target layouts proven by a controlled run on disposable state, keyed by an
#: id recorded together with the Codex version it was observed on.
#:
#: Deliberately empty: see docs/dev/experiments/session-layout-adapter.md. While it
#: is empty a plan can be built and read, and nothing can be written into a
#: directory the Codex runtime reads except over a branch already there at the
#: path the catalogue names (`IN_PLACE`). Writes towards the cloud mirror are a
#: separate destination and are not gated on this dict.
PROVEN_LAYOUTS: dict[str, str] = {}

#: A chat this machine has never held goes to the path it has on the machine it
#: came from, relative to `.codex` -- which is what 0.1 did by copying
#: `sessions/` wholesale, and what the owner observed Codex pick up (D-020).
#: That was observed on an older Codex and not in the controlled run, so it is
#: not a `PROVEN_LAYOUTS` entry: it is used only where `[semantic] new_chats`
#: asks for it, every item it places carries `NEW_CHAT_SAME_PATH`, and `doctor`
#: reports each chat on disk the runtime's catalogue has not taken up
#: (`session_visibility`), so a chat Codex does not show is a warning rather
#: than a silence.
SAME_PATH_LAYOUT_ID = "codex-same-relative-path-v1"
_SAME_PATH_TEMPLATE = "{state}/{source_dir}/{file_name}"

#: Layout id a plan is built under, per `[semantic] new_chats` value.
#: `keep_in_cloud` is the id every plan had before the setting existed, so those
#: plans keep their id.
NEW_CHATS_LAYOUTS: dict[str, str] = {
    "same_path": SAME_PATH_LAYOUT_ID,
    "keep_in_cloud": "unproven",
}

#: A chat this machine never held, placed at its source path under
#: `SAME_PATH_LAYOUT_ID`. The runtime's catalogue has no row for it yet; the
#: chat shows in Codex only if Codex takes the file up itself.
NEW_CHAT_SAME_PATH = "NEW_CHAT_SAME_PATH"


def layout_for_new_chats(setting: str) -> str:
    try:
        return NEW_CHATS_LAYOUTS[setting]
    except KeyError:
        raise FailSafeError(f"Unknown semantic.new_chats value {setting!r}") from None


def _layout_template(layout_id: str) -> str | None:
    if layout_id in PROVEN_LAYOUTS:
        return PROVEN_LAYOUTS[layout_id]
    if layout_id == SAME_PATH_LAYOUT_ID:
        return _SAME_PATH_TEMPLATE
    return None

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


#: A branch bound for `.codex` whose working folder does not exist here. The
#: chat still reads without it, and the folder may be created later, so this
#: blocks nothing; it says in the plan what a person would otherwise find out
#: only by opening the chat. Projects kept outside the synced folder are the
#: usual reason, and a working set is the usual answer.
CWD_ABSENT_HERE = "CWD_ABSENT_HERE"
#: Two `[[path_mappings]]` rules send the folder to different places, so where
#: it would be here is not known -- which is not the same as absent.
CWD_MAPPING_AMBIGUOUS = "CWD_MAPPING_AMBIGUOUS"
_CWD_CODES = (CWD_ABSENT_HERE, CWD_MAPPING_AMBIGUOUS)

#: Two copies of one session that disagree because the runtime rewrote one of
#: them into a newer record format -- the September 2026 desktop build did that
#: to every session file at once. It stays a conflict: the rewrite also dropped
#: records (turns the person had rolled back, resumed `session_meta`, injected
#: instructions), so the two are not the same history and nothing proves they
#: are. The code only says what kind of conflict it is, so that one explicit
#: decision can cover all of them (`format_migration_resolutions`).
FORMAT_MIGRATION = "FORMAT_MIGRATION"
#: Which side holds the newer format. The bulk decision keeps that side.
NEWER_FORMAT_LOCAL = "NEWER_FORMAT_LOCAL"
NEWER_FORMAT_REMOTE = "NEWER_FORMAT_REMOTE"
#: The older-format copy has a record later than anything in the newer one, so
#: it may carry work the rewrite never saw -- a turn taken on another machine
#: before it was upgraded. The bulk decision leaves such a conflict to a person.
OLDER_FORMAT_HAS_LATER_RECORDS = "OLDER_FORMAT_HAS_LATER_RECORDS"


#: Conflicts of content, the only kind a format rewrite can explain.
_DIVERGENCES = frozenset({BranchRelation.DIVERGED, BranchRelation.DIVERGED_NO_COMMON_RECORDS})

#: The two copies could not be read side by side, though each read alone.
COMPARISON_FAILED = "COMPARISON_FAILED"
#: A file already sits where a copy of a session the destination lacks would go.
DESTINATION_OCCUPIED = "DESTINATION_OCCUPIED"
#: The mirror already holds this branch under another path than the source
#: does, and it is rewritten there rather than gaining a second file.
MIRROR_PATH_KEPT = "MIRROR_PATH_KEPT"
#: A proven layout renders another path than the one this machine keeps the
#: session under.
LAYOUT_DISAGREES_WITH_BRANCH = "LAYOUT_DISAGREES_WITH_BRANCH"
#: A working set was chosen and covers no session: nothing is written into
#: `.codex`, and the mirror is written in full as always.
WORKING_SET_MATCHES_NOTHING = "WORKING_SET_MATCHES_NOTHING"
#: A branch bound for `.codex` is written over the file this machine already
#: keeps it in, at the path the runtime's own catalogue names for it. No layout
#: is rendered, so none has to be proven: where the session lives here is read,
#: not chosen (CS-330a).
IN_PLACE = "IN_PLACE"
#: Why a branch this machine holds could not be written in place. Each keeps the
#: item blocked on the layout, as before in-place writes existed.
IN_PLACE_CATALOG_ABSENT = "IN_PLACE_CATALOG_ABSENT"
IN_PLACE_STATE_CHANGES = "IN_PLACE_STATE_CHANGES"
IN_PLACE_CONTAINER = "IN_PLACE_CONTAINER"
IN_PLACE_ARCHIVE_FLAG_DIFFERS = "IN_PLACE_ARCHIVE_FLAG_DIFFERS"
#: The catalogue names another file of the same chat -- the page its history
#: now ends in, or the file it began in -- which is how Codex reaches this one
#: (CS-356). Codex lists the chat from whichever file its row names.
CATALOG_NAMES_OTHER_PART = "CATALOG_NAMES_OTHER_PART"

#: An archive move (D-023): one machine archived a chat or took it out of the
#: archive, and the other side follows. Which side moved is read from this
#: machine's own semantic manifest entry -- the state both sides last agreed
#: on -- never from clocks: the side that still holds that state is the one
#: that did not move. With no entry of its own this machine follows the
#: mirror, whose state another machine vouched for when it wrote it.
#:
#: `ARCHIVE_FOLLOWS_REMOTE`: this machine's `.codex` follows the mirror.
ARCHIVE_FOLLOWS_REMOTE = "ARCHIVE_FOLLOWS_REMOTE"
#: `ARCHIVE_FOLLOWS_LOCAL`: the mirror follows this machine.
ARCHIVE_FOLLOWS_LOCAL = "ARCHIVE_FOLLOWS_LOCAL"
#: The side being written keeps the session in the other state folder, so the
#: branch is written where the followed side keeps it and the old file is
#: removed -- after a verified backup, in the same envelope (D-023).
MOVES_BRANCH = "MOVES_BRANCH"
#: One machine archived the chat and the other continued it: the side that did
#: not move holds records the moved side lacks. Which of the two to keep is a
#: person's decision, like any other divergence.
ARCHIVED_AND_CONTINUED = "ARCHIVED_AND_CONTINUED"
#: One session id in two files on this machine, settled by the runtime's own
#: catalogue naming exactly one of them (CS-348). The other file is a copy the
#: runtime no longer reads; it is left where it is and never transferred.
STALE_DUPLICATE_BY_CATALOG = "STALE_DUPLICATE_BY_CATALOG"


def transfer_direction(item: "TransferItem") -> str | None:
    """Which side a writable item writes: ``"local"`` (`.codex`) or ``"mirror"``."""
    if item.action is TransferAction.FAST_FORWARD_LOCAL:
        return "local"
    if item.action is TransferAction.FAST_FORWARD_REMOTE:
        return "mirror"
    if item.action is TransferAction.ARCHIVE_TRANSITION:
        return "mirror" if ARCHIVE_FOLLOWS_LOCAL in item.codes else "local"
    return None


def prefer_catalogued_copies(
    catalog: SessionCatalog, placements: ThreadPlacements | None
) -> SessionCatalog:
    """Settle a duplicate session id by the runtime's own thread catalogue.

    Two files holding the *same* part of one chat. The case that prompted this
    (CS-348) turned out to be a paginated chat -- `rollout-…-<id>.jsonl` and a
    page `rollout-…-<id>_<other>.jsonl` that continues it -- which the catalogue
    now keeps apart by `branch_key` (CS-356), so it never reaches here. What is
    left is a true duplicate: where the catalogue names exactly one of the
    copies, that copy is the branch; the others stay on disk untouched -- still
    occupying their paths -- and are marked `STALE_DUPLICATE_BY_CATALOG`
    instead of making the whole session unusable.

    Anything less certain leaves the duplicate as it was: no catalogue, one that
    cannot be read, one naming none of the copies, or a copy that is broken in
    its own right.
    """
    if not catalog.branches or placements is None or placements.status is not PlacementStatus.AVAILABLE:
        return catalog
    chosen: dict[str, str] = {}
    for key, branch in catalog.branches.items():
        recorded = placements.placement_of(branch.session_id)
        named = [item for item in branch.descriptors if item.relative_path == recorded]
        if len(named) != 1:
            continue
        if any(code in INVALID_CODES for item in branch.descriptors for code in item.codes):
            continue
        chosen[key] = named[0].relative_path
    if not chosen:
        return catalog
    descriptors: list[SessionDescriptor] = []
    for item in catalog.descriptors:
        if item.branch_key not in chosen:
            descriptors.append(item)
        elif item.relative_path == chosen[item.branch_key]:
            descriptors.append(replace(
                item,
                state=_state_of_folder(item.relative_path),
                codes=tuple(code for code in item.codes if code != DUPLICATE_SESSION_ID),
            ))
        else:
            descriptors.append(replace(item, codes=item.codes + (STALE_DUPLICATE_BY_CATALOG,)))
    branches = {key: value for key, value in catalog.branches.items() if key not in chosen}
    return SessionCatalog(descriptors, branches, catalog.codes, catalog.volatile)


def _state_of_folder(relative_path: str) -> SessionState:
    return SessionState.ARCHIVED if relative_path.startswith("archived_sessions/") else SessionState.ACTIVE


def _format_migration_codes(local: SessionDescriptor, remote: SessionDescriptor) -> tuple[str, ...]:
    """What a conflict between two copies in different record formats is."""
    local_rank = RECORD_FORMAT_RANK.get(local.record_format or "")
    remote_rank = RECORD_FORMAT_RANK.get(remote.record_format or "")
    if local_rank is None or remote_rank is None or local_rank == remote_rank:
        return ()
    newer, older = (local, remote) if local_rank > remote_rank else (remote, local)
    codes = [FORMAT_MIGRATION, NEWER_FORMAT_LOCAL if newer is local else NEWER_FORMAT_REMOTE]
    # As moments, not text: two copies need not write the same fractional
    # digits. An older copy with a time that does not parse may be later.
    older_at, newer_at = _record_time(older.last_record_at), _record_time(newer.last_record_at)
    if older.last_record_at and (older_at is None or newer_at is None or older_at > newer_at):
        codes.append(OLDER_FORMAT_HAS_LATER_RECORDS)
    return tuple(codes)


def local_folder_exists(path: str) -> bool:
    """Whether ``path`` names a directory on this machine. Reads only.

    A path of another platform's shape is absent rather than tried: on Windows
    ``/Users/me/project`` would otherwise be looked up on the current drive,
    which answers a question nobody asked.
    """
    native = ("windows", "unc") if os.name == "nt" else ("posix",)
    if path_flavor(path) not in native:
        return False
    return os.path.isdir(path)


def _working_folder_check(
    rules: list[PathMappingRule],
    source_machine: str,
    target_machine: str,
    folder_exists: Callable[[str], bool],
) -> Callable[[SessionDescriptor], str | None]:
    """What a branch recorded on ``source_machine`` has to say about its folder here.

    The folder goes through the same `[[path_mappings]]` rules `chats` and
    `repair-projects` use, in the same direction, so all three agree on where
    a chat's folder is on this machine. Many chats share a folder, so each is
    looked up once.
    """
    seen: dict[str, str | None] = {}

    def check(descriptor: SessionDescriptor) -> str | None:
        cwd = descriptor.cwd
        if not cwd:
            return None
        if cwd not in seen:
            seen[cwd] = _folder_code(cwd)
        return seen[cwd]

    def _folder_code(cwd: str) -> str | None:
        here = cwd
        if rules:
            try:
                here = apply_path_mapping(
                    cwd, source_machine=source_machine, target_machine=target_machine, rules=rules
                ).target_path
            except PathMappingError as exc:
                if str(exc) == "AMBIGUOUS_MAPPING":
                    return CWD_MAPPING_AMBIGUOUS
                # No rule for this folder: it is expected at the same path.
        return None if folder_exists(here) else CWD_ABSENT_HERE

    return check


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
    #: A working set was chosen and names no session at all -- a project with
    #: no chats yet, say. `scope` alone cannot say that, because empty means
    #: "everything": without this a plan would be built narrowed, rebuilt
    #: unnarrowed, and its id could never match.
    scope_matches_nothing: bool = False
    #: The rule divergences were decided by (`CONFLICT_RULES`) and the
    #: direction writes were held to. An apply rebuilds the plan under these,
    #: never under the config as it is by then -- the user confirmed this id.
    #: Each is left out of the id material at its default, so plans built
    #: before either existed keep their id.
    conflict_rule: str = RULE_ASK
    direction: str = "bidirectional"

    @property
    def out_of_scope_items(self) -> tuple["TransferItem", ...]:
        return tuple(item for item in self.items if item.action is TransferAction.OUT_OF_SCOPE)

    @property
    def writable_items(self) -> tuple[TransferItem, ...]:
        return tuple(item for item in self.items if item.action.writes)

    @property
    def blocked_items(self) -> tuple[TransferItem, ...]:
        return tuple(item for item in self.items if item.action.is_blocked)


def plan_scope(plan: TransferPlan) -> tuple[str, ...] | None:
    """The working set a plan was built under, in the form `build_transfer_plan` takes."""
    if plan.scope or plan.scope_matches_nothing:
        return plan.scope
    return None


def conflict_id_for(session_hash: str, local_sha256: str, remote_sha256: str) -> str:
    """Stable id for one exact pair of divergent branches."""
    material = "\0".join(sorted((local_sha256, remote_sha256)))
    return hashlib.sha256(f"{session_hash}\0{material}".encode("utf-8")).hexdigest()


def format_migration_resolutions(
    plan: TransferPlan,
) -> tuple[list[BranchResolution], list[TransferItem]]:
    """One decision for every conflict that is only a record-format rewrite.

    Each keeps the side in the newer format and is an ordinary resolution:
    pinned to both branch hashes, so a branch that moves afterwards makes it
    stale, and the losing branch is preserved when the plan is applied. A
    conflict whose older copy has a later record is returned separately and
    never decided here -- that copy may hold work the rewrite never saw.
    """
    decided: list[BranchResolution] = []
    held: list[TransferItem] = []
    for item in plan.items:
        if item.action is not TransferAction.BLOCKED_CONFLICT or FORMAT_MIGRATION not in item.codes:
            continue
        if item.conflict_id is None:
            continue
        if OLDER_FORMAT_HAS_LATER_RECORDS in item.codes:
            held.append(item)
            continue
        choice = (
            ResolutionChoice.KEEP_LOCAL if NEWER_FORMAT_LOCAL in item.codes else ResolutionChoice.KEEP_REMOTE
        )
        decided.append(BranchResolution(
            item.conflict_id, item.session_hash, item.local_sha256, item.remote_sha256, choice,
        ))
    return decided, held


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
    path_rules: list[PathMappingRule] | None = None,
    folder_exists: Callable[[str], bool] | None = None,
    agreed_states: Mapping[str, str] | None = None,
    conflict_rule: str = RULE_ASK,
    direction: str = "bidirectional",
) -> TransferPlan:
    """Classify every session present on either side and freeze the decisions.

    ``agreed_states`` maps a session hash to the state (``ACTIVE``/``ARCHIVED``)
    this machine last recorded agreeing on with the mirror. It decides which
    side of an archive move moved; without it this machine follows the mirror.

    ``resolutions`` are keyed by conflict id. ``confirmed_bases`` holds the
    session hashes for which the semantic store has a recorded common ancestor.
    ``placements`` is the runtime's own thread catalogue; without it a write
    into the Codex state directory is unconstrained, which is only correct when
    no such catalogue exists.

    ``scope`` is the working set: session hashes that may be written into
    `.codex`. ``None`` means everything, which is what every plan meant before
    working sets existed. The mirror is written in full either way.

    ``folder_exists`` turns on the working-folder check: a branch bound for
    `.codex` whose folder, mapped through ``path_rules``, is not a directory
    here carries `CWD_ABSENT_HERE`. The codes are part of the plan id, so a
    folder created between the scan and the apply asks for a rescan like any
    other change. Without it no code is added and a plan hashes as before.

    ``conflict_rule`` decides a divergence nobody resolved by hand (D-027), and
    ``direction`` holds back every write the other way (`HELD_BY_DIRECTION`).

    A chat continued in pages is one item per file, keyed by `branch_key`; the
    first file keeps the session's own hash, so every earlier plan, manifest
    entry and resolution about it still applies.
    """
    if conflict_rule not in CONFLICT_RULES:
        raise FailSafeError(f"Unknown conflict rule {conflict_rule!r}")
    if direction not in DIRECTIONS:
        raise FailSafeError(f"Unknown sync direction {direction!r}")
    cwd_code = (
        _working_folder_check(path_rules or [], source_machine, target_machine, folder_exists)
        if folder_exists is not None else None
    )
    resolutions = resolutions or {}
    # A changed branch changes the conflict id, so an earlier choice would
    # simply not be found. Index by session too, to say "your decision is
    # stale" instead of silently presenting a brand new conflict.
    by_session: dict[str, BranchResolution] = {
        resolution.session_hash: resolution for resolution in resolutions.values()
    }
    confirmed_bases = confirmed_bases or set()
    codes: list[str] = []

    # Every copy that names a session id, readable or not. A branch that cannot
    # be read is still that session's branch: treating it as absent would make
    # the session one-sided and let a copy land on top of it unread.
    local_groups = _groups_by_branch(local_catalog)
    remote_groups = _groups_by_branch(remote_catalog)
    # Every file of a chat on this machine, whatever part it is: the catalogue
    # names one of them, and through it Codex reaches the rest (CS-356).
    chains: dict[str, set[str]] = {}
    for descriptor in local_catalog.descriptors:
        if descriptor.session_id:
            chains.setdefault(descriptor.session_id, set()).add(descriptor.relative_path)
    codes.extend(_idless_codes(local_catalog, "LOCAL"))
    codes.extend(_idless_codes(remote_catalog, "REMOTE"))
    occupied = _destination_check(local_root, remote_root, local_catalog, remote_catalog)
    if local_catalog.volatile or remote_catalog.volatile:
        volatile = True

    items: list[TransferItem] = []
    claimed_targets: dict[str, str] = {}
    agreed_states = agreed_states or {}
    # Copies the catalogue settled as no longer read (CS-348). They are not
    # branches, but the item says they exist, so the file left on disk is
    # never a silence.
    stale_duplicates = {
        descriptor.branch_key for descriptor in local_catalog.descriptors
        if STALE_DUPLICATE_BY_CATALOG in descriptor.codes
    }

    for key in sorted(set(local_groups) | set(remote_groups)):
        session_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        local_group = local_groups.get(key, ())
        group = local_group + remote_groups.get(key, ())
        session_id = next(descriptor.session_id for descriptor in group if descriptor.session_id)
        item, one_sided = _classify(
            session_hash, session_id,
            local_group, remote_groups.get(key, ()),
            local_root=local_root, remote_root=remote_root,
            confirmed_bases=confirmed_bases, max_line_bytes=max_line_bytes,
            resolutions=resolutions, resolutions_by_session=by_session,
            conflict_rule=conflict_rule,
            placements=placements, layout_id=layout_id, mirror_codec=mirror_codec,
            claimed_targets=claimed_targets, cwd_code=cwd_code, occupied=occupied,
            agreed_state=agreed_states.get(session_hash),
            chain=frozenset(
                chains.get(session_id, set()) - {descriptor.relative_path for descriptor in local_group}
            ),
        )
        if any(descriptor.page is not None for descriptor in group):
            item = replace(item, codes=tuple(dict.fromkeys((*item.codes, HISTORY_PAGE))))
        if key in stale_duplicates:
            item = replace(item, codes=tuple(dict.fromkeys((*item.codes, STALE_DUPLICATE_BY_CATALOG))))
            codes.append(STALE_DUPLICATE_BY_CATALOG)
        items.append(item)
        # A one-sided item does not feed the plan's codes (its folder code
        # does, below): being on one side only is the normal case.
        if not one_sided:
            codes.extend(item.codes)

    scope_matches_nothing = False
    if scope is not None:
        scope = set(scope)
        scope_matches_nothing = not scope
        # A working set names chats; every page of a chat in it is in it too.
        scope |= {
            hashlib.sha256(key.encode("utf-8")).hexdigest()
            for key, group in (*local_groups.items(), *remote_groups.items())
            if group[0].session_id
            and hashlib.sha256(group[0].session_id.encode("utf-8")).hexdigest() in scope
        }
        items = [_apply_scope(item, scope) for item in items]
        codes.extend(
            code for item in items if item.action is TransferAction.OUT_OF_SCOPE
            for code in item.codes
        )
        if scope_matches_nothing:
            codes.append(WORKING_SET_MATCHES_NOTHING)
    if direction != "bidirectional":
        items = [_apply_direction(item, direction) for item in items]
    # One-sided items do not feed the plan's codes, but a missing folder is
    # worth saying at plan level whichever kind of item it was found on.
    codes.extend(code for item in items for code in item.codes if code in _CWD_CODES)
    if any(item.action.is_blocked for item in items):
        codes.append("PLAN_HAS_BLOCKED_ITEMS")
    plan = TransferPlan(
        TRANSFER_PLAN_VERSION, "", _now(), source_machine, target_machine,
        layout_id, CANONICAL_DIGEST_VERSION, volatile,
        tuple(items), tuple(dict.fromkeys(codes)), mirror_layout_id(mirror_codec),
        tuple(sorted(scope)) if scope else (),
        scope_matches_nothing,
        conflict_rule,
        direction,
    )
    return _with_plan_id(plan)


def _classify(
    session_hash: str,
    session_id: str,
    local_all: tuple[SessionDescriptor, ...],
    remote_all: tuple[SessionDescriptor, ...],
    *,
    local_root: Path,
    remote_root: Path,
    confirmed_bases: set[str],
    max_line_bytes: int,
    agreed_state: str | None,
    **gate,
) -> tuple[TransferItem, bool]:
    """One session's item, and whether it is one-sided."""
    unusable = _unusable_item(session_hash, local_all, remote_all)
    if unusable is not None:
        return unusable, False
    # Past that check each side holds at most one copy, and it is valid.
    local = next(iter(local_all), None)
    remote = next(iter(remote_all), None)
    gate_only = {
        key: value for key, value in gate.items()
        if key not in {"resolutions", "resolutions_by_session", "conflict_rule"}
    }
    if local is None or remote is None:
        # One side simply does not have this session yet. That is a plain
        # copy, but where it lands is still a placement decision, so it goes
        # through the same gate a fast-forward does.
        return _one_sided_item(session_hash, session_id, local, remote, **gate_only), True
    comparison = compare_session_branches(
        local_root / _os_path(local.relative_path),
        remote_root / _os_path(remote.relative_path),
        local_state=_branch_state(local.state),
        remote_state=_branch_state(remote.state),
        has_confirmed_base=session_hash in confirmed_bases,
        max_line_bytes=max_line_bytes,
    )
    return _decide(
        session_hash, session_id, comparison, local, remote, agreed_state=agreed_state, **gate,
    ), False


def _apply_direction(item: TransferItem, direction: str) -> TransferItem:
    """Hold back a write a one-way `sync.direction` does not go.

    `to_cloud` never writes `.codex`, `to_local` never writes the mirror. What
    the item would have been is kept in its codes, as a working set does.
    """
    side = transfer_direction(item)
    if side is None or (side == "local") != (direction == "to_cloud"):
        return item
    return TransferItem(
        item.session_hash, item.relation, TransferAction.HELD_BY_DIRECTION,
        item.local_sha256, item.remote_sha256, item.local_records, item.remote_records,
        item.target_relative_path, item.conflict_id,
        tuple(dict.fromkeys((*item.codes, f"WOULD_BE_{item.action.value}"))),
    )


def _rule_choice(
    rule: str, local: SessionDescriptor, remote: SessionDescriptor, kind: tuple[str, ...]
) -> ResolutionChoice | None:
    """What ``rule`` keeps of two divergent copies, or ``None`` if it cannot say."""
    if rule == RULE_LOCAL:
        return ResolutionChoice.KEEP_LOCAL
    if rule == RULE_REMOTE:
        return ResolutionChoice.KEEP_REMOTE
    if rule != RULE_NEWER:
        return None
    local_at, remote_at = _record_time(local.last_record_at), _record_time(remote.last_record_at)
    if local_at is None or remote_at is None:
        # A copy whose last moment is unknown cannot be ordered -- not even
        # behind a newer format, since a turn taken before the upgrade may be
        # exactly what it holds.
        return None
    if local_at != remote_at:
        return ResolutionChoice.KEEP_LOCAL if local_at > remote_at else ResolutionChoice.KEEP_REMOTE
    if FORMAT_MIGRATION in kind and OLDER_FORMAT_HAS_LATER_RECORDS not in kind:
        # The same moment in both: only the rewrite differs, and the newer
        # format is what Codex reads from now on.
        return ResolutionChoice.KEEP_LOCAL if NEWER_FORMAT_LOCAL in kind else ResolutionChoice.KEEP_REMOTE
    return None


def _record_time(value: str | None) -> datetime | None:
    """A record's `timestamp` as a moment; ``None`` when it is not one.

    Parsed rather than compared as text: two copies need not write the same
    number of fractional digits, and a string comparison would then rank by
    precision instead of by time.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


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
    # An unusable branch keeps saying so: it holds back the mirror too, which a
    # working set never narrows, so calling it out of scope would hide that.
    if item.action in {
        TransferAction.NOOP, TransferAction.FAST_FORWARD_REMOTE, TransferAction.BLOCKED_INVALID_BRANCH,
    }:
        return item
    if transfer_direction(item) == "mirror":
        # An archive move the mirror follows writes only the mirror.
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
    cwd_code: Callable[[SessionDescriptor], str | None] | None = None,
    occupied: Callable[[str, str], bool] | None = None,
    agreed_state: str | None = None,
    conflict_rule: str = RULE_ASK,
    chain: frozenset[str] = frozenset(),
) -> TransferItem:
    def make(action: TransferAction, *, target: str | None = None, conflict: str | None = None,
             extra: tuple[str, ...] = ()) -> TransferItem:
        return TransferItem(
            session_hash, comparison.relation, action,
            comparison.local_sha256, comparison.remote_sha256,
            comparison.local_records, comparison.remote_records,
            target, conflict, extra,
        )

    def write(action: TransferAction, **kwargs) -> TransferItem:
        return _gate_write(
            make, action, session_id, local, remote,
            placements=placements, layout_id=layout_id, mirror_codec=mirror_codec,
            claimed_targets=claimed_targets, cwd_code=cwd_code, occupied=occupied, chain=chain, **kwargs,
        )

    def resolved_write(choice: ResolutionChoice, conflict: str, extra: tuple[str, ...]) -> TransferItem:
        resolved = (
            TransferAction.FAST_FORWARD_REMOTE if choice is ResolutionChoice.KEEP_LOCAL
            else TransferAction.FAST_FORWARD_LOCAL
        )
        return write(resolved, conflict=conflict, extra=extra)

    def conflict_item(kind: tuple[str, ...]) -> TransferItem:
        conflict = conflict_id_for(session_hash, comparison.local_sha256, comparison.remote_sha256)
        resolution = resolutions.get(conflict)
        if resolution is not None and resolution.matches(comparison):
            # A person's decision about exactly these bytes outranks any rule,
            # "decide later" included.
            if resolution.choice is ResolutionChoice.DEFER:
                return make(TransferAction.BLOCKED_CONFLICT, conflict=conflict, extra=kind + ("DEFERRED",))
            return resolved_write(resolution.choice, conflict, kind + ("RESOLVED_BY_USER",))
        # A decision exists for this session but was made about other bytes:
        # it is stale, and says so if the rule does not settle the conflict.
        previous = resolution or resolutions_by_session.get(session_hash)
        stale = ("STALE_RESOLUTION",) if previous is not None and not previous.matches(comparison) else ()
        choice = _rule_choice(conflict_rule, local, remote, kind)
        if choice is not None:
            return resolved_write(
                choice, conflict, kind + (RESOLVED_BY_RULE, f"RULE_{conflict_rule.upper()}"),
            )
        undecided = (RULE_CANNOT_DECIDE,) if conflict_rule == RULE_NEWER else ()
        return make(TransferAction.BLOCKED_CONFLICT, conflict=conflict, extra=kind + stale + undecided)

    if comparison.relation is BranchRelation.IDENTICAL:
        return make(TransferAction.NOOP)

    if comparison.relation is BranchRelation.INVALID:
        # Each copy read cleanly alone, but not side by side: one changed or
        # broke in between. There are no branch hashes a decision could be
        # pinned to, so this is not a conflict anyone can resolve -- the
        # session is left alone until it reads cleanly.
        return TransferItem(
            session_hash, comparison.relation, TransferAction.BLOCKED_INVALID_BRANCH,
            local.sha256, remote.sha256, local.line_count, remote.line_count,
            None, None, (COMPARISON_FAILED,),
        )

    if comparison.is_conflict:
        # Only a divergence of content can be a format rewrite. A missing base
        # is an archive move waiting for proof of ancestry, and labelling it
        # would let `--format-migrations` overwrite one side of it in bulk.
        return conflict_item(
            _format_migration_codes(local, remote)
            if comparison.relation in _DIVERGENCES else ()
        )

    if comparison.relation is BranchRelation.ARCHIVE_TRANSITION:
        # Which side moved: the one that no longer holds the state both sides
        # last agreed on. With no agreement of this machine's own on record,
        # the mirror's state is followed -- another machine vouched for it.
        follows_local = agreed_state is not None and agreed_state == remote.state.value
        followed, other = (
            (comparison.local_records, comparison.remote_records) if follows_local
            else (comparison.remote_records, comparison.local_records)
        )
        if other > followed:
            # Archived on one machine and continued on the other: adopting the
            # move would drop the continuation, and keeping it would undo the
            # move. A person picks the branch, state included.
            return conflict_item((ARCHIVED_AND_CONTINUED,))
        return write(
            TransferAction.ARCHIVE_TRANSITION,
            towards_local=not follows_local,
            extra=(ARCHIVE_FOLLOWS_LOCAL if follows_local else ARCHIVE_FOLLOWS_REMOTE,),
        )

    action = {
        BranchRelation.FAST_FORWARD_LOCAL: TransferAction.FAST_FORWARD_LOCAL,
        BranchRelation.FAST_FORWARD_REMOTE: TransferAction.FAST_FORWARD_REMOTE,
    }[comparison.relation]
    return write(action)


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
    cwd_code: Callable[[SessionDescriptor], str | None] | None = None,
    occupied: Callable[[str, str], bool] | None = None,
    chain: frozenset[str] = frozenset(),
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
        claimed_targets=claimed_targets, cwd_code=cwd_code, occupied=occupied,
        extra=("SESSION_ON_ONE_SIDE_ONLY",), chain=chain,
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
    cwd_code: Callable[[SessionDescriptor], str | None] | None = None,
    occupied: Callable[[str, str], bool] | None = None,
    conflict: str | None = None,
    extra: tuple[str, ...] = (),
    towards_local: bool | None = None,
    chain: frozenset[str] = frozenset(),
) -> TransferItem:
    if towards_local is None:
        towards_local = action is TransferAction.FAST_FORWARD_LOCAL
    source = remote if towards_local else local
    if source is None:
        raise FailSafeError("A transfer decision has no source branch to copy")
    # The side being written keeps this session in the other state folder: an
    # archive move, or a resolution that keeps the side archived (or not) on
    # its own. The branch then goes where the followed side keeps it, and the
    # old file is removed after its backup (D-023).
    moving = False
    if towards_local and cwd_code is not None:
        # Said before the gate decides, so a branch blocked on the layout or the
        # catalogue -- or later held back by the working set -- still tells a
        # person what it would have been like here.
        folder = cwd_code(source)
        if folder is not None:
            extra = extra + (folder,)
    if not towards_local:
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
        #
        # The path follows the same rule. A branch the mirror already holds is
        # rewritten where it is, whatever path the source keeps it under: a
        # second file there would be the same duplicate by another route. The
        # one exception is an archive state that changes, which moves the
        # mirror's file into the other state folder and removes the old one.
        stored = codec_of(remote.relative_path) if remote is not None else None
        codec = mirror_codec if stored is None else stored
        side = "mirror"
        extra = extra + ("MIRROR_DESTINATION",)
        if remote is not None and remote.state is not source.state:
            moving = True
            target = mirror_relative_path(source, codec)
            extra = extra + (MOVES_BRANCH,)
        else:
            target = remote.relative_path if remote is not None else mirror_relative_path(source, codec)
        if stored is not None and stored is not mirror_codec:
            extra = extra + ("MIRROR_CONTAINER_KEPT",)
        if (
            remote is not None and not moving
            and logical_relative_path(target) != logical_relative_path(source.relative_path)
        ):
            extra = extra + (MIRROR_PATH_KEPT,)
    elif local is not None and local.state is not source.state:
        # This machine keeps the chat in the other state folder. It goes where
        # the other machine's Codex put it -- the same relative path, as
        # `new_chats = same_path` places a new chat -- and only when the
        # catalogue names exactly the file being moved, so the file the runtime
        # reads is the one that moves. The catalogue row itself still names the
        # old path afterwards; codexSync never writes it, and `doctor`'s
        # `session_visibility` counts such chats.
        #
        # Under a proven layout the layout names the new path and the
        # catalogue is consulted as for any write there.
        if layout_id in PROVEN_LAYOUTS:
            target = target_relative_path(layout_id, source)
            objection = _catalogue_objection(placements, session_id, local.relative_path, chain)
            if objection is not None:
                return make(
                    TransferAction.BLOCKED_UNSUPPORTED_BACKEND,
                    target=target, conflict=conflict, extra=extra + objection,
                )
        else:
            refusal = _in_place_refusal(placements, session_id, local, source, moving=True, chain=chain)
            if refusal is not None:
                return make(TransferAction.BLOCKED_UNPROVEN_LAYOUT, conflict=conflict, extra=extra + refusal)
            target = logical_relative_path(source.relative_path)
        moving = True
        side = "local"
        extra = extra + (MOVES_BRANCH,)
    elif local is None and layout_id == SAME_PATH_LAYOUT_ID:
        # A chat this machine never held, asked for by `new_chats = same_path`.
        # The catalogue has no row for it, and that is the case being taken on
        # trust; a catalogue that does know the thread and places it somewhere
        # else, or cannot be read, still refuses, since a file here would then
        # be a second copy the runtime ignores.
        side, target = "local", target_relative_path(layout_id, source)
        objection = _catalogue_objection(placements, session_id, target, chain)
        if objection is not None and objection != ("SESSION_NOT_IN_CATALOG",):
            return make(
                TransferAction.BLOCKED_UNSUPPORTED_BACKEND,
                target=target, conflict=conflict, extra=extra + objection,
            )
        extra = extra + (NEW_CHAT_SAME_PATH,) + _other_part_code(placements, session_id, target, chain)
    elif layout_id not in PROVEN_LAYOUTS:
        if local is None:
            # A session this machine has never held: where it would have to go,
            # and whether the runtime would find it there, is what the layout
            # experiment is for (CS-330b).
            return make(TransferAction.BLOCKED_UNPROVEN_LAYOUT, conflict=conflict, extra=extra)
        refusal = _in_place_refusal(placements, session_id, local, source, chain=chain)
        if refusal is not None:
            return make(TransferAction.BLOCKED_UNPROVEN_LAYOUT, conflict=conflict, extra=extra + refusal)
        side, target = "local", local.relative_path
        extra = extra + (IN_PLACE,) + _other_part_code(placements, session_id, target, chain)
    else:
        side, target = "local", target_relative_path(layout_id, source)
        objection = _catalogue_objection(placements, session_id, target, chain)
        if objection is not None:
            return make(
                TransferAction.BLOCKED_UNSUPPORTED_BACKEND,
                target=target, conflict=conflict, extra=extra + objection,
            )
        if local is not None and target != local.relative_path:
            # This machine keeps the session somewhere the layout does not
            # render. Writing the rendered path would leave two files for one
            # id, and moving the old one needs a delete.
            return make(
                TransferAction.BLOCKED_UNPROVEN_LAYOUT,
                target=target, conflict=conflict, extra=extra + (LAYOUT_DISAGREES_WITH_BRANCH,),
            )

    destination = remote if side == "mirror" else local
    if (destination is None or moving) and occupied is not None and occupied(side, target):
        # Nothing on that side is this session, yet a file sits where the copy
        # would go: a branch whose id could not be read, or one not attributed
        # to any session. Replacing it would destroy a history nobody compared.
        return make(
            TransferAction.BLOCKED_INVALID_BRANCH,
            target=target, conflict=conflict, extra=extra + (DESTINATION_OCCUPIED,),
        )

    # Two destinations only collide when they are the same file, and the two
    # sides are different roots, so the side is part of the claim.
    claim = f"{side}:{target}"
    owner = claimed_targets.get(claim)
    if owner is not None and owner != session_id:
        return make(TransferAction.BLOCKED_TARGET_COLLISION, target=target, conflict=conflict, extra=extra)
    claimed_targets[claim] = session_id
    return make(action, target=target, conflict=conflict, extra=extra)


def _in_place_refusal(
    placements: ThreadPlacements | None,
    session_id: str,
    local: SessionDescriptor,
    source: SessionDescriptor,
    *,
    moving: bool = False,
    chain: frozenset[str] = frozenset(),
) -> tuple[str, ...] | None:
    """Why ``source`` may not replace ``local`` where it lies, if it may not.

    With ``moving`` the branch is not written over ``local`` but beside it, in
    the other state folder, and ``local`` is removed: the state check and the
    container check do not apply, every catalogue check does.

    An in-place write chooses no path: it is the file this machine already
    keeps the session in, and it is allowed only where the runtime's own
    catalogue names exactly that file for exactly this thread. Then nothing
    about the layout is guessed -- the runtime already reads that path -- and
    only the contents move on, which is what continuing a chat on the other
    machine produced. Everything else stays blocked as it was:

    * no catalogue, or one that cannot be read: nothing proves the runtime
      finds the session by that file, so here `ABSENT` refuses, where under a
      proven layout it constrains nothing;
    * a catalogue that does not know the thread or places it elsewhere;
    * a branch that is archived on one side and active on the other, or whose
      archive flag in the catalogue disagrees with the folder it sits in --
      that is a move, and a move needs a delete;
    * a local branch in a compressed container, which the runtime would not
      read as the plain JSONL the write produces.
    """
    if not moving and codec_of(local.relative_path) is not JsonlCodec.NONE:
        return (IN_PLACE_CONTAINER,)
    if not moving and source.state is not local.state:
        return (IN_PLACE_STATE_CHANGES,)
    if placements is None or placements.status is PlacementStatus.ABSENT:
        return (IN_PLACE_CATALOG_ABSENT,)
    objection = _catalogue_objection(placements, session_id, local.relative_path, chain)
    if objection is not None:
        return objection
    if (session_id in placements.archived) != (local.state is SessionState.ARCHIVED):
        return (IN_PLACE_ARCHIVE_FLAG_DIFFERS,)
    return None


def _catalogue_objection(
    placements: ThreadPlacements | None,
    session_id: str,
    target: str,
    chain: frozenset[str] = frozenset(),
) -> tuple[str, ...] | None:
    """Why the runtime would not see a branch written at ``target``, if it would not.

    The catalogue is treated as advisory about *other* branches — a second file
    it does not mention is never an orphan to delete — but as authoritative
    about whether our own write will be found. A session it does not list at
    all would need a new row, and codexSync does not write these databases.

    ``chain`` holds the other files of the same chat on this machine. A chat
    continued in pages is listed by one of them, and Codex reaches the others
    through `history_base`, so a row naming any part of the chain places the
    whole chat (CS-356).
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
    if recorded != target and recorded not in chain:
        return ("CATALOG_PLACES_ELSEWHERE",)
    return None


def _other_part_code(
    placements: ThreadPlacements | None, session_id: str, target: str, chain: frozenset[str]
) -> tuple[str, ...]:
    """`CATALOG_NAMES_OTHER_PART` when the catalogue accepted a write through the chain."""
    if placements is None or placements.status is not PlacementStatus.AVAILABLE:
        return ()
    recorded = placements.placement_of(session_id)
    if recorded is not None and recorded != target and recorded in chain:
        return (CATALOG_NAMES_OTHER_PART,)
    return ()


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
    not an instruction for this one. `SAME_PATH_LAYOUT_ID` is the one exception,
    and only because a person asked for it (`[semantic] new_chats`).

    The logical name is used, never the stored one. A branch coming back out of
    the cloud mirror is stored in a container there, and the Codex runtime
    reads plain JSONL: a destination still carrying `.xz` would be a session
    the runtime never sees, with no error anywhere.

    The template is rendered with ``state``, ``source_dir``, ``file_name`` and
    ``session_id``; empty segments collapse, so one template can describe a
    date-partitioned ``sessions/`` tree and a flat ``archived_sessions/`` one.
    """
    template = _layout_template(layout_id)
    if template is None:
        raise FailSafeError(
            f"Session layout {layout_id!r} is not proven, so a destination path cannot be chosen. "
            "Run the controlled experiment in docs/dev/experiments/session-layout-adapter.md."
        )
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
            raw.get("scope_matches_nothing") is True,
            str(raw.get("conflict_rule", RULE_ASK)),
            str(raw.get("direction", "bidirectional")),
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
        # The same rule: present only when it says something, so no plan
        # built before it existed changes its id.
        **({"scope_matches_nothing": True} if plan.scope_matches_nothing else {}),
        # Both left out at their defaults, for the same reason.
        **({"conflict_rule": plan.conflict_rule} if plan.conflict_rule != RULE_ASK else {}),
        **({"direction": plan.direction} if plan.direction != "bidirectional" else {}),
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
        plan.mirror_layout_id, plan.scope, plan.scope_matches_nothing,
        plan.conflict_rule, plan.direction,
    )


def descriptors_by_session_hash(catalog: SessionCatalog) -> dict[str, SessionDescriptor]:
    """Map an item's session hash to its descriptor, so a plan need not carry file names.

    The hash is of the `branch_key`: the session id for the file a chat begins
    in, and the id plus its page for a page (CS-356).
    """
    return {
        hashlib.sha256(key.encode("utf-8")).hexdigest(): descriptor
        for key, descriptor in _by_branch_key(catalog).items()
    }


def _groups_by_branch(catalog: SessionCatalog) -> dict[str, tuple[SessionDescriptor, ...]]:
    """Every descriptor that names a session id, valid or not, per `branch_key`."""
    groups: dict[str, list[SessionDescriptor]] = {}
    for descriptor in catalog.descriptors:
        if STALE_DUPLICATE_BY_CATALOG in descriptor.codes:
            # Not a branch: the catalogue settled which copy is (CS-348). The
            # file still occupies its path through `_destination_check`.
            continue
        if descriptor.branch_key:
            groups.setdefault(descriptor.branch_key, []).append(descriptor)
    return {key: tuple(items) for key, items in groups.items()}


def _idless_codes(catalog: SessionCatalog, side: str) -> tuple[str, ...]:
    """Whether a branch file's session id could not be read at all.

    Such a file belongs to no item, because an item is a session, but it is
    still a file a copy could land on, and the plan says it is there.
    """
    if any(not descriptor.session_id for descriptor in catalog.descriptors):
        return (f"{side}_BRANCH_WITHOUT_ID",)
    return ()


def _unusable_item(
    session_hash: str,
    local_all: tuple[SessionDescriptor, ...],
    remote_all: tuple[SessionDescriptor, ...],
) -> TransferItem | None:
    """The blocked item for a session one of whose copies cannot be used, if any."""
    codes: list[str] = []
    for side, group in (("LOCAL", local_all), ("REMOTE", remote_all)):
        if len(group) > 1:
            codes.append(f"{side}_{DUPLICATE_SESSION_ID}")
        elif group and group[0].state in {SessionState.INVALID, SessionState.AMBIGUOUS}:
            codes.append(f"{side}_BRANCH_INVALID")
            codes.extend(code for code in group[0].codes if code in INVALID_CODES)
    if not codes:
        return None
    return TransferItem(
        session_hash, BranchRelation.INVALID, TransferAction.BLOCKED_INVALID_BRANCH,
        _group_sha256(local_all), _group_sha256(remote_all),
        local_all[0].line_count if len(local_all) == 1 else 0,
        remote_all[0].line_count if len(remote_all) == 1 else 0,
        None, None, tuple(dict.fromkeys(codes)),
    )


def _group_sha256(group: tuple[SessionDescriptor, ...]) -> str:
    """One copy's branch hash, or one hash over several copies.

    Either way a change to any copy changes the plan id, so a plan confirmed
    while a file was broken is not applied after it was repaired.
    """
    if not group:
        return ""
    if len(group) == 1:
        return group[0].sha256
    material = "\0".join(sorted(descriptor.sha256 for descriptor in group))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _destination_check(
    local_root: Path, remote_root: Path, local_catalog: SessionCatalog, remote_catalog: SessionCatalog
) -> Callable[[str, str], bool]:
    """Whether a destination already holds a file, in any container. Reads only.

    Asked only for a session the destination side does not hold, so anything
    found there belongs to something else, which the copy must not replace.
    The catalogue is consulted first and the disk second: a file the catalogue
    skipped is still a file.
    """
    known = {
        "local": {logical_relative_path(item.relative_path) for item in local_catalog.descriptors},
        "mirror": {logical_relative_path(item.relative_path) for item in remote_catalog.descriptors},
    }
    roots = {"local": local_root, "mirror": remote_root}

    def occupied(side: str, target: str) -> bool:
        logical = logical_relative_path(target)
        if logical in known[side]:
            return True
        return any(
            os.path.lexists(roots[side] / _os_path(with_codec(logical, codec))) for codec in JsonlCodec
        )

    return occupied


def _by_branch_key(catalog: SessionCatalog) -> dict[str, SessionDescriptor]:
    out: dict[str, SessionDescriptor] = {}
    for descriptor in catalog.valid:
        if descriptor.branch_key:
            # A duplicate inside one catalog is a branch the catalog already
            # flagged; the first descriptor wins here and the rest surface as
            # catalog codes rather than being silently merged.
            out.setdefault(descriptor.branch_key, descriptor)
    return out


def _branch_state(state: SessionState) -> BranchState:
    return BranchState.ARCHIVED if state is SessionState.ARCHIVED else BranchState.ACTIVE


def _os_path(relative: str) -> Path:
    return Path(*relative.split("/"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
