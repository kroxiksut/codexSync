# Decisions

## D-001: Sync strategy
We use cold sync only.
Sync happens only when Codex is fully closed.

## D-002: Legal/safety boundary
The project only works with user-local files.
No token handling, no network interception, no reverse engineering.

## D-003: Scope
The project is a utility, not a Codex plugin.

## D-004: Storage
Cloud folder can be OneDrive, Dropbox, Syncthing, Google Drive mirror, etc, or a network folder.

## D-005: Conflict policy
Single-writer assumption.
User should not actively work in Codex on two machines at the same time.

## D-006: Initial platform
Windows first.

## D-007: Operational handoff contract
The expected workflow is strict and manual:
close Codex on machine A, wait for cloud propagation, then sync on machine B.

## D-008: Responsibility boundary for cloud environment
The utility does not verify cloud client process status or free space in cloud/network storage.
These checks are out of scope and owned by the user.

## D-009: CI target matrix for MVP
CI runs on Windows and macOS runners (`windows-latest`, `macos-latest`).
Linux CI is intentionally disabled for MVP until Linux runtime support is explicitly in scope.

## D-010: Preflight diagnostics mode
The CLI provides `doctor` and `preflight` commands (equivalent behavior).
These checks are read-only and validate runtime readiness before sync:
- config/runtime path readiness
- local/cloud/backup/temp readability and directory shape
- Codex process precondition
- manifest data-version compatibility
- session catalog audit (invalid/ambiguous sessions, graph codes)
- read-only SQLite audit
- orphan temp file detection

If any preflight check fails, the command exits with code `5` (`fail-safe`).

Amendment (0.2): the original list promised write-access probes and a
local/cloud mtime-drift probe. Both wrote probe files, one of them inside the
Codex state directory. `OperationKind.DOCTOR` is declared `side_effect_free`,
so the drift probe was dropped and the path checks were reduced to readability
checks. Drift detection may return later, but only in a form that writes
nothing into state.

## D-011: Bounded retry on a locked destination
`os.replace` is the commit step of every mutation. Destinations live in
directories a cloud client, search indexer or antivirus may open at any moment,
which on Windows surfaces as `WinError 5`/`32`/`33` for as long as that handle
lives — usually milliseconds.

Treating this as a hard failure aborted the run and left a `RECOVERY_REQUIRED`
journal for a condition that had already cleared, which is a worse outcome than
waiting. The engine therefore retries a *transient lock* up to five times with
exponential backoff (~1.5s total) before giving up.

This does not weaken `fail-safe`:
- every attempt is the same atomic `os.replace`, so a destination is never
  partially written;
- the destination hash is still verified after the replace;
- the process-safety check is re-proved before each further attempt, so Codex
  starting during the wait still stops the commit;
- errors that are not a transient lock (for example `EXDEV`) are raised on the
  first attempt, unretried.

Retry counts are constants, not configuration: they are a property of the
filesystem behaviour, not a user preference.

## D-012: One-way sync directions are configurable
`sync.direction` accepts `bidirectional` (the default), `to_cloud` and
`to_local`. The one-way modes exist because a machine is often only a source or
only a destination during a handoff, and copying the other way at that moment
is exactly what a person wants to prevent.

Nothing about the cold-sync model changes: a one-way run still needs Codex
stopped, still backs up before overwriting and still refuses on uncertainty.
Two properties keep it honest:

- **A skipped action is never recorded as synchronised.** The manifest holds a
  two-sided fingerprint so that a one-sided change can be told from a conflict.
  Writing the skipped side's current fingerprint would make the next
  bidirectional run believe both sides had agreed, and it would then take the
  older file for the newer one. For every path the direction skipped, the
  previous manifest entry is carried over unchanged.
- **A conflict stays a conflict.** When both sides changed, a one-way run does
  not quietly overwrite the other side; `conflict.policy` decides, exactly as
  in a bidirectional run.

`validate`, `doctor` and the run report state the direction, so a run that
copied nothing in one direction says why.

Amendment (CS-288): "the manifest" is one baseline per machine. The file lives
in the shared workspace, and a single two-sided entry made machine B take
machine A's last sync for its own and copy its older file over A's newer one.
Each machine now reads and writes only its own pair (local and cloud as *it*
saw them); a skipped path carries this machine's previous entry. An entry from
the unkeyed pre-0.2 format is attributed to nobody, which makes the first run
after the upgrade a first sync. Any conflict left in a plan stops the run,
whatever `conflict.policy` is (CS-295).

## D-013: Deletions may be propagated, but only against proof
`sync.delete_policy` accepts `never` (the default) and `propagate`.

Under `propagate`, a file missing on one side is deleted on the other **only
when the previous manifest proves that both sides held it and the surviving
side has not changed since**. Anything else — no manifest, an unknown path, a
side that changed after the recorded fingerprint — is a conflict, not a
deletion. The first run after enabling it therefore deletes nothing, because
there is no proof yet.

The safety rules are unchanged and apply in full (`AI_RULES` 3 and 6):

- a verified backup of the file is created before it is removed, and the
  removal is logged as its own dangerous action;
- the deletion runs inside the same mutation envelope as every other write —
  operation lock, journal, gate re-checked before each step — so `recover` can
  roll it back from that backup;
- a dry run deletes nothing;
- semantic-owned paths (`sessions/`, `archived_sessions/`, the session index,
  the global state, SQLite) are never deleted by `sync`. Moving a session to
  the archive is a separate question (`ARCHIVE_TRANSITION`) and this setting
  does not open it.

