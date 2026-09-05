# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-09-05

Guardian, a safety spine for every mutation, machine handoff repair, semantic
session transfer and chats. Every build reports its own version through
`codexsync.__version__`, which is read from installed package metadata.

### Added
- `sessions index`: a read-only report of what each side's `session_index.jsonl`
  holds and where the two disagree, plus the same check in `doctor`. The index
  is an append/update journal, so a repeated id and a session with no line at
  all are normal and neither is a warning; what is reported is a record that
  will not parse, a half-written tail, a divergent record for one session, and
  the two plausible readings of a repeated id disagreeing
  (`REDUCTION_AMBIGUOUS`), which happens exactly when a clock ran backwards. No
  index is rewritten while the consumer contract is unproven, and the report
  says so rather than leaving the silence unexplained. Session ids and thread
  names never appear: a divergence is addressed by a hashed id.
- `semantic.mirror_compression` (`none|gzip|xz`, default `xz`): the cloud
  mirror stores each session branch in a compressed container. Only the
  mirror — a branch written back into `.codex` is always plain JSONL. Every
  value that decides anything is taken from the decompressed stream, so a
  compressed copy compares `IDENTICAL` to the plain branch instead of looking
  like a divergence, and the container is named in the mirror layout id and
  hashed into the plan id. Per branch rather than one archive: an archive of
  everything would be re-uploaded whole after a single session grew by a line.
- Guardian: immutable, verified snapshots of `.codex-global-state.json` taken
  while Codex is running, written only outside `.codex` (`guardian watch`,
  `guardian snapshot --once`, `guardian scheduler`).
- Central process-safety policy (`safety_gate`): every command is classified as
  read-only or mutating, `UNKNOWN` process state fails closed, and a mutation
  requires a continuously stopped window plus a final check before commit.
- Durable mutation evidence: per-operation lock (`operation_lock`) and a
  payload-free journal (`mutation_journal`), inspectable via `recover inspect`.
- `recover resume` and `recover rollback` close an interrupted journal. Before
  this an interrupted mutation was a dead end: the journal blocked every later
  `sync`/`restore`/`repair-projects apply` and could only be cleared by deleting
  the file by hand. The journal now records the backup snapshot the operation
  created, so a rollback can be proven rather than guessed.
- Read-only project analysis and exact-plan apply (`repair-projects scan|apply`)
  with host-independent path mappings, including `--dry-run` for the apply.
- Backup snapshots carry a verified `codexsync-backup-v1` manifest; legacy
  snapshots require an explicit id plus `--allow-legacy-snapshot`.
- Session catalog and read-only SQLite audits surfaced in `doctor`/`preflight`.
- `session_index.jsonl` parsing, three-way merge and a rendering gate. The
  consumer contract is treated as unproven: both plausible reductions
  (last-line-wins, max `updated_at`) are computed, disagreement is reported as
  `REDUCTION_AMBIGUOUS`, and rendering a new index is refused until a controlled
  experiment records the runtime's real behaviour
  (`docs/experiments/session-index-contract.md`, with a fixture generator in
  `scripts/experiments/`).
- `sessions scan` and `sessions resolve`: branch classification across two
  machines into `IDENTICAL`, `FAST_FORWARD_LOCAL`, `FAST_FORWARD_REMOTE`,
  `ARCHIVE_TRANSITION`, `DIVERGED`, `DIVERGED_NO_COMMON_RECORDS`, `INVALID` and
  `MISSING_BASE`, with a frozen plan id, versioned conflict resolutions pinned to
  both branch hashes, and refusal of stale decisions. Comparison streams both
  files and falls back to raw bytes wherever canonical JSON equality cannot be
  proven, so two different records can never collapse into one.
- `sessions apply`: cold transfer of whole branches through the same envelope as
  sync and restore — operation lock, mutation journal, complete verified backup
  before the first replace, staging plus atomic replace, and a post-write re-read
  of every branch against the plan. The plan is rebuilt and its id re-checked, so
  a state that moved since the scan refuses the apply. A branch that loses a
  resolution is copied into an immutable conflict bundle under `semantic.root_dir`,
  because backup snapshots expire under retention and a divergent history must
  never be the sole copy in something that expires.
