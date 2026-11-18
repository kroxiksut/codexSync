"""Bounded, toolkit-neutral work scheduling within one process.

This coordinates preparation and read work only. OperationLock and SafetyGate
remain the authority for cross-process exclusion and safe filesystem writes.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from enum import Enum, auto
import os
from threading import Condition, Lock, Semaphore, local
from typing import Callable, Iterable, Iterator, TypeVar

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


class WorkClass(Enum):
    READ_PARALLEL = auto()
    PREPARE_PARALLEL_COMMIT_SERIAL = auto()
    SERIAL_ONLY = auto()
    EXCLUSIVE = auto()


class _MutationWork:
    """A mutation's preparation may fan out; its commit cannot."""

    work_class = WorkClass.PREPARE_PARALLEL_COMMIT_SERIAL

    def __init__(self, scheduler: WorkScheduler) -> None:
        self._scheduler = scheduler
        self._open = True

    def map_prepare(
        self, function: Callable[[_Item], _Result], items: Iterable[_Item], *,
        pool: str = "io", large_file: Callable[[_Item], bool] | None = None,
    ) -> list[_Result]:
        self._require_open()
        return self._scheduler._ordered_map(function, items, pool=pool, large_file=large_file)

    def commit(self, key: str, function: Callable[[], _Result]) -> _Result:
        """Run one final mutation step on the operation's calling thread."""
        self._require_open()
        return self._scheduler._run_key(key, function)

    def _require_open(self) -> None:
        if not self._open:
            raise RuntimeError("mutation scope is closed")


class _ExclusiveWork:
    work_class = WorkClass.EXCLUSIVE

    def __init__(self) -> None:
        self._open = True

    def run(self, function: Callable[[], _Result]) -> _Result:
        if not self._open:
            raise RuntimeError("exclusive scope is closed")
        return function()


