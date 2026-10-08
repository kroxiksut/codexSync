# Changelog

All notable changes to this project are documented in this file.

## [0.2.0a1] - 2026-10-08

**The first alpha of 0.2.** codexSync now hands your Codex work between
machines as one guarded step — settings, chats, chat names and projects — and
comes with a window next to the command line.

This is an alpha because 0.2 substantially expands the state that codexSync
understands and modifies. The safety model is intentional and tested, but wider
real-world validation across Codex installations and machine handoffs is still
needed. "Alpha" does not mean it barely runs: it is in daily use on Windows,
and every write still goes through the lock, journal and verified backup.

**What is new, in short**
- The window (`codexsync[gui]`, or `codexsync-gui.exe`) — every screen also has
  a command, so automation never needs the window.
- A full sync by default: settings, chats and projects in one run, by hand or
  on its own when Codex closes; each machine knows what the other handed off.
- Chats continued on two machines are never merged: one whole copy is kept, by
  your rule or your choice, and the other is saved.
- Guardian snapshots of the global state while Codex runs; restore, recovery of
  interrupted writes, history of every sync, verified copies of `.codex`.

**Install**
- `pip install --pre "codexsync[gui]"` — pip skips an alpha unless asked, so
  `pip install -U codexsync` leaves an existing 0.1 install as it is.
- Windows builds, no Python needed: `codexsync-gui-<tag>-windows-amd64.zip` or
  `-arm64.zip` (the window, which also runs every command) and
  `codexsync-<tag>-windows-amd64|arm64|x86.zip` (the command line only).
- Needs Windows 10 (1809) or later; the window needs x64 or ARM64, since Qt 6
  has no 32-bit Windows build. With pip: Python 3.11+, and for the window on
  macOS, macOS 13 or later.

**Upgrading from 0.1.** Configurations written by 0.1 are recognised; a few
0.1 values are refused by every command that writes, and `config check` /
`config upgrade` (or the window) shows each change before making it and keeps
the previous file in `config-history/`. A task the 0.1 scheduler scripts
installed is named on the Automation page with the command that removes it.
A machine that has synced with 0.2 should stay on 0.2: 0.1 does not know the
per-machine baselines, starts over with a sync decided by modification time,
and its save drops every machine's 0.2 baseline.

**Data written by this alpha stays readable.** Everything 0.2.0a1 writes into
the workspace — manifests, handoff records, journals, Guardian snapshots, the
session store — is read by every later alpha and by 0.2.0 (`D-030`).

