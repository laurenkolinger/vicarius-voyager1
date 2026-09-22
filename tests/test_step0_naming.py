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

    def test_a_row_naming_one_part_is_the_whole_recording(self):
        # A lone part is the whole recording (Lauren, 2026-09-11). Mermaid's
        # Chair transect 3, 2023 annual, is the real case: its only readable
        # archive file is part1 (its sibling has a question mark in its name
        # and no parser can read it). Prep's refusal of that one file, at
        # ingest before step 0 ran, paused four timepoints of that transect
        # in the first Carousel batch; step 0 refused such a row too until
        # 2026-09-11. Prep now renames such a file to the standard name
        # before a Carousel run; a row that still names the part comes back
        # exactly as it is, and the caller notes the fact.
        for original in ("TCRMP20240307_demo_SHR_T5_pt1.MP4",
                         "TCRMP20231207_demo_MRS_T3_part1.MP4",
                         "TCRMP20240422_demo_MRS_T1_3.MP4",
                         "TCRMP20241113_3D_JKB_T2_2_Proxy.MOV"):
            with self.subTest(original):
                row = {"original_videos": original, "readable_id": "SHR_T5_2024_pbl"}
                self.assertEqual(step0_naming.identity_for(row=row, tcrmp=True),
                                 (original, "SHR_T5_2024_pbl"))

    def test_a_whole_recording_is_not_mistaken_for_a_part(self):
        for original in ("TCRMP20240307_3D_SHR_T5.MP4",
                         "TCRMP20241122_3D_MRS_T1_Proxy.MOV",
                         "TCRMP20250321_3D_MRS_T1.MOV",
                         "TCRMP20231207_3D_MRS_T2.MP4"):
            with self.subTest(original):
                row = {"original_videos": original, "readable_id": "MRS_T1_2025_pbl"}
                got, rid = step0_naming.identity_for(row=row, tcrmp=True)
                self.assertEqual(got, original)
                self.assertEqual(rid, "MRS_T1_2025_pbl")

    def test_names_a_single_part_on_its_own(self):
        self.assertTrue(step0_naming.names_a_single_part("X_pt1.MP4"))
        self.assertTrue(step0_naming.names_a_single_part("X_part12.MP4"))
        self.assertTrue(step0_naming.names_a_single_part("X_7.MOV"))
        self.assertFalse(step0_naming.names_a_single_part("TCRMP20240307_3D_SHR_T5.MP4"))
        self.assertFalse(step0_naming.names_a_single_part(""))
        self.assertFalse(step0_naming.names_a_single_part(None))

    def test_lone_part_note_names_the_id_the_file_and_the_rule(self):
        note = step0_naming.lone_part_note(
            "TCRMP20231207_demo_MRS_T3_part1.MP4", "MRS_T3_2023ann")
        self.assertIn("MRS_T3_2023ann", note)
        self.assertIn("TCRMP20231207_demo_MRS_T3_part1.MP4", note)
        self.assertIn("lone part", note)
        self.assertIn("whole recording", note)
        # One plain sentence: it goes into a log line and a console line as is.
        self.assertNotIn("\n", note)
        self.assertEqual(
            note,
            "MRS_T3_2023ann: TCRMP20231207_demo_MRS_T3_part1.MP4 is lone part 1, "
            "taken as the whole recording (Lauren, 2026-09-11)")

    def test_lone_part_note_keeps_the_part_number_however_it_is_spelled(self):
        for original, number in (("TCRMP20240307_demo_SHR_T5_pt1.MP4", 1),
                                 ("TCRMP20240422_demo_MRS_T1_3.MP4", 3),
                                 ("TCRMP20241113_3D_JKB_T2_2_Proxy.MOV", 2),
                                 ("  TCRMP20240307_demo_SHR_T5_pt1.MP4  ", 1)):
            with self.subTest(original):
                note = step0_naming.lone_part_note(original, "SHR_T5_2024_pbl")
                self.assertIn(original.strip(), note)
                self.assertIn(f"lone part {number}", note)
                self.assertNotIn("  ", note)

    def test_part_number_reads_every_spelling_and_refuses_zero(self):
        self.assertEqual(step0_naming.part_number("X_pt1.MP4"), 1)
        self.assertEqual(step0_naming.part_number("X_part12.MP4"), 12)
        self.assertEqual(step0_naming.part_number("X_7.MOV"), 7)
        self.assertEqual(step0_naming.part_number("X_2_Proxy.MOV"), 2)
        self.assertIsNone(step0_naming.part_number("X_0.MP4"))
        self.assertIsNone(step0_naming.part_number("TCRMP20240307_3D_SHR_T5.MP4"))

    def test_lone_part_note_refuses_a_name_that_is_not_a_part(self):
        # The note exists to keep a part number on record; writing one for a
        # standard-named file (or nothing at all) would record a fact that is
        # not true.
        for original in ("TCRMP20240307_3D_SHR_T5.MP4", "", None):
            with self.subTest(original):
                with self.assertRaises(ValueError):
                    step0_naming.lone_part_note(original, "SHR_T5_2024_pbl")

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
