from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
import hashlib
from collections.abc import Callable

from .backup import BackupManager
from .exceptions import FailSafeError
from .jsonl_codec import JsonlCodec, codec_of, open_jsonl, transcode
from .models import CopyAction, DeleteAction, SyncPlan

LOG = logging.getLogger(__name__)

_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.1
# Windows: ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})
_TRANSIENT_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.ETXTBSY})


def _is_transient_lock(exc: OSError) -> bool:
    """True when the target was momentarily held open by another process."""
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERRORS
    return exc.errno in _TRANSIENT_ERRNOS


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
    ) -> None:
        self._backup_manager = backup_manager
        self._temp_dir = temp_dir
        self._backup_before_overwrite = backup_before_overwrite
        self._fail_on_unknown = fail_on_unknown
        self._pre_commit_check = pre_commit_check
        self._before_replace_check = before_replace_check
        self._after_backup = after_backup
        self._after_success = after_success

    def execute(self, plan: SyncPlan, dry_run: bool = True) -> None:
        actions = [*plan.to_local, *plan.to_cloud]
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

        self._cleanup_orphaned_temp_files()
        stage_root = self._temp_dir / f".codexsync-stage-{uuid.uuid4().hex}"
        staged: list[tuple[CopyAction, Path]] = []
        try:
            # Stage and verify *every* payload outside destination state before
            # backing up or replacing the first destination.
            stage_root.mkdir(parents=True, exist_ok=False)
            for index, action in enumerate(actions):
                staged_path = stage_root / f"{index:08d}.payload"
                self._stage_verified(action.src, staged_path, action.codec)
                staged.append((action, staged_path))

            # A complete backup set exists before any destination mutation.
            for action, _ in staged:
                if action.dst.exists():
                    backup_path = self._backup_manager.backup_file(action.dst, action.relative_path)
                    if backup_path is None:
                        raise FailSafeError("Failed to create required backup before mutation")
            for deletion in deletions:
                # A file that vanished between the plan and now is not a file
                # this run may delete: the plan described something else.
                backup_path = self._backup_manager.backup_file(deletion.path, deletion.relative_path)
                if backup_path is None:
                    raise FailSafeError(
                        f"Cannot delete {deletion.relative_path}: it could not be backed up"
                    )
                LOG.warning("backup before delete %s -> %s", deletion.path, backup_path)

            self._backup_manager.finalize()

            if self._after_backup is not None:
                self._after_backup()

            if self._pre_commit_check is not None:
                self._pre_commit_check()
            for action, staged_path in staged:
                if self._before_replace_check is not None:
                    self._before_replace_check()
                self._replace_staged(action, staged_path)
            for deletion in deletions:
                if self._before_replace_check is not None:
                    self._before_replace_check()
                self._remove_backed_up(deletion)
            if self._after_success is not None:
                self._after_success()
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

    def _remove_backed_up(self, deletion: DeleteAction) -> None:
        """Remove one file whose backup is already committed and verified.

        The backup was written and hashed in the backup phase above, before any
        destination was touched, so by the time this runs the copy that
        `recover --rollback` would restore from already exists.
        """
        try:
            deletion.path.unlink()
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

    def _replace_staged(self, action: CopyAction, staged: Path) -> None:
        self._ensure_parent(action.dst)
        expected_hash = _sha256_file(staged)
        try:
            self._replace_with_retry(staged, action.dst)
        except OSError:
            if self._fail_on_unknown:
                LOG.exception("atomic replace failed for staged file %s -> %s", staged, action.dst)
                raise
            try:
                shutil.copy2(staged, action.dst)
            except OSError:
                LOG.exception("fallback copy failed for staged file %s -> %s", staged, action.dst)
                raise
        if _sha256_file(action.dst) != expected_hash:
            raise FailSafeError("Destination verification failed after atomic replace")

    def _replace_with_retry(self, staged: Path, dst: Path) -> None:
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
        transient lock are raised on the first attempt.
        """
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(staged, dst)
                return
            except OSError as exc:
                if attempt == _REPLACE_ATTEMPTS - 1 or not _is_transient_lock(exc):
                    raise
                LOG.warning(
                    "atomic replace blocked by another process (%s); retry %d of %d for %s",
                    exc, attempt + 1, _REPLACE_ATTEMPTS - 1, dst,
                )
                time.sleep(_REPLACE_BACKOFF_SECONDS * (2 ** attempt))
                if self._before_replace_check is not None:
                    self._before_replace_check()

    @staticmethod
    def _ensure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _stage_verified(source: Path, staged: Path, codec: JsonlCodec | None = None) -> None:
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
        """
        source_codec = codec_of(source) or JsonlCodec.NONE
        if codec is None or source_codec is codec:
            before = _sha256_file(source)
            shutil.copy2(source, staged)
            staged_hash = _sha256_file(staged)
            after = _sha256_file(source)
        else:
            before = _sha256_stream(source, source_codec)
            transcode(source, staged, source_codec, codec)
            staged_hash = _sha256_stream(staged, codec)
            after = _sha256_stream(source, source_codec)
        if before != staged_hash or before != after:
            raise FailSafeError("Source changed while preparing the mutation stage")

    def _cleanup_orphaned_temp_files(self) -> None:
        if not self._temp_dir.exists():
            return
        removed = 0
        for path in self._temp_dir.rglob("*.tmp"):
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed += 1
        if removed:
            LOG.info("removed %d orphaned temporary file(s) in %s", removed, self._temp_dir)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(path: Path, codec: JsonlCodec) -> str:
    """Hash what a staged container decompresses to, never its stored bytes."""
    digest = hashlib.sha256()
    with open_jsonl(path, codec) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
