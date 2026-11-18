"""Stopping a real file sync must leave a truthful journal and a usable mirror."""
import json
import os
import threading
from unittest.mock import patch

import pytest

from codexsync.app import build_context, run_sync
from codexsync.backup import BackupManager
from codexsync.cancellation import CancellationToken, OperationCancelled, OperationRolledBack
from codexsync.exceptions import FailSafeError, SafetyPreconditionError
from codexsync.models import DeleteAction
from codexsync.mutation_journal import JournalState, JournalStore
from codexsync.recovery import resume_operation, rollback_operation
from codexsync.work_scheduler import WorkScheduler
from test_recovery import _StoppedGate, _write_config


@pytest.fixture
def mirror(tmp_path):
    local, cloud = tmp_path / "local-state/data", tmp_path / "cloud/data"
    local.mkdir(parents=True)
    cloud.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (local / name).write_text("new " + name)
        if name != "a":
            (cloud / name).write_text("old")
            os.utime(cloud / name, (1000, 1000))
    config = _write_config(tmp_path)
    gate = _StoppedGate()
    with patch("codexsync.app._make_safety_gate", return_value=gate), patch(
        "codexsync.recovery._make_safety_gate", return_value=gate,
    ):
        yield local, cloud, config, JournalStore(tmp_path / ".tmp"), gate


@pytest.mark.parametrize("phase", ["stage_chats", "backup_chats", "precommit"])
def test_stop_before_commit_cleans_own_staging_and_next_run_replans(mirror, phase):
    local, cloud, config, store, gate = mirror
    token = CancellationToken()
    foreign = cloud / ".b.someone-else.codexsync.tmp"
    foreign.write_bytes(b"foreign")
    events = []

    def progress(event):
        events.append(event)
        if event.phase == phase and event.completed == 1:
            token.request_cancel()

    real_require = gate.require
    def check(operation, *, final=False):
        if final and phase == "precommit":
            token.request_cancel()
        return real_require(operation, final=final)

    with patch.object(gate, "require", side_effect=check), pytest.raises(OperationCancelled):
        run_sync(build_context(config), False, cancellation=token, event_progress=progress)
    assert events[-1].status == "cancelled"
    assert store.non_terminal() == []
    only = store.load(next(store.root.glob("*.json")).stem)
    assert only.failure == "OperationCancelled"
    assert only.failure_code == "OPERATION_CANCELLED"
    assert sorted(p.name for p in cloud.iterdir()) == [foreign.name, "b", "c"]
    assert (cloud / "b").read_text() == (cloud / "c").read_text() == "old"
    assert foreign.read_bytes() == b"foreign"
    (local / "a").write_text("edited after stop")
    run_sync(build_context(config), False)
    assert (cloud / "a").read_text() == "edited after stop"
    assert build_context(config).plan.action_count == 0


def test_stop_in_scan_never_begins_a_journal(mirror):
    _, _, config, store, _ = mirror
    from codexsync.filters import PathFilter
    token = CancellationToken()
    visited = []
    def cancel(_self, rel):
        visited.append(rel)
        token.request_cancel()
        return False
    with patch.object(PathFilter, "is_excluded", cancel), pytest.raises(OperationCancelled):
        build_context(config, cancellation=token)
    assert 1 <= len(visited) <= 2
    assert not store.root.exists()


def test_worker_error_during_stop_survives_join_and_no_new_items_start():
    token = CancellationToken()
    active = threading.Barrier(2)
    stopped = threading.Event()
    started = []
    def work(item):
        started.append(item)
        active.wait(timeout=5)
        if item == 0:
            token.request_cancel()
            stopped.set()
        else:
            assert stopped.wait(5)
            raise OSError("cloud file disappeared")
    with WorkScheduler(io_workers=2) as scheduler, pytest.raises(OSError, match="disappeared"):
        scheduler.map_read(work, range(10), cancellation=token)
    assert sorted(started) == [0, 1]


