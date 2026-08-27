"""Tests for the flat processing-folder layout and identity-first status.csv
schema in src/config.py (Task 7)."""

import os, sys, tempfile, unittest, importlib


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sys.argv = ["x", self.tmp]  # config reads the project dir from argv like the steps do
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
        open(os.path.join(self.tmp, "analysis_params.yaml"), "w").write(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis_params.yaml")).read())
        import config; importlib.reload(config); self.c = config

    def test_headers_start_with_identity(self):
        self.assertEqual(self.c.headers[:3], ["original_videos", "readable_id", "Model ID"])
        i = self.c.headers.index("Scale Error (m)")
        self.assertEqual(self.c.headers[i + 1:i + 3], ["Scale Error (ppm)", "Scale Bars"])

    def test_layout(self):
        self.assertEqual(self.c.TRACKING_FILE, os.path.join(self.tmp, "status.csv"))
        for k in ("frames", "reports", "console"):
            self.assertEqual(self.c.DIRECTORIES[k], os.path.join(self.tmp, k))
        for k in list(self.c.DIRECTORIES):
            self.assertNotIn("processing", self.c.DIRECTORIES[k]); self.assertNotIn("video_source", self.c.DIRECTORIES[k])
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "console")))

    def test_step_log_path(self):
        self.assertTrue(self.c.step_log_path("step1").startswith(os.path.join(self.tmp, "console", "step1_")))


if __name__ == "__main__":
    unittest.main()