Amendment (CS-288, CS-324): the proof is this machine's own baseline, never
another machine's, so an entry the other machine wrote cannot delete a file
here. And a deletion out of an include root that holds no file at all on the
side it went missing from is a conflict: an empty or missing root is far more
likely a folder being re-downloaded or a disconnected drive than a person
deleting each file. Paths that differ only in letter case are a conflict too.

## D-014: A person may accept a suspicious shrink as Guardian's new baseline
Guardian quarantines a state whose project or binding count fell sharply
(`guardian_shrink`), and nothing suspicious becomes `latest-good` on its own.
That rule stays. What it did not cover is a drop that is real: the comparison is
always against `latest-good`, so after one every later state is suspicious too
and `latest-good` never moves again. Observed on 2026-09-13, when the desktop
build re-created all 16 projects under new ids and the 6 bindings naming the old
ids disappeared; the store stayed on the 2026-09-05 snapshot.

`guardian accept` is the sanctioned way out, and its limits are the decision:

- Only `SUSPICIOUS` with a shrink code can be accepted. `INVALID` and
  `INDETERMINATE` cannot, whatever is confirmed: acceptance overrides a
  judgement about counts, never an integrity check.
- The preview explains the drop in counts only (projects replaced — gone while
  a project with the same roots exists under a new id — removed and added; lost
  bindings attributed to each). Names, roots and thread ids stay in core, as
  everywhere else in Guardian.
- The plan id covers the baseline (id and hash), the counts, the shrink codes
  and a digest of which bindings and projects went and why, but not the state's
  own hash. Codex rewrites the file every few minutes; an id that followed the
  bytes could not be confirmed while Codex runs, which is when Guardian works. A
  different drop is a different id.
- The acceptance runs the watcher's pipeline under the runner lock (stable
  reads, validation, shrink assessment) and commits through the ordinary store
  path. The manifest is `PASS_WITH_WARNING` with the shrink codes plus
  `SHRINK_ACCEPTED`, and names the overridden baseline as its predecessor.
- Retention never prunes an accepted snapshot or the baseline it overrode.
- It writes only into the Guardian root, so it is allowed while Codex is open.
  `doctor` warns when shrink quarantines are newer than `latest-good`.

## D-015: A record-format rewrite by Codex is a conflict decided in bulk
The session model assumed a history only grows: a branch changes at its end or
not at all, which is what makes a prefix a fast-forward and anything else a
divergence. The September 2026 desktop build broke that itself. It rewrote every
existing session file into numbered records (`ordinal` on each record, messages
moved into `item` payloads, `session_meta` carrying `session_id` and
`history_mode`), kept each file's mtime, and dropped records on the way: turns
the person had rolled back, repeated `session_meta` records, injected
instructions and most of the guardian sub-agent reviews. Observed on 2026-09-19:
all 277 local sessions rewritten, the mirror written on 2026-09-05 still holding
242 of them in the old format, and every one of those a
`DIVERGED_NO_COMMON_RECORDS` that refused `sessions apply` as a whole.

What was decided:

- It stays a conflict. The two copies are not the same history, and proving
  "same history, re-encoded" would mean trusting a projection of one format onto
  the other that the dropped records already contradict. The catalogue records
  each branch's record format (`legacy`, `ordinal`, `mixed`) and the latest
  record time; a conflict between two formats is labelled `FORMAT_MIGRATION`
  with the newer side, and nothing else about it changes — not its conflict id,
  not the fact that it blocks.
- One explicit decision covers all of them. `sessions resolve
  --format-migrations` writes an ordinary pinned resolution keeping the newer
  side for each. It never decides a conflict whose older copy has a record later
  than anything in the newer one (`OLDER_FORMAT_HAS_LATER_RECORDS`): that copy
  may hold work done elsewhere before the upgrade.
- The loser is kept, alone and compressed. A conflict bundle stores both raw
  branches; here the winner is what the destination is about to hold, and both
  copies of 242 sessions would have been about two gigabytes written into a
  folder the config allows to be in the cloud. `superseded/<branch sha256>/`
  holds the old branch in the mirror's container, verified by decompressing it
  before the directory is committed. It is the only surviving copy of what the
  rewrite dropped, and nothing prunes it.
- `doctor` reports both sides' formats from each file's first record
  (`session_format`), so a rewrite that reached one side is a known step rather
  than two hundred unexplained conflicts.

## D-016: One settings sync may run unattended, once, after sign-in
CS-232 made every scheduled job read-only: a task runs while nobody watches,
and each mutation has its own plan and an explicit confirmation. On the owner's
machine Codex stays open until shutdown, so the only cold window is right after
sign-in, and asking for a manual sync then is asking for it to be forgotten.

What was decided (2026-09-23, by the owner):

- It is opt-in: `[scheduler] sync_at_login`, a checkbox, off by default and
  independent of the periodic job.
- It is one job and it runs once: `sync --apply --unattended` on a sign-in
  trigger in a task of its own (`LOGIN_SYNC_SLOT`), never repeated, never a
  periodic mode, so switching it off removes exactly that task.
- It is the plain settings sync only, because that is the one mutation that
  needs no plan id — its envelope (gate, lock, journal, verified backup) does
  not depend on a person. Sessions, restore, repair and moves keep their
  confirmation and are never scheduled.
