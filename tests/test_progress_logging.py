"""Tests for the throttled progress logging added to step 1 and step 0.

step1._progress_logger is the callback factory handed to Metashape's long
build calls (matchPhotos, alignCameras, buildDepthMaps, buildModel, buildDem,
buildUV, buildTexture): it must log at most once per interval, always log
100 percent exactly once, and never let an exception escape into Metashape's
C++ caller. step0._extraction_progress is the per-video frame-count reporter
the ffmpeg wait loop calls about every 30 seconds.

step1.py imports Metashape at module scope and config.py reads the project
directory from sys.argv[1] with import-time side effects, so both are staged
here the same way tests/test_psx_naming.py does it.

Run from github_repo:  python3 -m unittest tests.test_progress_logging -v
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")
LIB_DIR = os.path.join(
    os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius"), "_METADATA", "3d")
for path in (SRC, LIB_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

MINIMAL_PARAMS = """
project:
  name: "progress logging test"
  notes: ""
processing:
  tcrmp: true
  frames_per_transect: 10
  chunk_size: 1000
  use_gpu: false
  max_chunks_per_psx: 4
  metashape:
    defaults:
      downscale: 1
      keypoint_limit: 40000
      tiepoint_limit: 0
      reconstruction_uncertainty: 15
      projection_accuracy: 5
      reprojection_error: 0.5
      depth_downscale: 1
  step1_products:
    decimation_factor: 10
    smooth_strength: 4
  model_processing:
    scale_bars:
      - start_marker: "target 1000"
        end_marker: "target 1010"
        distance: 0.75
