from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import stat
import struct
import time
import uuid
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import BinaryIO

from .backup import BackupManager
from .cancellation import CancellationToken, OperationCancelled, RollbackRequested
from .mutation_journal import AppliedFile
from .exceptions import FailSafeError
from .fs_replace import replace_with_retry
from .jsonl_codec import CONTAINER_DECODE_ERRORS, JSONL_READ_ERRORS, JsonlCodec, codec_of, open_jsonl, transcode
from .models import CopyAction, DeleteAction, DestinationPrecondition, SyncPlan
from .progress_log import ProgressLog
from .work_scheduler import WorkScheduler

LOG = logging.getLogger(__name__)

#: Marks a payload staged beside its destination. Greppable on purpose: this is
#: what a user finds in `.codex` after a run that was killed mid-staging.
STAGE_SUFFIX = ".codexsync.tmp"
#: Staging directories `restore` (and older versions of this module) create.
STAGE_DIR_PREFIXES = (".codexsync-stage-", ".codexsync-restore-")
_ORPHAN_STAGE_AGE_SECONDS = 3600.0
#: ``(st_dev, st_ino, st_size, st_mtime_ns)`` of a destination: a replacement
#: changes its file identity, a write in place the last two. ``None`` -- absent.
_Identity = tuple[int, int, int, int] | None
_StageStamp = tuple[int, int, int, int, int]


@dataclass(slots=True)
class _StageReservation:
    # Set by the worker from the handle it created. Only the caller reads
    # this after the workers have joined.
    stamp: _StageStamp | None = None


@dataclass(frozen=True)
class _PreparedCopy:
    action: CopyAction
    path: Path
    source_identity: _Identity
    stage_identity: _Identity
    sha256: str
    content_sha256: str | None
    stage_stamp: _StageStamp


def _stage_path(dst: Path, token: str) -> Path:
    """Where the payload for ``dst`` is prepared: beside it, never over it."""
    return dst.with_name(f".{dst.name}.{token}{STAGE_SUFFIX}")


def _older_than(path: Path, seconds: float, now: float) -> bool:
    """How long ago this item appeared, never how old its content is.

    A staged payload carries the *source's* mtime — `shutil.copy2` copies it —
    so an hour-old file staged a second ago would read as an hour old and a
    concurrent run could delete it mid-flight. `st_ctime` is the creation time
    on Windows and the last metadata change elsewhere; both are the copy.
    """
    try:
        stat = path.stat()
    except OSError:
        return False
    return (now - max(stat.st_ctime, stat.st_mtime)) > seconds


