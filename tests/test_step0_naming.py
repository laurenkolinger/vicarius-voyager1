"""Tests for src/step0_naming.py (Task 10): pure frame-naming, identity, and
frame-count helpers used by step0.py before any ffmpeg call. No config import
(config.py has import-time argv/filesystem side effects), no Metashape.
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
import step0_naming  # noqa: E402


class FrameOutputPatternTests(unittest.TestCase):
    def test_basic_pattern(self):
        pattern = step0_naming.frame_output_pattern(
            "TCRMP20231015_3D_MRS_T1.MOV", "frames/MRS_T1_2023ann"
        )
        self.assertEqual(pattern, "frames/MRS_T1_2023ann/TCRMP20231015_3D_MRS_T1_%05d.tiff")

    def test_ignores_source_directory_component(self):
        # video_path may be an absolute path elsewhere on disk (never copied);
        # only its base name feeds the frame file name.
        pattern = step0_naming.frame_output_pattern(
            "/mnt/videos/2023/TCRMP20231015_3D_MRS_T1.MOV", "/proj/frames/MRS_T1_2023ann"
        )
        self.assertEqual(pattern, "/proj/frames/MRS_T1_2023ann/TCRMP20231015_3D_MRS_T1_%05d.tiff")

    def test_non_tcrmp_video_name(self):
        pattern = step0_naming.frame_output_pattern("GOPR0001.MP4", "frames/GOPR0001")
        self.assertEqual(pattern, "frames/GOPR0001/GOPR0001_%05d.tiff")


class IdentityForTests(unittest.TestCase):
    def test_tcrmp_row_identity(self):
        row = {"original_videos": "TCRMP20231015_3D_MRS_T1.MOV", "readable_id": "MRS_T1_2023ann"}
        original_videos, readable_id = step0_naming.identity_for(row=row, tcrmp=True)
        self.assertEqual(original_videos, "TCRMP20231015_3D_MRS_T1.MOV")
        self.assertEqual(readable_id, "MRS_T1_2023ann")

    def test_tcrmp_multi_part_raises_value_error(self):
        row = {
            "original_videos": "TCRMP20231015_3D_MRS_T1_1.MOV;TCRMP20231015_3D_MRS_T1_2.MOV",
            "readable_id": "MRS_T1_2023ann",
        }
        with self.assertRaises(ValueError):
            step0_naming.identity_for(row=row, tcrmp=True)

    def test_tcrmp_requires_row(self):
        with self.assertRaises(ValueError):
            step0_naming.identity_for(row=None, tcrmp=True)

    def test_non_tcrmp_identity_from_video_path(self):
        original_videos, readable_id = step0_naming.identity_for(
            video_path="GOPR0001.MP4", tcrmp=False
        )
        self.assertEqual(original_videos, "GOPR0001.MP4")
        self.assertEqual(readable_id, "GOPR0001")

    def test_non_tcrmp_identity_ignores_source_directory(self):
        original_videos, readable_id = step0_naming.identity_for(
            video_path="/mnt/videos/2024/GOPR0002.mp4", tcrmp=False
        )
        self.assertEqual(original_videos, "GOPR0002.mp4")
        self.assertEqual(readable_id, "GOPR0002")

    def test_non_tcrmp_requires_video_path(self):
        with self.assertRaises(ValueError):
            step0_naming.identity_for(video_path=None, tcrmp=False)


class EffectiveFrameCountTests(unittest.TestCase):
    # pass2 fix: a requested extraction count above the source video's own
    # frame count must clamp, not silently ask ffmpeg to duplicate frames
    # (which later surfaces as an opaque tie-point failure in step1).

    def test_requested_below_source_count_passes_through_unchanged(self):
        self.assertEqual(step0_naming.effective_frame_count(200, 1000), 200)

    def test_requested_above_source_count_clamps_to_source_count(self):
        # The pass2 reproduction: 1000 frames requested from a 200-frame source.
        self.assertEqual(step0_naming.effective_frame_count(1000, 200), 200)

    def test_requested_equal_to_source_count_passes_through(self):
        self.assertEqual(step0_naming.effective_frame_count(200, 200), 200)

    def test_unknown_source_count_skips_the_clamp(self):
        # ffprobe could not report nb_frames for this container/codec; there
        # is nothing reliable to compare against, so the request is honored
        # as-is rather than guessed at.
        self.assertEqual(step0_naming.effective_frame_count(1000, None), 1000)

    def test_zero_source_count_clamps_to_zero(self):
        self.assertEqual(step0_naming.effective_frame_count(1000, 0), 0)


if __name__ == "__main__":
    unittest.main()