- A write into the Codex state directory is also refused unless the runtime's
  own thread catalogue already places that branch at exactly that path
  (`SESSION_NOT_IN_CATALOG`, `CATALOG_PLACES_ELSEWHERE`, `CATALOG_UNREADABLE`).
  Every session on an observed machine has a catalogue row naming its rollout
  file, so a branch written anywhere else is invisible with no error, and making
  the runtime see a new session would need a row codexSync does not write. Only
  the thread id, rollout path and archived flag are read; titles and messages in
  the same table are never touched. No catalogue at all constrains nothing.
- Versioned semantic manifest: one atomically replaced, self-verifying entry
  per machine and session, recording the branch hash, record count, state,
  parent and the base an agreement established. A completed transfer writes it,
  so an active/archive transition can be decided from a proven ancestor instead
  of being refused forever as `MISSING_BASE`. Entries from another machine are
  read and accepted only when their digest verifies, they claim the directory
  they sit in, and their generation has not gone backwards.
- The manifest stores no session payload, and the earlier publication API that
  copied one was removed. Nothing prunes this store and `semantic.root_dir` may
  live in the user's cloud folder, so copying every reconciled session would
  duplicate the whole session directory there and keep growing per edit. Full
  payloads remain only in conflict bundles, where the losing branch would
  otherwise survive nowhere but a backup that retention expires.
- Writing a transferred branch *into the Codex state directory* is gated on a
  proven target layout (`docs/experiments/session-layout-adapter.md`); a session
  bound to SQLite is reported as `UNSUPPORTED_STATE_BACKEND` rather than
  half-transferred. Writing towards the cloud folder is not gated: that copy is
  codexSync's own mirror, so a branch keeps the relative path it has locally
  (`codexsync-mirror-v1`). Without this the mirror could not be rebuilt at all,
  because session state is excluded from generic mtime copying.
- `repair-projects` proposes `REMAP_ROOT` when an existing project's recorded
  root maps onto the directory the sessions actually point at: the project keeps
  its id and no session file is touched. Rewriting a `cwd` inside a session would
  instead make the same history on two machines permanently divergent.
- A remap always carries bindings for the chats it would otherwise detach. Older
  chats recorded the previous directory, and pointing the project elsewhere is
  precisely what removes them from the sidebar, so every session under the old
  root is pinned to the project with an explicit `thread-project-assignments`
  entry. A plan that would still leave one behind reports
  `REMAP_ORPHANS_SESSIONS` and cannot be applied.
- Tests proving Guardian performs no write, create, delete or rename inside the
  Codex state directory, by intercepting the mutating filesystem calls rather
  than comparing the tree afterwards.
- A static guard that fails on any name used but never imported or defined,
  standing in for the linter the project does not run.

- `chats list` / `chats tree`: a read-only view of which chats exist and which
  project each is in. A chat is told apart from a thread an agent spawned by the
  structure the runtime records (`parent_thread_id`, `thread_source`), never by
  reading the conversation. Each chat reports *why* it is where it is: pinned by
  an explicit binding, derived from its recorded directory, or reachable only
  through a `[[path_mappings]]` rule — the last meaning Codex itself shows it
  under no project, which is the state a machine handoff leaves behind.
- `chats move`: put chosen chats under one project by writing a
  `thread-project-assignments` entry in the detected schema's shape. Preview
  first; nothing is written until the command is repeated with the plan id it
  printed, and that id covers the exact state bytes, so it stops matching if
  anything changed. Runs through the same envelope as every other mutation.
- Guardian recognises a desktop state that has no thread assignments yet. With
  no bindings and no app-server ids, nothing the two adapters read could tell
  the shapes apart, so the legacy adapter claimed a brand-new Electron state and
  the repair writer would have created legacy-shaped project entries inside it.
  The project entry's own `rootPaths` key now settles it in both directions.

### Not part of this release

- **GUI groundwork only.** `codexsync[gui]` exists as an optional extra with a
  Qt-free controller, a launcher and one read-only screen, and the boundary is
  enforced by tests: nothing in it reaches the sync engine, the backup manager,
  the operation lock, the journal or the safety gate, and the core and CLI keep
  zero runtime dependencies. It is **not a finished interface** and 0.2 is a
  command-line release. Do not install the extra expecting to drive codexSync
  from a window.