class SyncEngine:
    def __init__(
        self,
        backup_manager: BackupManager,
        temp_dir: Path,
        backup_before_overwrite: bool = True,
        fail_on_unknown: bool = True,
        pre_commit_check: Callable[[], None] | None = None,
        before_replace_check: Callable[[], None] | None = None,
        after_backup: Callable[[], None] | None = None,
        after_success: Callable[[], None] | None = None,
        now: Callable[[], float] = time.time,
        before_stage_check: Callable[[], None] | None = None,
        progress: Callable[[int, int], None] | None = None,
        phase_progress: Callable[[str, int, int], None] | None = None,
        scheduler: WorkScheduler | None = None,
        stage_operation_id: str | None = None,
        progress_events: ProgressLog | None = None,
        cancellation: CancellationToken | None = None,
        verify_destination_after_replace: bool = True,
    ) -> None:
        self._backup_manager = backup_manager
        self._temp_dir = temp_dir
        self._backup_before_overwrite = backup_before_overwrite
        self._fail_on_unknown = fail_on_unknown
        self._pre_commit_check = pre_commit_check
        self._before_replace_check = before_replace_check
        self._after_backup = after_backup
        self._after_success = after_success
        self._now = now
        #: Called once before the first staging file is written. Staging puts
        #: a sibling beside each destination -- inside `.codex` for a copy to
        #: the local side -- and the plan may be minutes old after hashing.
        self._before_stage_check = before_stage_check
        #: ``(done, total)`` over the files this run copies and deletes, so a
        #: window can say "file 3 of 10" while it writes (CS-339). It only
        #: reports: whatever it raises is dropped, and it decides nothing.
        self._progress = progress
        self._phase_progress = phase_progress
        # Parallel preparation uses the same verified stage for raw copies and
        # session containers. Restore can use the serial path.
        self._scheduler = scheduler
        self._stage_operation_id = stage_operation_id
        self._progress_events = progress_events
        self._cancellation = cancellation
        self._verify_destination_after_replace = verify_destination_after_replace
        self.applied_files: list[AppliedFile] = []
        self._event_phase = "stage_chats"
        self._event_done = 0
        self._event_total: int | None = None
        self._event_bytes = 0
        self._event_bytes_total: int | None = None
        self._event_action: CopyAction | DeleteAction | None = None
        self._event_local: set[int] = set()
        self.failure_item: dict[str, object] = {}
        self.staged_count = 0
        self.applied_count = 0
        self.deleted_count = 0
        self._replacement_completed = False

    def execute(self, plan: SyncPlan, dry_run: bool = True) -> None:
        self._event_local = {id(action) for action in plan.to_local}
        try:
            if self._scheduler is not None and not dry_run:
                with self._scheduler.mutation() as work:
                    self._execute(plan, dry_run, prepare=work.map_prepare)
            else:
                self._execute(plan, dry_run)
        except (OperationCancelled, RollbackRequested):
            raise
        except Exception:
            self._emit_event("failed", self._event_action)
            raise

    def _execute(
        self, plan: SyncPlan, dry_run: bool, *,
        prepare: Callable[..., list[_PreparedCopy]] | None = None,
    ) -> None:
        actions = [*plan.to_local, *plan.to_cloud]
        self._checkpoint()
        deletions = list(plan.deletions)
        for action in actions:
            LOG.info("copy %s -> %s", action.src, action.dst)
        for deletion in deletions:
            # Its own line, at its own level: a deletion is the one action that
            # cannot be undone by running the sync again (`AI_RULES` 6).
            LOG.warning("delete %s (%s side)", deletion.path, deletion.side)
        if dry_run:
            return
        if not self._backup_before_overwrite:
            raise FailSafeError("backup_before_overwrite=false is not permitted for apply")

        if self._scheduler is None:
            self._cleanup_orphaned_temp_files()
        # Parallel preparation cleans up its own reservations. A stage left by
        # another process has no live reservation we can use to prove ownership.
        if actions and self._before_stage_check is not None:
            self._before_stage_check()
        token = uuid.uuid4().hex[:8]
        swept: set[Path] = set()
        staged: list[tuple[CopyAction, Path]] = []
        # Every path this run may have created, including the one a failure
        # interrupted: that half-written payload is this module's to remove too.
        stage_paths: list[Path] = []
        reservations: dict[Path, _StageReservation] = {}
        stage_dirs: dict[Path, _Identity] = {}
        prepared: dict[int, _PreparedCopy] = {}
        try:
            # Stage and verify *every* payload before backing up or replacing
            # the first destination, each one in the folder its destination
            # lives in. `os.replace` is atomic only within one filesystem, and
            # a staging directory is not on it: `paths.temp_dir` sits beside the
            # cloud copy, and `.codex` is routinely on another drive, which made
            # every write into it fail with `WinError 17` (CS-253). A sibling is
            # on the destination's volume by construction, and it is never the
            # destination itself — nothing is overwritten until the whole set is
            # staged and proven. `app.commit_global_state` and `project_move`
            # already stage this way.
            total = len(actions) + len(deletions)
            self._report(0, total)
            self._report_phase("stage_chats", 0, len(actions))
            self._begin_event_phase("stage_chats", len(actions))
            sizes: dict[int, int] = {}
            if self._scheduler is not None:
                operation = self._stage_operation_id or uuid.uuid4().hex
                inputs = []
                # Prepare the directories on the caller. Each worker opens
                # its sibling exclusively and keeps that handle while writing.
                for index, action in enumerate(actions, start=1):
                    self._checkpoint()
                    self._event_action = action
                    self.failure_item = {"phase": "stage_chats", "index": index, "relative_path": action.relative_path}
                    self._ensure_stage_parent(action.dst.parent, stage_dirs)
                    # Keep the existing short suffix on long Codex session names.
                    path = _stage_path(action.dst, operation[:8] if action.codec is not None else operation)
                    if path in reservations:
                        raise FileExistsError(path)
                    reservations[path] = _StageReservation()
                    inputs.append((action, path, reservations[path]))
                # Treat every payload as large: the shared budget bounds
                # whole copy/hash jobs without a racy size classification.
                assert prepare is not None
                self._event_action = None
                self.failure_item = {"phase": "stage_chats"}

                def staged_result(index: int, _input, result: _PreparedCopy) -> None:
                    size = result.stage_identity[2] if result.stage_identity is not None else None
                    if size is not None:
                        sizes[id(result.action)] = size
                    self.staged_count = index + 1
                    self._report(self.staged_count, total)
                    self._report_phase("stage_chats", self.staged_count, len(actions))
                    self._count_event(index + 1, "pending", result.action, size)

                try:
                    results = prepare(
                        self._prepare_copy, inputs, large_file=lambda _item: True, on_result=staged_result,
                        cancellation=self._cancellation,
                    )
                    # Re-prove the entire set after joining, before any backup or target write.
                    results = prepare(self._verify_prepared, results, large_file=lambda _item: True,
                                      cancellation=self._cancellation)
                except Exception as exc:
                    action = getattr(exc, "staged_copy_action", None)
                    if action is not None:
                        self._event_action = action
                        self.failure_item.update(index=actions.index(action) + 1, relative_path=action.relative_path)
                    raise
                for result in results:
                    self._checkpoint()
                    prepared[id(result.action)] = result
                    staged.append((result.action, result.path))
                for result in results:
                    self._checkpoint()
                    self._event_action = result.action
                    self._require_prepared(result)
            else:
                for index, action in enumerate(actions, start=1):
                    self._event_action = action
                    self.failure_item = {"phase": "stage_chats", "index": index, "relative_path": action.relative_path}
                    self._ensure_parent(action.dst)
                    self._sweep_stale_stage_files(action.dst.parent, swept)
                    staged_path = _stage_path(action.dst, token)
                    stage_paths.append(staged_path)
                    self._stage_verified(
                        action.src, staged_path, action.codec, action.expected_sha256,
                    )
                    staged.append((action, staged_path))
                    self.staged_count = len(staged)
                    self._report(len(staged), total)
                    self._report_phase("stage_chats", len(staged), len(actions))
                    if self._progress_events is not None:
                        try:
                            sizes[id(action)] = staged_path.stat().st_size
                        except OSError:
                            pass
                    self._count_event(index, "pending", action, sizes.get(id(action)))

            # A complete backup set exists before any destination mutation.
            # Each entry names its side, so a rollback puts it back into the
            # root it came from (CS-293).
            to_local = {id(action) for action in plan.to_local}
            # What each destination was when it was backed up. A file that is
            # no longer that file at its replace is not what the backup holds
            # (CS-493): a cloud client delivering another machine's edit after
            # the backup phase had that edit overwritten, kept nowhere.
            backed_up: dict[int, _Identity] = {}
            self.failure_item = {}
            self._begin_event_phase("backup_chats", total)
            for index, (action, _) in enumerate(staged, start=1):
                self._checkpoint()
                self._event_action = action
                self.failure_item = {"phase": "backup_chats", "index": index, "relative_path": action.relative_path}
                before = _require_planned_destination(
                    action.dst, action.relative_path, action.destination_precondition,
                )
                if before is not None:
                    _require_destination_streams(action.dst, before)
                    backup_path = self._backup_manager.backup_file(
                        action.dst, action.relative_path,
                        side="local" if id(action) in to_local else "cloud",
                    )
                    if backup_path is None:
                        raise FailSafeError("Failed to create required backup before mutation")
                    backed_up[id(action)] = _same_while_backed_up(action.dst, action.relative_path, before)
                identity = backed_up.get(id(action))
                self._count_event(index, "pending", action, identity[2] if identity else None)
            deleted_as: dict[int, _Identity] = {}
            for index, deletion in enumerate(deletions, start=1):
                self._checkpoint()
                self._event_action = deletion
                self.failure_item = {"phase": "backup_chats", "index": index, "relative_path": deletion.relative_path}
                before = _require_planned_destination(
                    deletion.path, deletion.relative_path, deletion.destination_precondition,
                )
                # A file that vanished between the plan and now is not a file
                # this run may delete: the plan described something else.
                if before is not None:
                    _require_destination_streams(deletion.path, before)
                backup_path = self._backup_manager.backup_file(
                    deletion.path, deletion.relative_path, side=deletion.side
                )
                if backup_path is None:
                    raise FailSafeError(
                        f"Cannot delete {deletion.relative_path}: it could not be backed up"
                    )
                deleted_as[id(deletion)] = _same_while_backed_up(deletion.path, deletion.relative_path, before)
                LOG.warning("backup before delete %s -> %s", deletion.path, backup_path)
                self._count_event(len(actions) + index, "pending", deletion, before[2] if before else None)

            self._event_action = None
            self._checkpoint()
            self._backup_manager.finalize()
            self.failure_item = {}

            if self._after_backup is not None:
                self._after_backup()

            if self._pre_commit_check is not None:
                self._checkpoint()
                self._pre_commit_check()
            self._report_phase("apply_chats", 0, len(actions))
            self._begin_event_phase(
                "apply_chats", len(actions), sum(sizes.values()) if len(sizes) == len(actions) else None,
            )
            for index, (action, staged_path) in enumerate(staged, start=1):
                self._checkpoint()
                self._event_action = action
                self.failure_item = {"phase": "apply_chats", "index": index, "relative_path": action.relative_path}
                if self._scheduler is None and self._before_replace_check is not None:
                    self._before_replace_check()

                def unchanged(action: CopyAction = action) -> None:
                    if id(action) in prepared:
                        self._require_prepared(prepared[id(action)])
                    expected = backed_up.get(id(action))
                    if expected is None:
                        if os.path.lexists(action.dst):
                            # A cloud client delivered a file after the backup
                            # phase: it has no backup, so overwriting it would
                            # lose it (CS-463).
                            raise FailSafeError(
                                f"{action.relative_path} appeared after the backup was taken; "
                                "it was not overwritten; run the sync again", code="DESTINATION_APPEARED"
                            )
                    elif _identity(action.dst) != expected:
                        raise FailSafeError(
                            f"{action.relative_path} changed after the backup was taken; "
                            "it was not overwritten; run the sync again",
                            code="STATE_CHANGED_RETRY",
                        )
                    else:
                        _require_destination_streams(action.dst, expected)

                if self._scheduler is None:
                    unchanged()
                self._replacement_completed = False
                try:
                    if self._scheduler is not None and self._verify_destination_after_replace:
                        self._replace_staged(action, staged_path, unchanged, prepared[id(action)].sha256)
                    else:
                        self._replace_staged(action, staged_path, unchanged)
                except Exception:
                    if self._replacement_completed:
                        self.applied_count = index
                        self._report_phase("apply_chats", index, len(actions))
                        self._event_done = index
                        self._event_bytes += sizes.get(id(action), 0)
                    raise
                finally:
                    if self._replacement_completed and self._cancellation is not None:
                        result = prepared[id(action)]
                        self.applied_files.append(AppliedFile(
                            action.relative_path, "local" if id(action) in to_local else "cloud",
                            id(action) in backed_up, result.sha256, result.stage_identity,
                        ))
                self.applied_count = index
                self._report_phase("apply_chats", index, len(actions))
                self._count_event(index, "applied", action, sizes.get(id(action)))
            self._report_phase("delete_chat_files", 0, len(deletions))
            self._begin_event_phase("delete_chat_files", len(deletions))
            for index, deletion in enumerate(deletions, start=1):
                self._checkpoint()
                self._event_action = deletion
                self.failure_item = {"phase": "delete_chat_files", "index": index, "relative_path": deletion.relative_path}
                if self._before_replace_check is not None:
                    self._before_replace_check()
                current = _identity(deletion.path)
                if current is not None and current != deleted_as[id(deletion)]:
                    raise FailSafeError(
                        f"{deletion.relative_path} changed after the backup was taken; "
                        "it was not deleted; run the sync again",
                        code="STATE_CHANGED_RETRY",
                    )
                self._replacement_completed = False
                try:
                    if current is not None:
                        _require_destination_streams(deletion.path, current)
                        self._remove_backed_up(deletion)
                finally:
                    if self._replacement_completed and self._cancellation is not None:
                        self.applied_files.append(AppliedFile(deletion.relative_path, deletion.side, True, None, None))
                self.deleted_count = index
                self._report(len(actions) + index, total)
                self._report_phase("delete_chat_files", index, len(deletions))
                identity = deleted_as[id(deletion)]
                self._count_event(index, "applied", deletion, identity[2] if identity else None)
            self.failure_item = {}
            self._event_action = None
            self._checkpoint()
            if self._after_success is not None:
                self._after_success()
        finally:
            # map_prepare joins active workers before raising. Cleanup occurs
            # only here, on the caller, and only for identities we reserved.
            for path, reservation in reservations.items():
                try:
                    if os.name == "nt" and reservation.stamp is not None:
                        _discard_windows_stage(path, reservation.stamp)
                except OSError:
                    # A cloud lock on cleanup must not hide the worker's
                    # failure or turn a completed replacement into a failure.
                    LOG.warning("could not remove owned staging file %s", path, exc_info=True)
            # POSIX pathname unlink cannot be conditioned on the file we
            # proved. Keep unmatched stages rather than remove a replacement.
            for staged_path in stage_paths:
                # Already gone for every action that was replaced into place.
                staged_path.unlink(missing_ok=True)
            if not self.applied_count and not self.deleted_count:
                for path, identity in reversed(stage_dirs.items()):
                    try:
                        current = _identity(path)
                        if current is not None and identity and identity[1] and current[:2] == identity[:2]:
                            path.rmdir()  # atomic refusal if a peer populated it
                    except OSError:
                        LOG.debug("could not remove empty staging directory %s", path, exc_info=True)

    @classmethod
    def _ensure_stage_parent(cls, parent: Path, created: dict[Path, _Identity]) -> None:
        if parent.is_dir():
            return
        if parent == parent.parent:
            raise FileNotFoundError(f"Staging volume is unavailable: {parent}")
        cls._ensure_stage_parent(parent.parent, created)
        try:
            parent.mkdir()
        except FileExistsError:
            if not parent.is_dir():
                raise
        else:
            created[parent] = _identity(parent)

    def _checkpoint(self) -> None:
        if self._cancellation is not None:
            self._cancellation.checkpoint()

    def _begin_event_phase(self, phase: str, total: int, bytes_total: int | None = None) -> None:
        self._event_phase, self._event_done, self._event_total = phase, 0, total
        self._event_bytes, self._event_bytes_total = 0, bytes_total
        self._event_action = None
        self._emit_event("running")

    def _count_event(self, done: int, status: str, action: CopyAction | DeleteAction, size: int | None) -> None:
        self._event_done = done
        self._event_bytes += size or 0
        self._emit_event(status, action, size)

    def _emit_event(
        self, status: str, action: CopyAction | DeleteAction | None = None, size: int | None = None,
    ) -> None:
        if self._progress_events is None:
            return
        direction = None
        if isinstance(action, CopyAction):
            direction = "local" if id(action) in self._event_local else "cloud"
        elif isinstance(action, DeleteAction):
            direction = action.side
        self._progress_events.report(
            self._event_phase, self._event_done, self._event_total, status=status,
            bytes_completed=self._event_bytes, bytes_total=self._event_bytes_total, item_bytes=size,
            relative_path=action.relative_path if action else None,
            item_id=action.session_hash if action else None, direction=direction,
        )

    def _prepare_copy(self, item: tuple[CopyAction, Path, _StageReservation]) -> _PreparedCopy:
        action, path, reservation = item
        try:
            before = _identity(action.src)
            # Never open a pre-existing pathname for writing, even if an inode
            # number can be recycled between opens.
            with path.open("xb+") as stage_file:
                if _stage_stamp(os.fstat(stage_file.fileno())) is None:
                    raise FailSafeError("Staging file identity is unavailable", code="STATE_CHANGED_RETRY")
                try:
                    content_sha256 = self._stage_verified(
                        action.src, path, action.codec, action.expected_sha256, stage_file,
                    )
                    stage_file.seek(0)
                    raw_sha256 = _sha256_reader(stage_file)
                finally:
                    stage_file.flush()
                    reservation.stamp = _stage_stamp(os.fstat(stage_file.fileno()))
            stamp = reservation.stamp
            self._require_reservation(path, stamp)
            if before is None or _identity(action.src) != before:
                raise FailSafeError("Source changed while preparing the mutation stage", code="STATE_CHANGED_RETRY")
            assert stamp is not None  # the reservation check refuses an unknown identity
            return _PreparedCopy(action, path, before, stamp[:4], raw_sha256, content_sha256, stamp)
        except Exception as exc:
            exc.staged_copy_action = action
            raise

    @staticmethod
    def _require_reservation(path: Path, reserved: _StageStamp | None) -> None:
        if reserved is None or _stage_stamp_at(path) != reserved:
            raise FailSafeError("Staging reservation changed during preparation", code="STATE_CHANGED_RETRY")

    @staticmethod
    def _require_prepared(prepared: _PreparedCopy) -> None:
        # Once content verification is complete, use the existing filesystem
        # precondition model at commit: identity, size and mtime. No series of
        # reads can exclude an undetectable same-identity/size/mtime edit after
        # its final verification; stronger exclusion requires an OS lease.
        if (
            prepared.source_identity is None
            or _identity(prepared.action.src) != prepared.source_identity
            or _stage_stamp_at(prepared.path) != prepared.stage_stamp
        ):
            raise FailSafeError("Source or staging file changed before commit", code="STATE_CHANGED_RETRY")

    @classmethod
    def _verify_prepared(cls, prepared: _PreparedCopy) -> _PreparedCopy:
        action = prepared.action
        try:
            cls._require_prepared(prepared)
            stage_sha256 = _sha256_file(prepared.path)
            source_codec = codec_of(action.src) or JsonlCodec.NONE
            if action.codec is None or (source_codec is action.codec and action.expected_sha256 is None):
                if os.name == "nt":
                    with action.src.open("rb") as reader:
                        _require_unnamed_windows_data_stream(reader)
                source_sha256 = _sha256_file(action.src)
                content_unchanged = source_sha256 == prepared.sha256
            else:
                # The encoded bytes need not equal the source's. Re-prove the
                # decoded bytes against the initial stage before trusting either.
                source_sha256 = _sha256_stream(action.src, source_codec)
                content_sha256 = (
                    stage_sha256 if action.codec is JsonlCodec.NONE else _sha256_stream(prepared.path, action.codec)
                )
                content_unchanged = source_sha256 == content_sha256 == prepared.content_sha256
            if stage_sha256 != prepared.sha256 or not content_unchanged:
                raise FailSafeError("Source or staging file changed before commit", code="STATE_CHANGED_RETRY")
            cls._require_prepared(prepared)
            return prepared
        except CONTAINER_DECODE_ERRORS as exc:
            failure = FailSafeError("Source or staging container changed before commit", code="STATE_CHANGED_RETRY")
            failure.staged_copy_action = action
            raise failure from exc
        except Exception as exc:
            exc.staged_copy_action = action
            raise

    def _report(self, done: int, total: int) -> None:
        if self._progress is None:
            return
        try:
            self._progress(done, total)
        except Exception:  # noqa: BLE001 - a broken indicator must not break a write
            LOG.debug("progress callback failed", exc_info=True)

    def _report_phase(self, phase: str, done: int, total: int) -> None:
        if self._phase_progress is not None:
            try:
                self._phase_progress(phase, done, total)
            except Exception:  # noqa: BLE001 - progress cannot decide a mutation
                LOG.debug("phase progress callback failed", exc_info=True)

    def _remove_backed_up(self, deletion: DeleteAction) -> None:
        """Remove one file whose backup is already committed and verified.

        The backup was written and hashed in the backup phase above, before any
        destination was touched, so by the time this runs the copy that
        `recover --rollback` would restore from already exists.
        """
        try:
            deletion.path.unlink()
            self._replacement_completed = True
        except FileNotFoundError:
            # Someone else removed it while this ran. The end state is the one
            # the plan asked for, and the backup still holds the content.
            LOG.warning("delete %s: already gone", deletion.path)
            return
        except OSError as exc:
            raise FailSafeError(f"Could not delete {deletion.relative_path}: {exc}") from exc
        if deletion.path.exists():
            raise FailSafeError(f"Deletion of {deletion.relative_path} did not take effect")
        LOG.warning("deleted %s", deletion.path)

    def _replace_staged(
        self, action: CopyAction, staged: Path, unchanged: Callable[[], None] | None = None,
        expected_hash: str | None = None,
    ) -> None:
        self._ensure_parent(action.dst)
        if expected_hash is None and self._verify_destination_after_replace:
            expected_hash = _sha256_file(staged)
        try:
            self._replace_with_retry(staged, action.dst, unchanged)
        except OSError:
            if self._fail_on_unknown:
                LOG.exception("atomic replace failed for staged file %s -> %s", staged, action.dst)
                raise
            try:
                shutil.copy2(staged, action.dst)
            except OSError:
                LOG.exception("fallback copy failed for staged file %s -> %s", staged, action.dst)
                raise
        self._replacement_completed = True
        if self._verify_destination_after_replace and _sha256_file(action.dst) != expected_hash:
            raise FailSafeError("Destination verification failed after atomic replace", code="DESTINATION_VERIFY_FAILED")

    def _replace_with_retry(
        self, staged: Path, dst: Path, unchanged: Callable[[], None] | None = None,
    ) -> None:
        """Atomically replace, retrying while another process holds the target.

        Destinations live where a cloud client, search indexer or antivirus is
        free to open a file at any moment; on Windows that turns ``os.replace``
        into ``WinError 5``/``32`` for as long as the handle lives, typically
        milliseconds.  Failing the whole mutation on one of those would strand
        the user in ``RECOVERY_REQUIRED`` for a condition that already cleared.

        The retry weakens nothing.  Every attempt is the same atomic replace,
        the destination hash is still verified afterwards, and the process
        safety check is re-proved before each further attempt, so a Codex that
        starts during the wait still stops the commit.  Errors that are not a
        transient lock are raised on the first attempt.  ``unchanged``
        re-proves before each further attempt that the destination is still
        the file the backup holds.
        """
        checks = [check for check in (
            (unchanged, self._before_replace_check, unchanged) if self._scheduler is not None
            else (self._before_replace_check, unchanged)
        ) if check is not None]

        def before_retry() -> None:
            for check in checks:
                check()

        # Check identity again after the process gate. A peer can deliver a
        # destination while the gate is reading process state.
        if self._scheduler is not None:
            before_retry()
        replace_with_retry(staged, dst, before_retry=before_retry if checks else None)

    @staticmethod
    def _ensure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _stage_verified(
        source: Path,
        staged: Path,
        codec: JsonlCodec | None = None,
        expected_sha256: str | None = None,
        stage_file: BinaryIO | None = None,
    ) -> str:
        """Stage the payload and prove it is the source, byte for byte.

        ``codec`` is the container the *destination* wants, and only a caller
        that knows it is moving a session branch passes one. ``None`` — every
        ordinary file copy — carries the bytes across untouched, whatever the
        file happens to be called. A user file named `notes.jsonl.gz` under an
        included root is a user file, and a sync that unpacked it because of
        its name would leave content that no longer matches the name.

        Where a branch is being moved the source has a container of its own,
        read from its name, and the two need not agree: it goes into the cloud
        mirror compressed and comes back out plain. Where they differ the staged
        file is a container, so what has to equal the source is what it
        decompresses to. Hashing the logical stream on both sides keeps the
        original guarantee exactly: a source that moved mid-copy, and a codec
        that lost a byte, both fail here rather than after a destination has
        been replaced.

        ``expected_sha256`` binds the copy to the plan: it is the logical hash
        the confirmed plan recorded for the source. A transfer plan is rebuilt
        and compared before the lock is taken, and a cloud client may deliver a
        newer branch into the mirror after that; "did not change while being
        copied" is then true of the wrong branch. Checked here, before any
        destination is touched, so that window fails safe instead of replacing
        a chat and only noticing afterwards (CS-515).

        A container that cannot be decoded (truncated by a cloud client,
        damaged) is a changed source, not an internal error (CS-518). Return
        the verified content hash so parallel preparation can recheck it.
        """
        def staged_digest(container: JsonlCodec | None) -> str:
            if stage_file is None:
                return _sha256_file(staged) if container is None else _sha256_stream(staged, container)
            stage_file.flush()
            stage_file.seek(0)
            if container is None or container is JsonlCodec.NONE:
                return _sha256_reader(stage_file)
            with open_jsonl(staged, container, raw=stage_file) as reader:
                return _sha256_reader(reader)

        source_codec = codec_of(source) or JsonlCodec.NONE
        try:
            if codec is None or source_codec is codec:
                # Carried across untouched. A branch is still compared by what
                # it holds, because that is the hash its plan recorded.
                if expected_sha256 is None or codec is None:
                    before = _sha256_file(source)
                else:
                    before = _sha256_stream(source, source_codec)
                if stage_file is None:
                    shutil.copy2(source, staged)
                else:
                    _copy_raw_to_stage(source, stage_file)
                if expected_sha256 is None or codec is None:
                    staged_hash = staged_digest(None)
                    after = _sha256_file(source)
                else:
                    staged_hash = staged_digest(source_codec)
                    after = _sha256_stream(source, source_codec)
            else:
                before = _sha256_stream(source, source_codec)
                if stage_file is not None:
                    stage_file.truncate(0)
                transcode(source, staged if stage_file is None else stage_file, source_codec, codec)
                staged_hash = staged_digest(codec)
                after = _sha256_stream(source, source_codec)
        except CONTAINER_DECODE_ERRORS as exc:
            raise FailSafeError(
                f"{source.name} could not be read as a whole; nothing was written ({exc})",
                code="STATE_CHANGED_RETRY",
            ) from exc
        if before != staged_hash or before != after:
            raise FailSafeError("Source changed while preparing the mutation stage", code="STATE_CHANGED_RETRY")
        if expected_sha256 is not None and before != expected_sha256:
            raise FailSafeError(
                f"{source.name} is no longer the branch the plan was built from; rescan and confirm the new plan",
                code="PLAN_CHANGED",
            )
        return before

    def _cleanup_orphaned_temp_files(self) -> None:
        if not self._temp_dir.exists():
            return
        removed = 0
        for path in self._temp_dir.rglob("*.tmp"):
            # Age-limited like the directories below: a journal of another
            # command family is written as `<id>.tmp` here and replaced a
            # moment later, and removing it mid-write fails that command.
            if not path.is_file() or not _older_than(path, _ORPHAN_STAGE_AGE_SECONDS, self._now()):
                continue
            path.unlink(missing_ok=True)
            removed += 1
        for path in self._temp_dir.iterdir():
            # A staging *directory* an interrupted run left behind — restore
            # extracts a snapshot into one. Only files were swept before, so
            # such a directory stayed for good (one from 2026-09-05 was still
            # there). Age-limited because `sync` and `restore` take different
            # operation locks and can be in flight at the same time.
            if not path.is_dir() or not path.name.startswith(STAGE_DIR_PREFIXES):
                continue
            if not _older_than(path, _ORPHAN_STAGE_AGE_SECONDS, self._now()):
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        if removed:
            LOG.info("removed %d orphaned temporary item(s) in %s", removed, self._temp_dir)

    def _sweep_stale_stage_files(self, directory: Path, swept: set[Path]) -> None:
        """Remove staging siblings a killed run left in a destination folder.

        Only folders this run is about to write into are looked at, and only
        names this module writes, so nothing else can be hit. Age-limited for
        the same reason as above: two machines may be writing into one cloud
        mirror, and one of those files may be in flight over there.
        """
        if directory in swept:
            return
        swept.add(directory)
        try:
            candidates = list(directory.glob(f"*{STAGE_SUFFIX}"))
        except OSError:
            return
        for path in candidates:
            if path.is_file() and _older_than(path, _ORPHAN_STAGE_AGE_SECONDS, self._now()):
                LOG.info("removed orphaned staging file %s", path)
                path.unlink(missing_ok=True)


