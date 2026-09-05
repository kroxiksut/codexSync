# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these first

This repo keeps its rules in dedicated AI files. Do not duplicate them here — read them:

- `AI_RULES.md` — hard constraints (scope, cold-sync model, safety guarantees, exit codes, DoD). **Binding.**
- `AI_CONTEXT.md` — purpose, target platforms/versions, run modes, recorded decisions.
- `AGENTS.md` — short project brief and MVP requirement list.
- `docs/DECISIONS.md` — numbered decisions `D-001…D-010` with rationale and amendments.
- `docs/PROJECT_CONTEXT.md` — why the project exists and what it deliberately is not.
- `README.md` — full CLI surface, config semantics, handoff protocol.
- `tasks.ru.md` — the working plan for 0.2 (Guardian / semantic / repair). Untracked and gitignored, but it is the source of truth for what the P0–P3 tasks mean and their acceptance criteria.
- `CONTRIBUTING.md` — contribution terms and conventions.

When a request conflicts with `AI_RULES.md` (e.g. "terminate Codex before sync", "sync while Codex runs", "read tokens"), stop and say so rather than implementing it.

## Commands

```powershell
python -m pytest -q                       # full suite (340 tests, no deps beyond pytest)
python -m pytest tests/test_sync_engine.py -q
python -m pytest tests/test_sync_engine.py::SyncEngineTests::test_name -q
pip install -e . pytest                   # exactly what CI does; core has zero runtime deps
python -m codexsync -c config.toml <cmd>  # run from a source checkout (see "Dual package layout")
```

There is no linter, formatter, or type-checker configured — CI (`.github/workflows/ci.yml`) only runs `pytest` on `windows-latest` + `macos-latest` × Python 3.11/3.12/3.13. Do not add tool config without being asked.

Windows exe build: `codexsync.spec` + `scripts/pyinstaller_entrypoint.py`, released by `.github/workflows/release-exe.yml` — `workflow_dispatch` only; the tag-push trigger stays off until the GUI ships.

`config.toml`, `config2.toml` and local runtime dirs are gitignored. `src/codexsync/config.example.toml` is the packaged template and the only config that ships; `config.example.toml` at the repo root is a byte-identical copy. `tests/test_config_template_parity.py` fails if the two drift and also asserts the shipped template passes `_require_mutation_compatible_config` — a template that fails it hands every `init-config` user a config whose sync/restore/repair/recover all exit 4. Changing the config schema means touching both files plus `_validate_config` in `config.py`.

## Dual package layout (important)

The real package is `src/codexsync/`. The root `codexsync/` directory is a shim that appends `src/codexsync` to `__path__` so `python -m codexsync` works from a checkout without installing, then mirrors the public surface (`ExitCode`, `__version__`). **Never add logic to root `codexsync/`.** Because the shim shadows `src/codexsync/__init__.py` in checkout mode, anything that must be importable in both modes belongs in its own module — that is why the version lives in `version.py`.

The version is single-sourced: `pyproject.toml` → installed metadata → `codexsync.version.__version__` → `PRODUCER_VERSION`, which is what Guardian writes into snapshot manifests. Never hardcode a version string.

## Architecture

Layered, CLI-last. `cli.py` only parses args and maps exceptions → exit codes. Below it:

| Module | Owns |
|---|---|
| `app.py` | command orchestration: `build_context`, `run_sync`, guardian/repair/recover entry points. Re-exports the moved names, so `codexsync.app` stays the import surface for the CLI and tests. |
| `runtime.py` | shared plumbing used by more than one command family: runtime path bootstrap, `_make_safety_gate`, index building, session-mode filtering, `_plan_hash`. Imports nothing from `app`/`restore`/`preflight`. |
| `restore.py` | snapshot verification and restore into local or cloud state. |
| `preflight.py` | `doctor`/`preflight` checks. |

When patching in tests, patch where the function is *defined*, not where it is re-exported: `codexsync.runtime.collect_process_snapshot`, `codexsync.restore._make_safety_gate`. Patching `codexsync.app.*` for something that now lives elsewhere fails silently — the test still passes, but against the real process detector.

