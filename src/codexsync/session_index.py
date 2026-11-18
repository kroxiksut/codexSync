"""Parser and conservative three-way merge for ``session_index.jsonl``.

The index is an append/update journal: an id may appear on several lines, a
session may exist with no line at all, and a line may exist for a session file
that is gone.  None of that is an error, and none of it may be "cleaned up" —
this module never decides a session exists or stops existing.

What the file cannot tell us is how the Codex runtime *reads* it.  Two
reductions are plausible for a repeated id — the last line in file order wins,
or the record with the greatest ``updated_at`` wins — and they disagree exactly
when a clock went backwards, which is the case that matters between two
machines.  Guessing would silently rewrite history, so both reductions are
computed, disagreement is reported, and rendering a new index is refused until
the real behaviour is recorded in ``PROVEN_CONTRACTS`` by the controlled
experiment in ``docs/dev/experiments/session-index-contract.md``.

Parsing and auditing stay available meanwhile: reading is always safe, only
writing is gated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re

from .exceptions import FailSafeError


#: Name of the index inside a Codex state directory, and of codexSync's own
#: copy of it in the cloud root. Named once so a check, an audit and the
#: semantic-owned path list cannot drift apart.
SESSION_INDEX_FILE = "session_index.jsonl"


class IndexContract(str, Enum):
    #: Records are objects carrying string ``id``/``thread_name``/``updated_at``.
    #: Recognising the shape says nothing about how a consumer reduces them.
    V1 = "v1"
    UNKNOWN = "unknown"


class Reduction(str, Enum):
    LAST_LINE_WINS = "last-line-wins"
    MAX_UPDATED_AT = "max-updated-at"


#: The two readings of a repeated id disagree, or one of them is undefined.
REDUCTION_AMBIGUOUS = "REDUCTION_AMBIGUOUS"
#: An ``updated_at`` that is not a moment with a zone, so it cannot be ordered.
UPDATED_AT_UNREADABLE = "UPDATED_AT_UNREADABLE"
#: Two different records of one id carry the same moment.
UPDATED_AT_TIE = "UPDATED_AT_TIE"


#: Contracts whose consumer semantics have been proven by a controlled test on
#: disposable state, together with the reduction that test observed.
#:
#: Deliberately empty. Until an entry is added here — by running the experiment
#: in ``docs/dev/experiments/session-index-contract.md`` and recording its result —
#: codexSync will parse and audit an index but refuse to render a new one.
PROVEN_CONTRACTS: dict[IndexContract, Reduction] = {}


@dataclass(frozen=True, slots=True)
class IndexRecord:
    session_id: str
    #: Never logged and never placed in a manifest: it is user content.
    thread_name: str = field(repr=False)
    updated_at: str
    digest: str
    raw: bytes = field(repr=False)
    line_number: int = 0


@dataclass(frozen=True, slots=True)
class IndexParseResult:
    records: tuple[IndexRecord, ...]
    #: Reduction under ``LAST_LINE_WINS``; the historical default view.
    reduced: dict[str, IndexRecord]
    #: Reduction under ``MAX_UPDATED_AT``, kept so the two can be compared.
    reduced_by_updated_at: dict[str, IndexRecord]
    contract: IndexContract
    codes: tuple[str, ...] = ()
    raw_tail_digest: str | None = None
    #: Why ``reduced_by_updated_at`` is not a defined reading: an ``updated_at``
    #: that is not a moment, or two different records at the same moment.
    #: Kept apart from ``codes`` because it matters only under that reduction.
    updated_at_codes: tuple[str, ...] = ()

    @property
    def reductions_agree(self) -> bool:
        """True when the choice of reduction cannot change the outcome."""
        if self.updated_at_codes:
            return False
        if set(self.reduced) != set(self.reduced_by_updated_at):
            return False
        return all(
            self.reduced[key].digest == self.reduced_by_updated_at[key].digest
            for key in self.reduced
        )

    def reduced_under(self, reduction: Reduction) -> dict[str, IndexRecord]:
        """The reading a consumer that reduces this way would see."""
        if reduction is Reduction.MAX_UPDATED_AT:
            return self.reduced_by_updated_at
        return self.reduced


@dataclass(frozen=True, slots=True)
class IndexMergeResult:
    merged: dict[str, IndexRecord]
    conflicts: tuple[str, ...]
    codes: tuple[str, ...] = ()
    contract: IndexContract = IndexContract.UNKNOWN


def parse_session_index(
    path: Path,
    *,
    contract: IndexContract | None = None,
) -> IndexParseResult:
    """Read an index without judging it.

    ``contract`` is normally detected from the records; pass it only to force a
    reading in a test.  A file that cannot be recognised still parses: the
    caller gets the records, the codes and an ``UNKNOWN`` contract.
    """
    records: list[IndexRecord] = []
    codes: list[str] = []
    tail_digest = None
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        # An absent index is not an absent set of sessions.
        return IndexParseResult((), {}, {}, contract or IndexContract.UNKNOWN, ("MISSING_INDEX",))

    lines = payload.splitlines(keepends=True)
    if not payload.strip():
        # A file that exists but holds nothing says exactly what an absent one
        # says: no session has a line yet. Calling that an unrecognised contract
        # would warn about a freshly created index.
        return IndexParseResult((), {}, {}, contract or IndexContract.UNKNOWN, ("EMPTY_INDEX",))
    recognised = bool(lines)
    for number, line in enumerate(lines, 1):
        complete = line.endswith((b"\n", b"\r"))
        raw = line.rstrip(b"\r\n")
        if number == len(lines) and not complete:
            # A half-written last line is evidence, not garbage: keep its digest
            # so the raw tail can be quarantined instead of dropped.
            tail_digest = hashlib.sha256(raw).hexdigest()
            codes.append("INCOMPLETE_TAIL")
            continue
        if not raw.strip():
            continue
        try:
            item = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            codes.append("INVALID_RECORD")
            recognised = False
            continue
        if not isinstance(item, dict):
            codes.append("RECORD_NOT_OBJECT")
            recognised = False
            continue
        session_id, thread_name, updated_at = item.get("id"), item.get("thread_name"), item.get("updated_at")
        if not all(isinstance(value, str) and value for value in (session_id, thread_name, updated_at)):
            codes.append("INVALID_REQUIRED_FIELDS")
            recognised = False
            continue
        # `raw` is stored verbatim, so unknown fields survive untouched.
        records.append(
            IndexRecord(session_id, thread_name, updated_at, hashlib.sha256(raw).hexdigest(), raw, number)
        )

    detected = contract if contract is not None else (
        IndexContract.V1 if recognised and records else IndexContract.UNKNOWN
    )
    if detected is IndexContract.UNKNOWN:
        codes.append("UNRECOGNISED_CONSUMER_CONTRACT")

    reduced, _ = _reduce(records, Reduction.LAST_LINE_WINS)
    by_updated, updated_codes = _reduce(records, Reduction.MAX_UPDATED_AT)
    result = IndexParseResult(
        tuple(records), reduced, by_updated, detected, tuple(dict.fromkeys(codes)), tail_digest,
        updated_codes,
    )
    if not result.reductions_agree:
        # The two plausible readings disagree, which is precisely a clock that
        # ran backwards. Recorded so materialization cannot proceed on a guess.
        codes.append(REDUCTION_AMBIGUOUS)
        result = IndexParseResult(
            tuple(records), reduced, by_updated, detected,
            tuple(dict.fromkeys(codes)), tail_digest, updated_codes,
        )
    return result


def merge_session_indexes(
    base: IndexParseResult | None,
    local: IndexParseResult,
    remote: IndexParseResult,
    *,
    reduction: Reduction | None = None,
) -> IndexMergeResult:
    """Three-way merge against a confirmed common base.

    A side that did not change takes the other side's record; an identical
    change on both sides deduplicates; two different new records for one id are
    a conflict that keeps both candidates where they already are.  Timestamps
    are never used to pick a winner between the sides — that is what
    ``updated_at`` cannot be trusted for across two machines.

    Each side is read the way the runtime reads it: by the reduction recorded
    in ``PROVEN_CONTRACTS`` (``reduction`` overrides it, for a test). Until one
    is recorded the last-line reading is used for the audit, and a side whose
    two readings disagree keeps ``REDUCTION_AMBIGUOUS``, which refuses any
    render. Once proven, that ambiguity is settled and no longer carried --
    except where the proven reading itself is undefined (CS-521).
    """
    if base is None:
        return IndexMergeResult({}, (), ("MISSING_BASE",))
    inputs = (base, local, remote)
    if any(result.contract is not IndexContract.V1 for result in inputs):
        return IndexMergeResult({}, (), ("UNRECOGNISED_CONSUMER_CONTRACT",))

    proven = reduction if reduction is not None else PROVEN_CONTRACTS.get(IndexContract.V1)
    chosen = proven or Reduction.LAST_LINE_WINS
    codes: list[str] = []
    for result in inputs:
        codes.extend(
            code for code in result.codes
            if code != "MISSING_INDEX" and not (proven is not None and code == REDUCTION_AMBIGUOUS)
        )
        if proven is Reduction.MAX_UPDATED_AT:
            codes.extend(result.updated_at_codes)

    base_view, local_view, remote_view = (result.reduced_under(chosen) for result in inputs)
    merged: dict[str, IndexRecord] = {}
    conflicts: list[str] = []
    for session_id in sorted(set(base_view) | set(local_view) | set(remote_view)):
        old = base_view.get(session_id)
        left = local_view.get(session_id)
        right = remote_view.get(session_id)
        if _same(left, right):
            if left is not None:
                merged[session_id] = left
        elif _same(left, old):
            if right is not None:
                merged[session_id] = right
        elif _same(right, old):
            if left is not None:
                merged[session_id] = left
        else:
            # Only a hash-safe identifier leaves this function; both raw
            # candidates stay in their input observations and are never dropped.
            conflicts.append(hashlib.sha256(session_id.encode("utf-8")).hexdigest())
    if conflicts:
        codes.append("INDEX_CONFLICT")
    return IndexMergeResult(merged, tuple(conflicts), tuple(dict.fromkeys(codes)), IndexContract.V1)


def render_session_index(result: IndexMergeResult) -> bytes:
    """Render a merged index, or refuse.

    Refuses unless the contract's consumer semantics are recorded in
    ``PROVEN_CONTRACTS``. Writing an index the runtime may read differently
    than we do can silently discard renames, so an unproven contract is a
    fail-safe stop rather than a best effort.

    Writing the bytes is deliberately not done here: replacing the real index
    is a mutation and belongs in the cold apply pipeline, behind the operation
    lock, backup-first and a post-write re-parse.
    """
    if result.codes or result.conflicts:
        raise FailSafeError(
            "Cannot render a session index that is conflicted or indeterminate: "
            + ", ".join(result.codes or ("INDEX_CONFLICT",))
        )
    if result.contract not in PROVEN_CONTRACTS:
        raise FailSafeError(
            f"Consumer contract {result.contract.value} is not proven, so a new session index "
            "cannot be rendered. Run the controlled experiment in "
            "docs/dev/experiments/session-index-contract.md and record its result in PROVEN_CONTRACTS."
        )
    # One line per id, ordered by time. Line numbers come from two different
    # files and say nothing about each other, so they cannot order a merge
    # (CS-521); a record whose moment is unreadable goes last, by its text.
    ordered = sorted(result.merged.values(), key=_render_order)
    return b"".join(record.raw + b"\n" for record in ordered)


#: Retained under the previous name; both names refuse on an unproven contract.
materialize_index = render_session_index


def _reduce(
    records: list[IndexRecord], reduction: Reduction
) -> tuple[dict[str, IndexRecord], tuple[str, ...]]:
    """One record per id under ``reduction``, and why that reading is undefined.

    ``updated_at`` is compared as a moment, never as text (the class of
    CS-466): `+03:00` against `Z`, or 6 against 7 fractional digits, orders
    text differently from time. A value that is not a moment, or two different
    records at one moment, leaves the max reading undefined, which is said
    rather than settled by file order.
    """
    out: dict[str, IndexRecord] = {}
    codes: list[str] = []
    moments: dict[str, tuple[datetime, int] | None] = {}
    for record in records:
        if reduction is Reduction.LAST_LINE_WINS:
            out[record.session_id] = record
            continue
        moment = _updated_moment(record.updated_at)
        if moment is None:
            codes.append(UPDATED_AT_UNREADABLE)
        current = out.get(record.session_id)
        if current is None:
            out[record.session_id], moments[record.session_id] = record, moment
            continue
        held = moments[record.session_id]
        if moment is None or held is None:
            continue
        if moment > held:
            out[record.session_id], moments[record.session_id] = record, moment
        elif moment == held and record.digest != current.digest:
            codes.append(UPDATED_AT_TIE)
    return out, tuple(dict.fromkeys(codes))


_MOMENT = re.compile(
    r"^(?P<base>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(?P<fraction>\d{1,9}))?(?P<zone>Z|[+-]\d\d:\d\d)$"
)


def _updated_moment(value: str) -> tuple[datetime, int] | None:
    """``updated_at`` as (moment to the second, nanoseconds), or ``None``.

    Codex writes seven fractional digits (`05:09:19.7567634Z`), one more than
    `datetime` keeps, so the fraction is held as an integer of nanoseconds
    instead of being truncated into a false tie. A moment without a zone is
    not one two machines can compare.
    """
    match = _MOMENT.match(value)
    if match is None:
        return None
    zone = "+00:00" if match["zone"] == "Z" else match["zone"]
    try:
        moment = datetime.fromisoformat(match["base"] + zone).astimezone(timezone.utc)
    except ValueError:
        return None
    return moment, int((match["fraction"] or "").ljust(9, "0"))


def _render_order(record: IndexRecord) -> tuple:
    moment = _updated_moment(record.updated_at)
    if moment is None:
        return (1, record.updated_at, 0, record.session_id)
    return (0, moment[0].isoformat(), moment[1], record.session_id)


def _same(left: IndexRecord | None, right: IndexRecord | None) -> bool:
    return (left is None and right is None) or (
        left is not None and right is not None and left.digest == right.digest
    )
