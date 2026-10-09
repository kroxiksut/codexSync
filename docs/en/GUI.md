# The window

**English** · [Русский](../ru/GUI.md) · [中文](../zh/GUI.md)

[← Documentation](README.md)

The window is a second shell over the same core as the command line: it can do
nothing the CLI cannot, and it follows the same rules. It is available in
English, Russian and Chinese, with a light and a dark theme.

- [Starting it](#starting-it)
- [Safety](#safety)
- Screens: [Home](#home) · [Overview](#overview) · [First run](#first-run) ·
  [Synchronisation](#synchronisation) · [Chat bindings](#chat-bindings) ·
  [Sessions](#sessions) · [Projects](#projects) ·
  [Snapshot guardian](#snapshot-guardian) · [Backups](#backups) ·
  [Recovery](#recovery) · [Automation](#automation) · [Settings](#settings) ·
  [About](#about)
- [Windows exe](#windows-exe)

The screenshots show invented demo data. They are rendered offscreen by
`python scripts/docs_screenshots.py`, which never reads a real `.codex`.

## Starting it

```powershell
python -m pip install --upgrade --pre "codexsync[gui]"
codexsync-gui                      # opens the config it used last
codexsync-gui -c config.toml       # or: python -m codexsync.gui -c config.toml
```

From a source checkout, use `pip install -e ".[gui]"` instead
([installing](README.md#install)).

Without `-c`, the window opens the config it used last; failing that, a
`config.toml` in the current folder or beside the executable. If there is none,
it starts on the *First run* screen, and every other page offers to open an
existing `config.toml` — from any folder — or to create one where you choose.
No location is proposed on your behalf, and choosing a file that already
exists in the *Config file* field opens it rather than replacing it.

The status bar at the bottom always shows which `config.toml` is open, and the
side panel shows this machine's name.

## Safety

Nothing about the safety rules changes in the window.

- **Every write goes through the same core as the CLI** — the process gate, the
  lock, the journal, a verified backup and a final process check — and is
  refused there, whatever the button looked like. A test reads the window's
  imports to prove it cannot reach anything beneath that.
- **"Codex is probably closed" is advisory** and says so. The check that counts
  is taken again at the moment of the write.
- **Plan first, then confirm.** Every change starts with a plan or a dry run;
  the confirmation dialog quotes the exact plan id. Restore, resume and rollback
  unlock only after a dry run of the same choice.
- Long read-only scans show their progress in the status bar and can be
  abandoned there; a write cannot be.
- Plans the window applies are saved under `<workspace>/plans/`.

## Home

![Home](../screenshots/en/01-home.png)

The first page: this machine at a glance, beside *Overview* rather than instead
of it. A banner says whether Codex looks open (only an indication, like every
such line), and one tile each shows the last synchronisation with the runs,
failures and files moved in the last 30 days and a link to the history; chats
and projects; copies of `.codex`; backups made before writes; the snapshot
guardian; and which automation tasks are on and whether the operating system
has them as configured. An unfinished journal is announced here as on
*Overview*.

Everything but the chat counts is read when the page opens, and none of it
opens a session file. Counting chats reads every session, so those numbers come
from the last scan — the *Chat bindings* page or **Recount** — are shown with
the time they were taken, and are kept in the application's cache folder
(`%LOCALAPPDATA%\CodexSync\cache` on Windows): counts only, never a name, a
path or a title. A part that cannot be read is one tile saying why.

## Overview

![Overview](../screenshots/en/02-overview.png)

A manual check of this machine; it authorises no write. The banner says whether
Codex looks closed. The table is the `doctor` report: configuration, state
directories, the Codex process, whether Codex can start, the detected global-state schema, the latest
restorable snapshot, session files, the session index, the SQLite thread
catalogue, what a sync is allowed to do, where projects are stored, and the sync
manifest. Below it: a dry run of the sync, the last synchronisation with a link to its
history, and the state of the scheduled task with a link to the *Automation* page.

## First run

![First run](../screenshots/en/03-first-run.png)

Creates `config.toml` from the template built into CodexSync, keeping every
comment:

- **Config file** — where to write it; it must not be inside `.codex`.
- **Machine name** — unique and permanent: backups, snapshots and mapping rules
  on the other machine refer to it.
- **Local .codex** — Codex's own state directory. It is read and, only during a
  cold operation, written; never created.
- **Synced workspace** — the folder your cloud client syncs between machines.
  Backups, snapshots, plans and logs go under it.
- **Session mirror** — where session files are mirrored; empty means
  `${workspace_root}/sync`.

The screen warns when the workspace already holds data for a machine of the same
name, finds a workspace codexSync created earlier, and creates nothing inside
`.codex`. You can also open an existing `config.toml` from here.

## Synchronisation

![Synchronisation](../screenshots/en/04-sync.png)

The cloud → local and local → cloud plan.

- **Check plan** writes nothing and works while Codex is open.
- **Dry run** and **Synchronise** need Codex closed. The sync builds its plan
  again at the moment it writes, so what is on screen is never what gets
  applied.
- **Synchronise** is the full sync: settings, then chats, then the project
  list, with the stage it is in shown while it runs and what it did — files,
  chats, projects added — when it ends. The plan and the dry run cover the
  settings files.
- Search, direction and folder filters change only what is shown: a sync always
  applies the whole plan. **This run** beside the buttons is different: it sets
  the direction of the next dry run or sync only (`--direction` on the command
  line), and *direction as in Settings* returns to the setting.
- Files changed on both sides are listed as conflicts and decided by
  [`conflict.policy`](SYNC.md#conflicts). A sync that stopped on a conflict
  of a file or a chat offers *Keep the newer copies*, *Keep this machine's* and
  *Keep the cloud's* right under the message, with *Always decide this way* to
  save the choice as the policy.
- The **History** tab lists past runs: when, what (settings files, chats,
  projects — one full sync is three rows), result, who started them, what they
  carried and their backup. See [Synchronisation → History](SYNC.md#history).
- A sync never asks Codex to rebuild its chat list. When it carried chats Codex
  does not show yet, the result says how many, with a button to
  [*Recovery → Codex state*](#recovery), where you ask for the rebuild when
  Codex can be left open until it finishes.

See [Synchronisation](SYNC.md).

## Chat bindings

![Chat bindings](../screenshots/en/05-chats.png)

Which project each chat is under, and why. This is not a chat window: it shows a
chat's opening message, date, id, record count and folder, grouped by project.

**Why here** is the reason a chat sits under its project:

| Reason | Meaning |
|---|---|
| pinned | An explicit binding. It follows the project if the project's path changes. |
| by path | The chat's folder falls under the project root. Moving the project leaves it behind. |
| by rule only | Only a `[[path_mappings]]` rule connects the chat to a project. Codex does not read those rules, so it shows the chat under no project until it is pinned. |

Search covers the opening message and the folder; you can filter by reason,
include agent sub-threads, and look at chats recorded on another machine.

**Move chats to a project** — select chats, choose a project and preview the
move; the write needs Codex closed and the preview's plan id. **Auto-repair**
proposes only unambiguous pins: chats a mapping rule connects to exactly one
project.

See [Projects and chats](PROJECTS.md#chats).

## Sessions

![Sessions](../screenshots/en/06-sessions.png)

How each branch of a session compares across two machines. Choose where the
sessions were recorded and which machine this is, then **Scan**. Scanning reads
both sides and writes only the plan file; a plan made while Codex is open is
marked volatile and cannot be applied.

Each branch is shown with its group (needs a decision, transferable, not
supported, outside the working set, identical), the action, the relation
("cloud is ahead", "this machine is ahead", "diverged"…), records on each side,
the chat and the destination.

- **A divergence is never merged or resolved by date.** Select it, choose which
  side to keep and **Record decision**; the scan is repeated with it.
- **Working set** narrows what is written into `.codex` to chosen projects and
  chats. The cloud mirror always receives every session.
- **Session index** shows what each side's `session_index.jsonl` holds.
- Dry run, then apply by the exact plan id.

See [Sessions](SESSIONS.md).

## Projects

![Projects](../screenshots/en/07-projects.png)

Projects, their roots and their chats counted by reason (pinned, by path, by rule
only), plus chats under no project. Select a project to **show its chats**,
**move its folder** or **open the folder**.

- **Move its folder…** copies the project into a new folder, verifies every
  file, and only then points Codex at the new root. The old folder is never
  modified or deleted.
- **Repair after moving to another machine** runs the sessions' recorded folders
  through `[[path_mappings]]` and builds a plan: pin chats, or remap a moved
  project's root. A remap always also pins every chat still under the old root,
  so none of them disappears. Dry run, then apply by id.

See [Projects and chats](PROJECTS.md).

## Snapshot guardian

![Snapshot guardian](../screenshots/en/08-guardian.png)

Verified snapshots of `.codex-global-state.json`, taken outside `.codex`.

- The banner shows the latest good snapshot and where snapshots are stored.
- **Take a snapshot now** is allowed while Codex is open: the state is only read.
- **Snapshots** lists each snapshot with its generation, status, project and
  binding counts, schema and size.
- **Restore a snapshot** — select one, *Show what changes*, *Dry run*, then
  *Restore*. Codex must be closed; the current file is backed up first.
- **Quarantine** lists states Guardian refused to trust, with the reason. None
  of them can ever become the latest good snapshot.
- **Accept the current state as the new baseline** appears when a real drop in
  projects or bindings has frozen `latest-good`; it explains the drop in counts
  only.

See [Guardian](GUARDIAN.md).

## Backups

![Backups](../screenshots/en/09-backups.png)

The backups in the backup directory with the machine that made them, whether
their manifest verifies, file count, size and format. **Restore** — choose a
backup and a target (the local `.codex` or the cloud mirror), run the dry run,
which verifies every file against the manifest; the restore itself unlocks only
after that, needs Codex closed, and backs up whatever it replaces first.

Those backups are taken by each write on its own, of what it is about to
replace. A full copy of `.codex` is the block below them: **Copies of .codex**
lists the copies in the folder and has **Make a copy now** (Codex closed). It
is the same as on Automation and is one job for both pages; the folder is
chosen there (**Set up…**). See [Copies of `.codex`](CONFIGURATION.md#copies-of-codex).

See [Backups and recovery](RECOVERY.md).

## Recovery

![Recovery](../screenshots/en/10-recovery.png)

- **Codex state** — *Check Codex* finds why Codex might not start or show your
  chats, whoever caused it, and *Repair* applies the repair it planned (Codex
  closed; the confirmation quotes the plan id). Here you also ask Codex to
  rebuild its chat list, after a preview that says how many chat files it will
  walk. See [When Codex does not start](RECOVERY.md#when-codex-does-not-start).

Every write keeps a journal. An unfinished journal blocks every new change until
it is resumed or rolled back — that block is the protection.

- **Mutation journals** — state, operation, start time, number of actions,
  backup snapshot and id.
- **Resume** closes the journal after verifying the backup still matches; then
  run the interrupted command again, which re-plans from what is on disk.
- **Roll back into** restores the snapshot the operation created, then closes the
  journal. Each file goes back to the side it was backed up from; choosing a side
  is needed only for an older restore whose snapshot does not record it.

Each action has its own check (dry run) that must pass first.

See [Backups and recovery](RECOVERY.md#interrupted-mutations).

## Automation

![Automation](../screenshots/en/11-automation.png)

Every task the operating system runs for CodexSync, on one page, edited the way
*Settings* edits: the values are `config.toml`, and **Save and update the
tasks** saves them (with the usual review) and then installs what is on and
removes what is off.

- **Regular safe job** — a Guardian snapshot, `preflight` or a sync dry run,
  never a write; its interval, start after sign-in and random delay; whether the
  installed task matches the config, its last and next run, and what the last
  exit code meant. **Run now** runs the job in the window.
- **After signing in** — the delay for everything that starts at sign-in, the
  one-time sync, and **Close Codex when a sync starts while it is open**
  (`[sync] close_codex`): Codex is asked to quit the way Windows closes an app
  for an update, never forced; if it does not quit, nothing is written. See
  [Configuration](CONFIGURATION.md#automation).
- **Copies of `.codex`** — the folder (your choice; nothing is proposed), a copy
  after signing in and/or every N hours, how many to keep, the copies that
  exist, and **Make a copy now**, which is refused while Codex is open: the task
  is the one that waits.
- **Sync on sign-out and sign-in** — the watcher that loads at sign-in and
  syncs when Codex closes, how long to wait for the other machine's work to
  arrive, notifications; a table of every machine (working or handed off,
  since when, and whether its last handoff was loaded here), and
  **Synchronise now**, which does not wait for the cloud: work that has not
  fully arrived is refused. There is no folder to choose: it is the
  `handoff` folder beside the sync manifest. See
  [Synchronisation → Handing work over](SYNC.md#handing-work-over).

See [Configuration → Automation](CONFIGURATION.md#automation) and
[Copies of `.codex`](CONFIGURATION.md#copies-of-codex).

## Settings

![Settings](../screenshots/en/12-settings.png)

**Settings are `config.toml`.** The file is read into a draft; nothing is
written until you save.

| Tab | What it holds |
|---|---|
| General | Machine name and directories. Each path shows what it resolves to; `${workspace_root}` stands for the workspace folder. |
| Synchronisation | Comparison, conflict policy, direction, deletions, what is included (picked from a tree) and excluded. |
| Protection | Process detection and Guardian. `safety.*` is shown but never editable. |
| Project paths | `[[path_mappings]]` rules, written in a form. |
| Service data | Backups, the session mirror, the sync manifest, logging, and the config history. |

A save rewrites only the values that changed and keeps every comment, is
validated by the same loader the command line uses, shows the difference, is
refused if the file changed on disk since it was opened, and keeps the replaced
version under `<workspace>/config-history/`. Open plans are discarded afterwards.
A setting the rules forbid is shown with its reason.

**A config from an earlier version** is announced at the top of this screen:
the 0.1 template set two values this version refuses for every write, so such a
file blocks `sync`, `restore` and every repair until it is brought up to date.
The card is one line until you open **Details** (it opens by itself when
something blocks every write); it lists what would change and why, shows the
exact difference, and
applies it all in one confirmed write that keeps your comments and copies the
replaced file into `config-history/`. An optional finding can be left alone with
its own tick. You do not have to find this card: when you open or start with
such a file, a banner above every page says so and **Review and update** opens
the card unfolded (optional differences alone raise no banner). A write refused
for this reason says the same in the window's language. See
[Configuration → Upgrading a config from an earlier version](CONFIGURATION.md#upgrading-a-config-from-an-earlier-version).

The language switch is at the top of the screen. The window itself remembers
only its size, the last screen, the language and which config it opened last —
never the contents of `config.toml`.

## About

![About](../screenshots/en/13-about.png)

What the program is, the four rules every write obeys, what it deliberately
leaves to you, and **this build** — version, whether it is a packaged
executable or a source checkout, the program file, Python, the system and the
config file in use. *Copy these facts* puts those lines on the clipboard for a
bug report; none of them comes from your Codex state, so they can be pasted as
they are.

The licence block names GPL-3.0-or-later and the separate commercial licence,
and states that the core and the command line have no third-party dependencies
while this window uses Qt through PySide6 under the LGPL-3.0. The links open
the documentation in the window's language, the project page, the changelog
and the issue tracker in your browser.

## Windows exe

`codexsync-gui.exe` is the windowed app, and it is also the command line: given a
command (`codexsync-gui.exe -c config.toml validate`) it runs that command without
opening a console, which is what a scheduled task of a frozen install runs. The
console-only `codexsync.exe` ships separately, without Qt.

Building both is described in [the release checklist](../dev/PUBLISHING.md#c-windows-exe).
