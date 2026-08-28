"""Unit tests for selection_utils.cap_adjusted_threshold.

Run from github_repo: python3 -m unittest tests.test_selection_utils -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from selection_utils import cap_adjusted_threshold, capped_gradual_selection


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


class _FakeTiePoints:
    def __init__(self, points):
        self.points = points


class _FakeChunk:
    def __init__(self, tie_points, label="MRS_T1_2023ann"):
        self.tie_points = tie_points
        self.label = label


class CappedGradualSelectionTiePointGuardTests(unittest.TestCase):
    # pass2 fix: alignment that produces no tie points must fail with a
    # clear RuntimeError naming the chunk, not crash inside _run_criterion's
    # len(chunk.tie_points.points) with an opaque
    # "TypeError: object of type 'NoneType' has no len()".

    def test_none_tie_points_raises_runtime_error_naming_the_chunk(self):
        chunk = _FakeChunk(tie_points=None, label="MRS_T1_2024ann")
        with self.assertRaises(RuntimeError) as ctx:
            capped_gradual_selection(
                None, chunk, {}, lambda *a: None, lambda: None,
            )
        message = str(ctx.exception)
        self.assertIn("MRS_T1_2024ann", message)
        self.assertIn("no tie points after alignment", message)

    def test_empty_points_list_raises_runtime_error(self):
        chunk = _FakeChunk(tie_points=_FakeTiePoints(points=[]), label="MRS_T1_2025_pbl")
        with self.assertRaises(RuntimeError) as ctx:
            capped_gradual_selection(
                None, chunk, {}, lambda *a: None, lambda: None,
            )
        self.assertIn("MRS_T1_2025_pbl", str(ctx.exception))

    def test_tie_points_object_with_none_points_attribute_raises(self):
        # The shape actually observed in the pass2 reproduction: chunk.tie_points
        # is a real object, but its own .points is None.
        chunk = _FakeChunk(tie_points=_FakeTiePoints(points=None))
        with self.assertRaises(RuntimeError):
            capped_gradual_selection(
                None, chunk, {}, lambda *a: None, lambda: None,
            )

    def test_guard_does_not_fire_on_a_populated_tie_point_cloud(self):
        # A non-empty tie_points must reach the real filtering loop (which
        # then fails on the deliberately-empty cfg dict) rather than being
        # rejected by the guard - proves the guard is not over-broad.
        chunk = _FakeChunk(tie_points=_FakeTiePoints(points=[object(), object()]))
        with self.assertRaises(KeyError):
            capped_gradual_selection(
                None, chunk, {}, lambda *a: None, lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
