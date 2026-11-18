"""A scheduler may speed up preparation without changing mutation ordering."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Barrier, Event, Lock, get_ident
import unittest
from unittest.mock import patch

from codexsync.work_scheduler import WorkClass, WorkScheduler


class WorkSchedulerTests(unittest.TestCase):
    def test_prepare_observer_accepts_results_in_order_on_the_caller_while_workers_run(self) -> None:
        release_second = Event()
        caller = get_ident()
        observed = []

        def prepare(item):
            if item == "second":
                self.assertTrue(release_second.wait(timeout=5))
            return item.upper()

        def accept(index, item, result):
            observed.append((index, item, result, get_ident()))
            if index == 0:
                release_second.set()

        with WorkScheduler(io_workers=2) as scheduler:
            with scheduler.mutation() as work:
                result = work.map_prepare(prepare, ["first", "second", "third"], on_result=accept)
        self.assertEqual(result, ["FIRST", "SECOND", "THIRD"])
        self.assertEqual(observed, [
            (0, "first", "FIRST", caller), (1, "second", "SECOND", caller), (2, "third", "THIRD", caller),
        ])

    def test_caller_steps_refill_a_bounded_window_before_the_next_step(self) -> None:
        """A long plain scan must not leave decompression idle after its first window."""
        completed = [Event() for _ in range(20)]
        with WorkScheduler(io_workers=1, large_file_limit=2) as scheduler:
            submit = scheduler._io.submit
            submitted = 0

            def tracked_submit(*args, **kwargs):
                nonlocal submitted
                index = submitted
                submitted += 1
                future = submit(*args, **kwargs)
                future.add_done_callback(lambda _future: completed[index].set())
                return future

            def step(index: int) -> None:
                self.assertTrue(completed[index].wait(timeout=5))
                self.assertEqual(submitted, index + 1)

            with patch.object(scheduler._io, "submit", side_effect=tracked_submit):
                result = scheduler.map_read(
                    lambda item: item, range(20), large_file=lambda _: True,
                    caller_work=(lambda index=index: step(index) for index in range(4)),
                )
        self.assertEqual(result, list(range(20)))

    def test_caller_reads_share_the_large_file_budget_of_one(self) -> None:
        caller_finished = Event()

        def worker(item: int) -> int:
            self.assertTrue(caller_finished.is_set())
            return item

        with WorkScheduler(io_workers=2, large_file_limit=1) as scheduler:
            result = scheduler.map_read(
                worker, [1, 2], large_file=lambda _: True,
                caller_work=[caller_finished.set],
            )
        self.assertEqual(result, [1, 2])

    def test_caller_read_overlaps_workers_and_failure_joins_them(self) -> None:
        started, release, finished = Event(), Event(), Event()
        failure = OSError("plain read failed")

        def work(item: int) -> int:
            started.set()
            release.wait(timeout=5)
            finished.set()
            return item

        def caller_work() -> None:
            self.assertTrue(started.wait(timeout=5))
            release.set()
            raise failure

        with WorkScheduler(io_workers=2, large_file_limit=2) as scheduler:
            with self.assertRaises(OSError) as raised:
                scheduler.map_read(work, [1], large_file=lambda _: True, caller_work=[caller_work])
            self.assertIs(raised.exception, failure)
            self.assertTrue(finished.is_set())

    def test_auto_defaults_and_work_classes(self) -> None:
        with patch("codexsync.work_scheduler.os.cpu_count", return_value=12):
            scheduler = WorkScheduler()
        self.assertEqual((scheduler.cpu_workers, scheduler.io_workers, scheduler.large_file_limit), (6, 8, 4))
        self.assertEqual({item.name for item in WorkClass}, {
            "READ_PARALLEL", "PREPARE_PARALLEL_COMMIT_SERIAL", "SERIAL_ONLY", "EXCLUSIVE",
        })
        scheduler.close()
        with patch("codexsync.work_scheduler.os.cpu_count", return_value=None):
            with WorkScheduler() as fallback:
                self.assertEqual((fallback.cpu_workers, fallback.io_workers), (1, 2))
        for kwargs in ({"cpu_workers": 0}, {"io_workers": 0}, {"large_file_limit": 0}):
            with self.assertRaises(ValueError):
                WorkScheduler(**kwargs)

    def test_read_jobs_overlap_and_keep_input_order(self) -> None:
        barrier = Barrier(2)
        with WorkScheduler(cpu_workers=2, io_workers=2) as scheduler:
            result = scheduler.map_read(lambda item: (barrier.wait(timeout=5), item)[1], [2, 1], pool="cpu")
            separate_jobs = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as callers:
                left = callers.submit(scheduler.map_read, lambda item: (separate_jobs.wait(timeout=5), item)[1], ["a"])
                right = callers.submit(scheduler.map_read, lambda item: (separate_jobs.wait(timeout=5), item)[1], ["b"])
                self.assertEqual(left.result(timeout=5), ["a"])
                self.assertEqual(right.result(timeout=5), ["b"])
        self.assertEqual(result, [2, 1])

    def test_global_worker_and_large_file_limits(self) -> None:
        lock = Lock()
        release = Event()
        started = Event()
        active = peak = 0

        def work(item: int) -> int:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
            release.wait(timeout=5)
            with lock:
                active -= 1
            return item

        with WorkScheduler(cpu_workers=3, io_workers=3, large_file_limit=2) as scheduler:
            with ThreadPoolExecutor(max_workers=2) as callers:
                first = callers.submit(scheduler.map_read, work, range(4), pool="cpu", large_file=lambda _: True)
                second = callers.submit(scheduler.map_read, work, range(4, 8), pool="io", large_file=lambda _: True)
                self.assertTrue(started.wait(timeout=5))
                self.assertEqual(peak, 2)
                release.set()
                self.assertEqual(first.result(timeout=5), list(range(4)))
                self.assertEqual(second.result(timeout=5), list(range(4, 8)))
        self.assertEqual(peak, 2)

    def test_failure_stops_submission_and_joins_active_work(self) -> None:
        active_started = Event()
        active_release = Event()
        active_finished = Event()
        failure = ValueError("disk failed")

        def work(item: int) -> int:
            if item == 0:
                active_started.wait(timeout=5)
                raise failure
            if item == 1:
                active_started.set()
                active_release.wait(timeout=5)
                active_finished.set()
            return item

        with WorkScheduler(cpu_workers=2, io_workers=2) as scheduler:
            with ThreadPoolExecutor(max_workers=1) as caller:
                future = caller.submit(scheduler.map_read, work, range(20), pool="cpu")
                self.assertTrue(active_started.wait(timeout=5))
                self.assertFalse(future.done())
                active_release.set()
                with self.assertRaises(ValueError) as raised:
                    future.result(timeout=5)
        self.assertIs(raised.exception, failure)
        self.assertTrue(active_finished.is_set())

    def test_failure_prevents_more_submission_after_it_is_observed(self) -> None:
        seen: list[int] = []
        failure = OSError("read failed")

        def work(item: int) -> int:
            seen.append(item)
            raise failure

        with WorkScheduler(cpu_workers=1, io_workers=1) as scheduler:
            with self.assertRaises(OSError) as raised:
                scheduler.map_read(work, range(20), pool="cpu")
        self.assertIs(raised.exception, failure)
        self.assertEqual(seen, [0])

    def test_mutations_and_serial_key_do_not_overlap(self) -> None:
        entered = Event()
        release = Event()
        second_entered = Event()
        second_attempted = Event()
        mutation_active = Event()
        overlaps: list[str] = []
        with WorkScheduler(cpu_workers=2, io_workers=2) as scheduler:
            def first() -> None:
                with scheduler.mutation() as mutation:
                    def hold() -> None:
                        mutation_active.set()
                        entered.set()
                        release.wait(timeout=5)
                        mutation_active.clear()
                    mutation.commit("state", hold)

            def second() -> None:
                second_attempted.set()
                with scheduler.mutation() as mutation:
                    def mark() -> None:
                        if mutation_active.is_set():
                            overlaps.append("mutation")
                        second_entered.set()
                    mutation.commit("state", mark)

            with ThreadPoolExecutor(max_workers=2) as callers:
                a = callers.submit(first)
                self.assertTrue(entered.wait(timeout=5))
                b = callers.submit(second)
                self.assertTrue(second_attempted.wait(timeout=5))
                release.set()
                a.result(timeout=5)
                b.result(timeout=5)
            self.assertTrue(second_entered.is_set())
            self.assertEqual(overlaps, [])

            serial_started = Event()
            serial_release = Event()
            serial_attempted = Event()
            serial_active = 0
            serial_overlap = []
            def mark_active() -> None:
                nonlocal serial_active
                serial_active += 1
                serial_overlap.append(serial_active)
                serial_active -= 1
            def serial_first() -> None:
                def hold() -> None:
                    nonlocal serial_active
                    serial_active += 1
                    serial_started.set()
                    serial_release.wait(timeout=5)
                    serial_active -= 1
                scheduler.run_serial("sqlite", hold)
            def serial_second() -> None:
                serial_attempted.set()
                scheduler.run_serial("sqlite", mark_active)
            with ThreadPoolExecutor(max_workers=2) as callers:
                a = callers.submit(serial_first)
                self.assertTrue(serial_started.wait(timeout=5))
                b = callers.submit(serial_second)
                self.assertTrue(serial_attempted.wait(timeout=5))
                serial_release.set()
                a.result(timeout=5)
                b.result(timeout=5)
            self.assertEqual(serial_overlap, [1])

            independent = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as callers:
                left = callers.submit(scheduler.run_serial, "sqlite-a", lambda: independent.wait(timeout=5))
                right = callers.submit(scheduler.run_serial, "sqlite-b", lambda: independent.wait(timeout=5))
                left.result(timeout=5)
                right.result(timeout=5)

    def test_exclusive_waits_for_read_and_blocks_mutation(self) -> None:
        read_started, read_release, exclusive_entered, exclusive_release, mutation_entered = (Event() for _ in range(5))
        read_active = Event()
        exclusive_active = Event()
        overlaps: list[str] = []
        with WorkScheduler(cpu_workers=2, io_workers=2) as scheduler:
            def read_one(_: int) -> None:
                read_active.set()
                read_started.set()
                read_release.wait(timeout=5)
                read_active.clear()

            def exclusive_one() -> None:
                with scheduler.exclusive() as work:
                    def hold() -> None:
                        if read_active.is_set():
                            overlaps.append("read and exclusive")
                        exclusive_active.set()
                        exclusive_entered.set()
                        exclusive_release.wait(timeout=5)
                        exclusive_active.clear()
                    work.run(hold)

            def mutation_one() -> None:
                with scheduler.mutation() as work:
                    def mark() -> None:
                        if exclusive_active.is_set():
                            overlaps.append("mutation and exclusive")
                        mutation_entered.set()
                    work.commit("state", mark)

            with ThreadPoolExecutor(max_workers=3) as callers:
                read = callers.submit(scheduler.map_read, read_one, [1])
                self.assertTrue(read_started.wait(timeout=5))
                exclusive = callers.submit(exclusive_one)
                read_release.set()
                read.result(timeout=5)
                self.assertTrue(exclusive_entered.wait(timeout=5))
                mutation = callers.submit(mutation_one)
                exclusive_release.set()
                exclusive.result(timeout=5)
                mutation.result(timeout=5)
                self.assertTrue(mutation_entered.is_set())
            self.assertEqual(overlaps, [])

    def test_single_and_many_workers_produce_identical_bytes_and_plan_id(self) -> None:
        def prepare(item: int) -> bytes:
            return f"chat-{item:04d}\n".encode()
        outputs = []
        for count in (1, 4):
            with WorkScheduler(cpu_workers=count, io_workers=count) as scheduler:
                with scheduler.mutation() as work:
                    chunks = work.map_prepare(prepare, range(40), pool="cpu")
                    def finalize() -> tuple[bytes, str]:
                        payload = b"".join(chunks)
                        return payload, hashlib.sha256(payload).hexdigest()
                    outputs.append(work.commit("state", finalize))
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
