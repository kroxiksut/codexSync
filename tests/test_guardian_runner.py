from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.guardian_models import GuardianConfig, GuardianResultStatus
from codexsync.guardian_runner import GuardianRunner, GuardianRunnerState
from codexsync.guardian_store import GuardianStore


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class GuardianRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "test-sandbox" / f"guardian-runner-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.source = self.root / ".codex-global-state.json"
        self.source.write_text('{"local-projects": {}, "project-order": []}', encoding="utf-8")
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
            self.source,
            self.store,
            self.config,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def test_once_commits_stable_valid_source(self) -> None:
        outcome = self._runner().once()
        self.assertEqual(outcome.status, GuardianResultStatus.COMMITTED)
        self.assertEqual(outcome.state, GuardianRunnerState.IDLE)
        self.assertIsNotNone(outcome.result and outcome.result.snapshot)

    def test_once_deduplicates_unchanged_source(self) -> None:
        self.assertEqual(self._runner().once().status, GuardianResultStatus.COMMITTED)
        self.assertEqual(self._runner().once().status, GuardianResultStatus.UNCHANGED)

    def test_missing_source_creates_only_one_quarantine_event_per_runner(self) -> None:
        self.source.unlink()
        runner = self._runner()
        first = runner._process_candidate(deadline=None, debounce=False)
        second = runner._process_candidate(deadline=None, debounce=False)
        self.assertEqual(first.status, GuardianResultStatus.QUARANTINED)
        self.assertEqual(second.status, GuardianResultStatus.UNCHANGED)


if __name__ == "__main__":
    unittest.main()
