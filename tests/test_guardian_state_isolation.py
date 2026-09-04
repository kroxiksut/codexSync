"""Guardian must never write inside the Codex state directory.

This is the guarantee that lets Guardian run while Codex is open, and both the
P0 and P1 acceptance criteria ask for it to be proven by test rather than by
inspection. Checking the tree is unchanged afterwards is not enough — a write
that is later reverted, or one that only touches an mtime, would pass. So the
mutating syscalls are intercepted and any of them aimed inside the state root
fails the test at the moment it is attempted, naming the call and the path.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import io
import os
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.guardian_models import GuardianConfig, GuardianResultStatus
from codexsync.guardian_runner import GuardianRunner, GuardianRunnerState
from codexsync.guardian_store import GuardianStore


_WRITE_MODE_CHARS = frozenset("wxa+")
# os.open flags that can create, truncate or write.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class StateWriteAttempt(AssertionError):
    pass


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@contextmanager
def forbid_writes_under(state_root: Path):
    """Fail on any mutating filesystem call targeting ``state_root``.

    Reads are delegated untouched: Guardian is expected to read the state file,
    just never to open it for writing or to touch anything around it.
    """
    root = state_root.resolve()

    def inside(candidate) -> bool:
        if isinstance(candidate, int):  # already-open descriptor
            return False
        try:
            resolved = Path(os.fspath(candidate)).resolve()
        except (TypeError, ValueError, OSError):
            return False
        return resolved == root or root in resolved.parents

    def guard(call_name: str, target) -> None:
        if inside(target):
            raise StateWriteAttempt(
                f"Guardian attempted {call_name} inside the Codex state directory: {target}"
            )

    real_open = builtins.open
    # pathlib calls io.open, not builtins.open, so both must be intercepted;
    # they are the same function object but two separate module attributes.
    real_io_open = io.open
    real_os_open = os.open
    patched_os_calls = (
        "replace", "rename", "remove", "unlink", "mkdir", "makedirs",
        "rmdir", "chmod", "utime", "truncate", "link", "symlink",
    )
    real_os = {name: getattr(os, name) for name in patched_os_calls if hasattr(os, name)}
    real_rmtree = shutil.rmtree

    def spy_open(file, mode="r", *args, **kwargs):
        if _WRITE_MODE_CHARS & set(mode):
            guard(f"open(mode={mode!r})", file)
        return real_open(file, mode, *args, **kwargs)

    def spy_os_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS:
            guard("os.open(write flags)", path)
        return real_os_open(path, flags, *args, **kwargs)

    def make_os_spy(name, real):
        def spy(*args, **kwargs):
            for positional in args[:2]:
                guard(f"os.{name}", positional)
            return real(*args, **kwargs)
        return spy

    def spy_rmtree(path, *args, **kwargs):
        guard("shutil.rmtree", path)
        return real_rmtree(path, *args, **kwargs)

    builtins.open = spy_open
    io.open = spy_open
    os.open = spy_os_open
    for name, real in real_os.items():
        setattr(os, name, make_os_spy(name, real))
    shutil.rmtree = spy_rmtree
    try:
        yield
    finally:
        builtins.open = real_open
        io.open = real_io_open
        os.open = real_os_open
        for name, real in real_os.items():
            setattr(os, name, real)
        shutil.rmtree = real_rmtree


class GuardianStateIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"guardian-isolation-{uuid.uuid4().hex}"
        # The state root is a real directory holding more than the state file,
        # so an accidental write to a sibling would be caught too.
        self.state_root = self.root / ".codex"
        (self.state_root / "sessions").mkdir(parents=True)
        self.source = self.state_root / ".codex-global-state.json"
        self.source.write_text(
            '{"local-projects": {"p1": {"root": "C:/x"}}, "project-order": ["p1"]}',
            encoding="utf-8",
        )
        (self.state_root / "sessions" / "s.jsonl").write_text("{}\n", encoding="utf-8")
        self.config = GuardianConfig(
            root_dir=self.root / "guardian",
            max_state_bytes=1024 * 1024,
            debounce_seconds=0.1,
            stable_read_interval_seconds=0.1,
            poll_interval_seconds=0.1,
            fallback_scan_seconds=60,
            once_timeout_seconds=2,
        )
        self.store = GuardianStore(self.config.root_dir, "machine-a", producer_version="test")
        self.clock = _Clock()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _runner(self) -> GuardianRunner:
        return GuardianRunner(
            self.source, self.store, self.config,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep,
        )

    def _tree(self) -> dict[str, tuple[int, int, bytes]]:
        out = {}
        for path in sorted(self.state_root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                out[str(path.relative_to(self.state_root))] = (
                    stat.st_size, stat.st_mtime_ns, path.read_bytes()
                )
        return out

    def test_the_spy_itself_catches_a_write(self) -> None:
        """A guard that never fires proves nothing; prove it fires."""
        with self.assertRaises(StateWriteAttempt):
            with forbid_writes_under(self.state_root):
                (self.state_root / "intruder.txt").write_text("x", encoding="utf-8")

    def test_snapshot_writes_nothing_into_state(self) -> None:
        before = self._tree()
        with forbid_writes_under(self.state_root):
            outcome = self._runner().once()
        self.assertEqual(outcome.status, GuardianResultStatus.COMMITTED)
        self.assertEqual(self._tree(), before, "state directory must be byte- and mtime-identical")

    def test_repeated_snapshot_writes_nothing_into_state(self) -> None:
        self._runner().once()
        before = self._tree()
        with forbid_writes_under(self.state_root):
            outcome = self._runner().once()
        self.assertEqual(outcome.status, GuardianResultStatus.UNCHANGED)
        self.assertEqual(self._tree(), before)

    def test_invalid_state_is_quarantined_without_touching_state(self) -> None:
        self.source.write_bytes(b'{"local-projects": {"p1"\x00 truncated')
        before = self._tree()
        with forbid_writes_under(self.state_root):
            outcome = self._runner().once()
        self.assertEqual(outcome.status, GuardianResultStatus.QUARANTINED)
        self.assertEqual(self._tree(), before, "a rejected state must not be repaired in place")

    def test_missing_state_writes_nothing_into_state(self) -> None:
        self.source.unlink()
        before = self._tree()
        with forbid_writes_under(self.state_root):
            outcome = self._runner().once()
        self.assertIn(
            outcome.state,
            {GuardianRunnerState.IDLE, GuardianRunnerState.BACKOFF, GuardianRunnerState.QUARANTINING},
        )
        self.assertEqual(self._tree(), before, "Guardian must not recreate a missing state file")

    def test_guardian_root_stays_outside_the_state_directory(self) -> None:
        self._runner().once()
        guardian_root = self.config.root_dir.resolve()
        state_root = self.state_root.resolve()
        self.assertNotEqual(guardian_root, state_root)
        self.assertNotIn(state_root, guardian_root.parents)
        written = list(guardian_root.rglob("*"))
        self.assertTrue(written, "the snapshot must actually land somewhere outside state")


if __name__ == "__main__":
    unittest.main()