def _identity(path: Path) -> _Identity:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _stage_stamp(info: os.stat_result) -> _StageStamp | None:
    birth_or_change = getattr(info, "st_birthtime_ns", info.st_ctime_ns)
    if not info.st_ino or not birth_or_change or not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, birth_or_change


def _stage_stamp_at(path: Path) -> _StageStamp | None:
    try:
        return _stage_stamp(os.lstat(path))
    except FileNotFoundError:
        return None


def _require_planned_destination(
    path: Path,
    relative_path: str,
    expected: DestinationPrecondition | None,
) -> _Identity:
    """Return the identity proved by the plan before taking its backup."""
    current = _identity(path)
    if expected is None:
        return current
    if not expected.exists:
        matches = current is None
    elif current is None:
        matches = False
    else:
        device, inode, size, mtime_ns = current
        matches = (
            (expected.size is None or size == expected.size)
            and (expected.mtime_ns is None or mtime_ns == expected.mtime_ns)
            and (expected.device is None or device == expected.device)
            and (expected.inode is None or inode == expected.inode)
        )
        if matches and expected.content_sha256 is not None:
            try:
                codec = codec_of(path) or JsonlCodec.NONE
                matches = _sha256_stream(path, codec) == expected.content_sha256 and _identity(path) == current
            except JSONL_READ_ERRORS:
                matches = False
    if not matches:
        raise FailSafeError(
            f"{relative_path} changed after the sync plan was built; "
            "nothing was replaced; rescan and confirm the new plan",
            code="DESTINATION_CHANGED_SINCE_PLAN",
        )
    return current


