# Projects and chats

**English** · [Русский](../ru/PROJECTS.md) · [中文](../zh/PROJECTS.md)

[← Documentation](README.md)

A Codex project is a folder (its *root*) plus the chats that belong to it. After a
machine handoff or a folder move, the paths no longer agree and chats silently
stop appearing under their project. This page covers finding them, pinning them,
repairing a handoff and moving a project's files. In the window these are the
[Chat bindings](GUI.md#chat-bindings) and [Projects](GUI.md#projects) screens.

> [!NOTE]
> Codex also keeps projects in `state_*.sqlite`, and codexSync never modifies
> those project records.
> Moving or remapping a project therefore rewrites the root in
> `.codex-global-state.json` only, and deleting or merging projects is not offered.
> `doctor` reports this on every run
> ([experiment](../dev/experiments/project-registry-contract.md)).

## Chats

`chats tree` prints the projects with their chats underneath, and the chats that
belong to no project at the end. `chats list` is the same information, filtered.
Both read only and run while Codex is open.

```powershell
codexsync -c config.toml chats tree
codexsync -c config.toml chats list --text "parser" --limit 20
codexsync -c config.toml chats list --project none
```

`chats list` also takes `--association`, `--since`/`--until`, `--sub-threads`
(threads agents spawned are hidden by default) and `--json`.

Each row says *why* the chat sits where it does, and the three reasons behave
differently:

| Marker | Association | Meaning |
|---|---|---|
| `pinned` | `BOUND` | An explicit `thread-project-assignments` entry. Follows the project if its path changes. |
| `by path` | `DERIVED` | No entry; the chat's recorded folder falls under the project root. Moving the project leaves it behind. |
| `by rule!` | `DERIVED_VIA_MAPPING` | The folder names *another machine's* path, and only a `[[path_mappings]]` rule connects it to a project here. Codex does not read those rules, so it shows this chat under no project at all. |

The last one is what a machine handoff produces: the project lives under `C:` on
the laptop while every chat that came from the desktop still records `D:`. List
exactly those:

```powershell
codexsync -c config.toml chats list --source-machine desktop --target-machine laptop --association DERIVED_VIA_MAPPING
```

## Moving chats to a project

Moving is a preview first. `chats move` writes nothing until you repeat it with
the plan id it printed, and Codex must be closed for the write:

```powershell
codexsync -c config.toml chats move --chat 3f9c2e71 --to Atlas
codexsync -c config.toml chats move --chat 3f9c2e71 --to Atlas --confirm <plan-id>
```

- There is no plan file: the id covers the decisions *and* the exact bytes of the
  state they were read from, so it stops matching the moment anything changes.
- The write is one binding per chat, in the shape the detected schema uses,
  behind the same envelope as every other write: verified full backup first,
  process re-checked immediately before the replace, and a verified rollback if
  anything afterwards fails.
- `--dry-run` runs every check and writes nothing.

## Projects between machines

Codex's sidebar — the projects, their order, which are pinned, and which chats
an explicit binding puts under a project — is part of `.codex-global-state.json`.
That file also holds things that belong to one machine (window position,
remote-control ids, migration markers), so it is never copied whole. Instead
each full sync carries the **project part** of it:

- every machine publishes its project list into the `projects` folder beside
  `state.manifest_file` (one self-verifying file per machine, written only by
  that machine);
- the next machine **merges** it in: a project is matched by id, then by its
  folder (through [`[[path_mappings]]`](CONFIGURATION.md#path_mappings)); one
  this machine lacks is added exactly as Codex wrote it on the other machine;
- **nothing is removed** — a project only this machine has stays where it was;
- for projects both machines have, the other machine's pins, order and chat
  bindings win, but only from a list this machine has not taken yet, so an
  unchanged list is never applied again over a change made here since;
- a project whose folder does not exist on this machine is still added, and
  the sync says so — create the folder or add a path mapping.

It runs as part of `handoff sync` and of **Synchronise** in the window. By hand:

```powershell
codexsync -c config.toml projects sync                        # preview: what would be added, plan id
codexsync -c config.toml projects sync --confirm-plan <id>    # Codex closed: merge, then publish this machine's list
```

The write goes through the same envelope as every other change to the global
state: Codex closed, a verified backup first, and a rollback through
[`recover`](RECOVERY.md#interrupted-mutations) if anything fails. Codex also
keeps projects in `state_*.sqlite`, whose project records codexSync never
modifies; the sidebar follows the JSON file.

### Project folders

Chats and the project list travel; the project's own files do not — they are in
git, in a cloud folder, or only on one disk. A chat continued against an older
copy of the code works on the wrong files, so every full sync also compares the
project folders (`D-026`). Each machine publishes what its folders hold into
`project-files` beside the manifest: for a git folder the commit, the branch and
whether changes were left uncommitted; for any other folder every file with
its size and SHA-256 (tool folders such as `node_modules` or `.venv` left out;
files over 100 MB by size and time). Only files whose size or time moved are
read again — the hashes are cached on each machine, outside the workspace. With
each project goes the last time one of its chats changed there. The next
machine compares every project — anything may change a folder — and says,
projects whose chats went on elsewhere first:

- the last commit from the other machine is not here — pull it;
- both machines committed different work — merge it;
- the other machine left changes uncommitted that are not here;
- a plain folder has files that changed later on the other machine, files only
  it has, or files it deleted that are still here — `projects files` lists them;
- the folder does not exist here, or exists here and not there.

Folders and files come and go at any time, so nothing is taken from an earlier
check: every check reads the folders afresh and publishes this machine's side —
a full sync, `projects files`, and the window on **every start**, which is how a
machine that has not synced yet still tells the others what it holds. A file
deleted on a machine is remembered in its publication for 90 days, so the next
machine can tell "deleted there" from "added here".

Nothing is copied, pulled or blocked, and work that exists only here says
nothing. Git is used when it is installed, and only to read: it never touches
the index. In the window, **Projects → Project folders** lists each project
that did not fully come along with the files under it, and a full sync's result
has a button there. `codexsync -c config.toml projects files` prints the same
list, every file included — run it after pulling to see the warning go
(`--all` lists every project).

## Repair after a machine handoff

When a project folder moves — renamed, put on another drive, or opened on a second
machine under a different root — Codex loses it. Declare where the old prefix
lives now with a [`[[path_mappings]]`](CONFIGURATION.md#path_mappings) rule, then
scan:

```powershell
codexsync -c config.toml repair-projects scan --source-machine desktop --target-machine laptop --save-plan repair-plan.json
codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <plan-id> --dry-run
codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <plan-id>
```

The plan is built from what the sessions actually record, run through the mapping
rules. The dry run performs every refusal the real apply performs, including the
process check.

- **`REMAP_ROOT`.** If an existing project's *recorded* root, run through the same
  mapping, lands on the folder the sessions now point at, the plan remaps that
  project instead of creating a second one. Only that one path value is rewritten,
  so the project keeps its id and name.
- **A remap never travels alone.** Chats created before the move recorded the old
  folder, and moving the root away from it would make them disappear. So every
  session still under the old root is also pinned to the project. If any such chat
  would be left uncovered, the plan reports `REMAP_ORPHANS_SESSIONS` and the apply
  refuses. A chat under the old root that already belongs to *another* project
  (a nested project, or one you moved it to) is not taken over: the plan reports
  `REMAP_SESSION_BOUND_ELSEWHERE` and refuses until you decide where it belongs.
- **A chat whose folder is no project here** is listed as `SKIP_NO_PROJECT` and
  left alone. The desktop build's project entries carry fields codexSync does not
  invent, so it never creates a project there: create it in Codex and scan again.
  Such a chat no longer blocks the rest of the plan.
- **Two candidates** that both map onto the same new root are reported as
  `AMBIGUOUS_PROJECT`, and nothing is applied.
- **Nothing inside a session file is ever edited.** A record's raw bytes are its
  identity for branch comparison, so rewriting a `cwd` there would make the same
  history on two machines permanently divergent.

## Moving a project's files

`project-move` does the move itself, on one machine:

```powershell
codexsync -c config.toml project-move scan --project Atlas --to D:/Work/atlas --save-plan move.json
codexsync -c config.toml project-move apply --plan move.json --confirm-plan <plan-id> --dry-run
codexsync -c config.toml project-move apply --plan move.json --confirm-plan <plan-id>
```

1. The project is copied into a folder that does not exist yet.
2. Every copied file is re-hashed against the plan.
3. The verified copy is renamed into place.
4. Only then the project root is remapped and the chats that reached the project
   by path are pinned.

**The old folder is never modified or deleted** — removing it is your decision
once you have checked the copy.

The scan refuses a target that exists, lies inside the project (or the project
inside it), overlaps `.codex`, the mirror, backups, temp or the snapshot stores, or
belongs to another project, and a project containing symlinks, junctions or
unreadable files. Cloud placeholders (Yandex.Disk's, for example) are ordinary
files, not links. If the state commit fails after the copy, the verified copy
stays and a new scan recognises it (`copy_complete`), so a rerun only updates
Codex.

The copy is not held to Windows' 260-character path limit, so a deep `.git`
moves even where long paths are switched off. The scan still names the longest
path the new folder will hold when it reaches that limit on such a machine,
because a program that is not long-path aware may fail to open it there. A copy
left by an earlier, failed attempt at the same move is reported by the scan and
removed before the next copy starts.