### Changed
- `sessions apply` is partial by design. A conflict or a target collision still
  refuses the whole plan, because each names a decision only the user can make;
  an item blocked on an unproven layout or a SQLite-held binding is reported and
  left in place, since neither is a decision anyone can take today and refusing
  on them would mean the cloud mirror can never be written.
- codexSync never terminates Codex. The termination CLI flags and
  `allow_terminate_if_running=true` are rejected for mutation commands.
- Session state (`sessions/`, `archived_sessions/`, `session_index.jsonl`,
  global project state, SQLite) is semantic-owned and excluded from generic
  mtime copying; `sync.session_mode=last_date_only` is rejected for mutations.
- `doctor`/`preflight` are side-effect free: path checks no longer write probe
  files and the local/cloud mtime-drift probe was removed (see `D-010`).
- The Windows exe build excludes PySide6, so the command-line binary cannot
  grow a Qt payload just because the build machine has the optional extra
  installed. Measured at 9.8 MiB.

### Fixed
- The read-only SQLite audit no longer writes inside the Codex state
  directory. Opening a WAL database creates `-wal` and `-shm` beside it when
  they are absent, and rebuilds a stale `-shm` when the write-ahead log is
  empty -- SQLite does both in C, below `test_guardian_state_isolation.py`,
  which can only see Python-level calls. Measured on a real machine: `doctor`,
  every `sessions scan` and every apply moved `state_5.sqlite-shm`'s timestamp,
  and on a cleanly closed database created both sidecars outright. The
  connection is now chosen by whether the log holds frames: none means the main
  file is the whole database and `immutable=1` reads it while creating and
  rebuilding nothing; frames with a shared index present is the running case
  and creates nothing either; frames without one is refused as
  `WAL_WITHOUT_SHARED_INDEX`, reported as `INDETERMINATE` and never as `ABSENT`,
  since those two mean opposite things to a caller deciding whether it may
  write. After the fix a read leaves the database, its log and its shared index
  byte- and timestamp-identical, with all 251 catalogue rows still returned.
- An ordinary `sync` never transforms a file because of its name. Staging read
  the container from the source file name, so a user's file called
  `notes.jsonl.gz` under an included root was written to the destination
  decompressed but still named `.gz`, and the next sync in the other direction
  failed trying to unpack plain text. Transforming is now opt-in: only a
  transfer plan, which knows it is carrying a session branch, asks for a
  container.
- A branch the cloud mirror already holds keeps the container it is stored in,
  and only a branch the mirror lacks gets the configured one. The container is
  part of the destination name and nothing deletes the old name
  (`delete_policy` is never), so turning compression on over an existing plain
  mirror wrote `rollout-x.jsonl.xz` beside `rollout-x.jsonl`: the catalogue
  read the pair as `DUPLICATE_SESSION_ID`, both dropped out of `valid`, and the
  session was never compared, mirrored or fast-forwarded again, with no error.
  An item that keeps its container is reported as `MIRROR_CONTAINER_KEPT`;
  converting an existing mirror needs a delete and is refused, like an archive
  transition.
- A truncated or corrupt container is classified rather than raised. `gzip`
  raises `EOFError` and `lzma` raises `LZMAError`, neither of which is an
  `OSError`, so a half-written file in a mirror a cloud client is still copying
  ended a scan with a traceback. It is now `READ_ERROR`/`INVALID` in the
  catalog, `INVALID` in a branch comparison, and a `FailSafeError` when a
  freshly written branch cannot be read back.
- An empty `session_index.jsonl` is reported as empty (`EMPTY_INDEX`) instead of
  as an unrecognised consumer contract, which warned about a freshly created
  index.
- A transfer plan from a build without `mirror_layout_id`, and a layout template
  with an unbalanced brace, are refused with a message that says which is which.
- A resumed session is no longer treated as a damaged one. The runtime writes a
  fresh `session_meta` record every time a session is picked up again, and the
  scanner marked any second one `DUPLICATE_SESSION_META` and the whole file
  invalid. On the machine this was measured against that was 54 of 252 session
  files — 267 MiB of 864 MiB, up to 298 resume records in a single file — and
  every one of them was silently dropped from `valid`: never mirrored, never
  compared, and never listed, so `chats tree` showed 98 chats where there are
  152. The id, cwd, timestamp, source, originator and parent are identical
  across every repeated record in all 54 files, so a repeat whose id matches is
  now the observational code `RESUMED_SESSION`. A file carrying two *different*
  identities is still refused, as `CONFLICTING_SESSION_META`: which history it
  holds cannot be decided.
