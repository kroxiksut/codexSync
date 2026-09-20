# Experiment: where a transferred session branch must land

## Status

**Not yet run.** `PROVEN_LAYOUTS` in `src/codexsync/semantic_transfer.py` is
empty.

What that blocks is now narrower than it was. The gate covers writes into a
directory the **Codex runtime reads** — your `.codex` state directory — and
those are still reported as `BLOCKED_UNPROVEN_LAYOUT`. Writes towards the
**cloud mirror** are not gated: no Codex reads that copy, so a branch keeps the
relative path it already has (`MIRROR_LAYOUT_ID`, `codexsync-mirror-v1`), and
the mirror can be rebuilt today. Gating it as well left the mirror with no way
to be created at all, because sessions are excluded from generic mtime copying.

So this experiment is what unlocks the *return* direction: taking a branch from
the cloud copy back into a Codex state directory.

## Why this cannot be settled by reading code

A session file's path on the source machine records where that branch lived
*there*. It is evidence about the machine it came from, not an instruction for
the machine it is going to. Codex may key a session by its path, by a date
folder, by an id inside the file, or by a row in SQLite — and if we place a
transferred branch where the runtime does not look for it, the session is
silently invisible even though the bytes are safely on disk.

The failure is quiet, which is what makes guessing unacceptable: nothing errors,
the user simply cannot find their session.

There is a second outcome that matters just as much. If the runtime only picks
up a session after its SQLite catalogue is written, then transfer is not
supported at all in 0.2, because codexSync opens those databases read-only and
will not write them. That outcome must be recorded as
`UNSUPPORTED_STATE_BACKEND` rather than worked around.

## What the state directory actually looks like

Measured read-only on one machine, from the thread catalogue and from disk:

| Where | Count | Shape |
|---|---|---|
| active | 228 | `sessions/<year>/<month>/<day>/rollout-….jsonl` |
| archived | 23 | `archived_sessions/rollout-….jsonl` (flat) |

So the two state folders do not have the same shape, and a template able to
name only the folder and the file can describe the archived one only. Under
such a template all 228 active branches render to `sessions/<file>`, which is
not where the catalogue says they are: the write is refused as
`CATALOG_PLACES_ELSEWHERE`, and were it allowed the session would simply not
appear. That is why `source_dir` exists.

## Safety rules for this run

- Use **disposable state**, or a full backup you are willing to restore from.
- Codex must be **closed** for every step that touches files.
- Take a Guardian snapshot first.
- Never move a branch out of its source location. Every step below copies.

## Procedure

1. Close Codex completely, including the background sandbox process.

2. Snapshot and back up:

   ```powershell
   python -m codexsync -c config.toml guardian snapshot --once
   Copy-Item -Recurse $env:USERPROFILE\.codex "$env:USERPROFILE\.codex-experiment-backup"
   ```

3. Pick a session that exists on the other machine but not on this one — an
   item whose action is `BLOCKED_UNPROVEN_LAYOUT`, not one of the mirror writes,
   which already work. A scan names how many there are without revealing any
   ids:

   ```powershell
   python -m codexsync -c config.toml sessions scan --source-machine <A> --target-machine <B>
   ```

4. Copy one such session file from the cloud copy into the local state
   directory, keeping the same relative path it had on the source machine:

   ```powershell
   Copy-Item <cloud>\sessions\<file> $env:USERPROFILE\.codex\sessions\<file>
   ```

5. Start Codex. Look for that session in the session list.

6. Record the outcome, then close Codex and restore the backup.

## Variants to try, in order

Stop at the first variant that works and record it.

| Variant | Target path | Template if it works |
|---|---|---|
| A | same relative path as on the source | `"{state}/{source_dir}/{file_name}"` |
| B | same file name, today's date folder | not expressible; see below |
| C | file renamed to the session id | `"{state}/{session_id}.jsonl"` |

Variant B cannot be recorded as a template and must not be approximated by one.
A destination that depends on the day it is written makes the plan id change
underneath a confirmation already given — a plan built before midnight would be
refused after it, which is the freshness check doing its job for the wrong
reason. If B is the answer, record it here and leave `PROVEN_LAYOUTS` empty
until the layout is resolved at apply time rather than frozen in the plan.

If none works, try starting Codex, creating a brand new session, closing it, and
comparing where that file landed against the variants — the runtime's own choice
is the answer.

## Recording the result

If a variant works, add it to `PROVEN_LAYOUTS` in
`src/codexsync/semantic_transfer.py`:

```python
PROVEN_LAYOUTS: dict[str, str] = {
    # observed <date>, Codex <version>, Windows
    "v1-mirror": "{state}/{source_dir}/{file_name}",
}
```

The template is formatted with four values:

| Placeholder | Value |
|---|---|
| `state` | `sessions` or `archived_sessions`, chosen by where the branch belongs **here** |
| `source_dir` | the directory the branch sat in below its state folder on the source machine (`2026/03/12`, or empty) |
| `file_name` | the branch's logical name, always plain `.jsonl` even when the mirror stores it compressed |
| `session_id` | the id inside the file |

An empty segment disappears from the result, so the one template above renders
`sessions/2026/03/12/rollout-….jsonl` for an active branch and
`archived_sessions/rollout-….jsonl` for an archived one. Then run a plan with
`--layout-id v1-mirror`.

If the session appears only after Codex rewrites its SQLite catalogue, record
that here and leave `PROVEN_LAYOUTS` empty. Transfer then stays scan- and
plan-only for 0.2, which is the outcome the plan already anticipates.

Note the Codex version and OS: a layout is proven for the runtime family it was
observed on, and a later Codex needs its own run.

## Second question: does merely opening a chat write to it?

Once branches can land in `.codex`, a chat will often be carried to a machine
where its working folder does not exist — the plan marks those
`CWD_ABSENT_HERE` — and only read there. What the return transfer then does
depends on one fact nobody has observed yet:

| What Codex does to the file on open | What the next scan reports |
|---|---|
| nothing | `NOOP` |
| appends records (a resume writes a `session_meta` record) | `FAST_FORWARD` back to the other machine, harmless on its own; a conflict if that machine also continued the chat |
| rewrites anything before the end | a divergence, which blocks until a decision is recorded |

The answer decides whether "read it there, keep working here" is safe advice, so
it is measured, not assumed. Run it in the same session as the layout steps
above, on the transferred branch or on any chat on disposable state:

1. With Codex closed, record the file's hash, record count and last record:

   ```powershell
   $f = "$env:USERPROFILE\.codex\sessions\<path>\rollout-<…>.jsonl"
   (Get-FileHash $f -Algorithm SHA256).Hash; (Get-Content $f).Count; Get-Content $f -Tail 1
   ```

2. Start Codex, open that chat, scroll through it, send nothing, close Codex
   completely (including the background process).

3. Record the same three values again.

4. Repeat with a chat whose `cwd` does not exist on this machine. Do not rename
   or delete a real project folder to create that case: use a chat whose folder
   is genuinely absent, or a folder made for the experiment.

A changed hash is the finding, not a failure. Record which row of the table it
matched, and if records were appended, their `type` values — never their
contents.

## Result log

| Date | Codex version | OS | Variant | Outcome | Recorded layout |
|---|---|---|---|---|---|
| _pending_ | | | | | |

| Date | Codex version | OS | Chat folder present | Opened only: file changed? | Appended record types |
|---|---|---|---|---|---|
| _pending_ | | | | | |