class WorkScheduler:
    """Share bounded CPU and I/O pools among operations in this process.

    Maps return in input order. A worker failure stops further submission and
    waits for already submitted work before raising that same exception object.
    ``mutation`` serializes entire mutation operations; ``exclusive`` waits for
    every managed operation and blocks new ones until it leaves. Neither is a
    substitute for the persistent OperationLock or the mutation envelope.
    """

    def __init__(
        self, *, cpu_workers: int | None = None, io_workers: int | None = None,
        large_file_limit: int = 4,
    ) -> None:
        count = os.cpu_count() or 1
        self.cpu_workers = min(8, max(1, count // 2)) if cpu_workers is None else cpu_workers
        self.io_workers = min(8, max(2, count)) if io_workers is None else io_workers
        self.large_file_limit = large_file_limit
        if min(self.cpu_workers, self.io_workers, self.large_file_limit) < 1:
            raise ValueError("worker and large-file limits must be positive")
        self._cpu = ThreadPoolExecutor(max_workers=self.cpu_workers, thread_name_prefix="codexsync-cpu")
        self._io = ThreadPoolExecutor(max_workers=self.io_workers, thread_name_prefix="codexsync-io")
        self._large_files = Semaphore(large_file_limit)
        self._condition = Condition()
        self._active = 0
        self._exclusive = False
        self._exclusive_waiters = 0
        self._mutation_lock = Lock()
        self._key_guard = Lock()
        self._key_locks: dict[str, Lock] = {}
        self._thread = local()
        self._closed = False

    def __enter__(self) -> WorkScheduler:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self._thread, "inside_scope", False):
            raise RuntimeError("cannot close scheduler inside a work scope")
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._active or self._exclusive:
                self._condition.wait()
        self._cpu.shutdown(wait=True)
        self._io.shutdown(wait=True)

    @contextmanager
    def _activity(self) -> Iterator[None]:
        if getattr(self._thread, "inside_scope", False):
            raise RuntimeError("nested scheduler scopes are not supported")
        with self._condition:
            while self._exclusive or self._exclusive_waiters:
                if self._closed:
                    raise RuntimeError("scheduler is closed")
                self._condition.wait()
            if self._closed:
                raise RuntimeError("scheduler is closed")
            self._active += 1
        self._thread.inside_scope = True
        try:
            yield
        finally:
            self._thread.inside_scope = False
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def map_read(
        self, function: Callable[[_Item], _Result], items: Iterable[_Item], *,
        pool: str = "io", large_file: Callable[[_Item], bool] | None = None,
        caller_work: Iterable[Callable[[], None]] | None = None,
    ) -> list[_Result]:
        """Run read-only work with bounded submission and ordered results.

        Optional independent ``caller_work`` steps run on this thread, sharing
        the large-file budget. Completed workers are collected and the bounded
        window refilled between steps. With a budget of one, caller steps finish
        before large worker jobs start; they must not wait for those jobs.
        A caller failure also joins submitted work before propagating.
        """
        with self._activity():
            return self._ordered_map(
                function, items, pool=pool, large_file=large_file, caller_work=caller_work,
            )

    @contextmanager
    def mutation(self) -> Iterator[_MutationWork]:
        """Keep two mutation operations apart, including their preparation."""
        with self._activity():
            with self._mutation_lock:
                work = _MutationWork(self)
                try:
                    yield work
                finally:
                    work._open = False

    def run_serial(self, key: str, function: Callable[[], _Result]) -> _Result:
        """Run SQLite/journal work serially for the same resource key."""
        with self._activity():
            return self._run_key(key, function)

    def _run_key(self, key: str, function: Callable[[], _Result]) -> _Result:
        if not key:
            raise ValueError("serial key must not be empty")
        with self._key_guard:
            key_lock = self._key_locks.setdefault(key, Lock())
        with key_lock:
            return function()

    @contextmanager
    def exclusive(self) -> Iterator[_ExclusiveWork]:
        """For recovery/rollback: wait for all managed work, then run alone."""
        if getattr(self._thread, "inside_scope", False):
            raise RuntimeError("exclusive cannot be nested inside another scheduler scope")
        with self._condition:
            self._exclusive_waiters += 1
            try:
                while self._exclusive or self._active:
                    if self._closed:
                        raise RuntimeError("scheduler is closed")
                    self._condition.wait()
                if self._closed:
                    raise RuntimeError("scheduler is closed")
                self._exclusive = True
            finally:
                self._exclusive_waiters -= 1
        self._thread.inside_scope = True
        work = _ExclusiveWork()
        try:
            yield work
        finally:
            work._open = False
            self._thread.inside_scope = False
            with self._condition:
                self._exclusive = False
                self._condition.notify_all()

    def _ordered_map(
        self, function: Callable[[_Item], _Result], items: Iterable[_Item], *,
        pool: str, large_file: Callable[[_Item], bool] | None,
        caller_work: Iterable[Callable[[], None]] | None = None,
    ) -> list[_Result]:
        if pool not in {"cpu", "io"}:
            raise ValueError("pool must be 'cpu' or 'io'")
        executor, limit = (self._cpu, self.cpu_workers) if pool == "cpu" else (self._io, self.io_workers)
        iterator = iter(items)
        pending: dict[Future[_Result], int] = {}
        results: list[_Result] = []
        completed: dict[int, _Result] = {}
        exhausted = False
        next_index = 0
        caller_steps = iter(caller_work) if caller_work is not None else iter(())
        caller_pending = caller_work is not None
        caller_reserved = False

        def release_caller() -> None:
            nonlocal caller_reserved
            if caller_reserved:
                self._large_files.release()
                caller_reserved = False

        def submit_one() -> None:
            nonlocal exhausted, next_index
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                return
            is_large = large_file(item) if large_file is not None else False
            index = next_index
            next_index += 1

            def invoke() -> _Result:
                if is_large:
                    with self._large_files:
                        return function(item)
                return function(item)

            pending[executor.submit(invoke)] = index

        failure: BaseException | None = None
        try:
            # Reserve before workers can claim the entire memory budget.
            if caller_pending:
                self._large_files.acquire()
                caller_reserved = True
            while next_index - len(results) < limit and not exhausted:
                submit_one()
            while pending or caller_pending:
                if caller_pending:
                    try:
                        step = next(caller_steps)
                    except StopIteration:
                        caller_pending = False
                        release_caller()
                    else:
                        step()
                    done = {future for future in pending if future.done()}
                else:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=pending.get):
                    error = future.exception()
                    if error is not None and failure is None:
                        failure = error
                for future in done:
                    index = pending.pop(future)
                    if failure is None:
                        completed[index] = future.result()
                if failure is not None:
                    break
                while len(results) in completed:
                    results.append(completed.pop(len(results)))
                while next_index - len(results) < limit and not exhausted:
                    submit_one()
        except BaseException:
            release_caller()
            for future in pending:
                try:
                    future.result()
                except BaseException:
                    pass
            raise
        finally:
            release_caller()
        if failure is not None:
            for future in pending:
                try:
                    future.result()
                except BaseException:
                    pass
            raise failure
        return results
