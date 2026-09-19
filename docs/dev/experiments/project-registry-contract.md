# Experiment: where does Codex read its projects from?

**Status: not run.** `PROVEN_PROJECT_REGISTRY` in
`src/codexsync/project_registry.py` is empty. While it is, codexSync offers no
way to delete or merge a project, and reports the moves it does offer
(`project-move`, `REMAP_ROOT`) as JSON-only.

## What is known

From `.codex-global-state.json` on the machine this was examined on:

- `local-projects` holds 18 projects; a project id is referenced by
  `project-order`, `electron-persisted-atom-state.unified-sidebar-project-order-v1`
  (`codex:project:<id>`), `app-server-project-id-by-legacy-project-id-by-host`,
  and possibly by `pinned-project-ids`, `selected-project`, the
  `sidebar-project-expanded-v1-codex:<id>` keys and `thread-project-assignments`;
- `app-server-projects-migration-by-host = {projectsMigrated: true,
  threadAssignmentsMigrated: false}`.

From a copy of `state_5.sqlite` (the original was never opened for writing):

- a `projects` table with 48 rows -- four NetRuleRouter and four CommentRake,
  most names repeated across id generations `01a04db6…`, `01a055eb…`,
  `01a055f1…`, `01a056b1…`, `01a0933d…`, which look like traces of earlier
  manual recoveries (`.codex` also holds `.pre-recovery-*` and
  `.pre-historic-projects-*` copies);
- a `project_roots` table;
- `threads.project_id REFERENCES projects`, NULL in all 269 rows.

From a screenshot of the Codex sidebar (2026-09-13): `LabTakt` and
`project-chloya` are pinned and `project-reconstellate` is first, which matches
the **JSON** ids exactly (`pinned-project-ids`, `selected-project`,
`unified-sidebar-project-order-v1`). In SQLite project-reconstellate is also at
position 0, but under a different id. So ordering and pinning follow the JSON.
The screenshot was cut off before the end of the list, so the *membership* of
the list and the *root path* of each project are still open.

## What to run, on disposable state

Copy a `.codex` aside first. Record the Codex version at every step.

1. **How many projects does the panel show?** Count the entries in the
   Projects section with the JSON holding 18 and SQLite holding 48. 18 with
   the duplicate NetRuleRouter and CommentRake means the list comes from the
   JSON; 48, or any other number, means it comes from the app-server registry
   and the JSON is a cache.

2. **Does the panel follow a root path changed in the JSON only?** Edit one
   project's `rootPaths` in `.codex-global-state.json` while Codex is closed,
   start Codex, and check:
   - does the project show the new folder?
   - does a *new* chat started in that project run in the new folder (check
     the `cwd` in the newest session file)?
   - do the chats that were in the project still appear under it?

   This is the one that decides whether `project-move` and `REMAP_ROOT` do what
   they claim, so it matters even if nothing else here is ever implemented.

3. **Does a project deleted in the JSON stay deleted?** Remove one project's
   entry from `local-projects`, from `project-order`, from
   `unified-sidebar-project-order-v1` and from
   `app-server-project-id-by-legacy-project-id-by-host`, with Codex closed.
   Start Codex and look for the project. Three outcomes are possible and they
   mean different things: it is gone (JSON is the truth), it comes back (the
   app-server registry is), or it remains as a ghost that cannot be opened
   (the two registries are both consulted and now disagree).

4. **What happens to its chats?** Whatever step 3 produced, check whether the
   chats that belonged to the project are still reachable, and whether their
   `thread-project-assignments` entries survived.

## What to write down

Add one entry to `PROVEN_PROJECT_REGISTRY` keyed by the schema id
(`electron-v2` today):

```python
PROVEN_PROJECT_REGISTRY = {
    "electron-v2": "Codex 1.2026.xxx, observed 2026-09-xx: the panel lists the "
                   "18 JSON projects, follows a rootPaths edit, and a project "
                   "removed from the JSON does not return",
}
```

and append the observations to this page, including the answer to step 2 even
if it is the only step that gets run.

## If the JSON turns out to be the truth

Then, and only then, implement what `tasks.ru.md` CS-238 describes:

- `projects remove`, refusing a project that any chat still reaches by binding,
  by path or by rule, with `--merge-into <project>` binding those chats to the
  target in the same commit;
- `projects relink`, which is the second half of `project_move` without the
  copy: `replace_project_root` onto a folder that already exists, plus the
  bindings for the chats that reached the project by path.

Both go through `app.commit_global_state` with a plan and its id, both leave
the files on disk alone, and the result must pass
`validate_global_state_references`. The Projects screen grows its buttons after
that, not before.

## If SQLite turns out to be the truth

codexSync does not implement any of it: it does not write SQLite, and this is
not the place to start. The GUI explains how to delete a project inside Codex
instead.
