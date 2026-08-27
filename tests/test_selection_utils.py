"""Unit tests for selection_utils.cap_adjusted_threshold.

Run from github_repo: python3 -m unittest tests.test_selection_utils -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from selection_utils import cap_adjusted_threshold


class CapAdjustedThresholdTests(unittest.TestCase):
    def test_converges_when_cap_met_at_start(self):
        # Cap is 500 of 1000; the starting threshold already selects 400.
        calls = []

        def count_selected(threshold):
            calls.append(threshold)
            return 400

        result = cap_adjusted_threshold(
            count_selected, start_threshold=15, max_fraction=0.5, total_points=1000
        )
        self.assertEqual(result, 15)
        self.assertEqual(calls, [15])  # only one probe needed

    def test_widens_correctly_until_under_cap(self):
        # Selection count halves each time the threshold widens; cap is 500
        # of 1000. start=10 -> 2000 selected (over cap), widen to 12.5 ->
        # 1000 selected (still over), widen to 15.625 -> 400 (under cap).
        counts_by_call_index = [2000, 1000, 400]
        calls = []

        def count_selected(threshold):
            calls.append(threshold)
            return counts_by_call_index[len(calls) - 1]

        result = cap_adjusted_threshold(
            count_selected, start_threshold=10, max_fraction=0.5, total_points=1000,
            widen_factor=1.25, max_iters=12,
        )
        expected_threshold = 10 * 1.25 * 1.25
        self.assertAlmostEqual(result, expected_threshold)
        self.assertEqual(len(calls), 3)
        self.assertAlmostEqual(calls[0], 10)
        self.assertAlmostEqual(calls[1], 12.5)
        self.assertAlmostEqual(calls[2], expected_threshold)

    def test_gives_up_at_max_iters(self):
        # Selection never drops below the cap no matter how far the
        # threshold widens; the search must stop after max_iters probes and
        # return the last threshold tested (not a further-widened, untested
        # one).
        calls = []

        def count_selected(threshold):
            calls.append(threshold)
            return 999  # always over the cap of 500

        result = cap_adjusted_threshold(
            count_selected, start_threshold=10, max_fraction=0.5, total_points=1000,
            widen_factor=1.25, max_iters=5,
        )
        self.assertEqual(len(calls), 5)
        expected_threshold = 10 * (1.25 ** 4)
        self.assertAlmostEqual(result, expected_threshold)
        self.assertAlmostEqual(calls[-1], expected_threshold)

    def test_single_max_iter_returns_start_threshold_untested_further(self):
        # max_iters=1 means exactly one probe is spent; even on failure we
        # do not widen past it.
        calls = []

        def count_selected(threshold):
            calls.append(threshold)
            return 999

        result = cap_adjusted_threshold(
            count_selected, start_threshold=10, max_fraction=0.5, total_points=1000,
            max_iters=1,
        )
        self.assertEqual(calls, [10])
        self.assertEqual(result, 10)

    def test_exact_cap_boundary_counts_as_satisfied(self):
        # A count exactly equal to the cap should be accepted (<=), not
        # trigger a widen.
        calls = []

        def count_selected(threshold):
            calls.append(threshold)
            return 500  # exactly max_fraction * total_points

        result = cap_adjusted_threshold(
            count_selected, start_threshold=15, max_fraction=0.5, total_points=1000
        )
        self.assertEqual(result, 15)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
