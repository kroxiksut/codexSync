# Publishing checklist

Two flows live here. Most releases are the second one.

Documentation language: everything is English except `README.ru.md`, which is the
Russian entry point and the only tracked `*.ru.md` file — `.gitignore` excludes
the rest, so a new `docs/<name>.ru.md` would silently never be committed.

## A. Releasing a new version of an existing repository

### 1. Prove the tree is releasable

```powershell
python -m pytest -q
python -m codexsync -c config.toml validate
python -m codexsync -c config.toml doctor
```

The suite must be green, and `doctor` must reach `fail=0` on a real machine. A
warning is acceptable and usually means Codex is open or a session is genuinely
ambiguous; a failure is not.

### 2. Check the release metadata

1. `pyproject.toml` carries the version being released.
2. `CHANGELOG.md` has a section for exactly that version, with a date, and no
   `[Unreleased]` heading left behind.
3. `README.md` and `README.ru.md` describe the surface that actually ships —
   in particular, nothing incomplete is presented as a feature.
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
2. Both READMEs render correctly on GitHub.
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

### GitHub release assets

- Workflow: `.github/workflows/release-exe.yml`
- Trigger:
  - push tag `v*` (for example `v0.2.0`)
  - manual `workflow_dispatch` with input `release_tag`, for a tag that already
    exists
- Output assets:
  - `codexsync-<tag>-windows-amd64.zip`
  - `codexsync-<tag>-windows-amd64.zip.sha256`
