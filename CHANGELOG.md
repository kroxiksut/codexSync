# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - Unreleased

The window, Guardian, a safety spine for every mutation, machine handoff
repair, semantic session transfer and chats. Every build reports its own
version through `codexsync.__version__`, which is read from installed package
metadata.

### Added
- **Sync history.** Every real sync's journal now records how many files went
  each way, who started it (`window`, `cli`, `unattended`), when it ended and,
  for a failure, the kind of error (never its message). `codexsync history`
  lists past runs (`--family all`, `--json`, `--limit`); the window shows them
  on a **History** tab of *Synchronisation* and the last run on *Overview*.
  Journals written before this show their total only; a dry run and a run
  stopped before its first write are not listed.
- **Sync after sign-in, as a checkbox** (`[scheduler] sync_at_login`, `D-016`).
  Off by default. A second user-level task runs `sync --apply --unattended` once
  after signing in; it is refused while Codex is open, any conflict stops it
  before a write, and Settings shows its last run and what the result meant.
- **The window no longer invents a config location.** With no `config.toml`
  found, pages offer to open one from any folder or to create one where you
  choose, instead of working against a made-up `%APPDATA%` file. Picking an
  existing file in the first-run form opens it. The window records the opened
  path in a pointer file (`%LOCALAPPDATA%\CodexSync\config-path.txt`), which
  the command line reads too, so both work on the same file.
- **`codexsync --version` (`-V`).** Until now the version appeared only on the
  window's About screen, so a downloaded exe could not be asked what it was —
  which is how both builds reported `0.0.0+unknown` without anyone noticing.
  `-v` still means `--verbose`.
- **Both shells now look for `config.toml` in the same places.** `-c` still
  names the file (including one to be created), but without it the command line
  used to mean the literal `config.toml` in the current directory, so a machine
  set up through the window — whose config sits in the per-user location, or
  beside a downloaded exe — answered every terminal command with "Config file
  not found". The search order (current directory, beside the executable,
  per-user location) lives in `config_locations.py`, which both shells use, and
  a config found outside the current directory is named in the log.
- **A config written by 0.1 can be upgraded from either shell.** The template
  0.1 shipped set `allow_terminate_if_running = true` and
  `session_mode = "last_date_only"`, both of which this version refuses for
  every mutating command — so an upgraded install answered `sync`, `restore`,
  `repair-projects apply` and `recover` with exit 4, and the Settings screen
  could not fix it (it has no field for the first key, and the save path runs
  the same check over the text it is asked to save). `config check` now reports
  every difference with its code, its exact edits and a diff; `config upgrade
  --confirm-plan <id>` applies them in one write, and the window offers the
  same thing on Settings as "Config from an earlier version". The plan id
  covers the file's bytes, so a config edited in between stops the upgrade;
  comments survive, arrays grow and shrink line by line rather than being
  re-rendered, an optional finding can be declined (`--skip CODE`), and the
  replaced file is kept in `config-history/`. `doctor` reports this as
  `config_compat` instead of saying `config: PASS` about a config nothing can
  write with.
- **What this version knows about Codex's processes lives in one module.**
  `process_knowledge.py` is now the single source for
  `process_detection.process_names` and the per-OS background markers: the
  loader defaults to it, the Settings screen renders it, the shipped template
  is checked against it, and the config upgrade offers it. Before this the
  loader still defaulted to what 0.1 knew, so a config without a
  `[process_detection]` section — or one written by 0.1 — detected fewer Codex
  processes than this version can, silently, and names are matched whole.
- **The scheduled task is per account, and says when it is broken.** On Windows
  it is registered as `CodexSync Job (<user>)`: the single shared name meant
  one account's `automation apply` overwrote another account's task and
  `automation remove` deleted it. A task belonging to another account is now
  reported (`FOREIGN_TASK`) and never written or removed, and a task still
  under the old shared name is found, reported and re-registered under this
  account's name (the old one is deleted only after the new one exists, and
  only when it was ours). `automation status` reads back what the installed
  task actually runs, so an executable that was renamed or moved — what an
  upgraded frozen install leaves behind — is named as `EXECUTABLE_MISSING` or
  `EXECUTABLE_MOVED` rather than a vague "differs from configuration", on all
  three platforms.
