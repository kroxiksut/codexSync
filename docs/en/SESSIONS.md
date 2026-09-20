# Sessions

**English** · [Русский](../ru/SESSIONS.md) · [中文](../zh/SESSIONS.md)

[← Documentation](README.md)

A Codex session is a JSONL file — a history that only grows. When the same
session continues on two machines, the two copies become two *branches* of one
history. Copying the newer file over the older one would silently lose whatever
was added on the other machine, which is why sessions are never copied by plain
`sync`. In the window this is the [Sessions](GUI.md#sessions) screen.

## Scanning

`sessions scan` compares every session branch on this machine with the copy in
the cloud folder and classifies each one:

- **identical**;
- **fast-forward** in either direction — one side is a prefix of the other, so
  the longer one can simply replace it;
- **active ↔ archive transition** — the same history moved between `sessions/`
  and `archived_sessions/`;
- **divergence** — both sides added different records.

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --save-plan sessions-plan.json
```

It writes nothing but the plan file and runs while Codex is open, though the plan
is then marked volatile and cannot be applied. The report names no session ids,
thread names or record contents: a conflict is addressed by its id alone.

## Working set

A laptop that needs only one project does not have to take every session with it:

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --project project-chloya --save-scope --save-plan sessions-plan.json
```

- `--project` and `--chat` may be repeated; `--scope-file` reads a set saved
  earlier; `--save-scope` remembers this one for the pair of machines in
  `plans/sessions-scope-<from>-<to>.json`.
- A project means all of its chats — pinned, found by path, or connected by a
  `[[path_mappings]]` rule — and the sub-threads they spawned, including chats
  that appeared on the other machine after the set was chosen.
- **The cloud mirror always receives every session**, so the backup never
  becomes partial. The set narrows only what is written into `.codex`.
- **The set is part of the plan id**, so a plan cannot be applied under another
  set.

A session held back is reported as `OUT_OF_SCOPE`. It blocks nothing, and it
carries what it would have been (`WOULD_BE_…`), so the summary says what is held
back instead of omitting it.

A chat whose working folder does not exist on this machine — usually a project
kept outside the synced folder — is marked `CWD_ABSENT_HERE`, and
`sessions scan` counts such chats in `cwd_absent_here`. The folder is looked for
where `[[path_mappings]]` puts it; when two rules disagree the chat is marked
`CWD_MAPPING_AMBIGUOUS` instead of guessed about. The mark blocks nothing: it is
there so you can leave those projects out of the working set rather than find
out by opening the chat. A folder created after the scan changes the plan id, so
the apply asks for a new scan.

## Divergences

A divergence is never resolved automatically — no interleaving, no sorting by
timestamp, no "newer wins". Both branches stay as they are, and the plan blocks
until a decision is recorded:

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --conflict <conflict-id> --choice KEEP_LOCAL --output resolutions.json
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --resolutions resolutions.json --save-plan sessions-plan.json
```

`--choice` is `KEEP_LOCAL`, `KEEP_REMOTE` or `DEFER`. A decision is pinned to the
exact bytes of both branches: if either changes afterwards, the decision is
refused as `STALE_RESOLUTION` rather than applied to a history you never saw.

Record equality is decided by raw bytes. Canonical JSON is consulted only where it
is provably unambiguous, so two different records can never collapse into one.

### When Codex rewrote its sessions

In September 2026 the Codex desktop build rewrote every existing session file
into a new record format: each record is numbered (`ordinal`), messages moved
into other fields, and some records were dropped on the way — turns you had
rolled back, repeated `session_meta` records, injected instructions. Each file
kept its old modification time. A cloud mirror written before that disagrees
with every session it holds.

Such a conflict is marked `FORMAT_MIGRATION`, with `NEWER_FORMAT_LOCAL` or
`NEWER_FORMAT_REMOTE`, and `doctor` warns (`session_format`) while one side is
still in the older format. It stays a conflict, because the two copies are not
the same history, but one decision covers all of them:

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --format-migrations --output resolutions.json
```

It keeps the copy in the newer format for each one, pinned like any other
decision. It leaves alone a conflict marked `OLDER_FORMAT_HAS_LATER_RECORDS`:
there the old copy has a record later than anything in the new one, which can be
work done elsewhere before the upgrade, so it needs its own `--conflict …
--choice …`. In the window this is the **Keep the new format for all** button.

When the plan is applied, each old copy about to be overwritten is kept once,
compressed, under `superseded/` in `semantic.root_dir` — the only place the
dropped records still exist. Unlike a conflict bundle, the copy that wins is not
stored a second time.

## Applying a plan

Applying needs Codex closed and the exact plan id. The plan is rebuilt from the
current state first and its id must still match, so any change since the scan —
a branch that grew, a new conflict, a decision gone stale — refuses the apply:

```powershell
codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <plan-id> --dry-run
codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <plan-id> --resolutions resolutions.json
```

- A branch is transferred **whole**: nothing is appended to a destination and no
  history is interleaved. The source is only read; the destination is in a
  verified backup before it is replaced.
- A branch that loses a decision is also kept in an immutable **conflict bundle**
  under `semantic.root_dir`. Backups expire by retention; the bundle is what
  guarantees a divergent history is never the only copy in something that
  expires.
- An apply is **partial by design**. A conflict or a target collision stops the
  whole plan, because each names a decision only you can make. Items blocked on
  an unproven layout or on the SQLite catalogue are reported and left where they
  are.
- Active ↔ archive transitions are reported but not applied in 0.2: the move
  needs a delete, and codexSync never deletes a session file.

### Writing into `.codex`

Where the Codex runtime looks for a session file is a property of that runtime:
put the file somewhere else and the session is invisible, with no error at all.
So a write **into** `.codex` needs two things:

- **a proven target layout.** Until a controlled experiment records it, such an
  item is reported as `BLOCKED_UNPROVEN_LAYOUT`
  ([experiment](../dev/experiments/session-layout-adapter.md));
- **a row in Codex's thread catalogue** (`state_*.sqlite`) naming exactly that
  file. codexSync does not write SQLite, so a session the catalogue has never
  heard of cannot be made visible, and is reported as
  `UNSUPPORTED_STATE_BACKEND`.

Writing **towards the cloud folder** is not gated: no Codex reads that copy, so a
branch keeps the relative path it has locally. This is what lets a stale or
missing mirror be rebuilt.

## The cloud mirror

Because no runtime reads the mirror, a branch can be stored there compressed:

```toml
[semantic]
root_dir = "${workspace_root}/semantic"   # branch manifests and conflict bundles
mirror_compression = "xz"                 # none | gzip | xz
```

- `xz` stores real session data in about a third of its size. Only the mirror is
  affected: a branch written back into `.codex` is always plain JSONL.
- **Compression is a property of the container, not of the history.** Branch
  hashes, record counts and every comparison are taken from the decompressed
  stream, so a compressed mirror copy is `IDENTICAL` to the plain local branch.
- The setting applies to a branch the mirror does not hold yet. A branch already
  there keeps its container (`MIRROR_CONTAINER_KEPT`): the container is part of
  the file name, and two names for one session would make the catalogue drop the
  session from every later plan.
- The container is part of the plan id, so changing the setting invalidates an
  existing plan instead of renaming destinations under a confirmation you already
  gave.

## The session index

`session_index.jsonl` is an append/update journal, not a list of the sessions
that exist: one id may appear on several lines, a session may have no line, and a
line may name a file that is gone. None of that is an error, and codexSync never
"cleans it up".

```powershell
codexsync -c config.toml sessions index
```

The report shows what each side's index holds and where the two disagree. It
reads only, runs while Codex is open, and names no session ids or thread names.

- A repeated id has two plausible readings — the last line wins, or the greatest
  `updated_at` wins — which differ exactly when a clock ran backwards. That is
  reported as `REDUCTION_AMBIGUOUS`; `doctor` carries the same check.
- Two sides holding different records for one session is a rename divergence, a
  decision rather than a merge.
- No index is rewritten while the runtime's reading of it is unproven
  (`UNPROVEN_CONSUMER_CONTRACT`,
  [experiment](../dev/experiments/session-index-contract.md)).
