# Publishing checklist

Two flows live here. Most releases are the second one.

Documentation language: user documentation is `README.md` + `docs/en/` and its
twins `README.ru.md` + `docs/ru/` and `README.zh.md` + `docs/zh/` (same pages,
same structure). Developer
documents under `docs/dev/` are English only. `.gitignore` excludes every other
`*.ru.md`, so a Russian page named `docs/<name>.ru.md` would silently never be
committed — put it in `docs/ru/` instead.

## A. Releasing a new version of an existing repository

### 1. Prove the tree is releasable

```powershell
python -m pytest -q
python -m codexsync -c config.toml validate
python -m codexsync -c config.toml doctor
```

The suite must be green, and `doctor` must reach `fail=0` on a real machine. A
warning is acceptable and usually means Codex is open or a session is genuinely
ambiguous; a failure is not. Some warnings are permanent by design and say so:
`sync_rules` reports a one-way direction or `delete_policy = propagate`, and
`project_registry` reports that projects also live in `state_*.sqlite`.

Three scenarios are what the release is actually judged on, and all three need
a real machine rather than the suite:

1. **Codex open** — `sync --apply`, `restore --apply`, `repair-projects apply`,
   `sessions apply`, `chats move --confirm` and `recover resume|rollback`
   must each exit `3`, and `guardian snapshot --once` must still succeed.
2. **Codex closed** — a sync runs, a verified backup exists before the first
   overwrite, and the journal ends `COMMITTED`.
3. **An interrupted mutation** — kill the process during a sync, then check
   that `recover inspect` reports it and that the Recovery screen offers
   inspect, resume and rollback, each behind a dry run of the same choice.

### 2. Check the release metadata

1. `pyproject.toml` carries the version being released.
2. `CHANGELOG.md` has a section for exactly that version, with a date, and no
   `[Unreleased]` heading left behind.
3. `README.md`, `README.ru.md`, `README.zh.md`, `docs/en/`, `docs/ru/` and
   `docs/zh/` describe the surface
   that actually ships — in particular, nothing incomplete is presented as a
   feature.
4. `config.example.toml` and `src/codexsync/config.example.toml` are still
   byte-identical (`tests/test_config_template_parity.py` fails otherwise).

### 3. Confirm the optional extra stays optional

The core and the CLI must install with no third-party dependency, and the
command line must keep working on a machine that has no Qt at all:

```powershell
python -m pytest -q tests/test_gui_boundary.py
```

That file blocks the PySide6 import in a subprocess and imports the CLI anyway.
If it passes, `pip install codexsync` pulls nothing extra.

### 4. Tag and push

```powershell
git tag v0.2.0
git push origin main
git push origin v0.2.0
```

### 5. Verify after push

1. CI workflow `CI` runs in `Actions` for `windows-latest` and `macos-latest`,
   on Python 3.11, 3.12 and 3.13.
2. All three READMEs and the `docs/en`/`docs/ru`/`docs/zh` pages render
   correctly on GitHub,
   badges and screenshots included.
3. The release assets appear (see section C).
4. No local or private file was uploaded — `config.toml`, `config2.toml`,
   `sessions-plan.json`, `*.ru.md` other than `README.ru.md`, and the local
   runtime folders must all be absent.

## B. First publication of a fresh clone

```powershell
git init
git add .
git status
```

Review the staged list and make sure none of these is in it:

- `config.toml`, `config2.toml`
- `.idea/`, `__pycache__/`
- local runtime folders (`logs/`, `backups/`, `sync/`, `state/`, `.tmp*`)
- `test-sandbox/`
- saved operation plans (`sessions-plan.json`, `repair-plan.json`,
  `resolutions.json`)

Then:

```powershell
git commit -m "chore: prepare codexSync for GitHub publication"
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

## C. Windows `.exe`

### Local smoke test

```powershell
python -m pip install --upgrade pip
pip install pyinstaller
pyinstaller --clean --noconfirm codexsync.spec
dist\codexsync.exe -c config.toml validate
```

The binary is the command line only. `codexsync.spec` excludes PySide6, so a
build machine that happens to have the optional GUI extra installed cannot make
the executable an order of magnitude larger; a healthy build is under about
15 MiB. If it is not, check that exclusion first.

### The windowed build

```powershell
pip install .[gui]
pyinstaller --clean --noconfirm codexsync-gui.spec
dist\codexsync-gui.exe -c config.toml validate
```

The name `codexsync-gui` differs from the console `codexsync` by more than
case on purpose: Windows file names are case-insensitive, so a windowed
`CodexSync.exe` and the console `codexsync.exe` would be one file and the
second build would replace the first. The windowed executable is also the
command line — given a subcommand it runs `cli.main` — which is what a frozen
install's scheduled task invokes. A healthy build is around 50 MiB, since it carries Qt.

Both executables are published by the same manual workflow.

### GitHub release assets

- Workflow: `.github/workflows/release-exe.yml`
- Trigger: manual `workflow_dispatch` only, with input `release_tag`, for a tag
  that already exists. The tag-push trigger is deliberately off until the GUI
  ships — pushing `v*` publishes no executable today.
- Output assets:
  - `codexsync-<tag>-windows-amd64.zip`
  - `codexsync-<tag>-windows-amd64.zip.sha256`
