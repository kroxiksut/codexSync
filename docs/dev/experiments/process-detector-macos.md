# Experiment: does the POSIX process detector see a running Codex?

**Status: run on Linux (2026-10-09, [below](#linux)); not run on a Mac -- the
GitHub runner workflow below is the stand-in there.** `PROVEN_DETECTORS` in
`src/codexsync/process_detector.py` holds `linux` only, so on macOS
`capability()` reports `supported=False`, every process reading is `UNKNOWN`,
and `safety.fail_on_unknown` turns that into a refusal: every mutating command
exits 3 there. That is the intended state until this page has been filled in
from a real Mac.

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

## On a GitHub runner, without a Mac

The project has no Mac, so the same observation runs on GitHub's macOS
runners: `.github/workflows/macos-detector.yml`, started by hand (Actions ->
macOS process detector -> Run workflow). It installs the real Codex CLI from
npm and runs `scripts/ci/macos_detector_probe.py`, which asks the detector --
through `collect_process_snapshot`, with the names the shipped template
configures -- whether Codex is running:

1. before anything starts (must be stopped);
2. while the Codex CLI runs (`codex app-server`, else `mcp-server`, else the
   interactive screen under a pty; must be running), and after it is stopped
   (must be stopped again, and how long a helper lingers is recorded);
3. while a stand-in program runs from a `ChatGPT.app/Contents/MacOS/` bundle
   (must be running: the desktop app's path marker), and while the same program
   runs as `ChatGPT` outside any bundle (must be stopped: that is the ordinary
   ChatGPT app).

The job summary and the `detector-report-*` artifact hold the result.

What it does not observe: the desktop app itself, and the helpers it starts
while a command runs in a chat. A runner cannot install it. So an entry made
from this run says so, and the first observation on a real Mac replaces it:

```python
PROVEN_DETECTORS = {
    "darwin": "GitHub macos-14/15 runner, Codex CLI 0.x via npm, observed 2026-10-07; "
              "desktop app matched by its bundle path with a stand-in, not observed",
}
```

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

Experimental (`D-031`). The Linux specifics worth remembering while running the
procedure: `comm` is cut to fifteen characters, so `codex-linux-sandbox` shows
as `codex-linux-san`, and the desktop preview installs under
`/usr/lib/chatgpt/`, which is why that path is the marker rather than any
process name.

### Observed 2026-10-09

Ubuntu 26.04 LTS, kernel 7.0.0-38, GNOME; Codex desktop from the
`chatgpt` deb 26.1002.52244, signed in; codexSync 0.2.0a1 in a venv on
Python 3.14.4. Each reading went through `collect_process_snapshot` with the
names the shipped template configures, polled every half second for the whole
session; the listings stayed on the machine.

1. **Open, idle.** Main: `codex` twice --
   `/usr/lib/chatgpt/resources/codex ... app-server` and `codex exec-server
   --remote ...` (a cloud environment the app keeps connected). Background:
   19 processes under `/usr/lib/chatgpt/` (`ChatGPT` -- the main process,
   zygotes, GPU, renderers, utilities -- and `browser_crashpad_handler`, which
   `comm` shows as `browser_crashpa`). User data is in `~/.config/Codex`; the
   state in `~/.codex`.
2. **A chat running `sleep 600` locally.** Added: `codex-code-mode-host`
   (`/usr/lib/chatgpt/resources/`, `comm` `codex-code-mode`), and the sandbox:
   `codex-linux-sandbox` started from
   `~/.codex/tmp/arg0/codex-arg0<random>/codex-linux-sandbox` -- **outside the
   install path**, so only its (truncated) name finds it -- then `bwrap`, then
   `codex-linux-sandbox` again inside it with `comm` `codex`, then the command.
   `node_repl` and a `MainThread` came and went while the chat was set up.
   Never seen: `codex-app-server` and `codex-execve-wrapper` as process names
   (the app-server runs as `codex app-server`); both stay in the list, harmless.
3. **Quit while the command ran** (SIGTERM to the main `ChatGPT` process, which
   Electron treats as a normal quit). After 2 s every process under
   `/usr/lib/chatgpt/`, the app-server and the outer helper were gone. The inner
   sandbox process (`comm` `codex`) and `sleep` were not: re-parented to the
   user's `systemd --user`, alive until `sleep` ended nine minutes later. The
   detector read that as running the whole time (main name `codex`) and as
   stopped from the moment it ended. That is the intended answer -- work Codex
   started is still in flight -- and the message names `codex (pid N)`.
4. **Closed**, read twice five seconds apart: nothing, stopped. `doctor`
   reported `codex_process` as undetermined (the gate was still closed). With
   `PROVEN_DETECTORS["linux"]` set in memory, `SafetyGate.check(SYNC)` passed
   its two-second stopped window.

The shipped names needed no change. `tests/test_process_detector_posix.py`
(`LinuxObservedTests`) replays the trimmed listings. What this does not cover:
a handoff to or from Linux, asking Codex to close (`PROVEN_CLOSERS` has no
Linux entry), and the user-level scheduler.
