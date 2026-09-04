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

| Variant | Target path | Meaning if it works |
|---|---|---|
| A | same relative path as on the source | `"{state}/{file_name}"` |
| B | same file name, today's date folder | layout is date-partitioned by arrival |
| C | file renamed to the session id | the file name is the key |

If none works, try starting Codex, creating a brand new session, closing it, and
comparing where that file landed against the variants — the runtime's own choice
is the answer.

## Recording the result

If a variant works, add it to `PROVEN_LAYOUTS` in
`src/codexsync/semantic_transfer.py`:

```python
PROVEN_LAYOUTS: dict[str, str] = {
    # observed <date>, Codex <version>, Windows
    "v1-mirror": "{state}/{file_name}",
}
```

The template is formatted with `state` (`sessions` or `archived_sessions`) and
`file_name`. Then run a plan with `--layout-id v1-mirror`.

If the session appears only after Codex rewrites its SQLite catalogue, record
that here and leave `PROVEN_LAYOUTS` empty. Transfer then stays scan- and
plan-only for 0.2, which is the outcome the plan already anticipates.

Note the Codex version and OS: a layout is proven for the runtime family it was
observed on, and a later Codex needs its own run.

## Result log

| Date | Codex version | OS | Variant | Outcome | Recorded layout |
|---|---|---|---|---|---|
| _pending_ | | | | | |