**Exception → exit code mapping lives in exactly one place** (`cli.py::main`'s except chain), driven by the `exceptions.py` hierarchy: `ConfigError`→4, `SafetyPreconditionError`→3, `ConflictError`→2, `FailSafeError` (and its subclasses `GuardianIntegrityError`, `GuardianBusyError`, `OperationBusyError`)→5. To make a new failure produce a given exit code, raise the right exception type — do not return codes from core.

### Safety spine

`safety_gate.py` is the single authority on whether an operation may mutate state. `OPERATION_PROFILES` classifies every `OperationKind` as read-only / mutating / side-effect free. Rules the rest of the code depends on:

- `UNKNOWN` process state is never optimistically treated as `STOPPED` — mutations fail closed with `FailSafeError`.
- A mutation requires a *continuously* stopped 2s window (`check(...)`), then a final direct check right before commit (`require(..., final=True)`).
- Callers must not implement their own bypass. `plan` runs while Codex is open but its result is marked `volatile` and cannot be reused by a mutation.
- `DOCTOR` is `side_effect_free`: a diagnostic may read and report but must never create a file, least of all inside the Codex state directory. A 0.1 drift check that wrote probe files was removed for this reason (`D-010` amendment).
- codexSync never starts or stops Codex. Legacy termination flags/config are rejected with exit 4 (`_require_mutation_compatible_config`).
- A transient lock on a destination (`WinError 5/32/33`, `EACCES/EBUSY/ETXTBSY`) is retried with bounded backoff in `sync_engine._replace_with_retry`, re-proving the process check before each attempt; anything else raises on the first attempt. Do not widen that error set — a retry is only correct while the operation stays atomic (`D-011`).

`app.commit_global_state` is the single envelope for editing `.codex-global-state.json` (repair apply and chat move both go through it): operation lock, journal, verified full backup, gate re-checked immediately before the replace, and a verified rollback from that backup if anything after it fails. A caller decides *what* to write and proves it valid; it does not get to decide how carefully the write happens.

A mutating command (`sync`, `restore`, `repair-projects apply`) always runs the same envelope, wired in `app.py::run_sync` / `restore.py::restore_from_backup`:

`OperationLock` (non-stealable, per state-root+machine+family) → `JournalStore.begin` (`mutation_journal.py`, payload-free durable evidence) → backup-first via `BackupManager` → `after_backup`/`pre_commit`/`before_replace_check` callbacks into the gate and journal → `SyncEngine.execute` (tmp + `os.replace`) → manifest rewrite → `COMMITTED`. A crash between `COMMITTING` and `COMMITTED` leaves `RECOVERY_REQUIRED`.

`JournalStore.begin` refuses to open a new journal while an earlier one is non-terminal — that block is the protection, and `recovery.py` is the only sanctioned way out of it (`recover inspect|resume|rollback`). Two invariants make it work, and both are load-bearing: `SyncEngine.execute` stamps the backup manifest *before* the first replace (so an unstamped snapshot proves nothing was overwritten), and `begin()` records the snapshot name in the journal (so rollback never has to guess which backup was its own). `rollback` needs an explicit `--target` because a sync can back up both sides and the manifest stores only relative paths. `COMMITTING` has no direct edge to `FAILED`; close it via `RECOVERY_REQUIRED` so the trail keeps saying the commit phase was entered.

### Sync path

`state_locator` → `scanner`/`filters` → `runtime._build_indexes` → `planner.build_sync_plan` (timestamp first, SHA-256 only when ambiguous) → `models.SyncPlan` → `sync_engine.SyncEngine`. `manifest.py` persists the previous two-sided fingerprint so a one-sided change can be distinguished from a conflict. Conflict handling is policy-driven (`conflict.policy`); `manual_abort` raises `ConflictError` before any write.

A session file carries a `session_meta` record *per resume*, not one per file — 54 of 252 on the machine this was built against, up to 298 in one file. Only the identity has to agree across them (`RESUMED_SESSION`); two different ids in one file is `CONFLICTING_SESSION_META` and invalid. Treating a repeat as damage dropped 267 MiB of 864 MiB out of `catalog.valid` — never mirrored, never listed, no error — and `chats tree` showed 98 chats instead of 152. Anything that decides a session is invalid costs exactly that much silence, so check it against a real `.codex` before adding one.

Paths under `sessions/`, `archived_sessions/`, `session_index.jsonl`, global project state and SQLite are **semantic-owned** (`runtime._is_semantic_owned`) and are excluded from generic mtime copying — never "fix" this by re-adding them to the copy plan.

### Guardian (0.2, runs while Codex is open)

`guardian_schema.py` holds one adapter per runtime family, tried in order, and the winner's `schema_id` is recorded in the snapshot manifest. Add a new adapter for a new shape; never loosen an existing one, because a state that two adapters could claim is a state whose meaning is guessed. `supports_project_creation` and `build_binding_value` exist so the repair writer follows the detected schema rather than assuming the legacy one.

This is where the worst bug of 0.2 lived: the desktop build's bindings (`projectKind`/`projectId`) matched no adapter, so every real state was quarantined and `latest-good` never existed — Guardian was inert on real data while every test passed against fixtures encoding the assumed shape. The same fault had a second instance in `repair_plan`, which read project roots from `root`/`path`/`cwd` keys the Electron build does not use (it keeps them in `rootPaths`) and compared an object-shaped binding to a bare id: on a real state it recognised 0 of 16 projects. Both were found the same way and by nothing else — **when touching anything schema-shaped, run it against a real `.codex-global-state.json`, not only fixtures.** Reading a root or a binding goes through `guardian_schema` (`project_root_paths`, `binding_project_id`, `replace_project_root`) so there is one place that knows each shape; `doctor` reports the detected schema and the restorable snapshot for the same reason.

Read-only observation of `.codex-global-state.json`, writing only into an external Guardian root that config validation forces to be outside `.codex`/sync/backup/temp. Split: `guardian_models` (contracts) → `stable_reader`/`guardian_runner` (3 stable reads, polling, fallback scan) → `guardian_validation` + `guardian_schema` + `guardian_shrink` (bytes/JSON/referential integrity/suspicious shrink) → `guardian_store` (staging → `COMMITTED` → `latest-good` pointer, quarantine, retention) → `guardian_lock` (single writer per machine). Ordering is by monotonic `generation`, not wall clock; a snapshot is visible only after `COMMITTED`; nothing suspicious can become `latest-good`.

### Session transfer (0.2, CS-224)

`semantic_merge` classifies two branches of one session by streaming both files; `semantic_transfer` turns that into a frozen `TransferPlan` whose id covers every decision. Three rules carry the safety and none is negotiable: a divergence is never merged, sorted or newer-wins — it produces a conflict id and blocks; a resolution is pinned to both branch hashes, so a branch that moves afterwards makes it `STALE_RESOLUTION`; and raw bytes decide record equality, with canonical JSON consulted only where it is provably unambiguous (no floats, NFC strings only) so two different records can never collapse.

`sessions apply` reuses the sync envelope wholesale, and two details are load-bearing. The plan id covers every decision but *not* the creation timestamp — including it would make each rebuild differ from the plan it checks, so the freshness check could never pass. And a branch that loses a resolution goes into a conflict bundle under `semantic.root_dir`, not just the backup: backups are pruned by `backup.retention_days`, so without the bundle the sole copy of a divergent history would expire on a timer. Archive transitions are classified but refused at apply: the move needs a delete and `delete_policy` is `never`.

`PROVEN_LAYOUTS` is the second empty gate, mirroring `PROVEN_CONTRACTS`. A source filename says where a branch lived on *another* machine; placing it wrongly here makes the session invisible with no error at all. Fill it only from `docs/experiments/session-layout-adapter.md`.

A template is rendered with `state`, `source_dir`, `file_name` and `session_id`, and empty segments collapse. `source_dir` is not decoration: a real machine has two shapes at once — 228 active branches under `sessions/<year>/<month>/<day>/` and 23 archived ones flat in `archived_sessions/` — so a template able to name only the folder and the file renders 176 of 196 sessions to a path the thread catalogue does not name (`CATALOG_PLACES_ELSEWHERE`), which is the same dead-on-real-data failure as Guardian's. Check a template change by rendering it against a real state directory and comparing with `read_thread_placements`, never against fixtures alone.

Two things decide a write besides the layout, and both were dead parameters until they were fed. `sqlite_audit.read_thread_placements` reads `threads.id/rollout_path/archived` (never `title`, `preview` or `first_user_message`) and a write into `.codex` is refused unless the catalogue already places that branch at exactly that path — on an observed machine all 195 sessions have a row naming their rollout file, so the file system is not the whole truth and a branch put elsewhere is invisible with no error. `SESSION_NOT_IN_CATALOG` is the honest meaning of `UNSUPPORTED_STATE_BACKEND`: making the runtime see a *new* session needs a row codexSync will not write. `PlacementStatus.ABSENT` (no catalogue at all) constrains nothing; `INDETERMINATE` blocks, and the two must never be collapsed. Rollout paths come back as `\\?\C:\...` for a good fraction of rows — strip the extended-length prefix or 39 of 250 threads look unplaceable.

`semantic_store.py` holds two things that are deliberately different in kind. The **manifest** is metadata: one atomically replaced, self-verifying JSON file per (machine, session) under `manifest/<machine>/<session hash>.json`, recording the branch hash, record count, state, parent and the base an agreement established. It feeds `confirmed_bases`, which is what makes `ARCHIVE_TRANSITION` reachable instead of a permanent `MISSING_BASE`. A **conflict bundle** is the one place a full payload is kept, because the losing branch's only other copy is a backup that retention expires.

The manifest never stores a session payload, and that is a decision, not an omission: `semantic.root_dir` may sit in the user's cloud folder, nothing prunes this store, and copying every reconciled session would upload the whole session directory (777 MiB on the machine this was built against) and grow per edit forever. An earlier `publish()` did exactly that and was removed. Do not reintroduce payload copies outside conflict bundles without asking.

The commit shape is one self-verifying file replaced in a single step, not a payload plus a separate `COMMITTED` marker: in a folder a cloud client syncs on its own schedule two files can arrive in either order or half-written, while one file whose `entry_digest` covers its own contents cannot be half-believed. An entry also claims its own machine and session, so a copy that lands in another directory is refused rather than adopted. Peer entries are accepted only when the digest verifies and the generation has not gone backwards for that machine — the guard is against a cloud client restoring an old copy, not a hostile writer.

Canonical record checkpoints are the one field from the task that is deliberately absent: computing them means streaming hundreds of megabytes to write digests nothing reads yet. Add them when something proves a prefix relation from the manifest instead of from the files.

`jsonl_codec.py` is the one place that knows a branch may sit in a container. The mirror stores each branch compressed (`semantic.mirror_compression`, default `xz`, ~a fifth of the size on real data); a branch written back into `.codex` never is. The rule that makes this safe is that compression is a property of the container and never of the history — every hash, record count and comparison comes from the decompressed stream, which is why reading goes through `open_jsonl` and not `path.open("rb")`. `SyncEngine._stage_verified` keeps its proof by hashing what the staged container *decompresses to*, so a compressor that loses a byte fails before any destination is touched. The codec is in the mirror layout id and therefore in the plan id: an apply writes the container the confirmed plan named, never what the config says now. Per branch and not one archive — an archive of 778 MiB would be re-uploaded whole after one session grew by a line, and would have to be unpacked before the remote side could be classified at all.

Two rules keep a container from costing a session. The mirror's codec applies only to a branch the mirror does not hold yet — a branch already there keeps its container (`MIRROR_CONTAINER_KEPT`), because the container is part of the file name, `delete_policy` is `never`, and `rollout-x.jsonl` beside `rollout-x.jsonl.xz` is one session id under two names, which the catalogue drops as `DUPLICATE_SESSION_ID` for good. And `CopyAction.codec` is `None` for every ordinary copy: transforming is opt-in, so a user's `notes.jsonl.gz` under an included root is never unpacked because of its name. Reading a container also fails outside `OSError` (`EOFError`, `LZMAError`) — catch `JSONL_READ_ERRORS`, since a half-written file is the normal state of a cloud-synced mirror.

The gate covers exactly one destination: a directory the Codex runtime reads. A write towards the cloud mirror is not gated (`MIRROR_LAYOUT_ID`, `mirror_relative_path`) — nothing but codexSync reads that copy, so a branch keeps its own relative path, and gating it left the mirror with no way to be rebuilt at all, since sessions are semantic-owned and never copied by plain `sync`. Consequently `sessions apply` is partial by design: `_UNRESOLVED_TRANSFER_BLOCKS` (conflict, target collision) refuses the whole plan because each names a user decision, while layout- and SQLite-blocked items are logged and left alone. Do not widen that set to every blocked action — that is the state the mirror could not be written from.

### Chats (0.2)

`chat_directory.py` answers "which chats exist and which project is each one in", and `chat_move.py` writes one `thread-project-assignments` entry per chat through `app.commit_global_state`. Three rules carry it.

A chat is told apart from a thread an agent spawned **structurally** — `parent_thread_id`, or a `thread_source` that is not `user` — never by reading the records. About half the session files on a real machine are spawned threads, so this decides what the list even contains, and a text heuristic would reclassify a chat the day someone quotes the wrong sentence in one.

The *reason* a chat sits under a project is reported, not just the project. `BOUND` follows a project that moves; `DERIVED` (cwd under `rootPaths`) is left behind by it; `DERIVED_VIA_MAPPING` means only a `[[path_mappings]]` rule connects them, and since Codex does not read those rules such a chat is invisible in the app until it is bound. Never fold that third value into `DERIVED` — it is the list of chats a machine handoff stranded.

A move has no plan file. The plan id hashes the decisions **and the exact state bytes**, so the id printed by a preview stops matching the moment anything changes, which is the same freshness guarantee `--confirm-plan` gives elsewhere without a file to leave lying around.

### Repair / machine handoff (0.2)

A read-only SQLite open is not automatically a read-only *file* operation, and this is the one place the project's hardest rule was being broken. SQLite creates `-wal`/`-shm` for a WAL database when they are absent, and rebuilds a stale `-shm` when the log is empty — in C, so `tests/test_guardian_state_isolation.py` cannot see it. `sqlite_audit._read_only_connect` therefore picks the connection from whether the log holds frames: none → `immutable=1` (creates and rebuilds nothing), frames + `-shm` → plain `mode=ro`, frames without `-shm` → refuse as `WAL_WITHOUT_SHARED_INDEX` and report `INDETERMINATE`, never `ABSENT`. Never add a bare `sqlite3.connect` here. A library's writes are only visible by listing the directory before and after (`tests/test_sqlite_creates_nothing.py`), not by intercepting calls.

`session_catalog` (streaming read-only session scan) + `path_mapping` (named-machine prefix rules from `[[path_mappings]]`) → `repair_plan` builds an immutable, hashed plan. `repair-projects apply` refuses to run unless `--confirm-plan` matches the plan id exactly.

`REMAP_ROOT` is how a project that moved keeps its chats. Evidence for it is narrow on purpose: an existing project qualifies only when its *recorded* root, run through the same mapping the sessions use, lands on the root those sessions now point at. Two candidates are `AMBIGUOUS_PROJECT`, not a pick. Applying it rewrites that one path value through `replace_project_root` and leaves names, ids and timestamps alone — which is why a remap is allowed on the Electron schema where `supports_project_creation` is false: it invents nothing. **Never migrate a path by rewriting `cwd` inside session JSONL** — raw bytes are a record's identity in `semantic_merge`, so that would make one history on two machines permanently `DIVERGED`.

**A remap is never emitted alone**, and this is the load-bearing part. How a thread reaches a project is not settled: on a real state 93 desktop chats have no `thread-project-assignments` entry at all and are reachable only by their `cwd` falling under a project's `rootPaths`, while 6 do have one — including a chat whose `cwd` stopped matching after its project moved, which is Codex itself writing an override for exactly this case. Under the first reading, replacing the root detaches every older chat, silently: the chat simply stops appearing. So `_bind_sessions_left_behind_by_a_remap` pins every session under the old root to the project with an explicit binding, and `_remap_orphan_codes` re-checks the cover and emits `REMAP_ORPHANS_SESSIONS` if one was missed (a non-empty `plan.codes` refuses the apply). Correct under both readings, which is why neither has to be settled first. Do not "simplify" by dropping either half.

Do **not** make a remap add the new root to `rootPaths` instead of replacing it. The field is a list, but every project ever observed has exactly one entry, so a second element is a guess about cardinality — the same class of guess that made Guardian inert.

`session_index.py` is wired **read-only**: `sessions index` (`app.audit_session_index`) reports both sides and their divergences, and `doctor` carries the same check. Its write path stays gated on `PROVEN_CONTRACTS`, see below. `semantic_store.py` and `semantic_merge.py` are wired through `sessions apply`.

`session_index.py` is gated on purpose. The index is an append/update journal, and how the Codex runtime reduces a repeated id — last line wins, or max `updated_at` — is not knowable from the file. The two readings differ exactly when a clock ran backwards, which is the case this project exists for, so both are computed, disagreement is reported as `REDUCTION_AMBIGUOUS`, and `render_session_index` refuses while `PROVEN_CONTRACTS` is empty. Do not populate that dict from reasoning: it is filled only by running `docs/experiments/session-index-contract.md` against disposable state, and the entry records the Codex version it was observed on. Reading is not gated and must not become so — the audit is what tells a user *why* nothing is written, and it is the only thing that exercises this module against a real index (191 records for 151 sessions on the machine it was checked against).

### GUI (0.2, optional extra)

`src/codexsync/gui/` is a second shell, not a second implementation. `controller.py` imports no Qt and calls only public `app.py` functions; `window.py` is the one module that imports PySide6; `__main__.py` checks for the toolkit and reports its absence as exit 4. The boundary points one way — nothing in the core may import `codexsync.gui` — and `tests/test_gui_boundary.py` enforces both directions by reading imports, including a planted-import test so the guard cannot silently stop firing.

Two properties are load-bearing. The window's "Codex is closed" line is **advisory and labelled as such**: the real refusal happens inside `app.py` against a check taken at the moment of the write, so a stale banner authorises nothing, and `codex_looks_stopped` is true only on an explicit `PASS` (a `WARN` covers both running and undetermined). And a mutation is always plan-then-id, exactly as on the CLI — the confirm button carries the plan id.

Never block Qt by editing `sys.modules` inside the suite. Clearing `codexsync` out of it and re-importing leaves every later `patch("codexsync.runtime.…")` aimed at a different module object; the first attempt at that test turned a 42-second suite into 27 failures over 8 minutes. The check runs in a subprocess instead.

## Tests

`tests/conftest.py` explains why sandboxes are built under `<repo>/test-sandbox` instead of `tmp_path` (staging and target must share a volume for `os.replace` to be atomic) and checks the sandbox is gone when the session ends, retrying removal first. A failure there means a handle survived the retries — that is codexSync's own leak, not a passing cloud-sync or antivirus scan; fix the leak rather than relaxing the check.

If this checkout lives inside a cloud-synced folder, that client also holds brief handles on files the tests write, so suite wall-clock swings widely and `os.replace` can intermittently fail with `WinError 5`. Excluding `test-sandbox` from that client's sync makes local runs stable.

### Guards that replace a linter

The project runs no linter, so two tests carry that weight and should not be weakened:

- `tests/test_module_hygiene.py` walks every module in the package and fails on a name that is loaded but never imported or defined. Moving helpers between modules once left `repair-projects apply` raising `NameError` at runtime while the suite stayed green, because that command had no test.
- `tests/test_guardian_state_isolation.py` intercepts the mutating filesystem calls (`builtins.open`, `io.open` — pathlib uses that one — `os.open` with write flags, and the `os` mutators) and fails the moment one targets the Codex state root. It includes a test that the spy itself catches a planted write; a guard that never fires proves nothing.

## Conventions

- All committed code, comments, docs and log messages are in English. Russian working notes use `*.ru.md` and are gitignored; `README.ru.md` is the one tracked exception.
- Markdown uses the lowercase `.md` extension; document names stay uppercase (`README.md`, `AI_RULES.md`).
- Core and CLI have **zero external runtime dependencies**. Keep it that way; a GUI, if added, ships as the optional `codexsync[gui]` extra.
- Every dangerous action logs separately (backup created / overwrite / skip). Sync must be idempotent within a run.
- Timing and process sampling are injected (`monotonic`, `sleep`, `sample`) so tests stay deterministic — follow that pattern for anything time-dependent.
