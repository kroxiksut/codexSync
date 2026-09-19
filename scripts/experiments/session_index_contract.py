"""Build the fixture for the session_index.jsonl consumer-contract experiment.

The fixture contains one session id written twice, with the *later* line
carrying the *older* ``updated_at``. That is the only shape where the two
plausible reductions disagree, so whichever thread name Codex displays tells us
which reduction it uses:

    displayed "LASTLINE-..."  -> the consumer reads last line wins
    displayed "MAXUPDATED-..." -> the consumer reads max updated_at

This script only writes a fixture file to a path you choose. It never reads,
writes or touches a real Codex state directory; moving the fixture into place
is a manual step, on disposable state, documented in
docs/dev/experiments/session-index-contract.md.

Usage:
    python scripts/experiments/session_index_contract.py --output fixture.jsonl
    python scripts/experiments/session_index_contract.py --output fixture.jsonl --session-id <existing-id>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import uuid


MARKER_LAST_LINE = "LASTLINE"
MARKER_MAX_UPDATED = "MAXUPDATED"


def build_fixture(session_id: str) -> bytes:
    """Two records for one id; the reductions disagree by construction."""
    token = uuid.uuid4().hex[:8]
    # Written first, but holds the greater updated_at.
    first = {
        "id": session_id,
        "thread_name": f"{MARKER_MAX_UPDATED}-{token}",
        "updated_at": "2999-01-01T00:00:00.000Z",
    }
    # Written last, but holds the older updated_at.
    last = {
        "id": session_id,
        "thread_name": f"{MARKER_LAST_LINE}-{token}",
        "updated_at": "2000-01-01T00:00:00.000Z",
    }
    return b"".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for record in (first, last)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, help="Where to write the fixture (never inside .codex)")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Existing session id to test with; omit to generate a synthetic one",
    )
    args = parser.parse_args(argv)

    session_id = args.session_id or str(uuid.uuid4())
    output = Path(args.output).expanduser().resolve()
    if ".codex" in output.parts:
        parser.error("refusing to write the fixture inside a .codex directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_fixture(session_id))

    print(f"Fixture written: {output}")
    print(f"Session id: {session_id}")
    print()
    print("Next: follow docs/dev/experiments/session-index-contract.md.")
    print(f"  If Codex shows a name starting with {MARKER_LAST_LINE}-  -> reduction is last-line-wins")
    print(f"  If Codex shows a name starting with {MARKER_MAX_UPDATED}- -> reduction is max-updated-at")
    print("  If it shows neither, or both entries appear, record that verbatim: the")
    print("  contract is something else and must stay unproven.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