- `chats tree` counts projects rather than groups. The `(no project)` bucket is
  a group but not a project, so a state with 16 projects reported 17.
- A proven session layout can describe the state directory that actually
  exists. The template could name only the state folder and the file name,
  while a real machine holds two shapes at once: 228 active branches under
  `sessions/<year>/<month>/<day>/` and 23 archived ones flat in
  `archived_sessions/`. Under the old template 176 of 196 sessions rendered to
  a path the runtime's own thread catalogue does not name, so the return
  direction would have been refused as `CATALOG_PLACES_ELSEWHERE` the moment
  the gate opened — or, had it been allowed, written sessions the runtime never
  shows. Templates now take `source_dir` and `session_id` as well, and empty
  segments collapse so one template covers both shapes. The fault was
  unreachable behind an empty `PROVEN_LAYOUTS` and invisible to fixtures that
  encoded the assumed flat shape; it is the same class as the two below.
- `repair-projects` reads project roots through the detected schema. It looked
  for `root`/`path`/`cwd` keys, which the Electron desktop build does not use —
  it keeps roots in a `rootPaths` list — so on a real state no existing project
  was ever recognised: every session produced `ADD_PROJECT`, and the apply then
  refused because creating an Electron project entry is not supported. Thread
  bindings were compared the same way, a bare id against an object, so a
  correct binding was always reported as missing. This is the same class of
  fault as the Guardian one below, found by running against a real state file.
- Guardian recognises the state written by the Electron Codex desktop build.
  Its bindings carry `projectKind`/`projectId` and its app-server ids live in a
  per-host map, neither of which the only existing adapter accepted, so a real
  state file was rejected as `UNKNOWN_SCHEMA` and quarantined: no snapshot was
  ever committed and `latest-good` never existed. The protection was inert on a
  real machine while the suite stayed green against fixtures written to the
  assumed shape. The new adapter is a separate versioned one, not a loosened old
  one, and the schema id is recorded in every snapshot manifest.
- `repair-projects apply` writes bindings in the shape the detected schema uses
  instead of assuming the legacy one, and refuses to invent a project entry for a
  schema whose entry fields have unconfirmed meaning.
- `doctor`/`preflight` now report the state schema and whether a restorable
  `latest-good` snapshot exists. Both conditions above were previously invisible.
- CI now also runs Python 3.13, which `pyproject.toml` already advertised.
- `repair-projects apply` now reports an unreadable or malformed `--plan` file as
  a caller error (exit `4`) instead of an internal error (exit `1`).
- The packaged `config.example.toml` shipped `sync.session_mode = "last_date_only"`,
  which every mutation command rejects — so a config produced by `init-config`
  could not sync, restore, repair or recover at all. The template now ships
  `"all"`, and a test asserts the shipped template passes the same mutation
  compatibility check the commands run.
- A destination momentarily held open by another process (cloud client, search
  indexer, antivirus) aborted the whole mutation with `WinError 5`/`32`. The
  commit now retries a transient lock with bounded backoff, re-proving process
  safety before each attempt (see `D-011`).

### Removed
- GUI termination confirmation prompt (`gui_prompt`), together with the
  termination flow it belonged to.

## [0.1.2] - 2026-03-21

### Fixed
- Corrected project links in package metadata to the canonical repository:
  - `https://github.com/kroxiksut/codexSync`
- This ensures PyPI project/repository/issues links point to the right GitHub repository for new releases.

## [0.1.1] - 2026-03-21

### Added
- New CLI command: `init-config`.
- `init-config` generates `config.toml` from packaged template and supports:
  - `--output <path>` for custom destination.
  - `--force` to overwrite existing file.
- Packaged template file `src/codexsync/config.example.toml` is now included in both wheel and sdist.
- CLI tests for `init-config` generation and overwrite behavior.

### Changed
- PyPI metadata and docs updated for `0.1.1`.
- README and scheduler docs now document `init-config`.
- AI context/rules explicitly define `init-config` as the config bootstrap mechanism.

## [0.1.0] - 2026-03-21

### Added
- Initial public MVP release.
- Cold-sync workflow with preflight diagnostics, planning, sync, restore, backup-first safety, and conflict handling.