def _same_while_backed_up(path: Path, relative_path: str, before: _Identity) -> _Identity:
    """The file's identity after its backup; it must equal the one before."""
    after = _identity(path)
    if after is None or after != before:
        raise FailSafeError(
            f"{relative_path} changed while it was being backed up; nothing was replaced; run the sync again",
            code="STATE_CHANGED_RETRY",
        )
    return after


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_reader(handle)


def _sha256_stream(path: Path, codec: JsonlCodec) -> str:
    """Hash what a staged container decompresses to, never its stored bytes."""
    with open_jsonl(path, codec) as handle:
        return _sha256_reader(handle)


def _sha256_reader(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _copy_raw_to_stage(source: Path, stage_file: BinaryIO) -> None:
    """Copy the payload and preserve metadata on the reserved handle."""
    with source.open("rb") as reader:
        if os.name == "nt":
            _require_unnamed_windows_data_stream(reader)
        stage_file.truncate(0)
        shutil.copyfileobj(reader, stage_file, 1024 * 1024)
        source_stat = os.fstat(reader.fileno())
        stage_file.flush()
        if os.name == "nt":
            _require_unnamed_windows_data_stream(reader)
            _copy_windows_metadata(stage_file, source_stat)
        else:
            descriptor = stage_file.fileno()
            os.utime(descriptor, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            shutil._copyxattr(reader.fileno(), descriptor)
            os.fchmod(descriptor, stat.S_IMODE(source_stat.st_mode))


@cache
def _windows_metadata_api():
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_int64), ("last_access_time", ctypes.c_int64),
            ("last_write_time", ctypes.c_int64), ("change_time", ctypes.c_int64),
            ("file_attributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    return FileBasicInfo, set_info, get_info


@cache
def _windows_cleanup_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return create_file, close_handle


def _discard_windows_stage(path: Path, stamp: _StageStamp) -> None:
    """Delete the verified file through its handle. Never unlink the pathname."""
    import ctypes
    import msvcrt

    create_file, close_handle = _windows_cleanup_api()
    delete_access, read_attributes, share_read = 0x10000, 0x80, 1
    open_existing, open_reparse_point = 3, 0x00200000
    handle = create_file(str(path), delete_access | read_attributes, share_read, None,
                         open_existing, open_reparse_point, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):  # file or parent already gone
            return
        raise ctypes.WinError(error)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close_handle(handle)
        raise
    try:
        info = os.fstat(descriptor)
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or _stage_stamp(info) != stamp:
            return
        _, set_info, _ = _windows_metadata_api()
        delete_on_close = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile
        if not set_info(handle, 4, ctypes.byref(delete_on_close), ctypes.sizeof(delete_on_close)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        os.close(descriptor)


def _require_unnamed_windows_data_stream(source: BinaryIO) -> None:
    """Refuse alternate streams without reading their data."""
    import ctypes
    import msvcrt

    _, _, get_info = _windows_metadata_api()
    # FileStreamInfo returns one or more 8-byte aligned FILE_STREAM_INFO
    # entries. A single default stream fits. An unreadable list fails closed.
    buffer = (ctypes.c_int64 * 64)()
    file_stream_info, error_handle_eof = 7, 38
    if not get_info(msvcrt.get_osfhandle(source.fileno()), file_stream_info, buffer, ctypes.sizeof(buffer)):
        error = ctypes.get_last_error()
        if error == error_handle_eof:
            return
        raise FailSafeError(f"Could not verify alternate data streams (Windows error {error})", code="STATE_CHANGED_RETRY")
    next_entry, name_length, _, _ = struct.unpack_from("<IIqq", buffer)
    name_offset = struct.calcsize("<IIqq")
    default = "::$DATA".encode("utf-16-le")
    if next_entry or name_length != len(default) or bytes(buffer)[name_offset:name_offset + name_length] != default:
        raise FailSafeError("File has an alternate data stream that cannot be preserved safely", code="STATE_CHANGED_RETRY")


def _require_destination_streams(path: Path, expected: _Identity) -> None:
    # ZIP and some supported copy/restore paths only carry the main stream.
    if os.name != "nt":
        return
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == expected:
                _require_unnamed_windows_data_stream(handle)
                return
    except FileNotFoundError:
        pass
    raise FailSafeError("Destination changed while its data streams were checked", code="STATE_CHANGED_RETRY")


def _copy_windows_metadata(stage_file: BinaryIO, source: os.stat_result) -> None:
    import ctypes
    import msvcrt

    info_type, set_info, _ = _windows_metadata_api()
    epoch_ticks = 116444736000000000  # FILETIME's Unix epoch in 100 ns ticks
    dos_attributes = (
        stat.FILE_ATTRIBUTE_ARCHIVE | stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_READONLY
        | stat.FILE_ATTRIBUTE_SYSTEM | stat.FILE_ATTRIBUTE_NOT_CONTENT_INDEXED
    )
    info = info_type(
        0, source.st_atime_ns // 100 + epoch_ticks, source.st_mtime_ns // 100 + epoch_ticks, 0,
        source.st_file_attributes & dos_attributes or stat.FILE_ATTRIBUTE_NORMAL,
    )
    if not set_info(msvcrt.get_osfhandle(stage_file.fileno()), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
