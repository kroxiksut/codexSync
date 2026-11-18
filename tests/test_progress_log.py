from __future__ import annotations

import json
from unittest.mock import patch

from codexsync.backup import BackupManager
from codexsync.models import CopyAction, SyncPlan
from codexsync.sync_engine import SyncEngine


def test_transfer_events_are_monotone_and_observer_failure_cannot_break_commit(tmp_path):
    from codexsync.progress_log import ProgressLog

    source = tmp_path / "input"
    source.write_bytes(b"private chat payload")
    target = tmp_path / ".codex" / "output"
    plan = SyncPlan(to_local=[CopyAction(source, target, "nested/config.toml")], to_cloud=[])
    seen = []
    legacy = []

    def broken_observer(event):
        seen.append(event)
        raise RuntimeError("private callback error")

    with ProgressLog(tmp_path / "logs", "sync", broken_observer) as events:
        SyncEngine(
            BackupManager(tmp_path / "backup", "machine-a", 7, 0),
            tmp_path / "temp",
            progress=lambda done, total: legacy.append((done, total)),
            progress_events=events,
        ).execute(plan, dry_run=False)

    assert target.read_bytes() == source.read_bytes()
    assert legacy == [(0, 1), (1, 1)]
    for phase in ("stage_chats", "apply_chats"):
        rows = [event for event in seen if event.phase == phase]
        assert [event.completed for event in rows] == [0, 1]
        assert [event.bytes_completed for event in rows] == [0, source.stat().st_size]
        assert rows[-1].relative_path == "nested/config.toml"
        assert rows[-1].direction == "local"
    assert [event.status for event in seen if event.phase == "apply_chats"] == ["running", "applied"]
    log, = (tmp_path / "logs").glob("*.jsonl")
    assert "private" not in log.read_text(encoding="utf-8")


def test_unsafe_metadata_never_reaches_observer_or_log(tmp_path):
    from codexsync.progress_log import ProgressLog, iter_progress_records

    seen = []
    with ProgressLog(tmp_path, "sessions", seen.append) as events:
        for path in ("C:\\private\\cwd", "/private/cwd", "../private", "dir/AUTH.JSON",
                     "x/../../private", "dir/file\nprivate", '{"cwd":"/private"}', "dir/.env.production"):
            events.report("apply_chats", 1, None, relative_path=path,
                          item_id="private title", direction="private direction", status="private error")
        events.report("apply_chats", 2, None, item_id="a" * 64, status="applied", direction="cloud")

    log, = tmp_path.glob("*.jsonl")
    assert "private" not in log.read_text(encoding="utf-8")
    assert all(row.relative_path is None for row in seen)
    assert all(row.total is None for row in seen if row.phase == "apply_chats")
    assert seen[-2].item_id == "a" * 12
    assert len(list(iter_progress_records(log))) == len(seen)


def test_interrupted_progress_publication_keeps_complete_records(tmp_path):
    from codexsync.progress_log import ProgressLog, iter_progress_records

    with patch("codexsync.progress_log.replace_with_retry", side_effect=OSError("disk error")):
        with ProgressLog(tmp_path, "sync") as events:
            events.report("stage_chats", 1, 2)

    partial, = tmp_path.glob("*.partial.jsonl")
    assert not list(tmp_path.glob("*.complete.jsonl"))
    with partial.open("ab") as handle:
        handle.write(b'{"truncated":')
    assert [row["phase"] for row in iter_progress_records(partial)] == ["stage_chats", "transfer"]

    with ProgressLog(tmp_path, "sync") as events:
        events.report("stage_chats", 2, 2)
    complete, = tmp_path.glob("*.complete.jsonl")
    assert all(json.loads(line) for line in complete.read_text(encoding="utf-8").splitlines())
    assert partial.exists()


def test_a_phase_boundary_flushes_the_live_log_before_operation_exit(tmp_path):
    from codexsync.progress_log import ProgressLog, iter_progress_records

    with ProgressLog(tmp_path, "sync") as events:
        events.report("stage_chats", 1, 1)
        events.report("apply_chats", 0, 1)
        active, = tmp_path.glob("*.partial.jsonl")
        assert [row["phase"] for row in iter_progress_records(active)] == ["stage_chats", "apply_chats"]


def test_log_directory_cannot_resolve_inside_a_codex_state_root(tmp_path):
    from codexsync.progress_log import progress_log_dir

    state = tmp_path / ".codex"
    assert progress_log_dir(state, state) is None
    assert progress_log_dir(tmp_path / "temp", state) == (tmp_path / "temp" / "progress").resolve()


def test_unwritable_log_does_not_suppress_live_progress_or_stop_commit(tmp_path):
    from codexsync.progress_log import ProgressLog

    source = tmp_path / "source"
    source.write_bytes(b"settings")
    target = tmp_path / "destination"
    seen = []
    # A file where the directory belongs simulates a logging failure without
    # interfering with the separate payload and backup paths.
    with ProgressLog(source, "sync", seen.append) as events:
        SyncEngine(BackupManager(tmp_path / "backup", "m", 7, 0), tmp_path / "temp",
                   progress_events=events).execute(
            SyncPlan(to_local=[CopyAction(source, target, "config.toml")], to_cloud=[]),
            dry_run=False,
        )

    assert target.read_bytes() == b"settings"
    assert any(event.status == "applied" for event in seen)
