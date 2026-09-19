# Experiment: the `session_index.jsonl` consumer contract

## Status

**Not yet run.** `PROVEN_CONTRACTS` in `src/codexsync/session_index.py` is empty,
so codexSync parses and audits the index but refuses to render a new one.

Reading is not gated and is already wired: `sessions index` reports what each
side's index holds and where the two disagree, and `doctor` carries the same
check. Both say `UNPROVEN_CONSUMER_CONTRACT`, which is this experiment. What
stays blocked is only the write — merging two indexes into a new file.

## Why this cannot be settled by reading code

`session_index.jsonl` is an append/update journal: the same session id may
appear on several lines. To merge two machines' indexes we must know which line
the Codex runtime actually honours. Two readings are plausible:

- **last line wins** — file order is the authority;
- **max `updated_at`** — the timestamp is the authority.

They agree on ordinary files, which is why the question is easy to overlook.
They disagree exactly when a clock ran backwards — and a clock that ran backwards
between two machines is the whole reason this project exists. Picking the wrong
one silently discards a rename, so codexSync treats the contract as unproven
until the runtime is observed, rather than defaulting to the likelier guess.

Nothing in the file format announces the answer, and codexSync deliberately does
not read Codex source, APIs or internals (`AI_RULES.md`). The only honest way to
learn it is to show the runtime a file where the two readings differ and see
which one it picks.

## Safety rules for this run

- Use **disposable state**. Either a scratch machine, or a full backup you are
  willing to restore from. Do not run this against state you care about.
- Codex must be **closed** for every step that touches files.
- Take a Guardian snapshot first; it is the fastest way back.
- The fixture generator never touches `.codex`; every move into place is manual
  and yours.

## Procedure

1. Close Codex completely (including the background sandbox process).

2. Take a snapshot and confirm it committed:

   ```powershell
   python -m codexsync -c config.toml guardian snapshot --once
   ```

3. Copy the whole state directory somewhere safe:

   ```powershell
   Copy-Item -Recurse $env:USERPROFILE\.codex "$env:USERPROFILE\.codex-experiment-backup"
   ```

4. Generate the fixture. Pass `--session-id` with an id that already exists in
   your index if you want the session to be visible in the UI; otherwise a
   synthetic id is used.

   ```powershell
   python scripts/experiments/session_index_contract.py --output fixture.jsonl --session-id <existing-id>
   ```

   The fixture writes the same id twice: the **first** line carries a far-future
   `updated_at` and a name starting `MAXUPDATED-`, the **last** line carries an
   old `updated_at` and a name starting `LASTLINE-`.

5. Replace the index with the fixture. Keep the original:

   ```powershell
   Move-Item $env:USERPROFILE\.codex\session_index.jsonl $env:USERPROFILE\.codex\session_index.jsonl.orig
   Copy-Item fixture.jsonl $env:USERPROFILE\.codex\session_index.jsonl
   ```

6. Start Codex and look at the name shown for that session.

7. Record the result, then close Codex and restore:

   ```powershell
   Remove-Item $env:USERPROFILE\.codex\session_index.jsonl
   Move-Item $env:USERPROFILE\.codex\session_index.jsonl.orig $env:USERPROFILE\.codex\session_index.jsonl
   ```

## Recording the result

| Observation | Meaning | What to record |
|---|---|---|
| Name starts `LASTLINE-` | File order is the authority | `Reduction.LAST_LINE_WINS` |
| Name starts `MAXUPDATED-` | The timestamp is the authority | `Reduction.MAX_UPDATED_AT` |
| Both entries appear separately | Not a reduction at all | Leave unproven; write down what you saw |
| Neither appears | The index is not the display source | Leave unproven; write down what you saw |

Only the first two outcomes unlock rendering. Add the entry to
`PROVEN_CONTRACTS` in `src/codexsync/session_index.py`:

```python
PROVEN_CONTRACTS: dict[IndexContract, Reduction] = {
    IndexContract.V1: Reduction.LAST_LINE_WINS,  # observed <date>, Codex <version>, Windows
}
```

Record the Codex version and OS in that comment. The contract is versioned by
runtime family: a later Codex that changes this behaviour needs its own run, and
until then the recorded result only speaks for the version it was observed on.

An inconclusive run is a real result. Write it down here and leave the gate
closed — CS-224 can still transfer session branches; only rewriting the index is
blocked.

## Result log

| Date | Codex version | OS | Observation | Recorded contract |
|---|---|---|---|---|
| _pending_ | | | | |
