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

1. **Codex open, `[sync] close_codex = false`** — `sync --apply`,
   `restore --apply`, `repair-projects apply`, `sessions apply`,
   `chats move --confirm` and `recover resume|rollback` must each exit `3`, and
   `guardian snapshot --once` must still succeed. With `close_codex = true` a
   sync asks Codex to quit instead: it goes on once the gate sees Codex gone,
   and exits `3` with nothing written if Codex stays open.
2. **Codex closed** — a sync runs, a verified backup exists before the first
   overwrite, and the journal ends `COMMITTED`.
3. **An interrupted mutation** — kill the process during a sync, then check
   that `recover inspect` reports it and that the Recovery screen offers
   inspect, resume and rollback, each behind a dry run of the same choice.

0.2 carries more than files, so these define what a 0.2 release actually
ships. Each needs two real machines (A and B) or a real 0.1 config; record the
date and the Codex version when one is run, and say in the release notes which
were not:

4. **0.1 config upgrade** — a `config.toml` written by 0.1.2: `config check`
   lists the findings, `config upgrade --confirm-plan` applies them, the old
   file is in `config-history/`, and a sync then runs.
5. **Full handoff A → B → A** — sync on A with Codex closed, wait for the
   cloud, sync on B, work in Codex on B, close it, sync on B, sync on A. Each
   side shows the other's work; `handoff status` agrees on both.
6. **A new chat travels** — a chat created on A appears on B after the sync
   and one Codex start (the catalogue rebuild, `D-024`); `doctor` on B reports
   `session_visibility` with `not_listed=0`.
7. **A chat name travels** — a chat renamed on A shows that name on B; a chat
   already named on B keeps its own (`D-025`).
8. **The project list** — a project added on A appears in B's sidebar with
   its chats (`D-022`); a project folder missing on B is reported, not created
   (`D-026`).
9. **A conflict under the rule** — the same chat continued on both machines:
   the newer one is kept under the default `prefer_newer_mtime`, the other is
   in the conflict bundle, and `manual_abort` stops with exit `2` instead
   (`D-027`).
10. **Closing Codex on request** — `close_codex = true` with Codex open: the
    desktop app quits normally, the sync runs, and Codex's state reads cleanly
    on the next start (`D-029`).

For an alpha, 1–3 must pass and 4–10 are recorded as run or not run; for a
stable release all ten must pass on Windows.

### 2. Check the release metadata

1. `pyproject.toml` carries the version being released, in its PEP 440 form
   (`0.2.0a1`, `0.2.0rc1`, `0.2.0`).
2. `CHANGELOG.md` has a section for exactly that version, with a date, and no
   `[Unreleased]` heading left behind. Its introduction — everything before the
   first `###` — becomes the GitHub release body, so it is written for someone
   arriving from the release page, with absolute links.

`python scripts/release_meta.py check --tag <tag>` checks both, and the
release workflow runs it before anything else.
3. `README.md`, `README.ru.md`, `README.zh.md`, `docs/en/`, `docs/ru/` and
   `docs/zh/` describe the surface
   that actually ships — in particular, nothing incomplete is presented as a
   feature.
4. `config.example.toml` and `src/codexsync/config.example.toml` are still
   byte-identical (`tests/test_config_template_parity.py` fails otherwise).
5. `SECURITY.md` → *Supported versions* names the version being released as
   supported, and says which older line stops receiving fixes. Through the
   0.2 alphas and release candidates it reads "0.2.0 pre-releases and main" and
   "0.1.x until 0.2.0 is released"; at 0.2.0 it becomes "0.2.x" and "0.1.x —
   no, upgrade to 0.2".
6. While the newest release is a pre-release, every `pip install` the READMEs
   and `docs/*/README.md` show carries `--pre` — without it pip installs the
   last final release. Remove it at the final release.

### 3. Confirm the optional extra stays optional

The core and the CLI must install with no third-party dependency, and the
command line must keep working on a machine that has no Qt at all:

```powershell
python -m pytest -q tests/test_gui_boundary.py
```

That file blocks the PySide6 import in a subprocess and imports the CLI anyway.
If it passes, `pip install codexsync` pulls nothing extra.

### 4. Rehearse, tag, release