- **Codex rewriting its own sessions is recognised.** The September 2026
  desktop build rewrote every session file into numbered records (keeping each
  file's mtime and dropping rolled-back turns on the way), which made a mirror
  written earlier conflict on every session and refused `sessions apply` as a
  whole. The catalogue now records each branch's record format; such a conflict
  is marked `FORMAT_MIGRATION` and stays a conflict, and
  `sessions resolve --format-migrations` (or one button on the Sessions screen)
  records the newer-format side for all of them at once, except where the old
  copy has a later record (`OLDER_FORMAT_HAS_LATER_RECORDS`). Each overwritten
  old copy is kept once, compressed, under `superseded/` in the semantic root
  rather than as a two-branch uncompressed conflict bundle. `doctor` reports the
  format of both sides (`session_format`), and chat titles are read from the
  rewritten records too.
- **A chat without its folder here is named in the session plan.** A branch
  bound for `.codex` whose working folder, mapped through `[[path_mappings]]`,
  is not a directory on this machine carries `CWD_ABSENT_HERE`
  (`CWD_MAPPING_AMBIGUOUS` when two rules disagree); `sessions scan` counts
  them in `cwd_absent_here` and the Sessions screen says how many. It blocks
  nothing and is part of the plan id, so a folder created after the scan asks
  for a rescan. Plans whose folders all exist keep their id.
- **The window.** `codexsync[gui]` is now a working interface over the same
  core: eleven screens (overview, first run, synchronisation, chat bindings,
  sessions, projects, snapshot guardian, backups, recovery, settings, about),
  English, Russian and Chinese from language files with per-language plural
  rules, a light and a dark theme from the design tokens, and the CodexSync
  icon. Every mutation is
  plan or dry run first, then a confirmation that quotes the exact plan id;
  restore, resume and rollback unlock only after a dry run of the same choice.
  The window reaches the core only through `app.py`, enforced by a test.
- **About screen.** What the program is, the four rules every write obeys, what
  it deliberately leaves to the user, and *this build* — version, packaged
  executable or source checkout, program file, Python, system and the config in
  use — copyable in one click for a bug report and holding nothing that comes
  from the Codex state. Plus the licence (GPL-3.0-or-later, the commercial
  path, and Qt through PySide6 under the LGPL-3.0) and links that open the
  documentation in the window's language.
- **Chinese.** `gui/locale/zh.json` completes the catalogue, so the window runs
  in 中文 like any other language, and the user documentation has a third twin:
  `README.zh.md` + `docs/zh/`, with its own screenshots under
  `docs/screenshots/zh/`. `tests/test_docs_links.py` holds all three languages
  to the same pages, switches and links.
- `config_edit`: `config.toml` edited in place without losing comments, validated
  by the CLI's own loader, refused when the file changed on disk since it was
  opened, saved atomically with the replaced version kept in
  `<workspace>/config-history/`. `create_config` writes a new config from the
  template. `load_config` now reports a TOML syntax error as a configuration
  error (exit 4) instead of a traceback, and accepts a UTF-8 byte-order mark.
- `[scheduler]` is a real, validated section (`enabled`, `mode` =
  `guardian_snapshot|preflight|sync_dry_run`, `interval_seconds` ≥ 60,
  `run_at_login`, `startup_delay_seconds`, `jitter_seconds`); legacy keys are
  ignored. `system_scheduler` installs, inspects and removes the user-level OS
  task (Task Scheduler, launchd, `systemd --user`) that runs that job, and can
  express no mutating job at all. `automation` applies the section to the task
  and reports whether the installed task still matches the config.
- Read-only listings for the window: `list_backup_snapshots`, `list_journals`
  and `read_guardian_inventory`. None of them creates, rebuilds or prunes
  anything.
- `guardian accept` and the "Accept the current state as the new baseline" card
  on the Guardian screen (`D-014`). After a real drop in projects or bindings
  Guardian compared every later state with the baseline from before it, so
  `latest-good` never moved again — the case on the machine this was built
  against since 2026-09-13, when Codex re-created all its projects under new
  ids. The preview explains the drop in counts; confirming commits the current
  state through the ordinary store path, marked `SHRINK_ACCEPTED`. A state that
  fails validation cannot be accepted, and retention keeps both the accepted
  snapshot and the baseline it overrode. `doctor` now warns when shrink
  quarantines are newer than `latest-good` instead of reporting `PASS`.
- `sync.direction` (`bidirectional|to_cloud|to_local`) and `sync.delete_policy`
  (`never|propagate`), decisions `D-012` and `D-013`. A one-way run records the
  paths it skipped and the manifest carries their previous entry over unchanged,
  so a later two-way run still sees the difference instead of taking the older
  file for the newer one. A propagated deletion happens only where the previous
  manifest proves both sides held the file and this side has not changed since;
  the verified backup is written first, in the same envelope, so `recover` can
  put the file back. The first run after switching either on changes nothing.
- A working set for session transfer (`sessions scan --project/--chat/--scope-file`,
  and the "Working set" block on the Sessions screen): bring one project's chats
  onto this machine instead of all 864 MiB. The cloud mirror is never narrowed,
  so the backup stays complete; a held-back branch is `OUT_OF_SCOPE`, blocks
  nothing, and keeps what it would have been in its codes. The set is part of the
  plan id, and a plan without one keeps exactly the id it had before.
- Progress for the long reads (`progress.py`): `scan_sessions`,
  `build_chat_directory`, `scan_chats`, `scan_session_transfer` and
  `scan_repair_projects` take an optional `progress(phase, done, total)`, and the
  window shows "Reading sessions 120 of 252" in the page and the status bar. The
  total is known before the hashing starts; a callback that raises never fails
  the read.
- A POSIX process detector for macOS and Linux, matching whole basenames plus
  the 15-character truncation `ps` produces on Linux, and path markers such as
  `ChatGPT.app/Contents/MacOS/` (the desktop build is called ChatGPT since
  2026-07-09). `capability()` keeps both platforms closed until
  `docs/dev/experiments/process-detector-macos.md` has been run on one.
- Settings: a tree picker for `targets.include_roots` over both state
  directories (credential files are never listed, semantic-owned paths are shown
  with their reason and cannot be ticked), a form for `[[path_mappings]]` rules
  with suggestions taken from the chats' own folders and a "this rule reaches N
  chats" preview, the computed value under every path field with the
  substitution rules beside it, every sync option offered with a visible reason
  where the rules forbid one, and the language chooser moved into the page header.
- Synchronisation screen: search, direction and folder filters with a
  "showing X of Y" counter, and a line stating that a sync always applies the
  whole plan.
- Projects screen: one selectable row with "Show its chats", "Move its folder"
  and "Open the folder". Deleting, merging and repointing stay absent while
  `PROVEN_PROJECT_REGISTRY` is empty.
- `doctor` reports what a sync is allowed to do (`sync_rules`) and that projects
  also live in SQLite (`project_registry`).
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
  (`docs/dev/experiments/session-index-contract.md`, with a fixture generator in
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
  proven target layout (`docs/dev/experiments/session-layout-adapter.md`); a session
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

### Changed
- Documentation is split by audience. `README.md` and `README.ru.md` are short
  entry points with the same structure; the user documentation is a set of pages
  in `docs/en/` with a Russian twin in `docs/ru/` and a Chinese one in
  `docs/zh/` (the window with a screenshot
  of every screen, the command line, configuration, sync, Guardian, sessions,
  projects and chats, backups and recovery); developer documents moved to
  `docs/dev/` (`DECISIONS.md`, `PUBLISHING.md`, `experiments/`), and the paths
  printed by refusals follow them. `docs/PROJECT_CONTEXT.md` was removed.
  `tests/test_docs_links.py` keeps every language in step and every link,
  anchor and documented path resolvable.
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

### Deprecated
- `guardian scheduler` and `scripts/scheduler/{windows,macos}`: they keep
  scheduler settings outside `config.toml`. Use `[scheduler]` and
  `automation apply`.

### Removed
- GUI termination confirmation prompt (`gui_prompt`), together with the
  termination flow it belonged to.

### Fixed

- **Settings: a config from an earlier version squeezed the tabs to nothing.**
  The upgrade card sat above the tabs in a page that did not scroll, so the
  form of every tab was about 50 px tall, and in a small window the card's
  own labels and buttons were drawn over each other. The page now scrolls, the
  tabs keep a minimum height, the card is one line until **Details** is
  pressed (it opens itself when something blocks every write), and removed
  finding rows are hidden at once instead of drawing under the new ones until
  deleted. The upgrade findings were shown in English in every language; they
  are now rendered from the language files by code.
- **An always-running Windows service made the safety gate say "Codex is open"
  forever.** `codex-windows-sandbox-service` was listed as a background marker,
  but on Windows it is the service `CodexSandboxService.OpenAI.Codex`, started
  automatically at boot and alive whether or not Codex ever ran. Every config
  generated from the shipped template inherited it, so on such a machine the
  window reported Codex as open with an empty system tray and `sync`, `restore`
  and `repair-projects apply` could never run — the continuously-stopped window
  they require would never arrive. The name is gone from the defaults, the
  template and the documentation, and the rule it broke is now written down: a
  name belongs in `background_process_names` only once it has been observed to
  disappear when Codex closes.
- **The safety gate now names the process it found.** "Codex or known
  background process detected" is equally true of a running Codex and of an
  unrelated service, and telling them apart took a live machine. The reason
  string, the `codex_process` check and the window's banner now carry the
  names and pids; an undetermined state stays a separate sentence, and the
  description can never influence the decision.
- **The windowed build no longer flashes console windows.** `codexsync-gui.exe`
  owns no console, so every `tasklist` and `powershell` the process detector
  started got a new one — up to eighteen per gated check, which is what a
  screen did on arrival. The detector now spawns with `CREATE_NO_WINDOW`, a
  hidden `STARTUPINFO` and an explicit `stdin`, the way the scheduler adapter
  always has, and reads `tasklist` in the console code page instead of losing
  non-ASCII names to UTF-8 decoding.
- **A screen no longer starts scanning because it was opened.** Sync, Chats and
  Sessions each ran a full read of `.codex` on arrival, which looked like the
  program acting on its own and (with the console windows above) alarmingly so.
  They now wait for their button. The Sessions screen still loads the stored
  working set by itself, because that comes from the semantic store, touches no
  Codex file and raises no gate — and without it the screen would claim it was
  about to carry everything.
- **A missing global state file now says which file and which config.**
  `SourceMissingError: .codex-global-state.json` could not distinguish "Codex
  is not installed" from "this is not the config you meant"; the two need
  opposite fixes. The message carries the full path, and a read-only scan
  reports it as a configuration error naming `paths.local_state_dir` and the
  config file it came from.
- **A remembered config path inside the temporary directory is no longer
  trusted.** The window keeps the path of the config it last opened; a config
  left in a scratch directory can outlive its purpose and be opened on every
  later start, against a throwaway state directory. Such a path is now skipped
  in favour of the ordinary search, and reported rather than dropped silently.
  The status bar also says how the open config was chosen, not only where it is.
- **The frozen builds report their real version.** Neither spec carried the
  package metadata `version.py` reads, so both exes answered `0.0.0+unknown` —
  and stamped that into every Guardian snapshot manifest through
  `PRODUCER_VERSION`, leaving snapshots that cannot say which build wrote them.
- **A sync or restore could not write into `.codex` at all when `paths.temp_dir`
  was on another drive.** Every payload was staged in the temp directory beside
  the cloud folder and then moved into place with an atomic replace, which only
  works within one filesystem — on the ordinary layout (cloud folder on one
  drive, `.codex` on another) the first write failed with `WinError 17`, nothing
  was written, and the run ended in `RECOVERY_REQUIRED`. A payload is now
  prepared in the folder of its own destination, so staging and target always
  share a volume; the destination itself is still untouched until every payload
  is staged and verified. Files a killed run left behind are swept from the
  folders the next run writes into, and `doctor` now also counts a staging
  *directory* left in the temp directory, which the orphan check never saw.
- `skills/.system/**` is excluded from sync by default. Those skills are
  installed and removed by the Codex runtime itself, and with
  `delete_policy = "never"` a file it deleted came back from the mirror on every
  run.
- The package metadata claimed `GPL-3.0-only` while the badge, both READMEs,
  `CONTRIBUTING.md` and the licence text say `GPL-3.0-or-later`; `pyproject.toml`
  now says what the project actually is.
- The Settings caption promised that "deletions are never propagated" beside the
  `delete_policy` field that can propagate them.
- The "?" beside every path field in Settings was an empty box: the button's
  ordinary 20px side padding left a 28px button no room for its label.
- The Sessions screen in Russian was wider than the default window, because the
  category counts did not wrap; the page scrolled sideways.
- Recovery showed `guardian-restore` and `project-move` journals by their raw
  family name; both have a label now.
- `filters.exclude_globs` now treats `**` as "any number of path segments".
  `PurePath.match` gives `**` the meaning of `*`, so the shipped `**/tmp/**`
  matched `a/tmp/b` but neither `tmp/arg0/lock` — which is where `.codex/tmp/`
  actually sits — nor anything two levels below a `cache/`. The module had no
  tests; it has them now.
- The window opens inside the screen: only its size is remembered, never its
  position, and after `show()` it is shrunk to the work area minus its own frame
  and centred. A window closed on a second monitor no longer reopens off the
  edge, and 1240x820 plus a title bar no longer pushes the title bar above the
  top of a 1536x864 screen.
- Started without `-c`, the GUI opens the config it last used, then the one in
  the working directory, then one beside the executable, then the per-user one;
  the path (never the content) is remembered. First run offers to open an
  existing `config.toml` and fills in a workspace it found under a cloud folder,
  listing the machine names already filed there as a warning rather than
  selecting one.

- `project-move scan|apply` and the Projects screen: move a project's files to a
  new folder. The project is copied into a folder that does not exist yet,
  every file is re-hashed against the previewed inventory, the verified copy is
  renamed into place, and only then is the root remapped and every chat that
  reached it by path pinned, through `commit_global_state`. The old folder is
  never modified or deleted. A failed commit keeps the verified copy, which a
  rescan recognises as `copy_complete`.
- `guardian list` and `guardian restore`, and restore on the Snapshot guardian
  screen: put a verified, committed snapshot of this machine back into
  `.codex-global-state.json` after a preview and a plan id, through the same
  envelope as every other state edit. A snapshot in another state format than
  the live file (`SCHEMA_CHANGED`) or one that fails today's validation is refused.
- `automation status|apply|remove|run` and `init-config --machine-id
  --local-state-dir --workspace-root [--cloud-root]`, so the command line can do
  what the window does.
- A windowed `codexsync-gui.exe` (`codexsync-gui.spec`) that is also the CLI when
  given a command, so a frozen install's scheduled task runs without a console;
  the CI runs the GUI tests offscreen in a second job.
- The window: chat titles next to session branches, the session index card, a
  status-bar activity indicator where read-only scans can be abandoned, every
  config key on the Settings screen, dark-palette tokens checked for WCAG AA
  contrast, and a light brand mark for the dark sidebar.
- Guardian quarantined every real desktop state again, and every global-state
  commit (chat move, project repair) refused its own result: the Electron
  schema was held to the legacy rule that `project-order` must list every
  project, while the desktop build itself writes projects its order omits
  (observed on 2026-09-13). For `electron-v2` a project missing from the order is
  now `PASS_WITH_WARNING` (`PROJECT_NOT_IN_ORDER`); an order naming a missing
  project, a duplicate in the order and a broken binding are still invalid, and
  the legacy schema keeps its complete-order rule.

- A state that carried a warning and was then judged a suspicious shrink made
  `guardian snapshot` stop with "unsupported reason codes" instead of
  quarantining it; warning codes are now accepted by quarantine, and a test
  reads every code the validators can emit.
- `guardian restore` now refuses a snapshot that shares no project id with the
  live state (`PROJECT_IDS_REPLACED`): Codex had re-created every project since
  the snapshot on a real machine, and restoring it would have dropped them all.
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