"""


def load_module(module_name, project_dir):
    """Import (or re-import) a src module against `project_dir`."""
    if "Metashape" not in sys.modules:
        sys.modules["Metashape"] = types.ModuleType("Metashape")
    saved_argv = sys.argv[:]
    sys.argv = [module_name, project_dir]
    try:
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])
        else:
            import config  # noqa: F401  (import-time side effects are the point)
        if module_name in sys.modules:
            return importlib.reload(sys.modules[module_name])
        return importlib.import_module(module_name)
    finally:
        sys.argv = saved_argv


class ProgressTestCase(unittest.TestCase):
    """Base: one throwaway project dir shared by the module imports."""

    @classmethod
    def setUpClass(cls):
        cls.config_dir = tempfile.mkdtemp(prefix="progress_cfg_")
        with open(os.path.join(cls.config_dir, "analysis_params.yaml"), "w") as fh:
            fh.write(MINIMAL_PARAMS)
        cls.step1 = load_module("step1", cls.config_dir)
        cls.step0 = load_module("step0", cls.config_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.config_dir, ignore_errors=True)


class ProgressLoggerThrottleTests(ProgressTestCase):
    """step1._progress_logger: the throttle contract."""

    def test_two_calls_one_second_apart_log_once(self):
        callback = self.step1._progress_logger("Building mesh for TEST", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0, 1.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(10)
                callback(11)
        self.assertEqual(len(caught.records), 1)
        self.assertIn("Building mesh for TEST: 10%", caught.records[0].getMessage())

    def test_two_calls_thirty_one_seconds_apart_log_twice(self):
        callback = self.step1._progress_logger("Building mesh for TEST", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0, 31.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(10)
                callback(55)
        messages = [r.getMessage() for r in caught.records]
        self.assertEqual(len(messages), 2)
        self.assertIn("Building mesh for TEST: 10%", messages[0])
        self.assertIn("Building mesh for TEST: 55%", messages[1])

    def test_exactly_thirty_seconds_apart_logs_twice(self):
        callback = self.step1._progress_logger("depth maps", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0, 30.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(5)
                callback(60)
        self.assertEqual(len(caught.records), 2)

    def test_suppressed_call_does_not_reset_the_timer(self):
        # t=0 logs, t=29 is suppressed, t=31 must still log (31 s since the
        # last LOGGED line, not since the suppressed one).
        callback = self.step1._progress_logger("texture", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0, 29.0, 31.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(10)
                callback(20)
                callback(30)
        messages = [r.getMessage() for r in caught.records]
        self.assertEqual(len(messages), 2)
        self.assertIn("10%", messages[0])
        self.assertIn("30%", messages[1])

    def test_hundred_percent_always_logs_even_inside_the_interval(self):
        callback = self.step1._progress_logger("aligning", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0, 1.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(50)
                callback(100)
        messages = [r.getMessage() for r in caught.records]
        self.assertEqual(len(messages), 2)
        self.assertIn("aligning: 100%", messages[1])

    def test_hundred_percent_logs_only_once(self):
        callback = self.step1._progress_logger("aligning", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(100)
                callback(100)
                callback(100)
        self.assertEqual(len(caught.records), 1)
        self.assertIn("aligning: 100%", caught.records[0].getMessage())

    def test_percentage_is_rounded(self):
        callback = self.step1._progress_logger("matching", min_interval_s=30)
        with mock.patch("time.monotonic", side_effect=[0.0]):
            with self.assertLogs(level="INFO") as caught:
                callback(12.6)
        self.assertIn("matching: 13%", caught.records[0].getMessage())

    def test_exception_inside_logging_never_escapes(self):
        callback = self.step1._progress_logger("mesh", min_interval_s=30)
        with mock.patch("time.monotonic", return_value=0.0):
            with mock.patch.object(self.step1.logging, "info",
                                   side_effect=RuntimeError("logging blew up")):
                callback(50)   # must not raise into Metashape's C++ caller
                callback(100)  # the 100 branch logs too; must not raise either

    def test_garbage_percentage_never_escapes(self):
        callback = self.step1._progress_logger("mesh", min_interval_s=30)
        callback(None)       # pct comparison raises TypeError internally
        callback("weird")    # swallowed the same way


class ExtractionProgressTests(ProgressTestCase):
    """step0._extraction_progress and the ffmpeg wait loop."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix="progress_frames_")

    def tearDown(self):
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _touch(self, name):
        with open(os.path.join(self.out_dir, name), "w") as fh:
            fh.write("x")

    def test_counts_only_new_tiff_frames(self):
        self._touch("old_0001.tiff")
        existing = set(os.listdir(self.out_dir))
        self._touch("GOPR001_0001.tiff")
        self._touch("GOPR001_0002.tiff")
        self._touch("notes.txt")  # not a frame
        report = self.step0._extraction_progress(
            "/videos/GOPR001.MP4", self.out_dir, existing)
        with self.assertLogs(level="INFO") as caught:
            report()
        self.assertIn("extracting GOPR001.MP4: 2 frames written",
                      caught.records[0].getMessage())

    def test_missing_output_dir_never_raises(self):
        report = self.step0._extraction_progress(
            "/videos/GOPR001.MP4", os.path.join(self.out_dir, "gone"), set())
        report()  # os.listdir fails inside; swallowed

    def test_wait_loop_reports_progress_between_timeouts(self):
        """_run_ffmpeg with progress_cb: each communicate() timeout ticks the
        callback once, and the final CompletedProcess carries the exit code."""
        ticks = []
        fake_proc = mock.Mock()
        fake_proc.returncode = 0
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
            (b"", b""),
        ]
        with mock.patch.object(self.step0.subprocess, "Popen", return_value=fake_proc):
            result = self.step0._run_ffmpeg(
                "/videos/GOPR001.MP4", os.path.join(self.out_dir, "f_%04d.tiff"),
                1.0, 1, [], progress_cb=lambda: ticks.append(1))
        self.assertEqual(len(ticks), 2)
        self.assertEqual(result.returncode, 0)

    def test_wait_loop_survives_a_failing_progress_callback(self):
        fake_proc = mock.Mock()
        fake_proc.returncode = 1
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30),
            (b"", b"boom"),
        ]

        def bad_cb():
            raise RuntimeError("progress callback blew up")

        with mock.patch.object(self.step0.subprocess, "Popen", return_value=fake_proc):
            result = self.step0._run_ffmpeg(
                "/videos/GOPR001.MP4", os.path.join(self.out_dir, "f_%04d.tiff"),
                1.0, 1, [], progress_cb=bad_cb)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"boom")


if __name__ == "__main__":
    unittest.main()
