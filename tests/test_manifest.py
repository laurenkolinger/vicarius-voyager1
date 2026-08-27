"""Unit tests for manifest. Run from github_repo:  python3 -m unittest tests.test_manifest -v"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import manifest


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "manifest.csv")

    def test_creates_header_then_appends(self):
        manifest.append_event("proj", "MODEL_A", "psx", "created", "/x/a.psx",
                              details="2 chunks", manifest_path=self.path)
        manifest.append_event("proj", "MODEL_A", "dem", "created", "/x/a_dem.tif",
                              manifest_path=self.path)
        with open(self.path) as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["timestamp", "module_version", "project",
                                   "model_id", "artifact", "action", "path", "details"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][2:], ["proj", "MODEL_A", "psx", "created", "/x/a.psx", "2 chunks"])
        self.assertEqual(rows[2][4], "dem")
        self.assertRegex(rows[1][0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(rows[1][1], manifest.MODULE_VERSION)

    def test_default_path_is_module_root(self):
        expected = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(manifest.__file__)))), "manifest.csv")
        self.assertEqual(manifest.default_manifest_path(), expected)

    def test_failure_is_swallowed(self):
        bad = os.path.join(self.tmp, "no_such_dir", "x", "manifest.csv")
        manifest.append_event("p", "m", "psx", "created", "/x", manifest_path=bad)  # must not raise


if __name__ == "__main__":
    unittest.main()