A tag is written the way people read it — `v0.2.0-alpha.1`, `v0.2.0-rc.1`,
`v0.2.0` — and means the PEP 440 version `pyproject.toml` declares
(`0.2.0a1`, `0.2.0rc1`, `0.2.0`). PEP 440 treats the two spellings as one
version, so `codexsync --version`, PyPI and the tag never name different builds.

1. **Rehearse before tagging.** Run the `Release` workflow by hand with
   `release_tag` set to the tag you are about to push, `ref` set to `main`,
   and both publishing switches off. It checks the metadata, runs the suite on
   each Windows architecture, builds every exe and the wheel, and leaves them
   as artifacts of the run. Nothing is published from anything but the tag.
2. **Tag and push:**

   ```powershell
   git push origin main
   git tag v0.2.0-alpha.1
   git push origin v0.2.0-alpha.1
   ```

3. **Release:** run `Release` with that tag, `ref` empty, `publish_pypi` on,
   `github_release` on and `draft` on. PyPI is uploaded only after every build
   succeeded; the GitHub release is created as a draft, marked pre-release
   automatically for an `alpha`/`beta`/`rc` tag. Read the draft, then publish
   it on GitHub.

If a build fails after the tag is pushed, fix it on `main` and release the next
number (`v0.2.0-alpha.2`) rather than moving the tag: a tag others may have
fetched is not rewritten, and a PyPI version can never be uploaded twice.

### PyPI: one-time setup

The workflow publishes through PyPI's *trusted publishing*, so no token is
stored in the repository or its secrets. Once, before the first release from
the workflow:

1. On pypi.org → project `codexsync` → *Publishing* → *Add a new publisher* →
   GitHub: owner `kroxiksut`, repository `codexSync`, workflow `release.yml`,
   environment `pypi`.
2. On GitHub → *Settings* → *Environments* → create `pypi`. Adding yourself as
   a required reviewer makes every upload wait for a click.

Without it the `Publish to PyPI` job fails and nothing is uploaded; the GitHub
release is created independently. A manual upload remains possible:

```powershell
python -m pip install --upgrade build twine
python scripts/release_meta.py pypi-readme --tag v0.2.0-alpha.1   # never commit the result
python -m build
python -m twine check --strict dist/*
python -m twine upload dist/*
git checkout README.md
```

`pypi-readme` pins every relative link and picture in `README.md` to the tag on
GitHub, because PyPI shows the file without the repository around it, and turns
GitHub's `> [!IMPORTANT]` alerts, which PyPI shows literally, into a bold label.

### 5. Verify after push

1. CI workflow `CI` runs in `Actions` for `windows-latest` and `macos-latest`,
   on Python 3.11, 3.12, 3.13 and 3.14 (green required), and for
   `ubuntu-latest` (experimental, D-031: read its result, it does not block).
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

- Workflow: `.github/workflows/release.yml` (`Release`), manual
  `workflow_dispatch` only — see section A.4 for its inputs.
- Builds, each with a `.sha256` beside it and `LICENSE` inside:

  | Asset | Contents | Runner |
  |---|---|---|
  | `codexsync-gui-<tag>-windows-amd64.zip` | window + command line | `windows-latest` |
  | `codexsync-gui-<tag>-windows-arm64.zip` | window + command line | `windows-11-arm` |
  | `codexsync-<tag>-windows-amd64.zip` | command line | `windows-latest` |
  | `codexsync-<tag>-windows-arm64.zip` | command line | `windows-11-arm` |
  | `codexsync-<version>-py3-none-any.whl`, `codexsync-<version>.tar.gz` | the package PyPI gets | `ubuntu-latest` |

- There is no 32-bit build: Codex itself is published for x64 and ARM64 only
  (Microsoft Store catalogue, checked 2026-10-09), so a 32-bit codexSync would
  have no Codex to sync. PySide6 has no `win32` wheel either.
- Each Windows job runs the whole suite with its own interpreter first (ARM64
  is not in the everyday CI matrix), then checks that `codexsync.exe
  --version` names the release and that `scripts/check_exe_version.py` reads the
  expected version resource back out of every exe.
- Minimum versions the builds carry: Windows 10 (1809) or later for the window
  (Qt 6); the command-line builds run on Python 3.12's own minimum, but only
  Windows 10 and 11 are tested.