- `--unattended` forces `manual_abort` on conflicts before planning, whatever
  `conflict.policy` says: nobody is there to decide one. (Amended by D-027:
  the configured rule is a decision made in advance and applies here too.)
- The process gate is not relaxed. A Codex that starts with the session makes
  the run a refusal (exit 3) rather than a race, and the window reports the
  task's last result in words.
- Installing it must not run it: the Windows logon trigger and systemd's
  `OnStartupSec` do not fire at install, and the LaunchAgent is written but not
  bootstrapped, since loading it would fire `RunAtLoad`.

## D-017: A copy of `.codex` may be taken on a schedule, and it waits for Codex
Asked for on 2026-09-24 as "automation of backups", and specified by the owner
on 2026-09-25: a copy of the Codex state at sign-in and/or on a timer, which
waits when Codex is open rather than copying it while it runs.

What was decided:

- It is a new, separate job (`state-backup create --wait`) in a task of its own
  (`STATE_BACKUP_SLOT`), not a periodic `[scheduler]` mode and not a variant of
  the pre-overwrite backups, which exist only inside a write.
- It reads `.codex` and writes only into `[state_backup] root_dir`, which the
  user chooses; nothing proposes a location (a cloud folder would upload every
  copy). Empty means off, and a schedule without a folder is a config error.
- What is copied is the valuable part — sessions, archive, global state and its
  `.bak`, the SQLite catalogues with `-wal`/`-shm`, the session index,
  `config.toml`, `AGENTS.md`, rules, skills, memories, automations. Secrets are
  refused by name at any depth (`sync_candidates.SECRET_NAMES`); caches, logs,
  the sandbox and temporaries are left out.
- It goes through the safety gate as `OperationKind.STATE_BACKUP`, which needs
  Codex stopped without being a mutation: a copy of files being written is a
  copy of no moment. The task waits (30 s polls, up to 23 h, under a 24 h OS
  limit); the window and `create` without `--wait` refuse instead. A file whose
  size or mtime moves while it is read fails the copy, and the gate is asked
  again before the copy is committed.
- One verified zip per copy, written as `.partial` and renamed only after every
  entry reads back with the hash it was written with. Only this machine's copies
  beyond `keep` (zip, keep 5 by default) are removed, only by the exact name
  pattern, and only after the new copy is committed.
- Restoring from a copy is by hand. A restore into `.codex` would be a mutation
  with its own plan and confirmation, and none was asked for.

Amendment (2026-09-27, review CS-301): Codex's own `config.toml` stays in the
copy, by the owner's decision. It can hold MCP server tokens (`env` tables,
bearer headers), which `SECRET_NAMES` cannot see because it works by file name;
leaving it out would lose the one file that says how Codex was set up, and
filtering its keys would be a guess about a format codexSync does not own. The
risk is accepted and written in the user documentation: the copy folder must
be private. Links (symlinks and junctions) are not followed, and a folder that
cannot be listed fails the copy rather than leaving a hole in it.

## D-018: Handing work between machines, including chats, with nobody watching
Asked for by the owner on 2026-09-27: the cold-sync protocol of `AI_RULES.md`
§2 (close Codex, let the cloud deliver, sync, then start Codex elsewhere) as
something codexSync does rather than something the user remembers, plus a
record in the synced folder of who synced last. Decided with the owner: a
background task rather than a tray window, operating-system notifications,
and a 15-minute wait for delivery.

What was decided:

- **One file per machine** in `[handoff] root_dir`, inside the synced
  workspace. Only its machine writes it, so a cloud client never has two
  writers for one file. It is one self-verifying JSON document (like a
  `semantic_store` entry): state (`working` / `handed_off`), the id of the last
  handoff, a fingerprint of the cloud copy as that handoff left it (size and
  SHA-256 per file, keyed by the SHA-256 of the path), and the id of every
  other machine's handoff already loaded here.
- **Ids, not clocks.** "Have I loaded A's work" compares ids. Two machines'
  clocks are never compared (`AI_RULES` §6); times are recorded for people.
- **Delivery is checked from files only.** Another machine's handoff counts as
  arrived when every file its fingerprint names is in the cloud copy with that
  size and hash. The cloud client is never asked (`AI_RULES` §1). A load waits
  up to `delivery_wait_minutes` and then refuses; `--accept-undelivered` is
  the explicit way past a handoff that will never fully arrive.
- **Amendment to D-016: chats may be transferred unattended**, under one
  condition that replaces the plan-id confirmation: the transfer plan has no
  conflict, no target collision and no archive transition. The plan is built,
  saved and applied by its own id in one process, so the freshness check that
  `--confirm-plan` gives is kept. Anything that needs a person stops the whole
  handoff before its first write — the chat plan is checked before the
  settings sync runs. (Amended by D-027: a conflict the configured rule
  decides no longer needs a person.) Recorded resolutions and the working set for the pair of
  machines are used, because each is already a decision the person made.
- **The handoff record is written only after both halves finished.** A new id
  and fingerprint only when this machine wrote to the cloud copy; a run that
  only loaded keeps its previous handoff, so other machines are not made to
  wait for a "delivery" of what they already hold.
- **The watcher** (`handoff watch`, `HANDOFF_SLOT`, no OS time limit) starts at
  sign-in: it loads while Codex is closed, marks the machine `working` when
  Codex starts (and warns when another machine is working or its handoff is not
  loaded here), and hands off when Codex closes. Only a `RUNNING`/`STOPPED`
  the process check is sure of counts; `UNKNOWN` is neither. It replaces the
  sign-in sync (`scheduler.sync_at_login` together with `handoff.enabled` is a
  config error). The process gate is not relaxed anywhere: every write inside a
  handoff goes through the same gate, lock, journal and backup as `sync` and
  `sessions apply`.
