"""Section-aware analysis_params.yaml editing and the --param surface
(final review I1), plus the manual-edit guard's decision helper (C1).

`run_phase1.set_yaml_path` replaced a regex that matched a key at any nesting
depth, so the collision that regex could not tell apart
(processing.metashape.defaults.smooth_strength versus
processing.step1_products.smooth_strength) is pinned here first.
"""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "src")
LIB_DIR = os.path.join(
    os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius"), "_METADATA", "3d")
for path in (SRC, LIB_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_phase1  # noqa: E402

# Two keys of the same name at different nesting, each with its own trailing
# comment: the exact shape the old any-depth regex could not distinguish.
COLLIDING = """\
# header comment that must survive

project:
  name: "collision test"

processing:
  tcrmp: true # registry-driven mode

  metashape:
    defaults:
      smooth_strength: 1 # smoothing on the full mesh
      downscale: 1 # matching resolution

  step1_products:
    smooth_strength: 4 # smoothing on the delivery mesh
    decimation_factor: 10 # delivery mesh divisor
"""


class SetYamlPathTests(unittest.TestCase):
    def _load(self, text):
        return yaml.safe_load(text)

    def test_nested_key_of_the_same_name_is_not_touched(self):
        out = run_phase1.set_yaml_path(
            COLLIDING, "processing.step1_products.smooth_strength", "9")
        tree = self._load(out)
        self.assertEqual(tree["processing"]["step1_products"]["smooth_strength"], 9)
        self.assertEqual(tree["processing"]["metashape"]["defaults"]["smooth_strength"], 1)

    def test_the_other_half_of_the_collision_edits_only_itself(self):
        out = run_phase1.set_yaml_path(
            COLLIDING, "processing.metashape.defaults.smooth_strength", "3")
        tree = self._load(out)
        self.assertEqual(tree["processing"]["metashape"]["defaults"]["smooth_strength"], 3)
        self.assertEqual(tree["processing"]["step1_products"]["smooth_strength"], 4)

    def test_comments_and_other_keys_survive(self):
        out = run_phase1.set_yaml_path(
            COLLIDING, "processing.step1_products.smooth_strength", "9")
        self.assertIn("# header comment that must survive", out)
        self.assertIn("# smoothing on the delivery mesh", out)
        self.assertIn("# smoothing on the full mesh", out)
        self.assertEqual(self._load(out)["processing"]["step1_products"]["decimation_factor"], 10)
        self.assertEqual(self._load(out)["project"]["name"], "collision test")

    def test_missing_key_is_created_under_its_parent(self):
        out = run_phase1.set_yaml_path(COLLIDING, "processing.force_rerun", "true")
        tree = self._load(out)
        self.assertIs(tree["processing"]["force_rerun"], True)
        self.assertIs(tree["processing"]["tcrmp"], True)

    def test_missing_parents_are_created_too(self):
        out = run_phase1.set_yaml_path(COLLIDING, "processing.newsection.newkey", "5")
        self.assertEqual(self._load(out)["processing"]["newsection"]["newkey"], 5)

    def test_missing_top_level_section_is_appended(self):
        out = run_phase1.set_yaml_path("project:\n  name: \"x\"\n", "processing.tcrmp", "true")
        tree = self._load(out)
        self.assertIs(tree["processing"]["tcrmp"], True)
        self.assertEqual(tree["project"]["name"], "x")

    def test_quoted_value_with_a_hash_is_not_read_as_a_comment(self):
        text = 'processing:\n  video_input_dir: "/old/path" # where the videos live\n'
        out = run_phase1.set_yaml_path(
            text, "processing.video_input_dir", '"/new/path # 2"')
        self.assertEqual(self._load(out)["processing"]["video_input_dir"], "/new/path # 2")
        self.assertIn("# where the videos live", out)

    def test_the_real_template_round_trips(self):
        text = Path(REPO, "analysis_params.yaml").read_text()
        out = run_phase1.set_yaml_path(text, "processing.frames_per_transect", "300")
        out = run_phase1.set_yaml_path(
            out, "processing.model_processing.scale_error_threshold", "0.02")
        tree = self._load(out)
        self.assertEqual(tree["processing"]["frames_per_transect"], 300)
        self.assertEqual(tree["processing"]["model_processing"]["scale_error_threshold"], 0.02)
        self.assertEqual(len(tree["processing"]["model_processing"]["scale_bars"]), 2)
        self.assertEqual(tree["processing"]["step1_products"]["smooth_strength"], 4)


class YamlScalarTests(unittest.TestCase):
    def test_booleans_and_numbers_stay_bare(self):
        self.assertEqual(run_phase1.yaml_scalar("true"), "true")
        self.assertEqual(run_phase1.yaml_scalar("False"), "false")
        self.assertEqual(run_phase1.yaml_scalar("300"), "300")
        self.assertEqual(run_phase1.yaml_scalar("0.009"), "0.009")
        self.assertEqual(run_phase1.yaml_scalar("-2"), "-2")

    def test_strings_are_quoted(self):
        self.assertEqual(run_phase1.yaml_scalar("/mnt/a b/c"), '"/mnt/a b/c"')
        self.assertEqual(run_phase1.yaml_scalar("MildFiltering"), '"MildFiltering"')
        self.assertEqual(run_phase1.yaml_scalar("nan"), '"nan"')


class ParseParamPairsTests(unittest.TestCase):
    def test_pairs_are_parsed_and_coerced(self):
        pairs = run_phase1.parse_param_pairs([
            "processing.frames_per_transect=300",
            "processing.use_gpu=false",
            "processing.video_input_dir=/mnt/videos",
        ])
        self.assertEqual(pairs, [
            ("processing.frames_per_transect", "300"),
            ("processing.use_gpu", "false"),
            ("processing.video_input_dir", '"/mnt/videos"'),
        ])

    def test_a_value_containing_an_equals_sign_is_kept_whole(self):
        self.assertEqual(
            run_phase1.parse_param_pairs(["project.notes=a=b"]),
            [("project.notes", '"a=b"')],
        )

    def test_malformed_pairs_raise(self):
        with self.assertRaises(ValueError):
            run_phase1.parse_param_pairs(["nokey"])
        with self.assertRaises(ValueError):
            run_phase1.parse_param_pairs(["=5"])


class ApplyParamOverridesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="param_apply_")
        shutil.copy2(os.path.join(REPO, "analysis_params.yaml"),
                     os.path.join(self.tmp, "analysis_params.yaml"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_overrides_reach_the_project_file(self):
        run_phase1.apply_param_overrides(Path(self.tmp), run_phase1.parse_param_pairs([
            "processing.frames_per_transect=200",
            "processing.step1_products.decimation_factor=20",
        ]))
        tree = yaml.safe_load(Path(self.tmp, "analysis_params.yaml").read_text())
        self.assertEqual(tree["processing"]["frames_per_transect"], 200)
        self.assertEqual(tree["processing"]["step1_products"]["decimation_factor"], 20)

    def test_no_pairs_leaves_the_file_byte_for_byte(self):
        before = Path(self.tmp, "analysis_params.yaml").read_text()
        run_phase1.apply_param_overrides(Path(self.tmp), [])
        self.assertEqual(Path(self.tmp, "analysis_params.yaml").read_text(), before)

    def test_force_rerun_is_written_and_cleared(self):
        run_phase1._set_force_rerun(Path(self.tmp), True)
        tree = yaml.safe_load(Path(self.tmp, "analysis_params.yaml").read_text())
        self.assertIs(tree["processing"]["force_rerun"], True)
        run_phase1._set_force_rerun(Path(self.tmp), False)
        tree = yaml.safe_load(Path(self.tmp, "analysis_params.yaml").read_text())
        self.assertIs(tree["processing"]["force_rerun"], False)


MINIMAL_PARAMS = """
project:
  name: "guard test"
  notes: ""
processing:
  tcrmp: true
  frames_per_transect: 10
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


class StaleChunkDecisionTests(unittest.TestCase):
    """C1: a chunk holding manual edits is never rebuilt without --force."""

    @classmethod
    def setUpClass(cls):
        cls.config_dir = tempfile.mkdtemp(prefix="guard_cfg_")
        with open(os.path.join(cls.config_dir, "analysis_params.yaml"), "w") as fh:
            fh.write(MINIMAL_PARAMS)
        if "Metashape" not in sys.modules:
            sys.modules["Metashape"] = types.ModuleType("Metashape")
        saved_argv = sys.argv[:]
        sys.argv = ["step1", cls.config_dir]
        try:
            if "config" in sys.modules:
                importlib.reload(sys.modules["config"])
            else:
                import config  # noqa: F401
            if "step1" in sys.modules:
                cls.step1 = importlib.reload(sys.modules["step1"])
            else:
                import step1
                cls.step1 = step1
        finally:
            sys.argv = saved_argv

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.config_dir, ignore_errors=True)

    def test_manual_edits_done_skips_without_force(self):
        self.assertEqual(self.step1.stale_chunk_decision(True, "done", False), "skip")

    def test_force_rebuilds_even_with_manual_edits(self):
        self.assertEqual(self.step1.stale_chunk_decision(True, "done", True), "replace")

    def test_awaiting_or_blank_is_replaced(self):
        self.assertEqual(self.step1.stale_chunk_decision(True, "awaiting", False), "replace")
        self.assertEqual(self.step1.stale_chunk_decision(True, "", False), "replace")
        self.assertEqual(self.step1.stale_chunk_decision(True, None, False), "replace")

    def test_case_and_padding_do_not_defeat_the_guard(self):
        self.assertEqual(self.step1.stale_chunk_decision(True, " Done ", False), "skip")

    def test_non_tcrmp_keeps_the_original_swap(self):
        self.assertEqual(self.step1.stale_chunk_decision(False, "done", False), "replace")

    def test_force_rerun_requested_reads_the_params_file(self):
        self.assertFalse(self.step1.force_rerun_requested())
        self.step1.PARAMS["processing"]["force_rerun"] = True
        try:
            self.assertTrue(self.step1.force_rerun_requested())
        finally:
            self.step1.PARAMS["processing"]["force_rerun"] = False


if __name__ == "__main__":
    unittest.main()
