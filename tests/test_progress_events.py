from __future__ import annotations

import unittest

from codexsync.progress import ProgressEstimator, ProgressEvent


class ProgressEventTests(unittest.TestCase):
    def test_event_rejects_impossible_counts(self) -> None:
        with self.assertRaises(ValueError):
            ProgressEvent("sync", "stage_chats", completed=3, total=2)
        with self.assertRaises(ValueError):
            ProgressEvent("sync", "stage_chats", completed=-1, total=2)

    def test_unknown_total_has_no_eta(self) -> None:
        estimator = ProgressEstimator(window=4)
        estimator.update(ProgressEvent("doctor", "checks", completed=0, total=None), now=10.0)
        estimate = estimator.update(
            ProgressEvent("doctor", "checks", completed=2, total=None), now=12.0,
        )
        self.assertEqual(estimate.items_per_second, 1.0)
        self.assertIsNone(estimate.remaining_seconds)

    def test_eta_uses_recent_rate_and_is_separate_for_each_phase(self) -> None:
        estimator = ProgressEstimator(window=3)
        estimator.update(ProgressEvent("sync", "stage_chats", completed=0, total=10), now=0.0)
        estimator.update(ProgressEvent("sync", "stage_chats", completed=2, total=10), now=2.0)
        estimate = estimator.update(
            ProgressEvent("sync", "stage_chats", completed=6, total=10), now=4.0,
        )
        self.assertEqual(estimate.items_per_second, 1.5)
        self.assertAlmostEqual(estimate.remaining_seconds or 0, 4 / 1.5)

        first_commit = estimator.update(
            ProgressEvent("sync", "apply_chats", completed=0, total=10), now=5.0,
        )
        self.assertIsNone(first_commit.items_per_second)
        self.assertIsNone(first_commit.remaining_seconds)

    def test_bytes_rate_uses_the_same_bounded_window(self) -> None:
        estimator = ProgressEstimator(window=2)
        estimator.update(
            ProgressEvent("backup", "payload", completed=0, total=4, bytes_completed=0), now=0.0,
        )
        estimator.update(
            ProgressEvent("backup", "payload", completed=1, total=4, bytes_completed=100), now=1.0,
        )
        estimate = estimator.update(
            ProgressEvent("backup", "payload", completed=3, total=4, bytes_completed=500), now=3.0,
        )
        self.assertEqual(estimate.items_per_second, 1.0)
        self.assertEqual(estimate.bytes_per_second, 200.0)
        self.assertEqual(estimate.remaining_seconds, 1.0)

    def test_regressing_progress_starts_a_new_measurement_window(self) -> None:
        estimator = ProgressEstimator(window=4)
        estimator.update(ProgressEvent("sync", "stage_chats", completed=5, total=10), now=1.0)
        reset = estimator.update(
            ProgressEvent("sync", "stage_chats", completed=0, total=12), now=2.0,
        )
        self.assertIsNone(reset.items_per_second)
        self.assertIsNone(reset.remaining_seconds)


if __name__ == "__main__":
    unittest.main()
