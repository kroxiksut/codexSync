# Experiment: does the POSIX process detector see a running Codex?

**Status: not run.** `PROVEN_DETECTORS` in `src/codexsync/process_detector.py`
is empty, so on macOS and Linux `capability()` reports `supported=False`, every
process reading is `UNKNOWN`, and `safety.fail_on_unknown` turns that into a
refusal. Every mutating command therefore exits 3 on those platforms. That is
the intended state until this page has been filled in from a real machine.

## Why it cannot be decided by reading the code

The parser is tested (`tests/test_process_detector_posix.py`) against recorded
`ps` output, which proves it handles names with spaces and Linux's fifteen
character truncation. It does not prove that *those are the lines a running
Codex produces*. The desktop build was renamed `ChatGPT.app` on 2026-07-09
while keeping the bundle id `com.openai.codex`, helper names have changed
before, and a sandbox helper may or may not outlive the window.

Getting this wrong in one direction refuses a sync that would have been safe.
Getting it wrong in the other direction lets codexSync write into a live Codex
state, which is the failure this whole project exists to prevent. So the gate
is closed until observed, and it is observed on the machine, not deduced.

## What to run

On a Mac (Apple Silicon, `AI_RULES` 4) with Codex installed:

1. **Codex fully closed.** Quit it from the menu bar, then:

   ```sh
   ps -A -o pid=,comm= > closed-comm.txt
   ps -A -o pid=,command= > closed-command.txt
   python -m codexsync -c config.toml doctor
   ```

   Record what `doctor` says about `codex_process`. Expected while the gate is
   closed: a warning that the platform is unsupported.

2. **Codex open**, with a chat running and a command executing in it (that is
   when the sandbox helpers exist):

   ```sh
   ps -A -o pid=,comm= > open-comm.txt
   ps -A -o pid=,command= > open-command.txt
   ```

3. Compare the two listings and write down:
   - the exact `comm` of the main application process;
   - every helper whose path is inside the app bundle;
   - anything named `codex-*` outside the bundle;
   - the macOS version, the Codex/ChatGPT app version, and the date.

4. Check the shipped `[process_detection.background_process_names].macos` list
   against what was observed. An entry that never appears is noise; a process
   that appears and is not covered is the important finding. Remember that an
   entry containing `/` is matched against the path, so a new helper inside the
   bundle is already covered by `ChatGPT.app/Contents/MacOS/`.

5. With the list corrected, run the detector directly against the live machine:

   ```sh
   python - <<'PY'
   from codexsync.config import load_config
   from codexsync.runtime import collect_process_snapshot
   from pathlib import Path
   cfg = load_config(Path("config.toml"))
   snapshot = collect_process_snapshot(cfg)
   print("main:", [p.name for p in snapshot.main_processes])
   print("background:", [p.name for p in snapshot.background_processes])
   PY
   ```

   With Codex open this must be non-empty; with Codex closed it must be empty.
   Repeat the closed case twice: a helper that lingers for a few seconds after
   quitting is a real finding and belongs in the notes.

## What to write down afterwards

Add one entry to `PROVEN_DETECTORS`:

```python
PROVEN_DETECTORS = {
    "darwin": "macOS 15.5 (24F74), ChatGPT 1.2026.256, observed 2026-09-20",
}
```

and append the observation to this page: the two listings, which names matched,
and anything that appeared only while a command was running. If the listings
disagree with the shipped template, change the template (both copies) in the
same commit.

Do **not** add an entry because the parser looks right. An entry here is a
statement that someone watched a live Codex and saw it detected.

## Linux

Out of MVP scope (`AI_RULES` 4) and not in CI, but the adapter and the template
entries exist so that the same procedure works there. The Linux specifics worth
remembering while running it: `comm` is cut to fifteen characters, so
`codex-linux-sandbox` shows as `codex-linux-san`, and the desktop preview
installs under `/usr/lib/chatgpt/`, which is why that path is the marker rather
than any process name.