@pytest.mark.parametrize("retry", [False, True])
def test_rollback_removes_new_file_restores_overwrite_and_preserves_untouched(mirror, retry):
    _, cloud, config, store, gate = mirror
    token = CancellationToken()
    events = []
    accepted = []
    def observe(event):
        events.append(event)
        if event.phase == "apply_chats" and event.completed == 2:
            accepted.extend([token.request_cancel(), token.request_rollback()])
            (cloud / "c").write_text("arrived from another machine")

    real_require = gate.require
    def check(operation, *, final=False):
        if retry and operation.value == "restore" and final:
            raise SafetyPreconditionError("Codex started")
        return real_require(operation, final=final)

    with patch.object(gate, "require", side_effect=check), pytest.raises(
        SafetyPreconditionError if retry else OperationRolledBack
    ):
        run_sync(build_context(config), False, cancellation=token, event_progress=observe)
    assert accepted == [False, True]
    if retry:
        [journal] = store.non_terminal()
        assert journal.state == JournalState.RECOVERY_REQUIRED
        assert events[-1].status == "recovery_required"
        rollback_operation(config, journal.operation_id, dry_run=False)
    else:
        assert events[-1].status == "rolled_back"
    assert not (cloud / "a").exists()
    assert (cloud / "b").read_text() == "old"
    assert (cloud / "c").read_text() == "arrived from another machine"
    assert store.non_terminal() == []
    assert any(json.loads(p.read_text())["family"] == "restore" for p in store.root.glob("*.json"))


def test_partial_rollback_retry_leaves_already_restored_files_alone(mirror):
    _, cloud, config, store, gate = mirror
    token = CancellationToken()
    def stop(event):
        if event.phase == "apply_chats" and event.completed == 3:
            token.request_rollback()
    real_require = gate.require
    finals = []
    def check(operation, *, final=False):
        if operation.value == "restore" and final:
            finals.append(True)
            if len(finals) == 3:
                raise SafetyPreconditionError("Codex started during rollback")
        return real_require(operation, final=final)
    with patch.object(gate, "require", side_effect=check), pytest.raises(SafetyPreconditionError):
        run_sync(build_context(config), False, cancellation=token, event_progress=stop)
    pending = store.non_terminal()
    assert {j.family for j in pending} == {"sync", "restore"}
    original = next(j for j in pending if j.family == "sync")
    assert (cloud / "b").read_text() == "old"
    restored = (cloud / "b").stat()
    rollback_operation(config, original.operation_id, dry_run=False)
    assert store.non_terminal() == []
    assert restored.st_ino == (cloud / "b").stat().st_ino
    assert (cloud / "c").read_text() == "old"
    assert not (cloud / "a").exists()


def test_changed_config_cannot_redirect_a_deletion_rollback_to_another_root(mirror):
    _, cloud, config, store, gate = mirror
    ctx = build_context(config)
    ctx.plan.to_cloud.clear()
    ctx.plan.deletions = [DeleteAction(cloud / "b", "data/b", "cloud")]
    token = CancellationToken()
    def stop(event):
        if event.phase == "delete_chat_files" and event.completed == 1:
            token.request_rollback()
    real_require = gate.require
    def check(operation, *, final=False):
        if operation.value == "recover_rollback":
            raise SafetyPreconditionError("Codex started")
        return real_require(operation, final=final)
    with patch.object(gate, "require", side_effect=check), pytest.raises(SafetyPreconditionError):
        run_sync(ctx, False, cancellation=token, event_progress=stop)
    [journal] = store.non_terminal()
    new_cloud = config.parent / "other-cloud"
    new_cloud.mkdir()
    config.write_text(config.read_text().replace(cloud.parent.as_posix(), new_cloud.as_posix()))
    with pytest.raises(FailSafeError):
        rollback_operation(config, journal.operation_id, dry_run=False)
    assert not (new_cloud / "data/b").exists()
    assert store.non_terminal() == [journal]


