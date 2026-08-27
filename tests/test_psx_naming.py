"""Tests for the psx-naming helpers at the top of src/step1.py (Task 11).

step1.py imports Metashape at module scope and config.py reads the project
directory from sys.argv[1] with import-time side effects, so both are staged
here (a stub module for Metashape, a throwaway project directory for config)
before step1 is imported. The helpers under test are pure: they take the
project directory, the identity parts, and a `count_chunks(path)` callable,
so no Metashape document is ever opened.

Run from github_repo:  python3 -m unittest tests.test_psx_naming -v
"""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")
LIB_DIR = os.path.join(
    os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius"), "_METADATA", "3d")
for path in (SRC, LIB_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

# Smallest analysis_params.yaml config.py accepts. Deliberately inline rather
# than copied from the repo file so this test exercises step1's helpers, not
# whatever the shipped parameter set happens to hold.
MINIMAL_PARAMS = """
project:
  name: "psx naming test"
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


def load_step1(project_dir):
    """Import (or re-import) src/step1.py against `project_dir`."""
    if "Metashape" not in sys.modules:
        sys.modules["Metashape"] = types.ModuleType("Metashape")
    saved_argv = sys.argv[:]
    sys.argv = ["step1", project_dir]
    try:
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])
        else:
            import config  # noqa: F401  (import-time side effects are the point)
        if "step1" in sys.modules:
            return importlib.reload(sys.modules["step1"])
        import step1
        return step1
    finally:
        sys.argv = saved_argv


def touch_psx(project_dir, name):
    """Create a fake psx bundle: the .psx file plus its sibling .files dir."""
    psx_path = os.path.join(project_dir, name)
    with open(psx_path, "w") as fh:
        fh.write("<document/>\n")
    files_dir = psx_path[:-len(".psx")] + ".files"
    os.makedirs(files_dir, exist_ok=True)
    with open(os.path.join(files_dir, "project.zip"), "w") as fh:
        fh.write("payload\n")
    return psx_path


class FakeChunk:
    def __init__(self, label):
        self.label = label


class FakeDoc:
    def __init__(self, labels):
        self.chunks = [FakeChunk(label) for label in labels]


class PsxHelperTestCase(unittest.TestCase):
    """Base: one throwaway project dir for the step1 import, one per test for
    the psx bundles under test."""

    @classmethod
    def setUpClass(cls):
        cls.config_dir = tempfile.mkdtemp(prefix="step1_cfg_")
        with open(os.path.join(cls.config_dir, "analysis_params.yaml"), "w") as fh:
            fh.write(MINIMAL_PARAMS)
        cls.step1 = load_step1(cls.config_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.config_dir, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="step1_psx_")
        self.seen = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def counter(self, counts):
        """count_chunks stand-in: path base name -> chunk count (default 0)."""
        def count_chunks(path):
            self.seen.append(os.path.basename(path))
            return counts.get(os.path.basename(path), 0)
        return count_chunks


class CurrentPsxTests(PsxHelperTestCase):
    def test_empty_folder_starts_a_single_year_psx(self):
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2023, 4, self.counter({}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2023_2023.psx"))
        self.assertTrue(is_new)
        self.assertEqual(self.seen, [])  # nothing to count in an empty folder

    def test_reuses_range_psx_under_the_cap(self):
        existing = touch_psx(self.tmp, "MRS_T1_2023_2024.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2025, 4, self.counter({"MRS_T1_2023_2024.psx": 2}))
        self.assertEqual(path, existing)
        self.assertFalse(is_new)
        self.assertEqual(self.seen, ["MRS_T1_2023_2024.psx"])

    def test_starts_a_new_psx_at_the_cap(self):
        touch_psx(self.tmp, "MRS_T1_2023_2024.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2025, 4, self.counter({"MRS_T1_2023_2024.psx": 4}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2025_2025.psx"))
        self.assertTrue(is_new)

    def test_newest_range_psx_wins(self):
        touch_psx(self.tmp, "MRS_T1_2020_2021.psx")
        newest = touch_psx(self.tmp, "MRS_T1_2022_2024.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2025, 4,
            self.counter({"MRS_T1_2020_2021.psx": 4, "MRS_T1_2022_2024.psx": 3}))
        self.assertEqual(path, newest)
        self.assertFalse(is_new)
        self.assertEqual(self.seen, ["MRS_T1_2022_2024.psx"])  # older ones never consulted

    def test_other_site_and_transect_ignored(self):
        touch_psx(self.tmp, "KGC_T2_2023_2024.psx")
        touch_psx(self.tmp, "MRS_T2_2023_2024.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2023, 4, self.counter({}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2023_2023.psx"))
        self.assertTrue(is_new)
        self.assertEqual(self.seen, [])

    def test_non_range_names_ignored(self):
        touch_psx(self.tmp, "psx_1_20260826.psx")
        touch_psx(self.tmp, "MRS_T1_2023_2023.psx.corrupt_20260826_120000")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2024, 4, self.counter({}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2024_2024.psx"))
        self.assertTrue(is_new)

    def test_cap_one_never_appends(self):
        touch_psx(self.tmp, "MRS_T1_2023_2023.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2024, 1, self.counter({"MRS_T1_2023_2023.psx": 1}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2024_2024.psx"))
        self.assertTrue(is_new)

    def test_new_name_collision_gets_a_numbered_sibling(self):
        # A full psx already sitting on the name a new one would take (two
        # timepoints of the same year with cap 1) must not be opened and
        # appended to; it gets a numbered sibling instead.
        touch_psx(self.tmp, "MRS_T1_2024_2024.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2024, 1, self.counter({"MRS_T1_2024_2024.psx": 1}))
        self.assertEqual(path, os.path.join(self.tmp, "MRS_T1_2024_2024_2.psx"))
        self.assertTrue(is_new)

    def test_unopenable_psx_counts_as_empty_and_is_reused(self):
        # count_chunks returns 0 for a bundle it cannot open; the run reuses
        # the path so process_model's quarantine path handles it.
        existing = touch_psx(self.tmp, "MRS_T1_2023_2023.psx")
        path, is_new = self.step1.current_psx(
            self.tmp, "MRS", "T1", 2024, 4, self.counter({}))
        self.assertEqual(path, existing)
        self.assertFalse(is_new)


class RenamePsxRangeTests(PsxHelperTestCase):
    def test_moves_both_halves(self):
        path = touch_psx(self.tmp, "MRS_T1_2023_2023.psx")
        new_path = self.step1.rename_psx_range(path, [2023, 2024])
        self.assertEqual(new_path, os.path.join(self.tmp, "MRS_T1_2023_2024.psx"))
        self.assertTrue(os.path.isfile(new_path))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "MRS_T1_2023_2024.files")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp, "MRS_T1_2023_2024.files", "project.zip")))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "MRS_T1_2023_2023.files")))

    def test_no_op_when_the_name_already_matches(self):
        path = touch_psx(self.tmp, "MRS_T1_2023_2025.psx")
        self.assertEqual(self.step1.rename_psx_range(path, [2023, 2024, 2025]), path)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "MRS_T1_2023_2025.files")))

    def test_no_op_on_a_non_range_name(self):
        path = touch_psx(self.tmp, "psx_1_20260826.psx")
        self.assertEqual(self.step1.rename_psx_range(path, [2023, 2024]), path)
        self.assertTrue(os.path.isfile(path))

    def test_no_op_without_years(self):
        path = touch_psx(self.tmp, "MRS_T1_2023_2023.psx")
        self.assertEqual(self.step1.rename_psx_range(path, []), path)
        self.assertTrue(os.path.isfile(path))

    def test_no_op_when_the_target_name_is_taken(self):
        path = touch_psx(self.tmp, "MRS_T1_2023_2023.psx")
        touch_psx(self.tmp, "MRS_T1_2023_2024.psx")
        self.assertEqual(self.step1.rename_psx_range(path, [2023, 2024]), path)
        self.assertTrue(os.path.isfile(path))

    def test_missing_files_dir_is_tolerated(self):
        path = os.path.join(self.tmp, "MRS_T1_2023_2023.psx")
        with open(path, "w") as fh:
            fh.write("<document/>\n")
        new_path = self.step1.rename_psx_range(path, [2023, 2024])
        self.assertEqual(new_path, os.path.join(self.tmp, "MRS_T1_2023_2024.psx"))
        self.assertTrue(os.path.isfile(new_path))


class YearsInPsxTests(PsxHelperTestCase):
    def test_years_from_chunk_labels(self):
        doc = FakeDoc(["MRS_T1_2023ann", "MRS_T1_2024_pbl", "MRS_T1_2024ann"])
        self.assertEqual(self.step1.years_in_psx(doc), [2023, 2024])

    def test_unparseable_labels_ignored(self):
        doc = FakeDoc(["Chunk 1", "GOPR0001", "MRS_T1_2025_pbl", ""])
        self.assertEqual(self.step1.years_in_psx(doc), [2025])

    def test_empty_document(self):
        self.assertEqual(self.step1.years_in_psx(FakeDoc([])), [])


class PsxRangePartsTests(PsxHelperTestCase):
    def test_parses_a_range_name(self):
        self.assertEqual(
            self.step1.psx_range_parts("MRS_T1_2023_2025.psx"), ("MRS", "T1", 2023, 2025))

    def test_rejects_other_names(self):
        for name in ("psx_1_20260826.psx", "MRS_T1_2023ann.psx", "MRS_T1_2023_2025.psx.corrupt_1",
                     "MRS_2023_2025.psx", "notes.txt"):
            self.assertIsNone(self.step1.psx_range_parts(name), name)


if __name__ == "__main__":
    unittest.main()