**Known limits.** Validated in practice Windows → Windows only. On macOS the
code and CI run, but every write is refused until the process detector has
been observed against a live Codex. Linux is not supported yet (Codex for Linux
is in preview). Some Codex behaviours are deliberately left unused until an
experiment records them:
[what is not proven yet](https://github.com/kroxiksut/codexSync/blob/main/docs/en/README.md#what-is-not-proven-yet).

**Please try it** if you use Codex on two or more machines, and tell us what
happened in an [issue](https://github.com/kroxiksut/codexSync/issues/new/choose).
Never attach real chats, `auth.json`, databases or your `config.toml` — see
[SECURITY.md](https://github.com/kroxiksut/codexSync/blob/main/SECURITY.md).

### Added
- **Builds for ARM64 and 32-bit Windows, and PyPI from the release workflow.**
  `.github/workflows/release.yml` (was `release-exe.yml`) checks that the tag,
  `pyproject.toml` and this file name one version (`scripts/release_meta.py`),
  runs the suite with each build's own interpreter, builds the command line for
  x64, ARM64 and x86 and the window for x64 and ARM64, checks each exe's
  version, and only then uploads to PyPI (trusted publishing) and creates the
  GitHub release — marked pre-release for an alpha, beta or rc tag. The README
  PyPI shows has its links and pictures pinned to the tag. A rehearsal on a
  branch builds everything and publishes nothing.
- **Data written by 0.2.0a1 is a compatibility promise** (`D-030`): a frozen
  workspace written by this version is part of the suite, and every later
  version must read it.
- **A sync can ask Codex to quit first** (`[sync] close_codex`, off by default,
  on the Automation page; `D-029`). When a sync — the button, the sign-in task,
  `codexsync sync` — finds Codex open, it asks the app to quit the way Windows
  closes an app for an update (the Restart Manager, never its force flag),
  waits up to a minute, and goes on once Codex is gone; if Codex declines or
  stays, nothing is written and the sync says why. A Codex CLI in a terminal is
  never closed, and the handoff watcher and dry runs never ask. Proven on
  Windows; off on macOS until seen on a Mac. 0.1's forced `taskkill` is not back.
- **A direction for one run.** `--direction` on `sync`, `handoff sync` and
  `sessions scan`, and a *This run* box beside *Synchronise*, override
  `sync.direction` once — files and chats alike.
- **A task left by 0.1 is named.** The 0.1 scheduler scripts installed a task
  (`codexSyncSync`, or the LaunchAgent `com.codexsync.sync`) that runs
  `codexsync sync` on its own timer — in 0.2 a settings-only sync, so it carried
  no chat and nothing said so. `automation status` and the Automation page now
  name it with the command that removes it; it is never removed for you.
- **The macOS process detector can be observed on a GitHub runner**
  (`.github/workflows/macos-detector.yml`, run by hand): the real Codex CLI is
  started and the detector must see it, see it go, and tell the desktop app's
  bundle apart from the ordinary ChatGPT app. Writing on macOS stays refused
  until a run is recorded in `PROVEN_DETECTORS`.
- **A security policy and issue forms.** `SECURITY.md` says what counts as a
  security or data-safety problem and how to report it privately (GitHub's
  private reporting is on); the bug form asks for version, install, platform
  and surface, and warns against attaching real chats, `auth.json` or
  databases. The README now says first why a cloud folder alone is not enough,
  what stays private, and installs from PyPI or a release build rather than
  from a source checkout.
- **The console does everything the window does.** New: `summary` (the Home
  page, `--recount`), `backups list` (the names `restore --from` takes),
  `sessions scope` (the stored working set), `config set` / `unset` (one value,
  edited the way Settings edit it: comments kept, checked like a load, refused if
  the file moved, the old file kept in `config-history/`; `--dry-run` shows the
  change), `config mapping list|suggest|add|remove`, `config roots` (what can be
  synchronised, one level at a time) and `config history`. What the window does
  not let you edit, the console refuses too.
- **An interrupted run that replaced nothing no longer blocks the next one.**
  A journal left at `PREPARED`/`BACKED_UP` by this machine is closed by the
  next write here (history reason *Abandoned*); a run that entered the commit
  phase, or another machine's, still waits for a person. `codexsync recover
  list` shows what is open and what closes each; in the window a sync stopped
  by an open journal says so in plain words and **Open Recovery** lands on
  that journal, and the Recovery page names each journal's machine.
- **A conflict is decided by a rule you set, everywhere** (`D-027`). A file or
  a chat changed on both machines no longer stops every sync: `[conflict]
  policy` keeps the newer copy by default (a chat by its last message), or
  always this machine's, or always the cloud's — in the window, the console
  and the sign-in task alike. The copy not kept is saved first (backup; a chat
  whole in the conflict bundle), a choice made by hand still wins, and two chats
  that end at the same moment are still asked about. A one-way
  `sync.direction` decides chats too. `--conflict-policy` on `sync`,
  `handoff sync` and `sessions scan` overrides the rule for one run; a sync
  that stopped offers *Keep the newer copies* / *this machine's* / *the
  cloud's* right there, with *Always decide this way*; the Sessions page
  decides every conflict of a scan at once.
- **Chat names travel** (`D-025`). A chat's name lives only in Codex's catalogue,
  so a carried chat showed its first message. Each machine now publishes its
  chat names, and a full sync sets them on chats unnamed here — never over a
  name given here (`codexsync sessions names`).
- **Project folders are checked against the other machine** (`D-026`). Chats
  travel, project files do not; a full sync now says which projects here lack
  the other machine's last commit, diverged from it, miss changes left
  uncommitted there, or hold older files. Nothing is copied or blocked;
  `codexsync projects files` re-checks.
- **Chats brought from another machine show up in Codex** (`D-024`). Codex
  lists chats from its own catalogue, which it filled from the files once; a
  chat file written afterwards stayed invisible (197 on the laptop, every
  project "no chats"). A full sync now asks Codex to rebuild that list from
  the files on its next start — one status row put back to the value Codex
  itself creates, after a verified backup — and says how many chats will
  appear. `codexsync sessions catalogue` shows and does the same on its own.
- **Archived chats follow the other machine** (`D-023`). A chat archived or
  taken out of the archive on one machine is moved the same way on the other:
  written where that machine keeps it, the old file removed after a verified
  backup. Which side moved is read from the last recorded agreement, never
  from clocks. Before, every such chat stopped the full sync for good while the
  Sessions page offered nothing to decide; a chat archived on one machine and
  continued on the other is now the only case that asks.
- **The Sessions page speaks plainly and knows the other machine.** Instead
  of raw codes it says what is left out and why — chats new to this machine
  kept in the cloud copy (with a button to the setting that loads them), copies
  that cannot be read, archive moves ahead. Opened by hand it now selects the
  machine the full sync pairs with, found from the shared workspace even
  without `[[path_mappings]]`; a sync that stops on chats stops only on what
  this page lists as needing a decision.
- **Projects travel between machines** (`codexsync projects sync`, `D-022`).
  0.1 copied `.codex-global-state.json` whole; 0.2 stopped (it holds
  per-machine values) and, until now, carried no project at all, so a laptop
  kept its own sidebar after a sync. Each machine now publishes its project
  list beside the sync manifest and the next one merges it in: projects
  matched by id or folder, the missing ones added as Codex wrote them, nothing
  removed, the other machine's pins and order taken once per new list. Part of
  `handoff sync` and of *Synchronise* in the window, which now runs the full
  sync — settings, chats and projects — and shows which stage it is in.
- **A sync that stops on chats says why and leads to the decision.** It
  lists what kind of decisions wait (only a newer record format, continued
  differently, a file in the way) in the window's language
  and opens Sessions for exactly that pair of machines. *Backups* can also
  make a copy of `.codex` now, the same job as on *Automation*.
- **Handing work between machines** (`[handoff]`, `codexsync handoff
  status|sync|watch`, `D-018`). Close Codex on one machine and it hands off:
  settings and chats go to the cloud copy. Sign in on the other and it waits
  for that handoff to arrive — checked from the files themselves, never from
  the cloud client — loads it, and only then should Codex be started there.
  One file per machine in the synced workspace says whether it is working or
  has handed off and which handoffs it has loaded; ids decide, clocks are only
  shown. Any conflict stops a handoff before its first write. An optional
  fourth task starts the watcher at sign-in (it replaces the sync after
  sign-in), reports with system notifications and warns when Codex is started
  while another machine has not handed off. The *Automation* page shows every
  machine and can hand off now. A chat started on the other machine is placed
  into `.codex` on the receiving one (`[semantic] new_chats`, below); a handoff says how many it wrote and how many stayed in
  the cloud copy instead of calling the work loaded.
- **Opening a config from an earlier version says so at once.** The window checks
  the file it opens (or starts with) and shows a banner above every page with
  *Review and update*, which opens the upgrade card unfolded; a write refused for
  an outdated value is said in the window's language (`ConfigOutdatedError`,
  still exit 4) instead of quoting core's English sentence.
- **Programs are never synced or copied** (`D-021`). A compiled program — Windows
  PE, Linux ELF, macOS Mach-O, recognised by its header — is left out of `sync`
  and of the `.codex` copies whatever the config says: it is built for one
  platform and version, goes out of date, and an installer restores it.
  `plugins/.plugin-appserver/**` and `plugins/.remote-plugin-install-staging/**`
  are also excluded in the template: Codex
  keeps `codex.exe`, the command runner and the sandbox setup there (416 MB on
  the machine this was found on), which are useless on another platform and
  would overwrite another Windows machine's own version. `config check` offers
  the exclusion to an existing config (`MISSING_EXCLUDE_CODEX_BINARIES`).
- **Chats started on another machine can be copied into `.codex`**
  (`[semantic] new_chats = "same_path"`, `D-020`), at the path they have there
  relative to `.codex`, as 0.1 did. On by default — carrying a chat started on
  one machine to the other is what the sync is for; `keep_in_cloud` keeps them
  in the cloud copy instead. A file already at the path is never overwritten. Codex lists such a chat only once it takes the file up
  itself, so `doctor` reports `session_visibility`: how many chat files Codex's
  catalogue does not list.
- **A chat continued on the other machine reaches `.codex`** (`IN_PLACE`,
  `D-019`). A session this machine already holds is written over its own file,
  at the path Codex's thread catalogue names for it, so no layout has to be
  proven. Only when the catalogue names exactly that file and the chat is
  active on both machines or archived on both; the old file is backed up first,
  as for every transfer. `sessions apply` and a handoff both do it.
- **Copies of `.codex`** (`[state_backup]`, `codexsync state-backup create|list`,
  `D-017`). A zip copy of the valuable part of the Codex state directory —
  sessions, the archive, the global state, the SQLite catalogues, `config.toml`,
  rules, skills, memories — into a folder you choose; secrets, caches, logs and
  temporary files are never copied. A copy is taken only while Codex is closed:
  a third user-level task takes one after signing in and/or every N hours and
  *waits* for Codex to close, while a manual copy refuses (exit 3). Every copy is
  verified before it counts, and the oldest of this machine's copies beyond
  `keep` is removed only after that. Off until a folder is chosen.
- **Automation page.** Every task the operating system runs for CodexSync — the
  safe periodic job, the sync after sign-in and the copies of `.codex` — on one
  page with its settings, its OS state, the copies that exist and *Make a copy
  now*. The *Automation* tab left *Settings*; the shared form logic moved into
  one class (`ConfigFormScreen`), so both pages save the same careful way.
- **Home page**, first in the menu and beside *Overview*: the last sync and the
  runs, failures and files moved in 30 days, chats and projects, copies of
  `.codex`, backups, the snapshot guardian and which tasks are on, each tile
  linking to its page. Chat counts need the full scan, so they come from a
  cache every chat scan refreshes (`%LOCALAPPDATA%\CodexSync\cache`, counts
  only) and are shown with the time they were taken.
- **Both exes describe themselves.** Version, product name, description in
  English, Russian and Chinese, and `© 2026 Fyodor Malkov — <project page>` in
  the file's *Details*, generated at build time from the package metadata and
  the window's language files (`scripts/exe_version_info.py`);
  `scripts/check_exe_version.py` reads it back from a built exe.
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
  `guardian_snapshot|preflight|sync_dry_run`, `interval_seconds` from 300
  (5 minutes) to 31 days, 1800 by default,
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
- **`sync` carries everything by default, as it did in 0.1** (`D-028`). With
  `[sync] scope = "full"` (the default) `codexsync sync` is the window's
  *Synchronise* — settings files, chats and projects, the same run as
  `handoff sync` — and its dry run builds every one of those plans and writes
  nothing. `scope = "settings"` keeps the files-only run; `--scope` chooses for
  one run. The sign-in task follows the same setting. Chats are carried by the
  safe transfer, never by copying `sessions/` by modification time.
- **`conflict.policy` defaults to `prefer_newer_mtime`** (was `manual_abort`),
  and an unattended run follows it instead of forcing `manual_abort`
  (`D-027`, amends `D-016`). Set `manual_abort` to keep the old behaviour.
- **The scheduled job runs every 30 minutes by default, and at most every 5.**
  A Guardian snapshot every minute was the default, which is far more often
  than the state is worth recording and meant a process sample and a possible
  write into a cloud-synced folder each time. `interval_seconds` defaults to
  1800 and must be at least 300; a shorter value is refused at load with a
  pointer to `config upgrade`, which raises it to 300. The Automation page
  edits the period in minutes.
- **A true/false setting must be written without quotes.** `"false"` used to
  be read as true, so `backup_before_overwrite = "false"` or
  `require_codex_stopped = "false"` meant the opposite of what it said. Any
  value that is not a TOML boolean is now a configuration error naming the key.
  `validate` also notes a config without `targets.include_roots` (fine for
  Guardian, refused by `sync`).
- Documentation examples no longer use real project names or chat ids.
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
- **A chat decided by the rule on both machines no longer stops every later
  sync.** The conflict bundle's id sorts the two branches, but finding an
  existing bundle compared them by position, so the second machine to decide
  the same conflict — its own branch on the other side — was refused with
  `FailSafeError` (exit 5) on every sync after. Found by the frozen 0.2.0a1
  workspace (`D-030`); either order is now accepted.

- **A full sync that had delivered everything could end without telling the
  other machine.** Projects, chat names and the request for Codex to list new
  chat files run after the settings and chats; one of them failing — another
  machine's names arriving mid-run, Codex starting, a locked database — stopped
  the run before the handoff record. Each is now logged and reported as not done
  this time, the record is written, and the next sync does it again. Each step
  also builds its plan once instead of twice, which spares a full read of Codex's
  database per step.
- **Writes into Codex's chat catalogue check once more that Codex is closed**
  right before the transaction, after the journal write that a cloud client may
  have held for seconds.
- **The *newer* rule no longer decides a chat whose last message time is
  unknown** in favour of the newer record format; it asks, as for two chats that
  end at the same moment. Record times are compared as moments there too.
- **Two moves of the same project at once** (the window and the console) could
  remove each other's copy in progress; the whole move now holds the state
  root's lock, not only the final state write.
- **Moving a project with a deep `.git` failed with `WinError 3` and left a
  partial copy.** The copy goes through a staging folder beside the target,
  whose name made a 219-character path inside `.git` exceed Windows' 260-character
  limit; and git's read-only object files then stopped the cleanup. The copy is
  now made with extended-length paths, read-only files are cleaned up, the scan
  warns when the finished copy would hold a path a program may not open, and a
  copy an earlier attempt at the same move left behind is removed first.
- **A sync failed with `WinError 5` on its own journal and then blocked every
  later sync.** A cloud client (Yandex.Disk here) opens a file the moment it is
  written, to upload it; replacing that file while it is held is refused on
  Windows. Only files copied into `.codex` and the cloud mirror waited such a
  lock out — the journal, the sync manifest, handoff and project boards,
  Guardian, the semantic store, saved plans and `config.toml` failed on the
  first refusal, and a journal left open that way stopped every mutation until
  `recover` closed it. Every replace in codexSync's own folders now waits up to
  about six seconds for such a lock; any other error still fails at once.
- **A long chat stopped every sync as "continued differently on two
  machines".** Codex 0.160 continues a long chat in a second file
  (`…-<id>_<other id>.jsonl`); codexSync took it for a stale copy of the same
  chat, compared it with the first file in the cloud and found nothing in
  common. A chat is now the chain of its files: each is carried on its own, so
  the first file's growth and the new part both travel. A chat continued that
  way earlier had only its second part in the cloud copy; the first part now
  follows.

- **The sync history said "0 / 0" after a sync that carried chats and
  projects.** A full sync writes three journals — settings files, chats,
  projects — and the window listed only the first. It now lists every run with
  what it was, chats and projects record how many went each way (and who
  started them), an empty run says "no changes", *Home* counts chats too, and
  `codexsync history` lists every kind by default.
- **Every message that sends you somewhere has a button that goes there**, to
  the very tab or field: a full sync's notes (path mappings, new chats, the
  Sessions page for that pair of machines) on *Synchronisation* and
  *Automation*, and the links on *Sessions*, *Projects*, *First run*,
  *Backups* and *Home*. "Settings → New chats from another machine" named a
  tab that does not exist; the texts now give the whole path.
- Every key of the shipped config has a place in the window (a test walks the
  template); *Back up before overwriting* is shown, locked like the other
  safety settings.
- Reading the chats is faster: the cloud copy's compressed files are unpacked
  in parallel and both sides are read at once (about 39 s → 23 s on the
  machine it was measured on; a full sync reads them twice).
- The console `codexsync.exe` has the same icon as the window.
- A chat Codex carried on in a new file, leaving the old one beside it, was
  blocked for good as "one session id in two files". The file Codex's own
  thread catalogue names is now the chat; the other is left alone.
- The mouse wheel no longer changes a number or a drop-down it passes over, or
  switches tabs, while scrolling *Settings* and *Automation*; a field takes the
  wheel only after it has been clicked into.
- The config upgrade card no longer shows a greyed-out *Update the
  configuration* when there is nothing to write.
- Python 3.11: importing the package failed (`ConfigFinding` had a mapping
  proxy as a dataclass default, which only 3.12 accepts).
- **A handoff between two machines could copy the older file over the newer
  one.** The sync manifest in the shared workspace held one baseline for all
  machines, so machine B took machine A's last sync for its own, read its own
  untouched, older file as a local edit and copied it into the cloud over A's
  version. The manifest now keeps a baseline per machine (CS-288); one written by
  an earlier version is attributed to no machine, so the first run after the
  upgrade plans a first sync and deletes nothing.
- **An empty `include_roots` synchronised the whole `.codex`, `auth.json`
  included.** A written empty list, `""` or `"."` is now refused (exit 4), a
  config without the key synchronises nothing, and a credential file is never
  indexed for `sync` at any depth (CS-289).
- **Pruning backups could delete things that were not its backups.** It removed
  any old folder in `backup_dir` — the mirror's `sessions/` when the two
  overlapped — and other machines' snapshots, including one an interrupted
  operation still needed. It now removes only this machine's own snapshots,
  never one an unfinished journal names, and nothing when the journals cannot
  be read; `cloud_root_dir`, `backup_dir`, `temp_dir` and
  `state.manifest_file` may no longer overlap (CS-290, CS-294).
- **A conflict was recorded as agreement under a non-default policy.** Equal
  times under `equal_mtime_action = "manual_abort"` and disputed deletions
  under `propagate` left the run at exit 0; any conflict now stops it with
  exit 2 (CS-295).
- **`propagate` could empty a folder that was only missing on the other side,**
  and two paths differing only in letter case were copied onto one file. Both
  are conflicts now (CS-324).
- **Config values of the wrong kind.** A word where a number belongs exited 1
  with a traceback, a string where a list belongs was split into letters
  (`exclude_globs = "**/*.lock"`), `logging.level = "LOUD"` silently meant INFO,
  and negative counts passed; each is now a `ConfigError` naming the key
  (CS-319). A path written as `\\?\C:\...` resolved to a rootless path and
  passed every overlap check; the prefix is removed first (CS-320).
- **Logging.** `logging.file` may not be inside `.codex`; with two codexSync
  processes, rotation zipped a file the other one still held on every line it
  wrote; and the commands a scheduled task runs (`guardian`, `preflight`,
  `state-backup`) did not write to the log file at all (CS-318).
- **Smaller sync fixes** (CS-325): the sync plan id now covers deletions; a
  malformed manifest is a `ConfigError` instead of a traceback, and it is
  flushed to disk before it replaces the old one; the sweep of `*.tmp` in
  `temp_dir` spares files younger than an hour; the gate is checked again
  right before anything is staged; `config upgrade` clamps a migrated interval
  to the 31-day maximum; and on Linux a sign-in timer is enabled but no longer
  started at install, which could run the sign-in sync or copy right away.
- **Overview showed no last sync and no open journal when the environment check
  failed.** The journals and the task state were read only after a successful
  check, which is exactly when they matter least; they are now read either way.
- **Recovery and the mutation envelope (review of 2026-09-27).**
  - A chat move, repair, project move or Guardian restore stopped before its
    replace (Codex starting, a backup that could not be written) left its
    journal open, so every later write exited 5 until `recover resume`. It now
    closes as failed; one stopped inside the commit phase with nothing replaced
    closes through `RECOVERY_REQUIRED`, keeping the trail (CS-291).
  - `recover rollback` closed the journal and only then found that the restore
    refused the global state and session files. Rollback is now planned and
    proven first; the global state goes back through the same careful write as
    the original, a session transfer is refused up front (use `resume`), and the
    Recovery screen and `list_journals` say why (CS-292).
  - A backup now records the side (`local`/`cloud`) of every file, and a
    rollback puts each file back where it came from. Rolling back a
    bidirectional sync into one `--target` wrote the cloud side's old files into
    `.codex`. `--target` is now optional and, given, must be the only side the
    backup holds; an older sync backup without sides is refused (CS-293).
  - A backup the journal names but that is gone is refused instead of reported
    as "nothing to roll back" (CS-294).
  - A leftover journal temp file made every later transition of that journal
    fail, including the ones `recover` makes; each write now uses its own name
    (CS-296).
  - One lock per Codex folder for every kind of write: `chats move` and
    `repair-projects apply`, or a sync and a restore, could run together. The
    global state is also re-read right before it is replaced and left alone if
    it moved (CS-304).
- **Exit codes and input.** A malformed command line exits 4, not argparse's 2,
  which means a conflict here — also through `codexsync-gui.exe`, which now
  answers `--version`/`--help` as the command line (CS-315). `doctor` fails on an
  unfinished mutation journal instead of passing while every write exits 5
  (CS-316), and no longer crashes when `temp_dir` is a file. A `--scope-file`
  that is missing or not a saved set is exit 4 instead of silently dropping the
  working set (CS-317). `validate` refuses a config every writing command
  refuses; `sessions scan` exits 2 only for a conflict or target collision, not
  for sessions the apply skips; `chats list --limit` refuses a negative number;
  a missing or unstable global state in `chats move`/`repair-projects
  apply`/scans is exit 4 or 5 instead of a traceback; the `-c` help names the
  real search order; `--dry-run` help of `guardian restore`/`chats move` says it
  checks the gate only with `--confirm` (CS-325).
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
- `repair-projects` on the desktop build no longer plans a project it cannot
  create. A chat whose folder belongs to no project produced `ADD_PROJECT`,
  which the apply refuses on that schema, so one such chat (5 on the machine
  checked) made every plan unappliable, including an unrelated remap; the window
  also showed those rows as "ok". It is now `SKIP_NO_PROJECT`: nothing is
  written for it, not even a binding, and the rest of the plan applies.
- A `repair-projects` remap no longer takes chats from another project. Pinning
  the chats under the old root bound every one of them to the remapped project,
  including a nested project's chats and chats explicitly assigned elsewhere, and
  the later binding silently won. Those are now left alone and reported as
  `REMAP_SESSION_BOUND_ELSEWHERE`, and the apply refuses any plan that binds one
  chat to two projects.
- A copy of `.codex` no longer follows Windows junctions. `is_symlink()` does
  not see a junction, so the copy walked `skills` into folders outside `.codex`
  (18 foreign files of 453 on the machine checked); and a folder that could not
  be listed was skipped silently, now it fails the copy. Junctions, symlinks and
  cloud placeholders are told apart by one rule shared by the copy, `project-move`
  and the session catalogue.
- The session catalogue no longer skips a cloud placeholder. It treated every
  reparse point as a link, so a session file a cloud client had marked was not
  scanned at all; only a reparse point that names another location is skipped
  now, as in `project-move`.
- `project-move` can be resumed from any crash point. The resume marker is
  written before the verified copy takes the target name (a crash right after the
  rename left the copy unrecognised, `TARGET_EXISTS` for good), and the staging
  marker stays until after the rename (a crash just before it left a staging
  folder the next run refused as `STAGING_OCCUPIED`).
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
- **Sessions: a copy that could not be read counted as no copy at all.** A
  mirror branch with a broken tail, or one session id in two files, was left out
  of the plan, so the session looked one-sided and the other side's branch was
  copied over it unread — no comparison, no conflict bundle. Such a session is
  now `BLOCKED_INVALID_BRANCH`: nothing is written for it on either side, the
  rest of the plan still applies, and a duplicate id is a plan code
  (`LOCAL_DUPLICATE_SESSION_ID`) instead of something only `doctor` mentioned. A
  copy of a session the destination lacks is refused when a file already sits
  where it would land (`DESTINATION_OCCUPIED`), and a comparison that fails is
  no longer a conflict you could "resolve" against empty hashes.
- **Sessions: a resolved conflict could give the mirror a second file.** Keeping
  a branch archived here over one still active in the mirror wrote it under the
  local path, leaving two files for one id, which drops the session from every
  later plan. A branch the mirror holds is now rewritten where it is
  (`MIRROR_PATH_KEPT`). `FORMAT_MIGRATION` is set only on a divergence, so
  `--format-migrations` no longer decides a missing base.
- **Sessions: a working set that covered no session could never be applied.**
  The plan was built narrowed and rebuilt unnarrowed, so its id never matched.
  Such a set now writes nothing into `.codex` (`WORKING_SET_MATCHES_NOTHING`)
  and applies.
- **Sessions: conflict bundles.** A bundle is now named by the conflict id you
  resolved (bundles written under the old name are still found), both copies
  are re-hashed before it is committed, and a directory left by an interrupted
  attempt is rebuilt instead of being taken for a bundle.
- **Sessions: smaller read faults.** A damaged `.jsonl.gz` in the mirror ended
  the scan with a traceback (`zlib.error`) instead of marking that branch
  unreadable; a file removed while the catalogue was being built did the same.
  Two records that differ only by a repeated key (`{"k":1,"k":2}` and
  `{"k":2}`) were treated as the same record. A thread catalogue that could not
  be opened, or sat in a folder whose name holds `#` or `%`, read as *no*
  catalogue (`ABSENT`) rather than an unreadable one (`INDETERMINATE`).
- **Guardian: review fixes.**
  - A snapshot a cloud client held for a moment was skipped when the next
    generation was numbered, so its number was issued twice and every later
    commit refused on duplicate generations, for good. The number now comes from
    every manifest and marker in the store plus the pointer; one that cannot be
    read stops the commit until it can. Stores the old numbering already broke
    recover on the next commit.
  - A new snapshot names `latest-good` — the state it was judged against — as its
    predecessor, not the highest generation, so retention keeps the baseline an
    accepted snapshot really overrode. Generations may now skip numbers.
  - A state equal to an *older* snapshot (the file after `guardian restore`) was
    answered `UNCHANGED` and left `latest-good` on a state the file no longer had;
    it is now a new generation.
  - A latest-good pointer naming another machine was trusted, and one with a
    non-text machine id crashed every Guardian command; both are now rebuilt.
  - `guardian restore` refuses a live file whose project state is in a shape no
    adapter recognises (`LIVE_SCHEMA_UNKNOWN`): that is a newer Codex, not damage.
  - A U+FEFF character inside a JSON string (a project name) was taken for a
    byte-order mark and quarantined every state; only a mark at offset 0 counts.
  - A schema change froze `latest-good` for good, since the watcher cannot
    compare two schemas. `guardian accept` now accepts a state in another known
    schema that passes every check (`BASELINE_SCHEMA_CHANGED`, marked
    `SCHEMA_CHANGE_ACCEPTED`).
  - Quarantine kept a full copy of every rewrite of a rejected state for 30 days;
    one drop now keeps its first and newest event, the newest counting the rest.
  - The `COMMITTED` marker's temporary name made it the longest path in the store
    (past 260 characters on a deep root); it is now short. Snapshot directories a
    crash left uncommitted are swept after `staging_retention_hours`.
  - `guardian watch` ended on the first store error (a locked file, a full disk,
    a busy writer lock); it now logs, backs off and continues. The writer lock no
    longer raises a raw `PermissionError` or leaks a locked handle.

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
