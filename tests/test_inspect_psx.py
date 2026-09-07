"""Tests for src/inspect_psx.py, the read-only psx inspection behind the manual
edit CHECKS. Everything runs in fake mode: FakeDocument objects built from a
JSON fixture stand in for Metashape, so no Metashape binary, licence or psx
bundle is needed. The Metashape import must stay inside real mode; a test
parses the source to prove it.

Run from github_repo:  python3 -m unittest tests.test_inspect_psx -v
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import inspect_psx  # noqa: E402
import scale_utils  # noqa: E402

SCRIPT = os.path.join(SRC, "inspect_psx.py")
TWO_BARS = [
    {"p0": [0.0, 0.0, 0.0], "p1": [0.752, 0.0, 0.0], "distance": 0.75},
    {"p0": [0.0, 1.0, 0.0], "p1": [0.748, 1.0, 0.0], "distance": 0.75},
]


def chunk_spec(label="MRS_T1_2024ann", **overrides):
    """One fixture chunk with sensible defaults."""
    spec = {
        "label": label, "transform": True, "scalebars": TWO_BARS, "faces": 700000, "textures": 1,
        "tie_points": 12345, "cameras": 400, "cameras_aligned": 398, "elevation_resolution_m": 0.0012,
        "model": True,
    }
    spec.update(overrides)
    return spec


def fixture(psx_map, **top):
    """A fixture document for --fake."""
    doc = {"app_version": "2.2.2-fake", "activated": True, "psx": psx_map}
    doc.update(top)
    return doc


class InspectChunkTests(unittest.TestCase):
    """Per-chunk facts from a fake chunk."""

    def chunk(self, **overrides):
        """A FakeChunk built from chunk_spec with the given overrides."""
        return inspect_psx.FakeChunk.from_spec(chunk_spec(**overrides))

    def test_two_bars_under_threshold(self):
        facts = inspect_psx.inspect_chunk(self.chunk(), 0.009, 0.75)
        self.assertEqual(facts["label"], "MRS_T1_2024ann")
        self.assertTrue(facts["has_transform"])
        self.assertEqual(facts["scale_bars"], 2)
        self.assertAlmostEqual(facts["scale_error_m"], 0.002, places=6)
        self.assertEqual(facts["scale_error_mm"], 2.0)
        self.assertEqual(facts["scale_error_ppm"], 2667)
        self.assertEqual(facts["scale_status"], scale_utils.PASS)
        self.assertEqual(facts["faces"], 700000)
        self.assertEqual(facts["textures"], 1)
        self.assertEqual(facts["tie_points"], 12345)
        self.assertEqual(facts["cameras_total"], 400)
        self.assertEqual(facts["cameras_aligned"], 398)
        self.assertEqual(facts["dem_mm_per_pix"], 1.2)
        self.assertEqual(facts["notes"], [])

    def test_field_set_is_pinned(self):
        facts = inspect_psx.inspect_chunk(self.chunk(), 0.009, 0.75)
        self.assertEqual(sorted(facts.keys()), sorted([
            "label", "has_transform", "scale_bars", "scale_error_m", "scale_error_mm", "scale_error_ppm",
            "scale_status", "faces", "textures", "tie_points", "cameras_total", "cameras_aligned",
            "dem_mm_per_pix", "notes",
        ]))

    def test_no_transform(self):
        facts = inspect_psx.inspect_chunk(self.chunk(transform=False), 0.009, 0.75)
        self.assertFalse(facts["has_transform"])
        self.assertEqual(facts["scale_bars"], 2)
        self.assertIsNone(facts["scale_error_m"])
        self.assertIsNone(facts["scale_error_mm"])
        self.assertIsNone(facts["scale_error_ppm"])
        self.assertEqual(facts["scale_status"], scale_utils.MANUAL_NEEDED)

    def test_no_bars_is_sentinel_reported_as_none(self):
        facts = inspect_psx.inspect_chunk(self.chunk(scalebars=[]), 0.009, 0.75)
        self.assertEqual(facts["scale_bars"], 0)
        self.assertIsNone(facts["scale_error_m"])
        self.assertEqual(facts["scale_status"], scale_utils.MANUAL_NEEDED)

    def test_one_bar_measured_but_manual_needed(self):
        facts = inspect_psx.inspect_chunk(self.chunk(scalebars=TWO_BARS[:1]), 0.009, 0.75)
        self.assertEqual(facts["scale_bars"], 1)
        self.assertAlmostEqual(facts["scale_error_m"], 0.002, places=6)
        self.assertEqual(facts["scale_status"], scale_utils.MANUAL_NEEDED)

    def test_error_exactly_at_threshold_is_manual_needed(self):
        bars = [
            {"p0": [0, 0, 0], "p1": [0.759, 0, 0], "distance": 0.75},
            {"p0": [0, 1, 0], "p1": [0.759, 1, 0], "distance": 0.75},
        ]
        facts = inspect_psx.inspect_chunk(self.chunk(scalebars=bars), 0.009, 0.75)
        self.assertAlmostEqual(facts["scale_error_m"], 0.009, places=9)
        self.assertEqual(facts["scale_status"], scale_utils.MANUAL_NEEDED)

    def test_no_model(self):
        facts = inspect_psx.inspect_chunk(self.chunk(model=False), 0.009, 0.75)
        self.assertIsNone(facts["faces"])
        self.assertIsNone(facts["textures"])

    def test_no_elevation(self):
        facts = inspect_psx.inspect_chunk(self.chunk(elevation_resolution_m=None), 0.009, 0.75)
        self.assertIsNone(facts["dem_mm_per_pix"])

    def test_no_tie_points(self):
        facts = inspect_psx.inspect_chunk(self.chunk(tie_points=None), 0.009, 0.75)
        self.assertIsNone(facts["tie_points"])

    def test_zero_bar_length_gives_zero_ppm(self):
        facts = inspect_psx.inspect_chunk(self.chunk(), 0.009, 0.0)
        self.assertEqual(facts["scale_error_ppm"], 0)

    def test_attribute_failures_become_notes_not_crashes(self):
        class Exploding:
            """A chunk whose transform and model accessors raise."""
            label = "boom"

            @property
            def transform(self):
                """Raise to stand in for an unreadable transform."""
                raise RuntimeError("no transform access")

            @property
            def model(self):
                """Raise to stand in for a missing model."""
                raise AttributeError("model gone")

            scalebars = []
            tie_points = None
            cameras = []
            elevation = None

        facts = inspect_psx.inspect_chunk(Exploding(), 0.009, 0.75)
        self.assertEqual(facts["label"], "boom")
        self.assertFalse(facts["has_transform"])
        self.assertIsNone(facts["faces"])
        self.assertTrue(any("transform" in n for n in facts["notes"]))
        self.assertTrue(any("model" in n for n in facts["notes"]))

    def test_validation(self):
        with self.assertRaises(ValueError):
            inspect_psx.inspect_chunk(self.chunk(), 0.0, 0.75)
        with self.assertRaises(TypeError):
            inspect_psx.inspect_chunk(self.chunk(), "0.009", 0.75)
        with self.assertRaises(ValueError):
            inspect_psx.inspect_chunk(self.chunk(), 0.009, -1.0)


class InspectDocumentTests(unittest.TestCase):
    """inspect_document over fake documents."""
    def test_orders_chunks_as_the_document_does(self):
        doc = inspect_psx.FakeDocument.from_spec({"chunks": [chunk_spec("b"), chunk_spec("a")]})
        facts = inspect_psx.inspect_document(doc, 0.009, 0.75)
        self.assertEqual([f["label"] for f in facts], ["b", "a"])

    def test_empty_document(self):
        doc = inspect_psx.FakeDocument.from_spec({"chunks": []})
        self.assertEqual(inspect_psx.inspect_document(doc, 0.009, 0.75), [])

    def test_huge_document(self):
        doc = inspect_psx.FakeDocument.from_spec({"chunks": [chunk_spec(f"c{i}") for i in range(500)]})
        self.assertEqual(len(inspect_psx.inspect_document(doc, 0.009, 0.75)), 500)

    def test_document_without_chunks_attribute(self):
        with self.assertRaises(TypeError):
            inspect_psx.inspect_document(object(), 0.009, 0.75)


class InspectPsxTests(unittest.TestCase):
    """Document-level facts: mtimes, app version, activation, errors."""

    def setUp(self):
        """A temp folder per test, removed at cleanup."""
        self.tmp = Path(tempfile.mkdtemp(prefix="inspect_psx_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_document_fields_and_mtimes_from_disk(self):
        psx = self.tmp / "MRS_T1_2023_2025.psx"
        psx.write_text('<document version="1.2.0" path="{projectname}.files/project.zip"/>')
        files_dir = self.tmp / "MRS_T1_2023_2025.files"
        files_dir.mkdir()
        # 1788327899 is 2026-09-02T01:44:59-04:00 (an earlier 1756791899 was 2025).
        os.utime(psx, (1788327899, 1788327899))
        os.utime(files_dir, (1788331499, 1788331499))
        fx = fixture({str(psx): {"chunks": [chunk_spec()]}})
        doc = inspect_psx.inspect_psx(str(psx), inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertEqual(doc["psx_path"], str(psx))
        self.assertEqual(doc["psx_basename"], "MRS_T1_2023_2025.psx")
        self.assertEqual(doc["psx_mtime_epoch"], 1788327899.0)
        self.assertEqual(doc["psx_mtime"], "2026-09-02T01:44:59-04:00")
        self.assertEqual(doc["files_mtime_epoch"], 1788331499.0)
        self.assertEqual(doc["files_mtime"], "2026-09-02T02:44:59-04:00")
        self.assertEqual(doc["app_version"], "2.2.2-fake")
        self.assertTrue(doc["activated"])
        self.assertEqual(doc["mode"], "fake")
        self.assertEqual(doc["threshold_m"], 0.009)
        self.assertEqual(doc["bar_length_m"], 0.75)
        self.assertIsNone(doc["error"])
        self.assertEqual(len(doc["chunks"]), 1)
        self.assertTrue(doc["inspected_at"].endswith("-04:00"))

    def test_mtimes_from_fixture_when_file_absent(self):
        fx = fixture({"/nowhere/x.psx": {"chunks": [], "psx_mtime_epoch": 100.0, "files_mtime_epoch": 200.0}})
        doc = inspect_psx.inspect_psx("/nowhere/x.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertEqual(doc["psx_mtime_epoch"], 100.0)
        self.assertEqual(doc["files_mtime_epoch"], 200.0)
        self.assertIsNone(doc["error"])

    def test_missing_mtimes_are_none(self):
        fx = fixture({"/nowhere/y.psx": {"chunks": []}})
        doc = inspect_psx.inspect_psx("/nowhere/y.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertIsNone(doc["psx_mtime"])
        self.assertIsNone(doc["psx_mtime_epoch"])
        self.assertIsNone(doc["files_mtime"])

    def test_open_failure_is_an_error_document(self):
        fx = fixture({"/nowhere/z.psx": {"open_error": "project is locked"}})
        doc = inspect_psx.inspect_psx("/nowhere/z.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertEqual(doc["chunks"], [])
        self.assertIn("project is locked", doc["error"])
        self.assertIn("/nowhere/z.psx", doc["error"])

    def test_psx_not_in_fixture_is_an_error_document(self):
        fx = fixture({})
        doc = inspect_psx.inspect_psx("/nowhere/q.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertIn("not found", doc["error"])

    def test_fixture_matches_by_basename(self):
        fx = fixture({"a.psx": {"chunks": [chunk_spec()]}})
        doc = inspect_psx.inspect_psx("/any/where/a.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertIsNone(doc["error"])
        self.assertEqual(len(doc["chunks"]), 1)

    def test_hostile_labels_survive_json(self):
        label = 'MRS "T1"; drop, \né☃'
        fx = fixture({"h.psx": {"chunks": [chunk_spec(label)]}})
        doc = inspect_psx.inspect_psx("h.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx),
                                      0.009, 0.75, mode="fake")
        self.assertEqual(json.loads(json.dumps(doc, ensure_ascii=False))["chunks"][0]["label"], label)

    def test_validation(self):
        fx = fixture({})
        with self.assertRaises(ValueError):
            inspect_psx.inspect_psx("", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx), 0.009, 0.75, mode="fake")
        with self.assertRaises(ValueError):
            inspect_psx.inspect_psx("x.txt", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx), 0.009, 0.75, mode="fake")
        with self.assertRaises(ValueError):
            inspect_psx.inspect_psx("x.psx", inspect_psx.fake_opener(fx), inspect_psx.fake_app_info(fx), 0.009, 0.75, mode="dream")


class ParamsTests(unittest.TestCase):
    """read_params and mean_bar_length."""
    def setUp(self):
        """A temp folder per test, removed at cleanup."""
        self.tmp = Path(tempfile.mkdtemp(prefix="inspect_params_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_reads_threshold_and_mean_bar_length(self):
        params = self.tmp / "analysis_params.yaml"
        params.write_text(
            "processing:\n  model_processing:\n    scale_error_threshold: 0.012\n"
            "    scale_bars:\n      - distance: 0.5\n      - distance: 1.0\n"
        )
        threshold, bar_length, note = inspect_psx.read_params(str(params))
        self.assertEqual(threshold, 0.012)
        self.assertEqual(bar_length, 0.75)
        self.assertEqual(note, "")

    def test_missing_file_falls_back_with_a_note(self):
        threshold, bar_length, note = inspect_psx.read_params(str(self.tmp / "none.yaml"))
        self.assertEqual(threshold, inspect_psx.DEFAULT_THRESHOLD_M)
        self.assertEqual(bar_length, inspect_psx.DEFAULT_BAR_LENGTH_M)
        self.assertIn("none.yaml", note)

    def test_malformed_and_odd_shapes_fall_back(self):
        params = self.tmp / "bad.yaml"
        params.write_text("processing: [\n")
        threshold, bar_length, note = inspect_psx.read_params(str(params))
        self.assertEqual((threshold, bar_length), (inspect_psx.DEFAULT_THRESHOLD_M, inspect_psx.DEFAULT_BAR_LENGTH_M))
        self.assertTrue(note)
        params.write_text("processing:\n  model_processing:\n    scale_error_threshold: fast\n    scale_bars: nope\n")
        threshold, bar_length, note = inspect_psx.read_params(str(params))
        self.assertEqual((threshold, bar_length), (inspect_psx.DEFAULT_THRESHOLD_M, inspect_psx.DEFAULT_BAR_LENGTH_M))
        params.write_text("- list\n")
        threshold, bar_length, note = inspect_psx.read_params(str(params))
        self.assertEqual((threshold, bar_length), (inspect_psx.DEFAULT_THRESHOLD_M, inspect_psx.DEFAULT_BAR_LENGTH_M))

    def test_mean_bar_length_matches_step1(self):
        self.assertEqual(inspect_psx.mean_bar_length({"scale_bars": [{"distance": 0.75}, {"distance": 0.75}]}), 0.75)
        self.assertEqual(inspect_psx.mean_bar_length({"scale_bars": [{"distance": "x"}, {"distance": 0}]}), 0.0)
        self.assertEqual(inspect_psx.mean_bar_length({}), 0.0)


class CliTests(unittest.TestCase):
    """The script in fake mode: one JSON document per psx, one per line."""

    def setUp(self):
        """A temp folder per test, removed at cleanup."""
        self.tmp = Path(tempfile.mkdtemp(prefix="inspect_cli_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        """Run the script in a subprocess with the given arguments."""
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, timeout=60)

    def write_fixture(self, doc, name="fixture.json"):
        """Write a fixture JSON under the temp folder and return its path."""
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def test_json_lines_on_stdout(self):
        fx = self.write_fixture(fixture({"a.psx": {"chunks": [chunk_spec("A1")]}, "b.psx": {"chunks": []}}))
        proc = self.run_cli("/x/a.psx", "/x/b.psx", "--fake", fx)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        docs = [json.loads(l) for l in lines]
        self.assertEqual([d["psx_basename"] for d in docs], ["a.psx", "b.psx"])
        self.assertEqual(docs[0]["chunks"][0]["label"], "A1")
        self.assertEqual(docs[0]["mode"], "fake")
        for key in ("psx_mtime", "files_mtime", "app_version", "activated", "chunks", "error"):
            self.assertIn(key, docs[0])

    def test_out_file(self):
        fx = self.write_fixture(fixture({"a.psx": {"chunks": [chunk_spec("A1")]}}))
        out = self.tmp / "result.jsonl"
        proc = self.run_cli("/x/a.psx", "--fake", fx, "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        docs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(docs[0]["psx_basename"], "a.psx")

    def test_error_document_gives_exit_1(self):
        fx = self.write_fixture(fixture({}))
        proc = self.run_cli("/x/missing.psx", "--fake", fx)
        self.assertEqual(proc.returncode, 1)
        doc = json.loads(proc.stdout.strip())
        self.assertIn("not found", doc["error"])

    def test_missing_fixture_gives_exit_2(self):
        proc = self.run_cli("/x/a.psx", "--fake", str(self.tmp / "nope.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("nope.json", proc.stderr)

    def test_malformed_fixture_gives_exit_2(self):
        path = self.tmp / "bad.json"
        path.write_text("{not json")
        proc = self.run_cli("/x/a.psx", "--fake", str(path))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bad.json", proc.stderr)
        path.write_text("[]")
        proc = self.run_cli("/x/a.psx", "--fake", str(path))
        self.assertEqual(proc.returncode, 2)

    def test_params_file_sets_threshold_and_bar_length(self):
        params = self.tmp / "analysis_params.yaml"
        params.write_text("processing:\n  model_processing:\n    scale_error_threshold: 0.001\n    scale_bars:\n      - distance: 0.5\n")
        fx = self.write_fixture(fixture({"a.psx": {"chunks": [chunk_spec("A1")]}}))
        proc = self.run_cli("/x/a.psx", "--fake", fx, "--params", str(params))
        doc = json.loads(proc.stdout.strip())
        self.assertEqual(doc["threshold_m"], 0.001)
        self.assertEqual(doc["bar_length_m"], 0.5)
        self.assertEqual(doc["chunks"][0]["scale_status"], scale_utils.MANUAL_NEEDED)
        self.assertEqual(doc["chunks"][0]["scale_error_ppm"], 4000)

    def test_params_beside_the_psx_are_picked_up(self):
        folder = self.tmp / "MRS_T1_2023ann_3dprocessing"
        folder.mkdir()
        (folder / "analysis_params.yaml").write_text("processing:\n  model_processing:\n    scale_error_threshold: 0.02\n")
        psx = folder / "MRS_T1_2023_2023.psx"
        psx.write_text("x")
        fx = self.write_fixture(fixture({str(psx): {"chunks": []}}))
        proc = self.run_cli(str(psx), "--fake", fx)
        doc = json.loads(proc.stdout.strip())
        self.assertEqual(doc["threshold_m"], 0.02)
        self.assertIsNotNone(doc["psx_mtime_epoch"])

    def test_explicit_threshold_and_bar_length_win(self):
        fx = self.write_fixture(fixture({"a.psx": {"chunks": []}}))
        proc = self.run_cli("/x/a.psx", "--fake", fx, "--threshold-m", "0.5", "--bar-length-m", "2")
        doc = json.loads(proc.stdout.strip())
        self.assertEqual(doc["threshold_m"], 0.5)
        self.assertEqual(doc["bar_length_m"], 2.0)
        proc = self.run_cli("/x/a.psx", "--fake", fx, "--threshold-m", "-1")
        self.assertEqual(proc.returncode, 2)

    def test_no_psx_arguments_is_usage_error(self):
        fx = self.write_fixture(fixture({}))
        proc = self.run_cli("--fake", fx)
        self.assertEqual(proc.returncode, 2)

    def test_non_psx_path_is_usage_error(self):
        fx = self.write_fixture(fixture({}))
        proc = self.run_cli("/x/a.txt", "--fake", fx)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a.txt", proc.stderr)

    def test_unicode_in_fixture(self):
        fx = self.write_fixture(fixture({"u.psx": {"chunks": [chunk_spec("MRS é☃")]}}))
        proc = self.run_cli("/x/u.psx", "--fake", fx)
        doc = json.loads(proc.stdout.strip())
        self.assertEqual(doc["chunks"][0]["label"], "MRS é☃")


class RealModeGuardTests(unittest.TestCase):
    """Metashape stays inside real mode."""
    def test_metashape_is_only_imported_inside_real_mode_functions(self):
        tree = ast.parse(Path(SCRIPT).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                self.assertNotIn("Metashape", names, "Metashape must not be imported at module scope")
        nested = [n for n in ast.walk(tree) if isinstance(n, ast.Import) and any(a.name == "Metashape" for a in n.names)]
        self.assertTrue(nested, "real mode must import Metashape inside a function")

    def test_real_open_without_metashape_raises_a_plain_error(self):
        saved = sys.modules.pop("Metashape", None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                inspect_psx.real_open("/nowhere/a.psx")
            self.assertIn("metashape -r", str(ctx.exception))
        finally:
            if saved is not None:
                sys.modules["Metashape"] = saved


if __name__ == "__main__":
    unittest.main()
