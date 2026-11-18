"""The handoff watcher ends on a request, between runs, never inside one (CS-506).

Removing its task used to end it from outside (`schtasks /End`, `systemctl
stop`), which cut a full sync half way and left RECOVERY_REQUIRED. Now the
removal leaves a stop request and the watcher ends itself once idle; on POSIX
a SIGTERM is taken the same way.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import unittest
from unittest import mock
import uuid

from codexsync.app import HandoffResult, watch_handoff
from codexsync.config import load_config
from codexsync.config_edit import create_config
from codexsync.handoff import STATE_HANDED_OFF, Delivery, HandoffRecord
from codexsync.safety_gate import ProcessState
from codexsync.watcher_stop import StopWatch, WatcherStopping, request_watcher_stop, stop_request_path

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"


def _result() -> HandoffResult:
    return HandoffResult("laptop", (), 0, 0, True, HandoffRecord("laptop", STATE_HANDED_OFF, "t"))


class _Config(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"watcher-stop-{uuid.uuid4().hex[:8]}"
        (self.root / "codex").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = self.root / "config.toml"
        create_config(
            self.config, machine_id="laptop", local_state_dir=str(self.root / "codex"),
            workspace_root_dir=str(self.root / "workspace"),
        )
        self.temp = load_config(self.config).paths.temp_dir


class StopWatchTests(_Config):
    def test_a_request_older_than_the_watcher_is_not_one_for_it(self) -> None:
        request_watcher_stop(self.temp)
        watch = StopWatch(self.temp)
        self.assertFalse(watch())
        request_watcher_stop(self.temp)
        self.assertTrue(watch())

    def test_no_request_file_means_keep_going(self) -> None:
        watch = StopWatch(self.temp)
        self.assertFalse(watch())
        self.assertFalse(stop_request_path(self.temp).exists(), "reading writes nothing")


class WatcherEndsBetweenRunsTests(_Config):
    def test_a_stop_request_ends_the_watcher_after_the_run_it_is_in(self) -> None:
        events: list[str] = []
        states = iter([ProcessState.RUNNING, ProcessState.RUNNING, ProcessState.STOPPED, ProcessState.STOPPED])

        def act(path):
            events.append("run started")
            # The task is removed while the sync runs: it must finish anyway.
            request_watcher_stop(self.temp)
            events.append("run finished")
            return _result()

        def probe():
            try:
                return next(states)
            except StopIteration:
                self.fail("the watcher kept polling after it was asked to stop")

        watch_handoff(self.config, probe=probe, handoff=act, notifier=lambda *a, **k: None, sleep=lambda s: None)
        self.assertEqual(events, ["run started", "run finished"])

    def test_a_stop_ends_a_wait_for_the_cloud_before_anything_is_written(self) -> None:
        seen: list[str] = []

        def fake_run_handoff(path, *, close_codex, on_wait):
            request_watcher_stop(self.temp)
            try:
                on_wait([Delivery("desktop", "d1", 3, 1, 0)])
            except WatcherStopping:
                seen.append("wait ended")
                raise
            seen.append("wrote anyway")
            return _result()

        notes: list[str] = []
        with mock.patch("codexsync.app.run_handoff", side_effect=fake_run_handoff):
            watch_handoff(
                self.config, probe=lambda: ProcessState.STOPPED, notifier=lambda key, **v: notes.append(key),
                sleep=lambda s: None,
            )
        self.assertEqual(seen, ["wait ended"])
        self.assertEqual(notes, [], "a stop is not a failure to report")

    @unittest.skipIf(os.name == "nt", "Windows ends a windowless process with no signal")
    def test_a_sigterm_during_a_run_lets_the_run_finish(self) -> None:
        events: list[str] = []
        before = signal.getsignal(signal.SIGTERM)

        def act(path):
            os.kill(os.getpid(), signal.SIGTERM)
            events.append("run finished")
            return _result()

        watch_handoff(
            self.config, probe=lambda: ProcessState.STOPPED, handoff=act,
            notifier=lambda *a, **k: None, sleep=lambda s: None,
        )
        self.assertEqual(events, ["run finished"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before, "the old handler is put back")


if __name__ == "__main__":
    unittest.main()