- **Chats not placed in `.codex` are counted, not hidden.** While
  `PROVEN_LAYOUTS` is empty a chat this machine has never held is not written
  into `.codex`, so a load reports how many newer chats stayed in the cloud
  copy only instead of calling the work loaded. (Amended by D-019: a chat this
  machine already holds is written in place.)

## D-019: A continued chat is written over its own file without a proven layout
Asked for by the owner on 2026-09-27 ("chats must sync, otherwise what is the
point of syncing"), CS-330a. `PROVEN_LAYOUTS` gates every write into `.codex`
because where the runtime looks for a session file cannot be learned from the
files. For a chat this machine already holds that question is already
answered: the runtime's thread catalogue (`threads.rollout_path`) names the
file, and the file is there.

What was decided:

- **In place, and only in place.** A branch bound for `.codex` whose session
  this machine already holds is written over that file (`IN_PLACE`, in the
  item's codes and therefore in the plan id). No path is rendered, so no
  layout is assumed. A session this machine has never held stays
  `BLOCKED_UNPROVEN_LAYOUT` until the layout experiment settles it (CS-330b).
- **The catalogue must name exactly that file for exactly that thread.** A
  missing catalogue refuses here (`IN_PLACE_CATALOG_ABSENT`), unlike under a
  proven layout where `ABSENT` constrains nothing: without a row nothing shows
  that the runtime finds the chat by that file. Unreadable, unknown thread,
  another path, a row without a path: refused, with the existing codes.
- **No move hides inside it.** The two copies must both be active or both
  archived, and the catalogue's archive flag must agree with the folder
  (`IN_PLACE_STATE_CHANGES`, `IN_PLACE_ARCHIVE_FLAG_DIFFERS`); a move needs a
  delete and `delete_policy` is `never`. A local branch in a compressed
  container is refused (`IN_PLACE_CONTAINER`): the write produces plain JSONL.
- **Nothing else changes.** A divergence is still a conflict; the old file is
  in a verified backup before the replace; the process gate, lock and journal
  are the `sessions apply` envelope. As for every session transfer,
  `recover rollback` does not write the old bytes back (it would bypass the
  catalogue and prefix checks); the old file stays in the backup snapshot, and
  a branch displaced by a resolution also in its conflict bundle or
  `superseded/`.

Checked against the real state it was built on: the catalogue was readable
(283 rows), and every one of 282 readable sessions satisfied the rule for its
own file (path and archive flag agree); the one refused is a session id kept in
two files, blocked as before.

Residual risk, accepted and written in the user documentation: the catalogue
also caches each chat's title, preview and time, and codexSync never writes
it, so the chat list may show the old values until Codex refreshes them. The
conversation is read from the file. Whether Codex rewrites a file merely
because it was opened (CS-251) is asked by the same experiment as CS-330b.

## D-020: A new chat may be written at its source path, on request
Asked for by the owner on 2026-09-29 ("the point is to sync everything; in 0.1
everything synced"), after D-019 had brought only chats both machines held. In
0.1 `sessions/` was copied wholesale and the owner saw chats started on one
machine appear in Codex on the other. That is evidence from an older Codex, not
the controlled run `PROVEN_LAYOUTS` asks for, and the state it was checked
against now argues for caution: `state_5.sqlite` has a `backfill_state` row
marked `complete` since 2026-03-09, i.e. Codex filled its thread catalogue from
the files once and may not look at the folder again.

What was decided:

- **A setting, not a gate entry.** `[semantic] new_chats = keep_in_cloud |
  same_path`, default `keep_in_cloud` (unchanged behaviour). `same_path` builds
  the plan under `SAME_PATH_LAYOUT_ID` (`{state}/{source_dir}/{file_name}`,
  relative to `.codex`, so user name and drive do not matter). `PROVEN_LAYOUTS`
  stays empty. Console, handoff and window read the same setting.
- **Only for a chat this machine never held.** A chat it holds is still written
  in place (D-019); the rendered path is never used for it.
- **The one relaxed check is the missing catalogue row**
  (`SESSION_NOT_IN_CATALOG`), because that row is exactly what Codex would have
  to add. A catalogue that places the thread elsewhere or cannot be read still
  refuses (`BLOCKED_UNSUPPORTED_BACKEND`); a file already at the target is
  `DESTINATION_OCCUPIED`; no catalogue at all constrains nothing, as under a
  proven layout. Every such item carries `NEW_CHAT_SAME_PATH`, which is in the
  plan id.
- **Visibility is measured, not assumed.** `doctor` gained `session_visibility`:
  chat files on disk whose id the thread catalogue does not list, and ones it
  lists at another path. On the state it was built on: 282 chats,
  `not_listed=0`. A handoff reports how many new chats it wrote.
- **Nothing is written into SQLite.** If Codex does not take the files up, the
  next step is a decision about writing its catalogue, which stays the owner's.

When the layout experiment (CS-330b) confirms the behaviour, the template moves
into `PROVEN_LAYOUTS` with the Codex version it was observed on.

**Amended 2026-10-03: `same_path` is the default.** The first full sync on the
second machine left 192 chats started on the other one in the cloud copy,
because that machine's config had no `new_chats` line and the default was
`keep_in_cloud`. The owner: carrying new chats is the purpose of the tool, the
way 0.1 did it, and a sync that needs a manual switch for every new chat is not
one. The loader, both templates and the window now default to `same_path`;
`keep_in_cloud` stays available for a machine that should not receive them.
Every other rule above is unchanged: a missing catalogue row is still the only
waived check, and `doctor`'s `session_visibility` is still what shows whether
Codex lists the chats.

## D-021: Compiled programs are never synced or copied
Asked for by the owner on 2026-09-29, after `plugins/.plugin-appserver/`
turned out to have carried `codex.exe`, `codex-command-runner.exe`, the sandbox
setup and the code-mode host (416 MB) into the cloud copy: "there is no point
copying binaries: they go out of date, a new install restores them, and
programs update all the time".

What was decided:

- **A rule in code, not a glob.** `native_programs.is_native_program` reads a
  file's header: Windows PE (`MZ` and the `PE` signature it points to, so a text
  file starting with "MZ" is not caught), ELF, Mach-O in every byte order and
  universal binaries. `runtime._build_indexes` drops such files from both sides
  and `state_backup.select_state_files` leaves them out of a `.codex` copy,
  whatever `include_roots`/`exclude_globs` say, like `SECRET_NAMES`.
- **By header, not by name**, because a program on macOS and Linux usually has
  no extension, and a Mac is exactly where a Windows `.exe` is useless.
- **The Codex folders are also excluded by glob** (`CODEX_BINARY_GLOBS`,
  `MISSING_EXCLUDE_CODEX_BINARIES` in `config check`), so the staging folder's
  non-program files stay out too and a person reading the config sees why.

Checked on the machine it was built on: 14 of 1334 files in the include roots
are programs (4 Windows executables in `.plugin-appserver`, 10 prebuilt Node
modules for Android, Linux and macOS in the plugin cache); none elsewhere.

## D-022: The project list travels by merge, and one button syncs everything
Asked for by the owner on 2026-10-01, after a sync on the laptop left its
sidebar showing the laptop's own projects: "I want to open the laptop after a
sync and carry on working; in 0.1 that worked". It worked because the 0.1
config copied `.codex-global-state.json` whole (the cloud copy still holds that
file from March). 0.2 made the file semantic-owned and carried nothing in its
place, and the window's *Synchronise* ran only the settings sync, so neither
chats nor projects moved.

What was decided:

- **Only the project part travels, by merge** (`project_sync.py`). The file
  also holds one machine's window bounds, remote-control ids and per-host
  migration markers, so copying it whole is wrong on the other machine. Each
  machine publishes `local-projects` entries as Codex wrote them, the order,
  the pins and the bindings (as legacy ids, which every machine shares; an
  app-server id is per host) into `<manifest folder>/projects/<machine>.json`,
  self-verifying like a handoff record. The receiver matches by id, then by
  root through `[[path_mappings]]`, adds what it lacks as the peer's own entry
  (nothing invented, which is why `supports_project_creation` does not apply),
  and **never removes** a project (owner's choice: merge, not mirror).
- **The peer's view wins for shared projects, once.** Pins, order and bindings
  of projects both machines have follow the peer's publication, taken only
  when its content id differs from the one recorded as accepted, so an
  unchanged peer list is never re-applied over a local change.
- **A missing folder does not stop the project** (owner's choice): it is added
  with `FOLDER_MISSING_HERE` and the result says so.
- **An empty project list commits to no shape.** The legacy adapter claims a
  state with no projects and no bindings because there is nothing for the
  Electron adapter to see; such a state takes the Electron shape every
  observed machine writes, and the merged result is validated before commit.
- **SQLite is not written.** On the reference machine `state_5.sqlite` holds
  49 project rows (the same names three or four times from repeated
  migrations) and every `threads.project_id` is NULL, while the sidebar follows
  the JSON order and pins exactly. Whether a project *added* to the JSON after
  `projectsMigrated: true` shows up has to be confirmed on the laptop; if it
  does not, this meets `PROVEN_PROJECT_REGISTRY` (CS-238).
- **Synchronise is the full sync.** The window's button runs what
  `handoff sync` runs — settings, chats, projects, one confirmation, stages
  reported as progress — and the handoff folder defaults to `handoff` beside
  `state.manifest_file` ("we synced with the cloud — there is your folder"),
  so there is nothing extra to choose.

Checked against the real global state, in memory: the publication carries 19
projects, 17 ordered, 2 pinned, 4 of 4 bindings; merging it into itself
writes nothing; into a simulated laptop with 3 of them under other ids plus
one of its own it adds 16, matches 3 by folder, and the result validates.

## D-023: An archive move follows the other machine
Asked for by the owner on 2026-10-02 ("archived chats go to the archive too"),
after the laptop's first full sync stopped for good on 11 chats the desktop
had archived. The sync said a person had to decide them on the Sessions page;
that page listed them as transferable and offered no choice, so nothing could
ever unblock it. Until then an `ARCHIVE_TRANSITION` was classified and refused
at apply, because the move needs a delete. This amends D-019 ("no move hides
inside it") and the `delete_policy` note under D-013: the move is applied by
`sessions apply` and the full sync, never by plain `sync`.

What was decided:

- **Which side moved is read, never guessed from clocks.** This machine's own
  semantic manifest entry records the state both sides last agreed on; the
  side that still holds it did not move (`ARCHIVE_FOLLOWS_REMOTE` /
  `ARCHIVE_FOLLOWS_LOCAL`). With no entry of its own, as on a laptop that had
  never transferred a chat, this machine follows the mirror, whose state
  another machine's agreement vouches for (the base `ARCHIVE_TRANSITION`
  already requires).
- **A move is a whole-branch copy plus a delete, in the sync envelope.** The
  branch is written where the followed side keeps it (the other machine's
  relative path, as `new_chats = same_path` places a chat), the old file is
  backed up and the backup verified before anything is replaced, and it is
  removed only after every copy is in place (`MOVES_BRANCH`). The mirror
  keeps its container. A resolved conflict whose kept branch sits in the
  other state folder moves the same way.
- **The catalogue still has to name the file that moves.** Into `.codex` a
  move needs `threads.rollout_path` to name exactly the local file and its
  archive flag to agree with the folder (`IN_PLACE_CATALOG_ABSENT`,
  `CATALOG_PLACES_ELSEWHERE`, `IN_PLACE_ARCHIVE_FLAG_DIFFERS` refuse). The
  new path must be free (`DESTINATION_OCCUPIED`).
- **Archived on one machine and continued on the other is a decision.** When
  the side that did not move holds records the moved side lacks,
  `ARCHIVED_AND_CONTINUED` makes it an ordinary conflict: keeping either
  branch keeps its state.
- **SQLite is still not written.** After a move the catalogue row names the
  old path and the old archive flag until Codex rewrites it; `doctor`'s
  `session_visibility` counts such chats. If Codex does not follow, the file is
  in `archived_sessions/` (or back in `sessions/`) and the old one is in the
  backup snapshot.

At the same time (CS-348): one session id in two files on this machine is
settled by the catalogue when it names exactly one of them. Observed on the
reference machine: `rollout-…T11-06-11-<id>.jsonl` and
`rollout-…T16-26-54-<id>_<other id>.jsonl`, both opening with the same
`session_meta`, the catalogue naming the second — Codex carried the thread on
in a new file and left the old one. The named copy is the branch; the other
is marked `STALE_DUPLICATE_BY_CATALOG`, never transferred, never touched. No
catalogue, or one naming neither copy, leaves the duplicate blocked as before.

Checked against the laptop's plan of 2026-10-02: the 11 transitions carry no
own entry, so all follow the mirror into `archived_sessions/`, and 10 of them
are byte-identical.

## D-024: Codex is asked to rebuild its chat list; one SQLite row is written
The worry recorded in D-020 came true on 2026-10-04. A full sync on the laptop
wrote 197 chats into `.codex`, every project in Codex said "no chats", and
`doctor` reported `not_listed=197 listed_at_another_path=11` with Codex
running. Codex lists a chat from `threads` in `state_5.sqlite`, fills that
table from the files only while `backfill_state.status` is not `complete`, and
had marked it complete on 2026-03-09. The 11 are the D-023 archive moves: the
row still names the old file.

Read from Codex's own binary (desktop 26.930.3930.0, `codex.exe`): the
migration inserts `backfill_state` as `(1, 'pending', NULL, NULL, now)`;
startup runs the backfill while the status is anything but `complete` and
waits for it; the backfill upserts each rollout file into `threads`
(`archived` and the path included); and Codex's own diagnostic advises
starting with no state database so that the backfill rebuilds it.

What was decided (owner, 2026-10-04: "let's write the code right away"):

- **codexSync writes one row, never a thread.** `thread_catalogue.reset_backfill`
  puts `backfill_state` back to the row the migration creates — `pending`,
  watermark and last success cleared — in one transaction that re-reads the
  status under a write lock and refuses unless it is still `complete`. Codex
  then writes every thread row itself, by its own rules. Writing rows was
  rejected: some forty columns whose meaning only Codex knows.
- **Only when a chat file is not reachable through the catalogue**: an id the
  catalogue lacks, or a catalogued file that is gone (a moved chat). Codex's
  own leftover copy of a catalogued thread does not count. The check lists file
  names only (the id is in `rollout-<time>-<id>.jsonl`), so it costs no scan.
- **Not repeated for the same files.** A digest of what was asked for is kept
  in `temp_dir/thread-catalogue/<machine>.json`; the same set again is
  `ALREADY_ASKED`, reported as files Codex does not list, never re-asked on
  every sync.
- **The usual envelope.** Plan id over the catalogue's bytes, the files and the
  status; Codex closed (`SESSION_APPLY`, the gate the transfer already uses);
  operation lock; journal family `thread-catalogue`; a verified backup of the
  database and its sidecars; the plan rebuilt right before the write. Rollback
  through `recover` is not offered: the write is one SQLite transaction, and
  the database is in the snapshot.
- Part of every full sync (`run_handoff`, after projects) and
  `codexsync sessions catalogue` on its own. The first Codex start after it
  takes longer.

This amends "codexSync never writes SQLite" for exactly this one row. Thread
rows, `session_index.jsonl` and the project registry stay unwritten and gated.

## D-025: Chat names travel; `threads.name` is set only where it is unset
Right after D-024 made the carried chats appear on the laptop (2026-10-04),
each showed its first message instead of its name. The name lives only in
`threads.name` (286 of 289 rows on the reference machine; `title` is the first
message), never in the chat file, so a row Codex builds from the file has
none. `session_index.jsonl` mirrors 164 of them and is not proven to feed the
list (`PROVEN_CONTRACTS` is still empty), so it is not used.

Decided (owner, 2026-10-04): each machine publishes the names it shows —
never the first-message fallback — in `chat-names/<machine>.json` beside the
manifest (`peer_board`, self-verifying, written only by its machine). A full
sync sets a peer's name on a chat whose name here is unset (`NULL`, empty, or
equal to its title) with the statement Codex itself uses to rename a thread
(`UPDATE threads SET name = ? WHERE id = ?`, from `codex.exe` 26.930), guarded
by the value the plan saw, in one transaction, in the catalogue envelope of
D-024 (journal family `chat-names`). A name given here is never replaced
(`kept`); two peers disagreeing leave the chat alone (`ambiguous`); a chat
Codex has no row for yet waits for the next sync (`waiting`). This is the
second, and last, SQLite write; thread rows are still never created.

## D-026: Project folders are compared, never carried
Owner, 2026-10-04: chats travel, so a chat can continue on a machine whose
copy of the code is older. On the reference machine 8 of 19 project folders
are outside the cloud folder and 4 are not git repositories. Each full sync
publishes, per project, what the folder holds (`project-files/<machine>.json`):
for git the commit, branch, and a digest of uncommitted changes; otherwise
every file with its size, SHA-256 and change time (owner: "a list of files with
hashes"), tool folders excluded, files over 100 MB by size and time, hashes
cached locally per machine (`HashCache`, `config_locations.cache_dir()`) so
only moved files are read. With each project goes the last time one of its
chats changed there (`threads.cwd`/`updated_at`); owner: chats show where work
happened, but every project is compared because anything may change a folder,
so chat activity only orders and labels the warnings. The receiving machine
warns on a missing commit, a divergence, changes left uncommitted there, plain
files changed later there or only there (listed), or a missing folder. The
publication is rewritten only when its content changes.

Amended the same day (owner): "run the check on first launch, on the other
machine too; folders and files appear and disappear at any time; mark in the
window which files and projects did not come along, the same in the console".
So every check reads afresh and publishes — a full sync, `projects files`, and
the window on every start (a machine that never synced still publishes); a
plain folder's publication carries the files it lost since its previous one
(`removed`, kept 90 days), so "deleted there, still here" is told apart from
"added here"; a folder present here and absent there is `MISSING_THERE`; the
window lists projects and their files in Projects → Project folders, and the
console prints every file. Nothing is
copied or blocked. Git is optional and only read (`GIT_OPTIONAL_LOCKS=0`, so a
status never rewrites the index); a folder an enclosing repository ignores is
read as plain files. A failed check is logged and never fails the sync.
`projects files` re-checks without syncing.

## D-027: A conflict is decided by the configured rule; a chat continued in pages is one chat
On 2026-10-05 a full sync on the reference machine stopped on "1 chat
continued differently on the two machines", and the Sessions page offered no
visible way to decide it. Two things were wrong.

The conflict was not real (CS-356). Codex 0.160 carries a long chat on in a
second file, `rollout-<time>-<id>_<other id>.jsonl`, whose `session_meta` has
the same id and `history_base = {thread_id, end_ordinal_exclusive,
end_byte_offset}`. CS-348 had read this as "Codex moved the chat and abandoned
the old file", kept the file the catalogue names (the page) and compared it
with the first file in the mirror: no common records. The first file here had
in fact only grown (its first 7448 lines were byte-identical to the mirror's),
and a second chat, paged in August, had never had its first 1203 records
mirrored at all. Decided: a page is a branch of its own, keyed `<id>#page-<first
ordinal>` (`SessionDescriptor.branch_key`); the first file keeps the session's
own hash, so earlier plans, manifest entries and resolutions still apply. The
catalogue's row places the whole chain, since Codex reaches the other files
through `history_base`; readers that count chats (`chats`, `repair-projects`,
project moves) take one file per chat, and `doctor` counts a chat whose row
names an earlier part as listed elsewhere. A `history_base` in another shape is
reported (`UNREADABLE_HISTORY_BASE`) and the duplicate rule applies as before.

And a real conflict could only be decided by hand (CS-357…360). Owner: "if a
newer version is clear, let it win; put the rules in the settings so the
console works the same without the window". Decided:

- **One rule for files and chats**: `[conflict] policy`, now
  `prefer_newer_mtime` by default (was `manual_abort`). For a chat "newer" is
  the time of its last record, parsed as a moment, never the file's mtime,
  which Codex kept through its September rewrite. Two chats that end at the
  same moment, or one without a time, still ask (`RULE_CANNOT_DECIDE`), except
  a pure format rewrite, where the newer format is kept. `prefer_local` /
  `prefer_cloud` always decide; `manual_abort` blocks as before.
- **The loser is never lost**: a chat decided by the rule goes through the same
  path as one decided by a person — the losing branch whole into the conflict
  bundle before anything is replaced (`RESOLVED_BY_RULE`, `RULE_<NAME>`) — and
  a file goes into the verified backup first, as every overwrite always did.
- **A person's choice outranks the rule**: a recorded resolution about exactly
  these bytes, `DEFER` included, is applied as before.
- **A one-way `sync.direction` decides too**: `to_cloud` keeps this machine's
  copy and never writes `.codex`, `to_local` the reverse; a write the direction
  does not go is `HELD_BY_DIRECTION`, which blocks nothing.
- **The rule is frozen into the plan** (`TransferPlan.conflict_rule`,
  `direction`, left out of the id material at their defaults) and an apply
  rebuilds under it, like the mirror codec: the id the user confirmed decides.
- **It applies in every run.** Amends D-016: `--unattended` no longer forces
  `manual_abort`; the configured rule is the person's decision made in
  advance. Amends D-018: a handoff stops only on what the rule leaves open.
- **One-run override**: `--conflict-policy` on `sync`, `handoff sync` and
  `sessions scan`; in the window, a stopped sync offers *Keep the newer
  copies* / *this machine's* / *the cloud's* where it stopped, with *Always
  decide this way* writing `[conflict] policy` through the Settings save path;
  the Sessions page decides all conflicts of a scan by one rule.

This amends `AI_RULES.md` §6 ("conflict: no writes, manual resolution"): that
remains the behaviour of `manual_abort`, which is still one setting away.

## D-028: `sync` carries everything by default, as in 0.1
In 0.1 `codexsync sync` copied `sessions/` whole, so one command carried the
chats. 0.2 made sessions semantic-owned and carried them through `handoff
sync` and the window's *Synchronise*, which left `sync` copying settings files
only — a 0.1 user, and every task that runs `sync`, silently stopped carrying
chats (owner, 2026-10-06: "make sync copy the sessions as in 0.1").

Copying `sessions/` by modification time is not brought back: it is what lost a
history continued on two machines, it cannot see Codex's own September rewrite,
and a copied file Codex's catalogue does not list stays invisible (D-015,
D-024). Instead `[sync] scope` decides what `sync` carries: `full` (the
default) runs exactly the full sync — `run_handoff`, settings, chats, projects,
names, the catalogue request, the handoff record — and `settings` keeps the
pre-0.2 files-only run. `--scope` overrides it for one run; the dry run of a
full sync (`preview_full_sync`) builds the same plans `run_handoff` would and
writes nothing. The window's *Synchronise* always runs the full sync. Amends
D-016: the sign-in task runs `sync`, so it follows `[sync] scope` too.

## D-029: Codex may be asked to quit before a sync, never forced
0.1 could stop Codex with `taskkill /T /F` on request; 0.2 removed that because
a forced stop is a crash to Codex -- the global state and its SQLite catalogue
can be left half written, and a running agent turn is lost -- and `AI_RULES` 2
made closing Codex the person's step. On 2026-10-06 the owner asked to have it
back: a person who starts a sync with Codex open, and has set things up that
way, wants Codex closed for them.

What came back is a request, not a stop. With `[sync] close_codex = true` (off
by default; on the Automation page, where automation is set up) a sync that
finds Codex open asks it once to quit and waits up to a minute for the process
check to see it gone; if it declines or stays, the sync is refused with why and
writes nothing. On Windows the desktop app is `ChatGPT.exe` from the
`OpenAI.Codex_` package (one window for ChatGPT and Codex since 26.930); its
close button only hides it to the tray, so the request is the Restart Manager's
-- what Windows uses to close an app for an update -- with `RmShutdown` flags 0,
never `RmForceShutdown`, aimed at the app's root process. Observed on the
reference machine before it was written (`PROVEN_CLOSERS`): answer 0, all
twelve `ChatGPT.exe` and both `codex.exe` gone in two seconds, the state intact.
macOS asks the app to quit by bundle id and stays gated until seen on a Mac. A
Codex CLI in a terminal is never asked. The handoff watcher never asks: it acts
when Codex closes, and a Codex open at its start is the person's. A dry run
never asks. Amends `AI_RULES` 2 and the "codexSync never starts or stops
Codex" rule: it still never stops Codex; it may ask Codex to stop itself.


## D-030: On-disk formats are a compatibility promise from 0.2.0a1
0.2 ships as a series of alphas, and the people running them keep real work in
the folders codexSync writes. A later alpha that cannot read what an earlier
one wrote would cost exactly the people who are helping (owner, 2026-10-06:
"compatibility between alphas is needed -- we will keep building on it").

From 0.2.0a1 on, everything codexSync writes into the shared workspace and its
own folders stays readable by every later version: the sync manifest with its
per-machine baselines, handoff records, the peer boards (`projects/`,
`chat-names/`, `project-files/`), the semantic store (manifest entries,
conflict bundles, `superseded/`), mutation journals and the thread-catalogue
marker beside them, stored plans and working sets, Guardian snapshots, the
latest-good pointer and quarantine, backup snapshots and their manifests,
copies of `.codex` and `config-history/`. A format changes only by adding a
field an older reader ignores, or by a new format name or version **with a
reader kept for the old one**; a field is never given a new meaning. A file a
reader refuses is a refusal with a reason, never a silent fresh start.

It is enforced, not just promised: `tests/fixtures/ws-a1/` was written once by
the 0.2.0a1 code through its real writers (`scripts/make_a1_fixture.py`, two
invented machines, invented chats and projects), and `tests/test_a1_compat.py`
holds today's readers to it. The fixture is frozen -- regenerating it with a
later version would test that version against itself. A change that makes the
test fail is the bug; the fixture is never edited to fit it. `superseded/`
(a record-format migration) is the one kind the fixture does not contain, as
producing it needs two record formats of one chat.

Going back is not covered. 0.1.2 reads the per-machine manifest as having no
baseline at all (it knows only a top-level `files` table), so its next sync is
a first sync decided by modification time, with `sessions/` copied whole as
0.1 always did -- beside the mirror's compressed copies -- and its save
rewrites the manifest as one unkeyed `files` table, dropping every machine's
0.2 baseline; the next 0.2 run then starts from a first sync too. A machine
that has synchronised with 0.2 should stay on 0.2.
