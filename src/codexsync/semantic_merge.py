"""Branch-preserving comparison for immutable JSONL session histories.

Two machines can hold two different continuations of the same session id. The
job here is to say precisely which of them is an ancestor of the other, and to
refuse to guess when neither is — never to produce a merged history. A record is
compared by its raw bytes; canonical JSON equality is consulted only to avoid
declaring a false divergence when two records differ solely in key order, and
only for records whose canonical form is provably unambiguous.

Nothing in this module writes: it reads two files and returns a classification.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import unicodedata


class BranchRelation(str, Enum):
    #: Same records in the same order.
    IDENTICAL = "IDENTICAL"
    #: Local is a strict prefix of remote: local may fast-forward to remote.
    FAST_FORWARD_LOCAL = "FAST_FORWARD_LOCAL"
    #: Remote is a strict prefix of local: remote may fast-forward to local.
    FAST_FORWARD_REMOTE = "FAST_FORWARD_REMOTE"
    #: Ancestry proven and the two sides disagree only about active/archived.
    ARCHIVE_TRANSITION = "ARCHIVE_TRANSITION"
    #: A shared prefix, then records that differ. Never auto-resolved.
    DIVERGED = "DIVERGED"
    #: Same session id, not one record in common. Observed in the wild.
    DIVERGED_NO_COMMON_RECORDS = "DIVERGED_NO_COMMON_RECORDS"
    #: A branch cannot be compared safely (unreadable, oversized, truncated).
    INVALID = "INVALID"
    #: The decision needs a confirmed common base and none is available.
    MISSING_BASE = "MISSING_BASE"


class BranchState(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class CanonicalStatus(str, Enum):
    #: Every compared record canonicalised unambiguously.
    EXACT = "EXACT"
    #: At least one record could not be canonicalised; raw bytes decided it.
    INDETERMINATE = "INDETERMINATE"


#: Version of the canonicalisation rules below. A change to what counts as
#: unambiguous changes what may be collapsed, so it is versioned explicitly and
#: recorded next to any decision that relied on it.
CANONICAL_DIGEST_VERSION = "canonical-json-v1"


@dataclass(frozen=True, slots=True)
class BranchComparison:
    relation: BranchRelation
    common_records: int
    local_records: int
    remote_records: int
    local_sha256: str
    remote_sha256: str
    #: Records that matched only after canonicalisation (raw bytes differed).
    canonical_only_matches: int = 0
    canonical_status: CanonicalStatus = CanonicalStatus.EXACT
    canonical_version: str = CANONICAL_DIGEST_VERSION
    detail: str = ""

    @property
    def is_conflict(self) -> bool:
        return self.relation in {
            BranchRelation.DIVERGED,
            BranchRelation.DIVERGED_NO_COMMON_RECORDS,
            BranchRelation.INVALID,
            BranchRelation.MISSING_BASE,
        }


def canonical_digest(raw: bytes) -> str | None:
    """Digest of a record's meaning, or ``None`` when that is not provable.

    Returning ``None`` is the safe answer: the caller then falls back to raw
    bytes, so two genuinely different records can never collapse into one. A
    record is refused when it is not a JSON object, or contains a float (whose
    textual form is not a reliable identity), or a string that is not already
    NFC-normalised (where two spellings would compare equal to a human and
    unequal to the runtime, or the reverse).
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not _canonicalisable(value):
        return None
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(CANONICAL_DIGEST_VERSION.encode("ascii") + b"\0" + encoded).hexdigest()


