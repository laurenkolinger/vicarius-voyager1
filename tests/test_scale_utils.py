"""Unit tests for scale_utils. Run from github_repo:  python3 -m unittest tests.test_scale_utils -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import scale_utils


class FakeVector:
    def __init__(self, x, y, z):
        self.v = (float(x), float(y), float(z))

    def __sub__(self, other):
        return FakeVector(*[a - b for a, b in zip(self.v, other.v)])

    def norm(self):
        return sum(a * a for a in self.v) ** 0.5


class FakeMatrix:
    def mulp(self, point):
        return point


class FakeTransform:
    matrix = FakeMatrix()


class FakeReference:
    def __init__(self):
        self.distance = None
        self.accuracy = None
        self.enabled = False


class FakeMarker:
    def __init__(self, label, position):
        self.label = label
        self.position = position


class FakePoint:
    def __init__(self, position):
        self.position = position


class FakeScalebar:
    def __init__(self, start, end):
        self.point0 = FakePoint(start.position)
        self.point1 = FakePoint(end.position)
        self.reference = FakeReference()


class FakeTargetType:
    CircularTarget20bit = "CircularTarget20bit"


class FakeMetashape:
    TargetType = FakeTargetType


class FakeChunk:
    def __init__(self, marker_specs):
        self._marker_specs = marker_specs
        self.markers = []
        self.scalebars = []
        self.transform = FakeTransform()
        self.detect_calls = []

    def detectMarkers(self, target_type=None, tolerance=None, filter_mask=None):
        self.detect_calls.append((target_type, tolerance, filter_mask))
        self.markers = [FakeMarker(label, FakeVector(*pos)) for label, pos in self._marker_specs]

    def remove(self, items):
        for item in items:
            self.markers.remove(item)

    def addScalebar(self, start, end):
        bar = FakeScalebar(start, end)
        self.scalebars.append(bar)
        return bar

    def updateTransform(self):
        pass


CONFIG = {
    "has_coded_scales": True,
    "remove_unlisted_markers": True,
    "scale_error_threshold": 0.009,
    "scale_bars": [
        {"start_marker": "target 1000", "end_marker": "target 1010", "distance": 0.75},
        {"start_marker": "target 1020", "end_marker": "target 1030", "distance": 0.75},
    ],
}


def quiet(_msg):
    pass


class ApplyScaleTests(unittest.TestCase):
    def test_two_bars_within_threshold_pass(self):
        chunk = FakeChunk([
            ("target 1000", (0.0, 0.0, 0.0)), ("target 1010", (0.752, 0.0, 0.0)),
            ("target 1020", (0.0, 1.0, 0.0)), ("target 1030", (0.748, 1.0, 0.0)),
        ])
        status, error = scale_utils.apply_scale(FakeMetashape, chunk, CONFIG, quiet)
        self.assertEqual(status, "PASS")
        self.assertAlmostEqual(error, 0.002, places=6)
        self.assertEqual(len(chunk.scalebars), 2)
        self.assertEqual(chunk.scalebars[0].reference.distance, 0.75)
        self.assertEqual(chunk.scalebars[0].reference.accuracy, 0.001)
        self.assertTrue(chunk.scalebars[0].reference.enabled)

    def test_error_above_threshold_fails(self):
        chunk = FakeChunk([
            ("target 1000", (0.0, 0.0, 0.0)), ("target 1010", (0.80, 0.0, 0.0)),
            ("target 1020", (0.0, 1.0, 0.0)), ("target 1030", (0.80, 1.0, 0.0)),
        ])
        status, error = scale_utils.apply_scale(FakeMetashape, chunk, CONFIG, quiet)
        self.assertEqual(status, "FAIL")
        self.assertAlmostEqual(error, 0.05, places=6)

    def test_single_bar_when_pair_missing(self):
        chunk = FakeChunk([
            ("target 1000", (0.0, 0.0, 0.0)), ("target 1010", (0.751, 0.0, 0.0)),
            ("target 1020", (0.0, 1.0, 0.0)),
        ])
        status, error = scale_utils.apply_scale(FakeMetashape, chunk, CONFIG, quiet)
        self.assertEqual(status, "PASS")
        self.assertEqual(len(chunk.scalebars), 1)
        self.assertAlmostEqual(error, 0.001, places=6)

    def test_no_markers_fails_with_sentinel(self):
        chunk = FakeChunk([])
        status, error = scale_utils.apply_scale(FakeMetashape, chunk, CONFIG, quiet)
        self.assertEqual(status, "FAIL")
        self.assertEqual(error, 999.0)

    def test_unlisted_markers_removed(self):
        chunk = FakeChunk([
            ("target 1000", (0.0, 0.0, 0.0)), ("target 1010", (0.75, 0.0, 0.0)),
            ("target 1020", (0.0, 1.0, 0.0)), ("target 1030", (0.75, 1.0, 0.0)),
            ("target 999", (5.0, 5.0, 0.0)),
        ])
        scale_utils.apply_scale(FakeMetashape, chunk, CONFIG, quiet)
        self.assertNotIn("target 999", [m.label for m in chunk.markers])

    def test_no_coded_scales_declared_skips(self):
        chunk = FakeChunk([("target 1000", (0.0, 0.0, 0.0))])
        status, error = scale_utils.apply_scale(
            FakeMetashape, chunk, {"has_coded_scales": False, "scale_bars": []}, quiet)
        self.assertEqual(status, "FAIL")
        self.assertEqual(error, 999.0)
        self.assertEqual(chunk.detect_calls, [])


class FakePixel:
    """2-component pixel-space vector standing in for the Metashape.Vector
    that camera.project() returns (x, y only -- no third component).

    __sub__ mirrors real Metashape.Vector: subtracting a vector of a
    different size raises, it does not silently truncate. That is what
    made "projected - coord" a live bug against the real 3-component
    marker.projections[camera].coord: these fakes must reproduce the same
    failure so the tests would have caught it."""

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.size = 2

    def __sub__(self, other):
        if getattr(other, "size", 2) != self.size:
            raise ValueError(
                f"different vector dimensions: {self.size} vs {other.size}"
            )
        return FakePixel(self.x - other.x, self.y - other.y)

    def norm(self):
        return (self.x ** 2 + self.y ** 2) ** 0.5


class FakeMarkerCoord:
    """3-component vector standing in for the Metashape.Vector that
    marker.projections[camera].coord holds in Metashape 2.x (x, y, and a
    third component beyond pixel space). Carries `size` so FakePixel.__sub__
    can detect the mismatch the same way real Metashape.Vector does."""

    def __init__(self, x, y, z=0.0):
        self.x = x
        self.y = y
        self.z = z
        self.size = 3

    def norm(self):
        return (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5


class FakeMarkerProjection:
    def __init__(self, coord):
        self.coord = coord
        self.valid = True


class FakeProjectingCamera:
    """Camera whose project() always returns the origin (2-component); error
    is then just the pixel-plane norm of the recorded projection coord,
    which keeps test fixtures readable (set the desired pixel error
    directly as the projection)."""

    def __init__(self, label, transform=True):
        self.label = label
        self.transform = transform
        self.project_calls = 0

    def project(self, point):
        self.project_calls += 1
        return FakePixel(0.0, 0.0)


class FakeMarkerForProjections:
    def __init__(self, label, projections):
        self.label = label
        self.position = None  # unused by FakeProjectingCamera.project
        self.projections = projections


class FakeChunkForProjections:
    def __init__(self, markers):
        self.markers = markers


def quiet_log_capture():
    messages = []
    return messages, messages.append


class PruneMarkerProjectionsTests(unittest.TestCase):
    def test_prunes_worst_until_under_threshold(self):
        # 8 cameras; two errors (2.0, 1.5) sit above the 0.8px cap, the rest
        # are already under it. Pruning should stop as soon as the cap is
        # satisfied, well above the min_projections=5 floor (6 remain).
        errs = [2.0, 1.5, 0.79, 0.5, 0.4, 0.3, 0.2, 0.1]
        cams = [FakeProjectingCamera(f"cam{i}") for i in range(len(errs))]
        projections = {cam: FakeMarkerProjection(FakeMarkerCoord(e, 0.0)) for cam, e in zip(cams, errs)}
        marker = FakeMarkerForProjections("target 1000", projections)
        chunk = FakeChunkForProjections([marker])
        messages, log = quiet_log_capture()

        scale_utils.prune_marker_projections(None, chunk, log, max_error_px=0.8, min_projections=5)

        remaining = [c for c in cams if marker.projections[c] is not None]
        self.assertEqual(len(remaining), 6)
        self.assertNotIn(cams[0], remaining)  # the 2.0px projection
        self.assertNotIn(cams[1], remaining)  # the 1.5px projection
        self.assertIsNone(marker.projections[cams[0]])
        self.assertIsNone(marker.projections[cams[1]])
        remaining_errors = [projections[c].coord.norm() for c in remaining]
        self.assertLessEqual(max(remaining_errors), 0.8)
        self.assertTrue(any("pruned 2 projection" in m for m in messages))

    def test_respects_min_projections_floor(self):
        # 6 cameras, all above the 0.8px cap. Pruning must stop once exactly
        # min_projections=5 remain, even though the worst survivor is still
        # over threshold.
        errs = [3.0, 2.5, 2.0, 1.5, 1.2, 0.9]
        cams = [FakeProjectingCamera(f"cam{i}") for i in range(len(errs))]
        projections = {cam: FakeMarkerProjection(FakeMarkerCoord(e, 0.0)) for cam, e in zip(cams, errs)}
        marker = FakeMarkerForProjections("target 1010", projections)
        chunk = FakeChunkForProjections([marker])
        messages, log = quiet_log_capture()

        scale_utils.prune_marker_projections(None, chunk, log, max_error_px=0.8, min_projections=5)

        remaining = [c for c in cams if marker.projections[c] is not None]
        self.assertEqual(len(remaining), 5)
        self.assertIsNone(marker.projections[cams[0]])  # only the single worst (3.0) removed
        self.assertTrue(any("pruned 1 projection" in m for m in messages))

    def test_skips_unaligned_cameras(self):
        # An unaligned camera (transform=None) carries a huge would-be error
        # but must be ignored entirely: not projected, not counted toward
        # min_projections, and never pruned.
        aligned = [FakeProjectingCamera("cam_a", transform=True),
                   FakeProjectingCamera("cam_b", transform=True)]
        unaligned = FakeProjectingCamera("cam_unaligned", transform=None)
        projections = {
            aligned[0]: FakeMarkerProjection(FakeMarkerCoord(0.1, 0.0)),
            aligned[1]: FakeMarkerProjection(FakeMarkerCoord(0.2, 0.0)),
            unaligned: FakeMarkerProjection(FakeMarkerCoord(50.0, 0.0)),
        }
        marker = FakeMarkerForProjections("target 1020", projections)
        chunk = FakeChunkForProjections([marker])
        messages, log = quiet_log_capture()

        scale_utils.prune_marker_projections(None, chunk, log, max_error_px=0.8, min_projections=1)

        self.assertEqual(unaligned.project_calls, 0)
        self.assertIs(marker.projections[unaligned], projections[unaligned])
        self.assertIsNotNone(marker.projections[unaligned])
        for cam in aligned:
            self.assertIsNotNone(marker.projections[cam])
        self.assertTrue(any("no pruning needed" in m for m in messages))

    def test_one_markers_failure_does_not_abort_the_rest(self):
        # A marker whose projection blows up (e.g. camera.project raises)
        # must be logged and skipped, not allowed to abort pruning for the
        # other markers in the chunk.
        good_cam = FakeProjectingCamera("cam_good")
        good_projections = {good_cam: FakeMarkerProjection(FakeMarkerCoord(2.0, 0.0))}
        good_marker = FakeMarkerForProjections("target 1000", good_projections)

        class ExplodingCamera(FakeProjectingCamera):
            def project(self, point):
                raise RuntimeError("boom")

        bad_cam = ExplodingCamera("cam_bad")
        bad_projections = {bad_cam: FakeMarkerProjection(FakeMarkerCoord(0.1, 0.0))}
        bad_marker = FakeMarkerForProjections("target 1010", bad_projections)

        chunk = FakeChunkForProjections([bad_marker, good_marker])
        messages, log = quiet_log_capture()

        scale_utils.prune_marker_projections(
            None, chunk, log, max_error_px=0.8, min_projections=0
        )

        # The failing marker's projection is left untouched (not pruned).
        self.assertIs(bad_marker.projections[bad_cam], bad_projections[bad_cam])
        # The good marker after it was still processed and pruned normally.
        self.assertIsNone(good_marker.projections[good_cam])
        self.assertTrue(any("WARNING" in m and "target 1010" in m for m in messages))


class TexturePagesTests(unittest.TestCase):
    def test_kgc_t2_area_is_one_page(self):
        self.assertEqual(scale_utils.compute_texture_pages(12.3, 0.0005, 8192, True, 4), 1)

    def test_large_area_multiple_pages(self):
        self.assertEqual(scale_utils.compute_texture_pages(100.0, 0.0005, 8192, True, 4), 6)

    def test_unscaled_uses_fixed_pages(self):
        self.assertEqual(scale_utils.compute_texture_pages(12.3, 0.0005, 8192, False, 4), 4)

    def test_zero_area_falls_back_to_one_page(self):
        self.assertEqual(scale_utils.compute_texture_pages(0.0, 0.0005, 8192, True, 4), 1)


if __name__ == "__main__":
    unittest.main()
