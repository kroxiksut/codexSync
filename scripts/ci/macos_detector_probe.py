"""Watch the POSIX process detector see a live Codex on a real Mac (GitHub runner).

`docs/dev/experiments/process-detector-macos.md` asks for a Mac with Codex
installed. The project has no Mac, so this runs the same observation on a
`macos-latest` runner: it starts the real Codex CLI (`npm i -g @openai/codex`)
and asks the detector -- through `collect_process_snapshot`, the path the
safety gate samples, with the names the shipped template configures -- whether
Codex is running, before, during and after.

The desktop app cannot be installed on a runner, so its path marker
(`ChatGPT.app/Contents/MacOS/`) is checked with a stand-in program placed in a
bundle of that shape, beside a decoy named `ChatGPT` outside any bundle -- the
ordinary ChatGPT app, which must never count as Codex.

Exit 0 only when every expectation held. The report goes to stdout, to
`$GITHUB_STEP_SUMMARY` and to `detector-report.json` for the artifact; it lists
process names and pids of this runner only, never a command line.

    python scripts/ci/macos_detector_probe.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from codexsync.config import load_config  # noqa: E402
from codexsync.config_edit import create_config  # noqa: E402
from codexsync.runtime import collect_process_snapshot  # noqa: E402

#: How long a process gets to appear, and to be gone after it was stopped.
SETTLE_SECONDS = 3.0
LINGER_LIMIT_SECONDS = 15.0
#: Ways to keep the real Codex CLI running without signing in. The first that
#: stays alive is used; a pty wrapper last, for the interactive screen.
CODEX_MODES = (("app-server",), ("mcp-server",), ())


class Probe:
    def __init__(self, work: Path) -> None:
        config = work / "config.toml"
        (work / "codex").mkdir()
        create_config(
            config, machine_id="ci-mac", local_state_dir=str(work / "codex"),
            workspace_root_dir=str(work / "workspace"),
        )
        self.cfg = load_config(config)
        self.checks: list[dict] = []

    def snapshot(self) -> dict:
        found = collect_process_snapshot(self.cfg)
        return {
            "main": sorted({p.name for p in found.main_processes}),
            "background": sorted({p.name for p in found.background_processes}),
            "sandbox": bool(found.sandbox_detected),
            "pids": sorted({p.pid for p in (*found.main_processes, *found.background_processes)}),
        }

    @staticmethod
    def running(seen: dict) -> bool:
        return bool(seen["main"] or seen["background"] or seen["sandbox"])

    def expect(self, name: str, want_running: bool, seen: dict, note: str = "") -> bool:
        ok = self.running(seen) == want_running
        self.checks.append({
            "check": name, "expected": "running" if want_running else "stopped",
            "seen": seen, "ok": ok, "note": note,
        })
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {seen}{' -- ' + note if note else ''}")
        return ok

    def wait_gone(self, name: str) -> bool:
        start = time.monotonic()
        while True:
            seen = self.snapshot()
            if not self.running(seen):
                return self.expect(name, False, seen, f"gone after {time.monotonic() - start:.1f}s")
            if time.monotonic() - start > LINGER_LIMIT_SECONDS:
                return self.expect(name, False, seen, f"still there after {LINGER_LIMIT_SECONDS:.0f}s")
            time.sleep(0.5)


def _start(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, 15)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, 9)
        process.wait(timeout=10)


def start_codex(codex: str) -> tuple[subprocess.Popen | None, str]:
    for mode in CODEX_MODES:
        argv = [codex, *mode] if mode else ["script", "-q", "/dev/null", codex]
        process = _start(argv)
        time.sleep(SETTLE_SECONDS)
        if process.poll() is None:
            return process, " ".join(mode) or "interactive (pty)"
        process.wait()
    return None, ""


def stand_in(folder: Path, name: str) -> Path:
    """A copy of `sleep` under ``folder/name``: a real process with that path."""
    target = folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    # copyfile, not copy2: copy2 also copies the file flags, and /bin/sleep
    # carries the system-protected one, which chflags may not set elsewhere.
    shutil.copyfile("/bin/sleep", target)
    target.chmod(0o755)
    return target


def codex_version(codex: str) -> str:
    try:
        return subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({exc})"


def main() -> int:
    if sys.platform != "darwin":
        print("this probe observes macOS; run it on a Mac or a macos-* runner", file=sys.stderr)
        return 2
    codex = shutil.which("codex")
    work = Path(tempfile.mkdtemp(prefix="codexsync-probe-"))
    probe = Probe(work)
    ok = probe.expect("nothing running at the start", False, probe.snapshot())

    mode = ""
    if codex is None:
        probe.checks.append({"check": "Codex CLI installed", "ok": False, "note": "`codex` is not on PATH"})
        print("[FAIL] `codex` is not on PATH (npm i -g @openai/codex)")
        ok = False
    else:
        process, mode = start_codex(codex)
        if process is None:
            probe.checks.append({"check": "Codex CLI stays running", "ok": False, "note": "every mode exited"})
            print("[FAIL] the Codex CLI exited in every mode tried")
            ok = False
        else:
            ok &= probe.expect(f"Codex CLI running ({mode})", True, probe.snapshot())
            _stop(process)
            ok &= probe.wait_gone("Codex CLI stopped")

    bundle = stand_in(work / "Applications" / "ChatGPT.app" / "Contents" / "MacOS", "ChatGPT")
    process = _start([str(bundle), "120"])
    time.sleep(SETTLE_SECONDS)
    ok &= probe.expect("desktop app (ChatGPT.app bundle path) running", True, probe.snapshot())
    _stop(process)
    ok &= probe.wait_gone("desktop app stopped")

    decoy = stand_in(work / "elsewhere", "ChatGPT")
    process = _start([str(decoy), "120"])
    time.sleep(SETTLE_SECONDS)
    ok &= probe.expect(
        "a program named ChatGPT outside the bundle is not Codex", False, probe.snapshot(),
        "the ordinary ChatGPT app",
    )
    _stop(process)

    report = {
        "ok": bool(ok),
        "macos": platform.mac_ver()[0],
        "machine": platform.machine(),
        "python": platform.python_version(),
        "codex_cli": codex_version(codex) if codex else None,
        "codex_mode": mode,
        "process_names": list(probe.cfg.process_detection.process_names),
        "checks": probe.checks,
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    Path("detector-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [
            f"## macOS process detector: {'held' if ok else 'FAILED'}",
            f"macOS {report['macos']} ({report['machine']}), Codex CLI {report['codex_cli']}, mode `{mode}`",
            "",
            "| Check | Expected | Seen | Result |",
            "|---|---|---|---|",
        ]
        for check in probe.checks:
            seen = check.get("seen") or {}
            names = ", ".join(seen.get("main", []) + seen.get("background", [])) or "-"
            lines.append(
                f"| {check['check']} | {check.get('expected', '-')} | {names} {check.get('note', '')} | "
                f"{'ok' if check['ok'] else 'FAIL'} |"
            )
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({key: report[key] for key in ("ok", "macos", "machine", "codex_cli", "codex_mode")}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
