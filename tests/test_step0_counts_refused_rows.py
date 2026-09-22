"""A registry row step 0 cannot start must be counted, not dropped.

On 2026-09-07 a Voyager 1 turn was given four timepoints of one transect. One
of them still held its video in unmerged parts, so `identity_for` refused it.
step0 skipped that row with `continue`, which took it out of the run's own
denominator: it extracted three, logged "3/3", exited zero, left the registry
row sitting on its "extracting" stage, and the Carousel moved on. Nothing
anywhere counted the fourth, so nothing downstream could have known.

These tests hold the count. They also hold the rule that a lone part is the
whole recording (Lauren, 2026-09-11): a row naming a single part-numbered file
is worked, not refused, and the part number is noted. They stage step0 against
a throwaway project folder and a throwaway registry exactly as
tests/test_progress_logging.py does; no ffmpeg runs and no video file is ever
opened.

Run from github_repo:  python3 tests/test_step0_counts_refused_rows.py
"""
import contextlib
import importlib
import io
import os
import shutil
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
  name: "refused row test"
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
"""

MERGED = "TCRMP20231015_3D_MRS_T1.MOV"
UNMERGED = "TCRMP20240412_3D_MRS_T1_1.MOV;TCRMP20240412_3D_MRS_T1_2.MOV"
# Meri Shoal transect 3, 2023 annual: the only readable archive file of
# that recording carries a part number. A lone part is the whole recording
# (Lauren, 2026-09-11), so this row is worked, not refused.
LONE_PART = "TCRMP20231207_demo_MRS_T3_part1.MP4"


def load_step0(project_dir):
    """Import (or re-import) step0 against `project_dir`, the way this module's
    other suites stage it: config.py reads the project directory from argv at
    import time, and step1's Metashape import is stubbed."""
    if "Metashape" not in sys.modules:
        sys.modules["Metashape"] = types.ModuleType("Metashape")
    saved_argv = sys.argv[:]
    sys.argv = ["step0", project_dir]
    try:
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])
        else:
            import config  # noqa: F401  (import-time side effects are the point)
        if "step0" in sys.modules:
            return importlib.reload(sys.modules["step0"])
        return importlib.import_module("step0")
    finally:
        sys.argv = saved_argv


