"""The lock that stops two mutations from touching one Codex state root.

Every mutating command — ``sync``, ``restore``, ``repair-projects apply``,
``sessions apply`` — opens this lock before its journal. Guardian's writer lock
has had its own cover since P0; this one had none, and it guards the more
dangerous side: Guardian only ever writes into its own root, while these
commands replace files inside the state directory itself.

Two properties matter and neither can be inferred from reading the code. It has
to hold against a *separate process*, because that is the case it exists for —
a scheduled run overlapping an interactive one. And it must never be stolen:
the file's age says nothing about whether its owner is still alive, so a stale
lock stays a held lock.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
import uuid

from codexsync.exceptions import FailSafeError, OperationBusyError
from codexsync.operation_lock import OperationLock


_CHILD_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.02


class OperationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"operation-lock-{uuid.uuid4().hex}"
        self.temp_dir = self.root / ".tmp"
        self.state_root = self.root / "local-state"
        self.state_root.mkdir(parents=True)
        self.temp_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _lock(self, *, state_root: Path | None = None, machine_id: str = "machine-a",
              family: str = "sync") -> OperationLock:
        return OperationLock(
            self.temp_dir,
            state_root=state_root or self.state_root,
            machine_id=machine_id,
            family=family,
        )

    # --- one mutation at a time -------------------------------------------

    def test_a_second_mutation_on_the_same_state_root_is_refused(self) -> None:
        with self._lock():
            with self.assertRaises(OperationBusyError):
                with self._lock():
                    self.fail("two mutations may never own one state root")

    def test_being_busy_is_a_fail_safe_error_so_it_exits_five(self) -> None:
        """The exit code is driven by the exception type, not by a return value."""
        self.assertTrue(issubclass(OperationBusyError, FailSafeError))

    def test_the_lock_is_released_when_the_operation_ends(self) -> None:
        with self._lock():
            pass
        with self._lock():
            pass

    def test_the_lock_is_released_when_the_operation_fails(self) -> None:
        """A crashed mutation must not wedge every later one."""
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with self._lock():
                raise RuntimeError("simulated failure")
        with self._lock():
            pass

    # --- what counts as the same lock -------------------------------------

    def test_different_families_do_not_block_each_other(self) -> None:
        with self._lock(family="sync"):
            with self._lock(family="repair"):
                pass

    def test_a_different_state_root_is_a_different_lock(self) -> None:
        other = self.root / "other-state"
        other.mkdir()
        with self._lock():
            with self._lock(state_root=other):
                pass

    def test_a_different_machine_id_is_a_different_lock(self) -> None:
        with self._lock(machine_id="machine-a"):
            with self._lock(machine_id="machine-b"):
                pass

    def test_one_state_root_is_one_lock_however_the_path_is_spelled(self) -> None:
        """A lock keyed by the literal string would be no lock at all."""
        spellings = [self.state_root / "." / "", Path(os.path.join(str(self.state_root), ""))]
        if os.name == "nt":
            spellings.append(Path(str(self.state_root).upper()))
        with self._lock():
            for spelling in spellings:
                with self.subTest(spelling=str(spelling)):
                    with self.assertRaises(OperationBusyError):
                        with self._lock(state_root=spelling):
                            self.fail("the same directory must resolve to one lock")

    def test_the_lock_file_names_a_digest_and_not_the_path(self) -> None:
        lock = self._lock()
        self.assertEqual(lock.path.parent, self.temp_dir / "locks")
        self.assertEqual(len(lock.path.stem), 64)
        int(lock.path.stem, 16)  # a plain sha256, so no path leaks into a file name
        self.assertNotIn(self.state_root.name.casefold(), lock.path.stem.casefold())

    # --- never stolen ------------------------------------------------------

    def test_age_never_breaks_ownership(self) -> None:
        """A lock file's timestamp says nothing about whether its owner is alive."""
        with self._lock() as held:
            long_ago = time.time() - 365 * 24 * 3600
            os.utime(held.path, (long_ago, long_ago))
            with self.assertRaises(OperationBusyError):
                with self._lock():
                    self.fail("an old lock is still a held lock")

    def test_a_leftover_lock_file_from_a_finished_run_does_not_block(self) -> None:
        with self._lock() as held:
            path = held.path
        self.assertTrue(path.is_file(), "the file outlives the lock; only the OS lock matters")
        with self._lock():
            pass

    # --- the case it exists for -------------------------------------------

    def test_a_separate_process_cannot_take_a_held_lock(self) -> None:
        held = self.root / "held.marker"
        release = self.root / "release.marker"
        child = subprocess.Popen(
            [sys.executable, "-c", self._child_source(held, release)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._wait_for(held, child)
            with self.assertRaises(OperationBusyError):
                with self._lock():
                    self.fail("a lock held by another process must not be taken here")
        finally:
            release.write_text("go", encoding="utf-8")
            try:
                out, err = child.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                child.kill()
                out, err = child.communicate()
                self.fail("the holding process did not finish")
        self.assertEqual(child.returncode, 0, f"child failed: {err}")
        self.assertEqual(out.strip(), "released")
        # And the lock is free again now that the other process is gone.
        with self._lock():
            pass

    def _child_source(self, held: Path, release: Path) -> str:
        return textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from codexsync.operation_lock import OperationLock

            with OperationLock(
                Path({str(self.temp_dir)!r}),
                state_root=Path({str(self.state_root)!r}),
                machine_id="machine-a",
                family="sync",
            ):
                Path({str(held)!r}).write_text("held", encoding="utf-8")
                deadline = time.monotonic() + {_CHILD_TIMEOUT_SECONDS}
                while not Path({str(release)!r}).exists():
                    if time.monotonic() > deadline:
                        raise SystemExit("parent never released the child")
                    time.sleep({_POLL_SECONDS})
            print("released")
            """
        )

    def _wait_for(self, marker: Path, child: subprocess.Popen) -> None:
        deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
        while not marker.exists():
            if child.poll() is not None:
                self.fail(f"the holding process exited early: {child.communicate()[1]}")
            if time.monotonic() > deadline:
                child.kill()
                self.fail("the holding process never acquired the lock")
            time.sleep(_POLL_SECONDS)


class OperationLockNamingTests(unittest.TestCase):
    """The lock name is a contract: every command family must agree on it."""

    def test_the_digest_covers_state_root_machine_and_family(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"operation-lock-name-{uuid.uuid4().hex}"
        state_root = root / "local-state"
        state_root.mkdir(parents=True)
        try:
            canonical = str(state_root.resolve())
            if os.name == "nt":
                canonical = canonical.casefold()
            expected = hashlib.sha256(
                "\0".join((canonical, "machine-a", "sync")).encode("utf-8")
            ).hexdigest()
            lock = OperationLock(
                root / ".tmp", state_root=state_root, machine_id="machine-a", family="sync"
            )
            self.assertEqual(lock.path.name, f"{expected}.lock")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
