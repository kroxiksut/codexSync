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
- **divergence** — both sides added different records;
- **a copy that cannot be used** (`BLOCKED_INVALID_BRANCH`) — unreadable,
  truncated, or one session id in two files (`LOCAL_DUPLICATE_SESSION_ID`,
  `REMOTE_DUPLICATE_SESSION_ID`), or a copy would land on a file that is not
  this session's (`DESTINATION_OCCUPIED`). Such a copy is never taken for a
  missing one: nothing is written for that session on either side until the file
  reads cleanly, and the other sessions are not held up by it. One exception:
  when Codex carried a chat on in a new file and its thread catalogue names
  exactly one of the two, that one is the chat and the other is left alone
  (`STALE_DUPLICATE_BY_CATALOG`).

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --save-plan sessions-plan.json
```

It writes nothing but the plan file and runs while Codex is open, though the plan
is then marked volatile and cannot be applied. The report names no session ids,
thread names or record contents: a conflict is addressed by its id alone.

## Working set

A laptop that needs only one project does not have to take every session with it:

```powershell
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --project project-orion --save-scope --save-plan sessions-plan.json
```

- `--project` and `--chat` may be repeated; `--scope-file` reads a set saved
  earlier (a file that is missing or is not a saved set stops the scan with exit
  code `4` instead of dropping the set); `--save-scope` remembers this one for
  the pair of machines in `plans/sessions-scope-<from>-<to>.json`.
- A project means all of its chats — pinned, found by path, or connected by a
  `[[path_mappings]]` rule — and the sub-threads they spawned, including chats
  that appeared on the other machine after the set was chosen.
- **The cloud mirror always receives every session**, so the backup never
  becomes partial. The set narrows only what is written into `.codex`.
- **The set is part of the plan id**, so a plan cannot be applied under another
  set.

A session held back is reported as `OUT_OF_SCOPE`. It blocks nothing, and it
carries what it would have been (`WOULD_BE_…`), so the summary says what is held
back instead of omitting it. A set that covers no session at all — a project
with no chats yet — writes nothing into `.codex` and is marked
`WORKING_SET_MATCHES_NOTHING`; the mirror is still written in full.

A chat whose working folder does not exist on this machine — usually a project
kept outside the synced folder — is marked `CWD_ABSENT_HERE`, and
`sessions scan` counts such chats in `cwd_absent_here`. The folder is looked for
where `[[path_mappings]]` puts it; when two rules disagree the chat is marked
`CWD_MAPPING_AMBIGUOUS` instead of guessed about. The mark blocks nothing: it is
there so you can leave those projects out of the working set rather than find
out by opening the chat. A folder created after the scan changes the plan id, so
the apply asks for a new scan.

## Divergences

A divergence is never merged — no interleaving, no sorting of records. One of
the two copies is kept, and the other goes whole into the conflict bundle
before anything is replaced. Which one is decided by
[`conflict.policy`](SYNC.md#conflicts) ([D-027](../dev/DECISIONS.md)): by
default the copy whose last message is later, or always this machine's, or
always the cloud's (`--conflict-policy` for one scan). Such an item carries
`RESOLVED_BY_RULE`. Under `manual_abort`, and for two copies that end at the
same moment (`RULE_CANNOT_DECIDE`), the plan blocks until a decision is
recorded:

```powershell
codexsync -c config.toml sessions resolve --plan sessions-plan.json --conflict <conflict-id> --choice KEEP_LOCAL --output resolutions.json
codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --resolutions resolutions.json --save-plan sessions-plan.json
```

`--choice` is `KEEP_LOCAL`, `KEEP_REMOTE` or `DEFER`. `DEFER` keeps the conflict
open, and an open conflict refuses the **whole** `sessions apply`, not just that
session — defer only what you will decide before the next apply. `sessions scan`
exits with code `2` exactly when the plan holds such a decision (a conflict or a
target collision); a session blocked only by an unproven layout or by the SQLite
catalogue is left alone by the apply and does not change the exit code.

A decision is pinned to the
exact bytes of both branches: if either changes afterwards, the decision is
refused as `STALE_RESOLUTION` rather than applied to a history you never saw.
A recorded decision, `DEFER` included, always outranks the policy. In the
window, the Sessions page decides every conflict of a scan at once by one rule
(*All conflicts at once*), or one at a time.

A long chat is continued by Codex in a second file,
`rollout-…-<id>_<other id>.jsonl`, which opens with the same chat id and says
where it carries on (`history_base`). The chat is the chain of files: each
file is carried on its own (a page shows `HISTORY_PAGE`), the first one keeps
growing as before, and neither is a conflict with the other.

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
`NEWER_FORMAT_REMOTE` — only a divergence of content is, never a missing base, and `doctor` warns (`session_format`) while one side is
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
  under `semantic.root_dir`, in `conflicts/<conflict id>` — the id you resolved —
  and both copies are re-hashed before it is committed. Backups expire by
  retention; the bundle is what guarantees a divergent history is never the only
  copy in something that expires.
- An apply is **partial by design**. A conflict or a target collision stops the
  whole plan, because each names a decision only you can make. Items blocked on
  an unproven layout, on the SQLite catalogue or on a copy that cannot be used
  are reported and left where they are.
- A chat archived (or taken out of the archive) on one machine is moved the
  same way on the other (`D-023`). Which side moved is read from this machine's
  record of the last agreement, never from clocks; a machine with no record of
  its own follows the cloud copy. The chat is written where the other machine
  keeps it and the old file is removed — after it is in a verified backup, in
  the same envelope. Into `.codex` this needs the thread catalogue to name the
  file being moved. A chat archived on one machine and continued on the other
  is a decision (`ARCHIVED_AND_CONTINUED`). The catalogue row still names the
  old place until Codex updates it; `doctor` counts such chats in
  `session_visibility`.

### Writing into `.codex`

Where the Codex runtime looks for a session file is a property of that runtime:
put the file somewhere else and the session is invisible, with no error at all.
So a write **into** `.codex` takes one of two routes.

**A chat this machine already has** — one you continued on the other machine —
is written over its own file (`IN_PLACE`). Codex's thread catalogue
(`state_*.sqlite`) names that file, so no path is chosen and nothing about the
layout is guessed: the newer copy of the same chat, with its continuation,
replaces the older one, after the older one is in a verified backup. It is
allowed only when the catalogue names exactly that file for that chat, and the
chat is active on both machines or archived on both. Otherwise the item stays
`BLOCKED_UNPROVEN_LAYOUT` with a code saying why: `IN_PLACE_CATALOG_ABSENT`,
`SESSION_NOT_IN_CATALOG`, `CATALOG_PLACES_ELSEWHERE`, `CATALOG_UNREADABLE`,
`IN_PLACE_STATE_CHANGES`, `IN_PLACE_ARCHIVE_FLAG_DIFFERS` or
`IN_PLACE_CONTAINER`.

Codex also keeps each chat's title, preview and time in that catalogue, and
codexSync never writes it. The chat list may therefore show the old ones until
Codex refreshes them; the conversation itself is read from the file.

**A chat this machine has never had** is decided by one setting:

```toml
[semantic]
new_chats = "same_path"   # same_path | keep_in_cloud
```

- `keep_in_cloud` leaves it in the cloud copy, reported as
  `BLOCKED_UNPROVEN_LAYOUT`: where Codex expects a new chat file has not been
  proven by the [controlled experiment](../dev/experiments/session-layout-adapter.md).
- `same_path` (the default) writes it into `.codex` at the path it has on the machine it came
  from, relative to `.codex` — `sessions/<year>/<month>/<day>/…` or
  `archived_sessions/…` — which is what 0.1 did by copying `sessions/` whole.
  The user name and drive do not matter, since the path is relative. Each such
  item carries `NEW_CHAT_SAME_PATH`. A file already at that path is never
  overwritten (`DESTINATION_OCCUPIED`), and a catalogue that places the chat
  somewhere else or cannot be read still refuses (`BLOCKED_UNSUPPORTED_BACKEND`).

Codex lists chats from its thread catalogue, which it fills from the chat files
once and then keeps up itself. A chat file written afterwards — every chat from
another machine — is not in it, so Codex does not show it. A full sync therefore
asks Codex to rebuild that list from the files (`D-024`): codexSync puts one
status row back to the value Codex creates it with, after a verified backup of
the catalogue, and Codex writes every chat into its list on its next start,
which takes longer than usual. codexSync never writes a chat into the catalogue
itself. `codexsync sessions catalogue` lists the chat files Codex does not
list and, with `--confirm-plan`, asks the same on its own. It is asked once per
set of files; if Codex still leaves some out, the sync says so instead of
asking again. `doctor` reports the same count as `session_visibility`;
`not_listed=0` means every chat is visible.

A chat's **name** is not in its file either: Codex keeps it only in that
catalogue, so a carried chat first shows its first message. Each machine
therefore publishes the names it shows into `chat-names` beside the manifest,
and a full sync sets the other machine's name on a chat whose name here is
unset — never over a name given here (`D-025`). A chat Codex lists only after
its next start gets its name on the sync after that. `codexsync sessions names`
previews and, with `--confirm-plan`, does the same on its own.

The chat's working folder may be elsewhere on this machine. The file is not
changed for that (a record's bytes are its identity); map the folder with
`[[path_mappings]]` or move the project instead, see [Projects](PROJECTS.md).

Writing **towards the cloud folder** is not gated: no Codex reads that copy, so a
branch the mirror does not hold yet keeps the relative path it has locally. A
branch it already holds is rewritten where it is, even when the local copy lives
elsewhere (`MIRROR_PATH_KEPT`), because a second file for one session would
make both drop out of every later plan; only an archive move relocates it
(`MOVES_BRANCH`). This is what lets a stale or missing mirror be rebuilt.

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