def test_stop_during_plan_fingerprint_does_not_open_other_sources(mirror):
    _, _, config, store, _ = mirror
    from pathlib import Path
    ctx = build_context(config)
    token = CancellationToken()
    paths = {a.src for a in ctx.plan.to_cloud}
    reads = []
    real_open = Path.open
    def intercept(path, *args, **kwargs):
        if path in paths:
            reads.append(path)
            token.request_cancel()
        return real_open(path, *args, **kwargs)
    with patch.object(Path, "open", intercept), pytest.raises(OperationCancelled):
        run_sync(ctx, False, cancellation=token)
    assert len(reads) == 1
    assert not store.root.exists()


def test_rollback_requested_during_replace_keeps_its_scope_if_verification_fails(mirror):
    _, cloud, config, store, _ = mirror
    token = CancellationToken()
    from codexsync import sync_engine
    real_replace = os.replace
    real_hash = sync_engine._sha256_file
    requested = []
    def replace_file(src, dst):
        if dst == cloud / "a":
            requested.append(token.request_rollback())
        return real_replace(src, dst)
    def hash_file(path):
        if path == cloud / "a" and requested:
            raise OSError("read error after replace")
        return real_hash(path)
    with patch("os.replace", replace_file), patch.object(sync_engine, "_sha256_file", hash_file), pytest.raises(OSError):
        run_sync(build_context(config), False, cancellation=token)
    assert requested == [True]
    assert (cloud / "a").read_text() == "new a"
    (cloud / "b").write_text("independent edit")
    [original] = store.non_terminal()
    rollback_operation(config, original.operation_id, dry_run=False)
    assert not (cloud / "a").exists()
    assert (cloud / "b").read_text() == "independent edit"


def test_stop_during_staging_removes_empty_directories_created_only_for_staging(mirror):
    local, cloud, config, _, _ = mirror
    (local / "new/nested").mkdir(parents=True)
    (local / "new/nested/file").write_text("payload")
    (local / "shared").mkdir()
    (local / "shared/file").write_text("payload")
    token = CancellationToken()
    def stop(event):
        if event.phase == "stage_chats" and event.completed == 1:
            (cloud / "shared/peer").write_text("foreign arrival")
            token.request_cancel()
    with pytest.raises(OperationCancelled):
        run_sync(build_context(config), False, cancellation=token, event_progress=stop)
    assert not (cloud / "new").exists()
    assert (cloud / "shared/peer").read_text() == "foreign arrival"
    assert not (cloud / "shared/file").exists()


def test_stop_while_backup_is_copying_does_not_start_the_next_backup(mirror):
    _, cloud, config, store, _ = mirror
    token = CancellationToken()
    started = []
    real = BackupManager.backup_file
    def backup(manager, file, relative, *, side):
        started.append(relative)
        token.request_cancel()
        return real(manager, file, relative, side=side)
    with patch.object(BackupManager, "backup_file", backup), pytest.raises(OperationCancelled):
        run_sync(build_context(config), False, cancellation=token)
    assert started == ["data/b"]
    assert not (cloud / "a").exists()
    assert (cloud / "b").read_text() == "old"
    assert store.non_terminal() == []


def test_retry_after_partial_commit_replans_and_leaves_installed_inode_untouched(mirror):
    local, cloud, config, store, gate = mirror
    token = CancellationToken()
    real_require = gate.require
    def check(operation, *, final=False):
        if final and (cloud / "a").exists():
            raise SafetyPreconditionError("Codex started after first replace")
        return real_require(operation, final=final)
    with patch.object(gate, "require", side_effect=check), pytest.raises(SafetyPreconditionError):
        run_sync(build_context(config), False, cancellation=token)
    [pending] = store.non_terminal()
    first = (cloud / "a").stat().st_ino
    resume_operation(config, pending.operation_id, dry_run=False)
    (local / "c").write_text("edited before retry")
    ctx = build_context(config)
    assert [a.relative_path for a in ctx.plan.to_cloud] == ["data/b", "data/c"]
    run_sync(ctx, False, cancellation=CancellationToken())
    assert (cloud / "a").stat().st_ino == first
    assert (cloud / "c").read_text() == "edited before retry"