class RefusedRowTestCase(unittest.TestCase):
    """One throwaway project folder and one fake registry per test."""

    def setUp(self):
        self.project_dir = tempfile.mkdtemp(prefix="refused_rows_")
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)
        with open(os.path.join(self.project_dir, "analysis_params.yaml"), "w") as fh:
            fh.write(MINIMAL_PARAMS)
        self.step0 = load_step0(self.project_dir)
        self.updates = []

    def _row(self, readable_id, original_videos):
        return {"readable_id": readable_id, "original_videos": original_videos,
                "processing_location": self.project_dir,
                "video_location": self.project_dir}

    def _stub_registry(self, rows):
        """Point step0's registry client at `rows` and record every update."""
        client = self.step0.registry_client
        patches = [
            mock.patch.object(client, "rows_for", return_value=rows),
            mock.patch.object(client, "enabled", return_value=True),
            mock.patch.object(client, "update",
                              side_effect=lambda rid, **kw: self.updates.append((rid, kw))),
            mock.patch.object(client, "configure", return_value=None),
            mock.patch.object(client, "row", side_effect=lambda rid: next(
                (r for r in rows if r["readable_id"] == rid), None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class WorkItemsKeepRefusedRows(RefusedRowTestCase):
    def test_an_unmerged_row_comes_back_as_refused_not_dropped(self):
        self._stub_registry([self._row("MRS_T1_2024_pbl", UNMERGED)])
        items, refused = self.step0._tcrmp_work_items()
        self.assertEqual(items, [])
        self.assertEqual(len(refused), 1, refused)
        self.assertEqual(refused[0][0], "MRS_T1_2024_pbl")
        self.assertIn("multi-part", refused[0][1])

    def test_good_rows_still_come_back_as_work(self):
        self._stub_registry([self._row("MRS_T1_2023ann", MERGED)])
        items, refused = self.step0._tcrmp_work_items()
        self.assertEqual(refused, [])
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0][0], "MRS_T1_2023ann")

    def test_a_mixed_folder_splits_without_losing_anyone(self):
        self._stub_registry([self._row("MRS_T1_2023ann", MERGED),
                             self._row("MRS_T1_2024_pbl", UNMERGED)])
        items, refused = self.step0._tcrmp_work_items()
        self.assertEqual([i[0] for i in items], ["MRS_T1_2023ann"])
        self.assertEqual([r[0] for r in refused], ["MRS_T1_2024_pbl"])

    def test_a_lone_part_row_is_worked_and_the_part_number_is_noted(self):
        # Until 2026-09-11 step 0 refused this row too. The refusal that paused
        # the first Carousel batch (four timepoints of Meri Shoal transect
        # 3, all for this one file) was prep's, at the ingest stage before
        # step 0 ran. Now the row is worked like any other, and the part
        # number goes to the step 0 log (a WARNING) and the console (a NOTE
        # line) so the fact is never lost.
        self._stub_registry([self._row("MRS_T3_2023ann", LONE_PART)])
        console = io.StringIO()
        with contextlib.redirect_stdout(console), \
             self.assertLogs(level="WARNING") as logged:
            items, refused = self.step0._tcrmp_work_items()
        self.assertEqual(refused, [])
        self.assertEqual([i[0] for i in items], ["MRS_T3_2023ann"])
        self.assertEqual(items[0][1], [os.path.join(self.project_dir, LONE_PART)])
        note = self.step0.lone_part_note(LONE_PART, "MRS_T3_2023ann")
        self.assertIn(f"NOTE: {note}", console.getvalue())
        self.assertTrue(any(note in line for line in logged.output), logged.output)

    def test_a_standard_named_row_gets_no_note(self):
        self._stub_registry([self._row("MRS_T1_2023ann", MERGED)])
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            items, refused = self.step0._tcrmp_work_items()
        self.assertEqual(len(items), 1)
        self.assertNotIn("NOTE:", console.getvalue())
        self.assertNotIn("lone part", console.getvalue())

    def test_a_folder_with_all_three_kinds_splits_by_the_rule(self):
        # Merged: worked. Lone part: worked and noted. ";"-joined parts: refused.
        self._stub_registry([self._row("MRS_T3_2023ann", LONE_PART),
                             self._row("MRS_T3_2024_pbl", UNMERGED),
                             self._row("MRS_T3_2025_pbl", MERGED)])
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            items, refused = self.step0._tcrmp_work_items()
        self.assertEqual([i[0] for i in items], ["MRS_T3_2023ann", "MRS_T3_2025_pbl"])
        self.assertEqual([r[0] for r in refused], ["MRS_T3_2024_pbl"])
        self.assertIn("multi-part", refused[0][1])
        self.assertEqual(console.getvalue().count("NOTE:"), 1, console.getvalue())

    def test_rows_of_another_processing_folder_are_not_this_run_s_problem(self):
        other = dict(self._row("BPT_T3_2023ann", UNMERGED), processing_location="/somewhere/else_3d")
        self._stub_registry([other])
        items, refused = self.step0._tcrmp_work_items()
        self.assertEqual((items, refused), ([], []))


class TheRunCountsWhatItWasGiven(RefusedRowTestCase):
    def _run_main(self, rows, per_timepoint):
        """main() with process_timepoint stubbed. Returns the SystemExit code
        (None when main returned normally) and everything it printed."""
        self._stub_registry(rows)
        printed = []
        code = None
        with mock.patch.object(self.step0, "process_timepoint",
                               side_effect=lambda rid, paths, row=None, tcrmp=False:
                                   (rid, per_timepoint)), \
             mock.patch.object(self.step0, "checkpoint_pause", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            try:
                self.step0.main()
            except SystemExit as exc:
                code = exc.code
        return code, "\n".join(printed)

    def test_three_good_and_one_refused_is_reported_as_one_of_four_failing(self):
        rows = [self._row(f"MRS_T1_{y}ann", MERGED) for y in (2021, 2022, 2023)]
        rows.append(self._row("MRS_T1_2024_pbl", UNMERGED))
        code, printed = self._run_main(rows, per_timepoint=True)
        # A partial run still succeeds (the three that extracted are real work
        # step 1 can pick up), but it says so, and it says out of FOUR.
        self.assertIsNone(code, printed)
        self.assertIn("1 of 4 timepoint(s) failed", printed)
        self.assertIn("MRS_T1_2024_pbl", printed)

    def test_the_refused_row_is_closed_out_in_the_registry(self):
        rows = [self._row("MRS_T1_2023ann", MERGED), self._row("MRS_T1_2024_pbl", UNMERGED)]
        self._run_main(rows, per_timepoint=True)
        stages = [(rid, kw.get("stage")) for rid, kw in self.updates if "stage" in kw]
        self.assertIn(("MRS_T1_2024_pbl", "failed"), stages,
                      "the refused row keeps its live stage, so the atlas shows a run that never ends")

    def test_every_row_refused_fails_the_run_without_blaming_the_videos(self):
        rows = [self._row("MRS_T1_2024_pbl", UNMERGED)]
        code, printed = self._run_main(rows, per_timepoint=True)
        self.assertEqual(code, 1, printed)
        self.assertNotIn("no videos to extract frames from", printed,
                         "the videos are on the disk; the row is unmerged, and saying otherwise "
                         "sends the reader to look in the wrong place")
        self.assertIn("multi-part", printed)

    def test_an_empty_folder_still_reports_nothing_to_do(self):
        code, printed = self._run_main([], per_timepoint=True)
        self.assertEqual(code, 1, printed)
        self.assertIn("no videos to extract frames from", printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
