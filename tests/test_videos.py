"""Tests for src/videos.py (Task 8): ffprobe-based video helpers. Extension
is never used to decide video-ness.
"""
import os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
import videos  # noqa: E402


def _make_video(path, vcodec="libx264", extra=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc=size=64x64:rate=2", "-frames:v", "2", "-c:v", vcodec]
    if vcodec == "libx264":
        cmd += ["-pix_fmt", "yuv420p"]
    if extra:
        cmd += extra
    cmd.append(path)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH")
class VideosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mp4 = os.path.join(self.tmp, "TCRMP20240215_3D_MRS_T1.mp4")
        self.mov = os.path.join(self.tmp, "TCRMP20240215_3D_MRS_T1_2.mov")
        self.mkv = os.path.join(self.tmp, "TCRMP20240215_3D_MRS_T1_part3.mkv")
        self.avi = os.path.join(self.tmp, "TCRMP20240215_3D_MRS_T1_Proxy.avi")
        _make_video(self.mp4)
        _make_video(self.mov)
        _make_video(self.mkv)
        _make_video(self.avi, vcodec="mjpeg")
        self.decoy = os.path.join(self.tmp, "notes.txt")
        with open(self.decoy, "w") as fh:
            fh.write("this is not a video, just text\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- probe --------------------------------------------------------
    def test_probe_mp4(self):
        info = videos.probe(self.mp4)
        self.assertEqual(info["codec"], "h264")
        self.assertEqual(info["width"], 64)
        self.assertEqual(info["height"], 64)
        self.assertEqual(info["fps"], 2.0)
        self.assertAlmostEqual(info["duration_s"], 1.0, places=2)
        self.assertEqual(info["nb_frames"], 2)
        self.assertIn("mp4", info["container"])

    def test_probe_mjpeg_avi(self):
        info = videos.probe(self.avi)
        self.assertEqual(info["codec"], "mjpeg")
        self.assertEqual(info["container"], "avi")

    def test_probe_nb_frames_none_when_ffprobe_omits_it(self):
        # mkv streams typically omit nb_frames without a full decode pass.
        info = videos.probe(self.mkv)
        self.assertIn(info["nb_frames"], (None, 2))  # tolerate either ffmpeg build's behavior
        self.assertEqual(info["codec"], "h264")

    def test_probe_raises_on_non_media(self):
        with self.assertRaises(Exception):
            videos.probe(self.decoy)

    # -- is_video -------------------------------------------------------
    def test_is_video_true_for_real_videos(self):
        for p in (self.mp4, self.mov, self.mkv, self.avi):
            self.assertTrue(videos.is_video(p), p)

    def test_is_video_false_for_decoy_text_file(self):
        self.assertFalse(videos.is_video(self.decoy))

    def test_is_video_false_for_missing_file(self):
        self.assertFalse(videos.is_video(os.path.join(self.tmp, "nope.mp4")))

    def test_is_video_false_for_directory(self):
        d = os.path.join(self.tmp, "adir")
        os.makedirs(d)
        self.assertFalse(videos.is_video(d))

    def test_is_video_ignores_extension(self):
        # rename a real video to a .txt extension; must still probe true
        wrong_ext = os.path.join(self.tmp, "video_but_txt_ext.txt")
        shutil.copy2(self.mp4, wrong_ext)
        self.assertTrue(videos.is_video(wrong_ext))

    # -- hwaccel_args -----------------------------------------------------
    def test_hwaccel_args_cuda_when_supported_codec_linux_and_nvidia(self):
        with mock.patch("videos._nvidia_present", return_value=True), \
             mock.patch("videos.platform.system", return_value="Linux"):
            for codec in ("h264", "hevc", "av1", "vp9"):
                self.assertEqual(videos.hwaccel_args(codec), ["-hwaccel", "cuda"])

    def test_hwaccel_args_empty_for_unsupported_codec(self):
        with mock.patch("videos._nvidia_present", return_value=True), \
             mock.patch("videos.platform.system", return_value="Linux"):
            self.assertEqual(videos.hwaccel_args("mjpeg"), [])

    def test_hwaccel_args_empty_when_no_nvidia(self):
        with mock.patch("videos._nvidia_present", return_value=False), \
             mock.patch("videos.platform.system", return_value="Linux"):
            self.assertEqual(videos.hwaccel_args("h264"), [])

    def test_hwaccel_args_empty_on_non_linux(self):
        with mock.patch("videos._nvidia_present", return_value=True), \
             mock.patch("videos.platform.system", return_value="Darwin"):
            self.assertEqual(videos.hwaccel_args("h264"), [])

    # -- list_videos ------------------------------------------------------
    def test_list_videos_sorted_excludes_decoy(self):
        names = videos.list_videos(self.tmp)
        self.assertEqual(names, sorted(names))
        self.assertNotIn(os.path.basename(self.decoy), names)
        for p in (self.mp4, self.mov, self.mkv, self.avi):
            self.assertIn(os.path.basename(p), names)

    # -- group_parts --------------------------------------------------
    def test_group_parts_tcrmp_orders_by_part_none_first(self):
        names = [os.path.basename(p) for p in (self.mp4, self.mov, self.mkv, self.avi)]
        groups = videos.group_parts(names, tcrmp=True)
        self.assertEqual(len(groups), 1)
        (key, files), = groups.items()
        self.assertEqual(key, "TCRMP20240215_3D_MRS_T1")
        # mp4 has no part (None -> first), then _2 (part 2), then _part3 (part 3);
        # the _Proxy file also has no part, so it ties with mp4 for "first" and
        # keeps its relative input order.
        self.assertEqual(files, [
            os.path.basename(self.mp4),
            os.path.basename(self.avi),
            os.path.basename(self.mov),
            os.path.basename(self.mkv),
        ])

    def test_group_parts_tcrmp_unparsed_name_falls_back_to_own_stem(self):
        groups = videos.group_parts(["random_clip.mp4"], tcrmp=True)
        self.assertEqual(groups, {"random_clip": ["random_clip.mp4"]})

    def test_group_parts_non_tcrmp_groups_each_file_alone(self):
        names = [os.path.basename(p) for p in (self.mp4, self.mov)]
        groups = videos.group_parts(names, tcrmp=False)
        self.assertEqual(groups, {
            "TCRMP20240215_3D_MRS_T1": [os.path.basename(self.mp4)],
            "TCRMP20240215_3D_MRS_T1_2": [os.path.basename(self.mov)],
        })


if __name__ == "__main__":
    unittest.main()
