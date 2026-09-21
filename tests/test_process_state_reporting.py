"""A refusal has to say what it found (CS-260, CS-261).

"Codex or known background process detected" is true of a running Codex and of
an unrelated always-on service alike, and the two need opposite responses. The
same holds for a missing state file: the file name alone cannot distinguish
"Codex is not installed" from "this is the wrong config".
"""
from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.process_detector import ProcessInfo
from codexsync.runtime import ProcessSnapshot, describe_process_snapshot
from codexsync.safety_gate import OperationKind, ProcessState, SafetyGate


def snapshot(main=(), background=()) -> ProcessSnapshot:
    return ProcessSnapshot(
        main_processes=list(main),
        subprocesses=[],
        sandbox_detected=False,
        background_processes=list(background),
    )


class DescribeSnapshotTests(unittest.TestCase):
    def test_names_and_pids(self) -> None:
        text = describe_process_snapshot(snapshot(main=[ProcessInfo(pid=27472, name="codex.exe")]))
        self.assertEqual(text, "codex.exe (pid 27472)")

    def test_a_background_marker_is_named_too(self) -> None:
        # The case that mattered: no Codex at all, one service matching.
        text = describe_process_snapshot(
            snapshot(background=[ProcessInfo(pid=8296, name="codex-windows-sandbox-service.exe")])
        )
        self.assertIn("codex-windows-sandbox-service.exe", text)
        self.assertIn("8296", text)

    def test_repeats_collapse_and_the_line_stays_short(self) -> None:
        many = [ProcessInfo(pid=n, name="ChatGPT.exe") for n in range(10, 19)]
        text = describe_process_snapshot(snapshot(main=many))
        self.assertEqual(text.count("ChatGPT.exe"), 1)
        self.assertIn("+6", text)

    def test_nothing_found_says_nothing(self) -> None:
        self.assertEqual(describe_process_snapshot(snapshot()), "")


class GateReasonTests(unittest.TestCase):
    def gate(self, state: ProcessState, detail: str) -> SafetyGate:
        return SafetyGate(
            lambda: state,
            describe=lambda: detail,
            monotonic=lambda: 0.0,
            sleep=lambda _s: None,
        )

    def test_a_running_reason_names_the_process(self) -> None:
        decision = self.gate(ProcessState.RUNNING, "codex.exe (pid 1)").check(OperationKind.SYNC)
        self.assertFalse(decision.allowed)
        self.assertIn("codex.exe (pid 1)", decision.reason)

    def test_a_describer_that_fails_does_not_break_the_decision(self) -> None:
        def boom() -> str:
            raise RuntimeError("enumeration gone")

        gate = SafetyGate(
            lambda: ProcessState.RUNNING,
            describe=boom,
            monotonic=lambda: 0.0,
            sleep=lambda _s: None,
        )
        decision = gate.check(OperationKind.SYNC)
        self.assertFalse(decision.allowed)
        self.assertIn("detected", decision.reason)

    def test_an_unknown_state_is_still_told_apart_from_a_running_one(self) -> None:
        unknown = self.gate(ProcessState.UNKNOWN, "irrelevant").check(OperationKind.SYNC)
        running = self.gate(ProcessState.RUNNING, "codex.exe (pid 1)").check(OperationKind.SYNC)
        self.assertNotEqual(unknown.reason, running.reason)
        self.assertIn("unknown state", unknown.reason)

    def test_a_gate_without_a_describer_keeps_the_old_sentence(self) -> None:
        gate = SafetyGate(
            lambda: ProcessState.RUNNING, monotonic=lambda: 0.0, sleep=lambda _s: None
        )
        self.assertEqual(
            gate.check(OperationKind.SYNC).reason,
            "Codex or known background process detected",
        )


class MissingStateMessageTests(unittest.TestCase):
    def test_the_reader_reports_the_whole_path(self) -> None:
        from codexsync.stable_reader import SourceMissingError, StableReader

        missing = Path("test-sandbox") / "no-such-dir" / ".codex-global-state.json"
        with self.assertRaises(SourceMissingError) as caught:
            StableReader(missing, max_bytes=1024).read_once()
        # Not just ".codex-global-state.json": which directory is the answer.
        self.assertIn("no-such-dir", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