def _canonicalisable(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        # 1.0 vs 1 vs 1e0 are one value in JSON and three spellings on disk.
        return False
    if isinstance(value, str):
        return unicodedata.is_normalized("NFC", value)
    if isinstance(value, list):
        return all(_canonicalisable(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and unicodedata.is_normalized("NFC", key) and _canonicalisable(item)
            for key, item in value.items()
        )
    return False


def compare_session_branches(
    local: Path,
    remote: Path,
    *,
    local_state: BranchState = BranchState.ACTIVE,
    remote_state: BranchState = BranchState.ACTIVE,
    has_confirmed_base: bool = True,
    max_line_bytes: int = 64 * 1024 * 1024,
) -> BranchComparison:
    """Classify two branches of one session, streaming both files.

    ``has_confirmed_base`` reports whether the semantic manifest holds a
    verified common ancestor for this session. It is required only for a
    decision that rests on ancestry — an active/archived transition — because
    accepting one there without a base would be a two-way newer-wins guess.
    """
    try:
        reading = _read_pair(local, remote, max_line_bytes)
    except (OSError, ValueError) as exc:
        return BranchComparison(
            BranchRelation.INVALID, 0, 0, 0, "", "", detail=f"branch unreadable: {exc}"
        )

    common, local_count, remote_count, local_sha, remote_sha, canonical_only, status = reading

    prefix_local = local_count < remote_count and common == local_count
    prefix_remote = remote_count < local_count and common == remote_count
    same_content = local_count == remote_count and common == local_count

    def result(relation: BranchRelation, detail: str = "") -> BranchComparison:
        return BranchComparison(
            relation, common, local_count, remote_count, local_sha, remote_sha,
            canonical_only, status, CANONICAL_DIGEST_VERSION, detail,
        )

    states_differ = local_state is not remote_state
    ancestry_proven = same_content or prefix_local or prefix_remote

    if states_differ and ancestry_proven:
        if not has_confirmed_base:
            return result(
                BranchRelation.MISSING_BASE,
                "active/archive transition needs a confirmed common base",
            )
        return result(
            BranchRelation.ARCHIVE_TRANSITION,
            f"local={local_state.value} remote={remote_state.value}",
        )
    if states_differ:
        # Divergent content plus disagreeing states is always a conflict.
        return result(
            BranchRelation.DIVERGED if common else BranchRelation.DIVERGED_NO_COMMON_RECORDS,
            "states disagree without proven ancestry",
        )
    if same_content:
        return result(BranchRelation.IDENTICAL)
    if prefix_local:
        return result(BranchRelation.FAST_FORWARD_LOCAL)
    if prefix_remote:
        return result(BranchRelation.FAST_FORWARD_REMOTE)
    if common == 0:
        return result(BranchRelation.DIVERGED_NO_COMMON_RECORDS)
    return result(BranchRelation.DIVERGED)


def _read_pair(
    local: Path, remote: Path, max_line_bytes: int
) -> tuple[int, int, int, str, str, int, CanonicalStatus]:
    local_digest = hashlib.sha256()
    remote_digest = hashlib.sha256()
    common = local_count = remote_count = canonical_only = 0
    diverged = False
    status = CanonicalStatus.EXACT

    with local.open("rb") as local_handle, remote.open("rb") as remote_handle:
        while True:
            local_line = _readline(local_handle, max_line_bytes)
            remote_line = _readline(remote_handle, max_line_bytes)
            if local_line:
                local_count += 1
                local_digest.update(local_line)
            if remote_line:
                remote_count += 1
                remote_digest.update(remote_line)

            if local_line and remote_line and not diverged:
                if local_line == remote_line:
                    common += 1
                else:
                    left_canonical = canonical_digest(local_line.rstrip(b"\r\n"))
                    right_canonical = canonical_digest(remote_line.rstrip(b"\r\n"))
                    if left_canonical is None or right_canonical is None:
                        # Raw bytes already said "different"; say so, and record
                        # that meaning could not be compared.
                        status = CanonicalStatus.INDETERMINATE
                        diverged = True
                    elif left_canonical == right_canonical:
                        common += 1
                        canonical_only += 1
                    else:
                        diverged = True

            if not local_line and not remote_line:
                break
            if not local_line:
                remote_count += _drain(remote_handle, remote_digest, max_line_bytes)
                break
            if not remote_line:
                local_count += _drain(local_handle, local_digest, max_line_bytes)
                break

    return (
        common, local_count, remote_count,
        local_digest.hexdigest(), remote_digest.hexdigest(), canonical_only, status,
    )


def _readline(handle, max_line_bytes: int) -> bytes:
    line = handle.readline(max_line_bytes + 1)
    if len(line) > max_line_bytes:
        raise ValueError("Session record exceeds configured maximum line size")
    return line


def _drain(handle, digest, max_line_bytes: int) -> int:
    count = 0
    while True:
        line = _readline(handle, max_line_bytes)
        if not line:
            return count
        count += 1
        digest.update(line)
