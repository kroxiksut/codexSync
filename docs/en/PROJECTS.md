# Projects and chats

**English** · [Русский](../ru/PROJECTS.md) · [中文](../zh/PROJECTS.md)

[← Documentation](README.md)

A Codex project is a folder (its *root*) plus the chats that belong to it. After a
machine handoff or a folder move, the paths no longer agree and chats silently
stop appearing under their project. This page covers finding them, pinning them,
repairing a handoff and moving a project's files. In the window these are the
[Chat bindings](GUI.md#chat-bindings) and [Projects](GUI.md#projects) screens.

> [!NOTE]
> Codex also keeps projects in `state_*.sqlite`, which codexSync never writes.
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
codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt
codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt --confirm <plan-id>
```

- There is no plan file: the id covers the decisions *and* the exact bytes of the
  state they were read from, so it stops matching the moment anything changes.
- The write is one binding per chat, in the shape the detected schema uses,
  behind the same envelope as every other write: verified full backup first,
  process re-checked immediately before the replace, and a verified rollback if
  anything afterwards fails.
- `--dry-run` runs every check and writes nothing.

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
  refuses.
- **Two candidates** that both map onto the same new root are reported as
  `AMBIGUOUS_PROJECT`, and nothing is applied.
- **Nothing inside a session file is ever edited.** A record's raw bytes are its
  identity for branch comparison, so rewriting a `cwd` there would make the same
  history on two machines permanently divergent.

## Moving a project's files

`project-move` does the move itself, on one machine:

```powershell
codexsync -c config.toml project-move scan --project LabTakt --to D:/Work/labtakt --save-plan move.json
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
