from __future__ import annotations

import unittest

from codexsync.exceptions import FailSafeError, SafetyPreconditionError
from codexsync.safety_gate import OperationKind, ProcessState, SafetyGate


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class SafetyGateTests(unittest.TestCase):
    def _gate(self, samples: list[ProcessState]) -> SafetyGate:
        clock = _Clock()
        values = iter(samples)
        last = samples[-1]

        def sample() -> ProcessState:
            nonlocal last
            try:
                last = next(values)
            except StopIteration:
                pass
            return last

        return SafetyGate(
            sample,
            stable_window_seconds=2.0,
            sample_interval_seconds=0.25,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    def test_mutation_requires_continuous_stopped_window(self) -> None:
        gate = self._gate([ProcessState.STOPPED] * 10)
        decision = gate.require(OperationKind.SYNC)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.process_state, ProcessState.STOPPED)

    def test_process_appearing_during_window_blocks_mutation(self) -> None:
        gate = self._gate([ProcessState.STOPPED, ProcessState.STOPPED, ProcessState.RUNNING])
        with self.assertRaises(SafetyPreconditionError):
            gate.require(OperationKind.RESTORE)

    def test_unknown_process_state_fails_closed(self) -> None:
        gate = self._gate([ProcessState.UNKNOWN])
        with self.assertRaises(FailSafeError):
            gate.require(OperationKind.SYNC)

    def test_guardian_is_allowed_while_codex_runs(self) -> None:
        gate = self._gate([ProcessState.RUNNING])
        decision = gate.require(OperationKind.GUARDIAN_WATCH)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.process_state, ProcessState.RUNNING)

    def test_final_check_is_single_direct_sample(self) -> None:
        gate = self._gate([ProcessState.STOPPED])
        self.assertTrue(gate.require(OperationKind.SYNC, final=True).allowed)


if __name__ == "__main__":
    unittest.main()
