"""Write the frozen 0.2.0a1 workspace fixture (`D-030`). Run once, never again.

    python scripts/make_a1_fixture.py

From 0.2.0a1 on, what codexSync writes into the shared workspace and its own
folders is a promise: every later version reads it. `tests/test_a1_compat.py`
holds today's readers to that promise against `tests/fixtures/ws-a1/`, which
this script wrote with the 0.2.0a1 code through its real writers. The point is
that the fixture is *frozen*: regenerating it with a later version would test
that version against itself and prove nothing. A format change that cannot
read these files is the bug, not the fixture.

Everything is invented -- two machines, `desk` and `lap`, three chats, two
projects. Never point this at a real `.codex`. The run happens in the test
sandbox with the process check replaced (Codex is never looked for) and a
clock that does not wait.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import textwrap
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from codexsync import app  # noqa: E402
from codexsync.config_edit import change_config_value  # noqa: E402
from codexsync.guardian_runner import GuardianRunner  # noqa: E402
from codexsync.safety_gate import OperationKind, ProcessState, SafetyDecision  # noqa: E402
from codexsync.session_scope import SessionScope  # noqa: E402
from codexsync.version import __version__  # noqa: E402

#: The run happens here, at a fixed place, so the paths inside the data are the
#: same on every run of the generator (the test never depends on them). The
#: mirror, backup and temp folders are named `mirror`, `bk` and `t` because the
#: repository ignores `sync/`, `backups/` and `.tmp/` wherever they appear.
WORK = REPO / "test-sandbox" / "a1"
FIXTURE = REPO / "tests" / "fixtures" / "ws-a1"

CHAT_A = "0199a1a1-0000-7000-8000-00000000000a"  # started on desk, carried to lap
CHAT_B = "0199a1a1-0000-7000-8000-00000000000b"  # started on lap, carried to desk
CHAT_C = "0199a1a1-0000-7000-8000-00000000000c"  # continued on both: decided by the rule


class _Gate:
    def check(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return SafetyDecision(operation, ProcessState.STOPPED, True, "fixture gate")

    def require(self, operation: OperationKind, *, final: bool = False) -> SafetyDecision:
        return self.check(operation, final=final)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _session(codex: Path, session_id: str, events: list[tuple[str, str]], cwd: str) -> Path:
    path = codex / "sessions" / "2026" / "10" / "01" / f"rollout-2026-10-01T09-00-00-{session_id}.jsonl"
    rows = [{
        "timestamp": "2026-10-01T09:00:00.000Z", "type": "session_meta",
        "payload": {"id": session_id, "cwd": cwd, "originator": "codex_cli_rs", "thread_source": "user"},
    }]
    rows += [
        {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "user", "text": text}}
        for stamp, text in events
    ]
    return _write(path, b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in rows))


def _append(path: Path, stamp: str, text: str) -> None:
    row = {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "user", "text": text}}
    with path.open("ab") as handle:
        handle.write(json.dumps(row, sort_keys=True).encode() + b"\n")


def _catalogue(codex: Path, rows: dict[str, tuple[Path, str | None]]) -> None:
    connection = sqlite3.connect(codex / "state_5.sqlite")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "archived INTEGER NOT NULL, title TEXT NOT NULL, name TEXT)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS backfill_state (id INTEGER PRIMARY KEY CHECK (id = 1), status TEXT NOT NULL, "
            "last_watermark TEXT, last_success_at INTEGER, updated_at INTEGER NOT NULL)"
        )
        connection.execute("INSERT OR REPLACE INTO backfill_state VALUES (1, 'complete', NULL, 1, 1)")
        for thread_id, (path, name) in rows.items():
            connection.execute(
                "INSERT OR REPLACE INTO threads VALUES (?, ?, 0, ?, ?)", (thread_id, str(path), "first message", name)
            )
        connection.commit()
    finally:
        connection.close()


def _state(projects: dict[str, Path], bindings: dict[str, str]) -> bytes:
    state = {
        "electron-main-window-bounds": {"x": 10, "y": 10},
        "local-projects": {
            project_id: {"createdAt": 1, "id": project_id, "name": f"Project {project_id}",
                         "rootPaths": [str(root)], "updatedAt": 2}
            for project_id, root in projects.items()
        },
        "project-order": list(projects),
        "pinned-project-ids": list(projects)[:1],
        "thread-project-assignments": {
            thread: {"projectKind": "local", "projectId": project} for thread, project in bindings.items()
        },
    }
    return json.dumps(state, indent=2).encode()


def _config(name: str, codex: Path, workspace: Path) -> Path:
    text = textwrap.dedent(f"""
        [identity]
        machine_id = "{name}"

        [sync]
        mode = "cold"
        session_mode = "all"

        [paths]
        workspace_root_dir = "{workspace.as_posix()}"
        local_state_dir = "{codex.as_posix()}"
        cloud_root_dir = "{(workspace / 'mirror').as_posix()}"
        backup_dir = "{(workspace / 'bk').as_posix()}"
        temp_dir = "{(workspace / 't').as_posix()}"

        [guardian]
        root_dir = "{(workspace / 'guardian').as_posix()}"

        [semantic]
        root_dir = "{(workspace / 'semantic').as_posix()}"

        [targets]
        include_roots = ["sessions", "skills"]

        [backup]
        backup_before_overwrite = true
        compression = "none"

        [state]
        manifest_file = "{(workspace / 'manifest.json').as_posix()}"

        [conflict]
        policy = "prefer_newer_mtime"

        [state_backup]
        root_dir = "{(WORK / 'copies').as_posix()}"

        [handoff]
        root_dir = "{(workspace / 'handoff').as_posix()}"
    """).strip() + "\n"
    return _write(WORK / name / "config.toml", text.encode())


def _guardian_once(config: Path) -> None:
    built = app.build_guardian_runner(config)
    clock = _Clock()
    runner = GuardianRunner(built.source_path, built.store, built.config, monotonic=clock.monotonic, sleep=clock.sleep)
    outcome = runner.once()
    print(f"  guardian: {outcome.status.value} {outcome.detail}")


def generate() -> None:
    if not __version__.startswith("0.2.0a1"):
        raise SystemExit(f"This fixture is written by 0.2.0a1 only; this is {__version__}")
    shutil.rmtree(WORK, ignore_errors=True)
    workspace = WORK / "ws"
    (workspace / "mirror").mkdir(parents=True)
    desk_codex, lap_codex = WORK / "desk" / "codex", WORK / "lap" / "codex"
    desk, lap = _config("desk", desk_codex, workspace), _config("lap", lap_codex, workspace)

    # Plain project folders: the project-files board lists every file.
    for name in ("alpha", "beta"):
        _write(WORK / "proj" / name / "README.txt", f"invented project {name}\n".encode())

    a_desk = _session(desk_codex, CHAT_A, [("2026-10-01T09:01:00.000Z", "hello from desk")], str(WORK / "proj" / "alpha"))
    c_desk = _session(desk_codex, CHAT_C, [("2026-10-01T09:02:00.000Z", "shared start")], str(WORK / "proj" / "beta"))
    _write(desk_codex / "skills" / "note.md", b"an invented skill\n")
    _write(desk_codex / ".codex-global-state.json", _state(
        {"alpha": WORK / "proj" / "alpha", "beta": WORK / "proj" / "beta"}, {CHAT_A: "alpha", CHAT_C: "beta"},
    ))
    _catalogue(desk_codex, {CHAT_A: (a_desk, "Desk chat"), CHAT_C: (c_desk, "Shared chat")})

    b_lap = _session(lap_codex, CHAT_B, [("2026-10-01T09:03:00.000Z", "hello from lap")], str(WORK / "proj" / "beta"))
    _write(lap_codex / ".codex-global-state.json", _state({"beta": WORK / "proj" / "beta"}, {CHAT_B: "beta"}))
    _catalogue(lap_codex, {CHAT_B: (b_lap, "Lap chat")})

    gate = _Gate()
    with mock.patch("codexsync.app._make_safety_gate", side_effect=lambda cfg: gate), \
            mock.patch("codexsync.app._project_hash_cache_root", return_value=WORK / "hash-cache"):
        print("round 1: desk hands off, lap loads and hands off, desk loads")
        app.run_handoff(desk)
        app.run_handoff(lap)
        app.run_handoff(desk)

        print("round 2: chat C continued differently on both machines; the newer rule decides")
        c_lap = next((lap_codex / "sessions").rglob(f"*{CHAT_C}.jsonl"))
        _append(c_desk, "2026-10-02T10:00:00.000Z", "continued on desk")
        app.run_handoff(desk)
        _append(c_lap, "2026-10-02T11:00:00.000Z", "continued on lap, later")
        app.run_handoff(lap)
        app.run_handoff(desk)

        print("guardian: a snapshot, then a shrink that goes to quarantine")
        _guardian_once(desk)
        state = json.loads((desk_codex / ".codex-global-state.json").read_bytes())
        shrunk = dict(state, **{"local-projects": {}, "project-order": [], "pinned-project-ids": [],
                                 "thread-project-assignments": {}})
        _write(WORK / "shrunk" / ".codex-global-state.json", json.dumps(shrunk, indent=2).encode())
        built = app.build_guardian_runner(desk)
        clock = _Clock()
        GuardianRunner(
            WORK / "shrunk" / ".codex-global-state.json", built.store, built.config,
            monotonic=clock.monotonic, sleep=clock.sleep,
        ).once()

        print("a copy of .codex, a working set, a config edit")
        app.create_codex_backup(desk)
        app.write_working_set(
            desk, SessionScope(projects=("alpha",), chats=()), source_machine="lap", target_machine="desk",
        )
        change_config_value(desk, "backup.retention_days", 21)

    _copy_out(workspace)


def _copy_out(workspace: Path) -> None:
    """Everything codexSync wrote, minus locks: a lock belongs to a running process."""
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True)
    shutil.copytree(workspace, FIXTURE / "ws", ignore=shutil.ignore_patterns("locks", "*.lock"))
    shutil.copytree(WORK / "copies", FIXTURE / "copies")
    _write(FIXTURE / "WRITTEN_BY.txt", (
        f"codexsync {__version__}, scripts/make_a1_fixture.py, at {WORK.as_posix()}.\n"
        "Frozen: never regenerate (D-030). Every name, chat and project in it is invented.\n"
    ).encode())
    # What the checkout must reproduce byte for byte (`.gitattributes` marks
    # the folder `-text`); some writers use text mode, so CRLF can be original.
    sums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(FIXTURE).as_posix()}\n"
        for path in sorted(FIXTURE.rglob("*")) if path.is_file()
    )
    _write(FIXTURE / "SHA256SUMS", sums.encode())
    total = sum(path.stat().st_size for path in FIXTURE.rglob("*") if path.is_file())
    for path in sorted(FIXTURE.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(FIXTURE).as_posix()}  {path.stat().st_size}")
    print(f"fixture: {total} bytes")


if __name__ == "__main__":
    generate()
