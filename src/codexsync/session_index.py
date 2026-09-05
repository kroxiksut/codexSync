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
experiment in ``docs/experiments/session-index-contract.md``.

Parsing and auditing stay available meanwhile: reading is always safe, only
writing is gated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path

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


#: Contracts whose consumer semantics have been proven by a controlled test on
#: disposable state, together with the reduction that test observed.
#:
#: Deliberately empty. Until an entry is added here — by running the experiment
#: in ``docs/experiments/session-index-contract.md`` and recording its result —
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

    @property
    def reductions_agree(self) -> bool:
        """True when the choice of reduction cannot change the outcome."""
        if set(self.reduced) != set(self.reduced_by_updated_at):
            return False
        return all(
            self.reduced[key].digest == self.reduced_by_updated_at[key].digest
            for key in self.reduced
        )


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

    reduced = _reduce(records, Reduction.LAST_LINE_WINS)
    by_updated = _reduce(records, Reduction.MAX_UPDATED_AT)
    result = IndexParseResult(
        tuple(records), reduced, by_updated, detected, tuple(dict.fromkeys(codes)), tail_digest
    )
    if not result.reductions_agree:
        # The two plausible readings disagree, which is precisely a clock that
        # ran backwards. Recorded so materialization cannot proceed on a guess.
        codes.append("REDUCTION_AMBIGUOUS")
        result = IndexParseResult(
            tuple(records), reduced, by_updated, detected,
            tuple(dict.fromkeys(codes)), tail_digest,
        )
    return result


def merge_session_indexes(
    base: IndexParseResult | None,
    local: IndexParseResult,
    remote: IndexParseResult,
) -> IndexMergeResult:
    """Three-way merge against a confirmed common base.

    A side that did not change takes the other side's record; an identical
    change on both sides deduplicates; two different new records for one id are
    a conflict that keeps both candidates where they already are.  Timestamps
    are never used to pick a winner — that is what ``updated_at`` cannot be
    trusted for across two machines.
    """
    if base is None:
        return IndexMergeResult({}, (), ("MISSING_BASE",))
    inputs = (base, local, remote)
    if any(result.contract is not IndexContract.V1 for result in inputs):
        return IndexMergeResult({}, (), ("UNRECOGNISED_CONSUMER_CONTRACT",))

    codes: list[str] = []
    for result in inputs:
        codes.extend(code for code in result.codes if code != "MISSING_INDEX")

    merged: dict[str, IndexRecord] = {}
    conflicts: list[str] = []
    for session_id in sorted(set(base.reduced) | set(local.reduced) | set(remote.reduced)):
        old = base.reduced.get(session_id)
        left = local.reduced.get(session_id)
        right = remote.reduced.get(session_id)
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
            "docs/experiments/session-index-contract.md and record its result in PROVEN_CONTRACTS."
        )
    ordered = sorted(result.merged.values(), key=lambda item: (item.line_number, item.session_id))
    return b"".join(record.raw + b"\n" for record in ordered)


#: Retained under the previous name; both names refuse on an unproven contract.
materialize_index = render_session_index


def _reduce(records: list[IndexRecord], reduction: Reduction) -> dict[str, IndexRecord]:
    out: dict[str, IndexRecord] = {}
    for record in records:
        if reduction is Reduction.LAST_LINE_WINS:
            out[record.session_id] = record
            continue
        current = out.get(record.session_id)
        if current is None or record.updated_at > current.updated_at:
            out[record.session_id] = record
    return out


def _same(left: IndexRecord | None, right: IndexRecord | None) -> bool:
    return (left is None and right is None) or (
        left is not None and right is not None and left.digest == right.digest
    )
