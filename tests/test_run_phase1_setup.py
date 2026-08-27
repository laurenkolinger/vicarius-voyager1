"""Tests for src/run_phase1.py's TCRMP setup (Task 9): select_rows() and
prepare_tcrmp_folder() must be usable without Metashape and without any
prompt. Uses a temp registry root (VICARIUS_3D_REGISTRY_ROOT) and tiny
lavfi videos so the whole flow runs without real survey data.
"""
import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import registry_client  # noqa: E402
import run_phase1  # noqa: E402
import status_rows  # noqa: E402

LIB_DIR = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius") + "/_METADATA/3d"
sys.path.insert(0, LIB_DIR)
import naming3d  # noqa: E402


def _tiny_video(path, vcodec="libx264"):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc=size=64x64:rate=2", "-frames:v", "2", "-c:v", vcodec]
    if vcodec == "libx264":
        cmd += ["-pix_fmt", "yuv420p"]
    cmd.append(path)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _args(**kw):
    base = {"ids": None, "site": None, "transect": None, "force": False}
    base.update(kw)
    return argparse.Namespace(**base)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class RunPhase1SetupTests(unittest.TestCase):
    def setUp(self):
        self.registry_root = tempfile.mkdtemp()
        os.environ["VICARIUS_3D_REGISTRY_ROOT"] = self.registry_root
        import importlib
        importlib.reload(registry_client)
        registry_client.configure({"processing": {"tcrmp": True}})
        self.assertTrue(registry_client.enabled(), "registry_client did not enable with tcrmp=True")

        # Two video source directories (deliberately different, so folder
        # reuse can be tested against the row's OWN video_location).
        self.video_dir_a = tempfile.mkdtemp()  # holds the earlier (2023ann) video
        self.video_dir_b = tempfile.mkdtemp()  # holds the later (2024_pbl) video
        self.video_a = os.path.join(self.video_dir_a, "TCRMP20231015_3D_MRS_T1.MOV")
        self.video_b = os.path.join(self.video_dir_b, "TCRMP20240412_3D_MRS_T1.MP4")
        _tiny_video(self.video_a)
        _tiny_video(self.video_b)

        registry_client.update(
            "MRS_T1_2023ann", site="MRS", transect="T1", year="2023", season_token="ann",
            process="true", video_location=self.video_dir_a,
            original_videos="TCRMP20231015_3D_MRS_T1.MOV",
        )
        registry_client.update(
            "MRS_T1_2024_pbl", site="MRS", transect="T1", year="2024", season_token="_pbl",
            process="true", video_location=self.video_dir_b,
            original_videos="TCRMP20240412_3D_MRS_T1.MP4",
        )

        # create_venv actually running python3.9 -m venv + pip install for
        # every test would be slow and network-dependent; stand in a fast
        # recorder that reproduces just the filesystem effect
        # ensure_project_ready's own idempotency check looks for
        # (.venv/bin/python), so the dedup behavior under test is real.
        self.venv_calls = []

        def _fake_create_venv(project_dir):
            self.venv_calls.append(Path(project_dir))
            venv_bin = Path(project_dir) / ".venv" / "bin"
            venv_bin.mkdir(parents=True, exist_ok=True)
            (venv_bin / "python").touch()

        patcher = mock.patch("run_phase1.create_venv", side_effect=_fake_create_venv)
        self.mock_create_venv = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.registry_root, ignore_errors=True)
        shutil.rmtree(self.video_dir_a, ignore_errors=True)
        shutil.rmtree(self.video_dir_b, ignore_errors=True)
        os.environ.pop("VICARIUS_3D_REGISTRY_ROOT", None)

    # -- select_rows ----------------------------------------------------

    def test_select_rows_default_returns_both_chronological(self):
        rows = run_phase1.select_rows(_args())
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2023ann", "MRS_T1_2024_pbl"])

    def test_select_rows_honors_site_and_transect(self):
        rows = run_phase1.select_rows(_args(site="MRS", transect="T1"))
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2023ann", "MRS_T1_2024_pbl"])
        rows = run_phase1.select_rows(_args(site="BID"))
        self.assertEqual(rows, [])

    def test_select_rows_honors_ids_and_returns_chronological_order(self):
        # Requested in reverse order; select_rows must still return
        # chronological order (the order the shared psx needs), not the
        # order the ids were listed in.
        rows = run_phase1.select_rows(_args(ids="MRS_T1_2024_pbl,MRS_T1_2023ann"))
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2023ann", "MRS_T1_2024_pbl"])

    def test_select_rows_skips_process_false(self):
        registry_client.update(
            "MRS_T2_2022ann", site="MRS", transect="T2", year="2022", season_token="ann",
            process="false", video_location=self.video_dir_a,
        )
        rows = run_phase1.select_rows(_args())
        self.assertNotIn("MRS_T2_2022ann", [r["readable_id"] for r in rows])

    def test_select_rows_skips_missing_video_location(self):
        registry_client.update(
            "MRS_T3_2022ann", site="MRS", transect="T3", year="2022", season_token="ann",
            process="true", video_location="/nonexistent/path/for/this/test",
        )
        rows = run_phase1.select_rows(_args())
        self.assertNotIn("MRS_T3_2022ann", [r["readable_id"] for r in rows])

    def test_select_rows_skips_complete_unless_force(self):
        registry_client.update("MRS_T1_2023ann", step1_status="complete")
        rows = run_phase1.select_rows(_args())
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2024_pbl"])

        rows = run_phase1.select_rows(_args(force=True))
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2023ann", "MRS_T1_2024_pbl"])

    # -- prepare_tcrmp_folder --------------------------------------------

    def test_prepare_tcrmp_folder_creates_named_folder_next_to_video(self):
        row = registry_client.row("MRS_T1_2023ann")
        project_dir = run_phase1.prepare_tcrmp_folder(row)

        expected_name = naming3d.processing_folder_name("MRS", "T1", "20231015")
        self.assertEqual(expected_name, "MRS_T1_2023ann_3dprocessing")
        self.assertEqual(project_dir, __import__("pathlib").Path(self.video_dir_a) / expected_name)
        self.assertTrue(project_dir.is_dir())
        for sub in ("console", "frames", "reports"):
            self.assertTrue((project_dir / sub).is_dir(), sub)

        params_file = project_dir / "analysis_params.yaml"
        self.assertTrue(params_file.is_file())
        import yaml
        params = yaml.safe_load(params_file.read_text())
        self.assertIs(params["processing"]["tcrmp"], True)

        updated = registry_client.row("MRS_T1_2023ann")
        self.assertEqual(updated["processing_folder"], expected_name)
        self.assertEqual(updated["processing_location"], str(project_dir))
        self.assertEqual(updated["console_log"], str(project_dir / "console"))
        self.assertEqual(updated["step"], "1")
        self.assertEqual(updated["stage"], "starting")

        status_csv = project_dir / "status.csv"
        self.assertTrue(status_csv.is_file())
        with open(status_csv, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["original_videos"], "TCRMP20231015_3D_MRS_T1.MOV")
        self.assertEqual(rows[0]["readable_id"], "MRS_T1_2023ann")
        self.assertEqual(rows[0]["Model ID"], "MRS_T1_2023ann")

    def test_prepare_tcrmp_folder_names_for_earliest_timepoint_even_when_later_runs_first(self):
        # Process the LATER timepoint first. The folder must still be named
        # for the earliest known timepoint of the site/transect (2023ann),
        # and it goes next to the row actually being processed (video_dir_b).
        later_row = registry_client.row("MRS_T1_2024_pbl")
        project_dir_first = run_phase1.prepare_tcrmp_folder(later_row)
        self.assertEqual(project_dir_first.name, "MRS_T1_2023ann_3dprocessing")
        self.assertEqual(project_dir_first.parent, __import__("pathlib").Path(self.video_dir_b))

        # Now process the earlier timepoint. It must REUSE the folder just
        # created next to the OTHER row's video, not create a new one next
        # to its own video_location (video_dir_a).
        earlier_row = registry_client.row("MRS_T1_2023ann")
        project_dir_second = run_phase1.prepare_tcrmp_folder(earlier_row)
        self.assertEqual(project_dir_second, project_dir_first)

        for readable_id in ("MRS_T1_2024_pbl", "MRS_T1_2023ann"):
            self.assertEqual(registry_client.row(readable_id)["processing_location"], str(project_dir_first))

    # -- ensure_project_ready / venv setup on the TCRMP path -------------

    def test_ensure_project_ready_calls_create_venv_once(self):
        project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(project_dir), True)

        run_phase1.ensure_project_ready(project_dir)
        run_phase1.ensure_project_ready(project_dir)

        self.assertEqual(self.mock_create_venv.call_count, 1)
        self.assertTrue((project_dir / ".venv" / "bin" / "python").exists())

    def test_prepare_tcrmp_folder_creates_venv_once_per_shared_folder(self):
        # Two rows that resolve to the SAME processing folder (folder reuse,
        # per test_prepare_tcrmp_folder_names_for_earliest_timepoint_...
        # above): create_venv must run exactly once for that folder, not
        # once per row, even though prepare_tcrmp_folder is called twice.
        later_row = registry_client.row("MRS_T1_2024_pbl")
        earlier_row = registry_client.row("MRS_T1_2023ann")

        project_dir_first = run_phase1.prepare_tcrmp_folder(later_row)
        project_dir_second = run_phase1.prepare_tcrmp_folder(earlier_row)

        self.assertEqual(project_dir_first, project_dir_second)
        self.assertEqual(self.mock_create_venv.call_count, 1)
        self.assertEqual(self.venv_calls, [project_dir_first])
        self.assertTrue((project_dir_first / ".venv" / "bin" / "python").exists())


class ResetStep1Tests(unittest.TestCase):
    """status_rows.reset_step1 is what makes --force reach step 1: step1.py
    skips on the status.csv cell, not on the registry. Runs on a throwaway
    project folder with no registry and no Metashape."""

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(os.path.dirname(HERE), "analysis_params.yaml"),
                    os.path.join(self.project_dir, "analysis_params.yaml"))

    def tearDown(self):
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def _row(self, readable_id):
        with open(os.path.join(self.project_dir, "status.csv"), newline="") as fh:
            for row in csv.DictReader(fh):
                if row["Model ID"] == readable_id:
                    return row
        return None

    def test_clears_the_verdict_and_leaves_the_rest_of_the_row(self):
        readable_id = "MRS_T1_2023ann"
        status_rows.write_identity_row(
            self.project_dir, "TCRMP20231015_3D_MRS_T1.MOV", readable_id)
        config = status_rows._config_for(self.project_dir)
        config.update_tracking(readable_id, {
            "Step 1 complete": "True",
            "Status": "Step 1 complete",
            "PSX file": "/somewhere/MRS_T1_2023_2023.psx",
            "Step 1 processing time (s)": "42.0",
            "Scale": "PASS",
        })

        status_rows.reset_step1(self.project_dir, readable_id)

        row = self._row(readable_id)
        self.assertEqual(row["Step 1 complete"], "False")
        self.assertEqual(row["Status"], "Forced rerun")
        # Everything the finished run recorded survives for the rerun to
        # overwrite, so a failed rerun does not erase the history.
        self.assertEqual(row["PSX file"], "/somewhere/MRS_T1_2023_2023.psx")
        self.assertEqual(row["Step 1 processing time (s)"], "42.0")
        self.assertEqual(row["Scale"], "PASS")
        self.assertEqual(row["readable_id"], readable_id)
        self.assertEqual(row["original_videos"], "TCRMP20231015_3D_MRS_T1.MOV")

    def test_creates_the_row_when_the_timepoint_has_none(self):
        readable_id = "MRS_T1_2024_pbl"
        status_rows.reset_step1(self.project_dir, readable_id)
        row = self._row(readable_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["Step 1 complete"], "False")
        self.assertEqual(row["Status"], "Forced rerun")

    def test_is_idempotent(self):
        readable_id = "MRS_T1_2023ann"
        status_rows.write_identity_row(self.project_dir, "v.MOV", readable_id)
        status_rows.reset_step1(self.project_dir, readable_id)
        status_rows.reset_step1(self.project_dir, readable_id)
        with open(os.path.join(self.project_dir, "status.csv"), newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["Model ID"] == readable_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Step 1 complete"], "False")


class PurposeAndConfirmPromptTests(unittest.TestCase):
    """Bug found by the 3D_phase_1 end-to-end pass: run_tcrmp_mode and
    run_non_tcrmp_mode called input() for the purpose whenever --purpose was
    absent, even under --yes, so a hands-free launch (the VICARIUS UI always
    runs with stdin not a terminal) died with EOFError instead of falling
    back to a default. resolve_purpose(), _confirm_proceed(), and
    _interactive() are the factored, testable-without-main() fix: no prompt
    fires unless a human is actually at the keyboard and did not pass --yes.
    """

    def _args(self, purpose=None, yes=False):
        return argparse.Namespace(purpose=purpose, yes=yes)

    def _refuse_input(self, *_args, **_kwargs):
        raise AssertionError("input() must not be called on this path")

    # -- _interactive -----------------------------------------------------

    def test_interactive_true_when_stdin_is_a_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            self.assertTrue(run_phase1._interactive())

    def test_interactive_false_when_stdin_is_not_a_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(run_phase1._interactive())

    def test_interactive_false_when_isatty_raises(self):
        # e.g. stdin closed/redirected in a way that raises rather than
        # returning False; must fail safe (never prompt), not propagate.
        with mock.patch("sys.stdin.isatty", side_effect=ValueError("closed")):
            self.assertFalse(run_phase1._interactive())

    # -- resolve_purpose ----------------------------------------------------

    def test_resolve_purpose_uses_explicit_purpose_flag_without_prompting(self):
        args = self._args(purpose="MRS T1 backfill", yes=False)
        with mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(run_phase1.resolve_purpose(args), "MRS T1 backfill")

    def test_resolve_purpose_defaults_under_yes_with_no_purpose(self):
        args = self._args(purpose=None, yes=True)
        with mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=True):
            self.assertEqual(run_phase1.resolve_purpose(args), run_phase1.DEFAULT_PURPOSE)

    def test_resolve_purpose_defaults_under_non_tty_without_yes(self):
        # The exact bug: no --purpose, no --yes, stdin not a terminal. Must
        # fall back to the default, never call input() / raise EOFError.
        args = self._args(purpose=None, yes=False)
        with mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(run_phase1.resolve_purpose(args), run_phase1.DEFAULT_PURPOSE)

    def test_resolve_purpose_prompts_only_when_interactive_and_not_yes(self):
        args = self._args(purpose=None, yes=False)
        with mock.patch("builtins.input", return_value="typed purpose") as mock_input, \
                mock.patch("sys.stdin.isatty", return_value=True):
            self.assertEqual(run_phase1.resolve_purpose(args), "typed purpose")
        mock_input.assert_called_once()

    def test_resolve_purpose_empty_interactive_answer_falls_back_to_default(self):
        args = self._args(purpose=None, yes=False)
        with mock.patch("builtins.input", return_value="   "), \
                mock.patch("sys.stdin.isatty", return_value=True):
            self.assertEqual(run_phase1.resolve_purpose(args), run_phase1.DEFAULT_PURPOSE)

    def test_resolve_purpose_honors_a_custom_default(self):
        args = self._args(purpose=None, yes=True)
        with mock.patch("builtins.input", side_effect=self._refuse_input):
            self.assertEqual(
                run_phase1.resolve_purpose(args, default="custom default"), "custom default"
            )

    # -- _confirm_proceed ---------------------------------------------------

    def test_confirm_proceed_returns_immediately_under_yes(self):
        args = self._args(yes=True)
        with mock.patch("builtins.input", side_effect=self._refuse_input):
            run_phase1._confirm_proceed(args)  # must not raise / exit

    def test_confirm_proceed_aborts_under_non_tty_without_yes(self):
        # The naming/summary confirmation must never block on input() when
        # nothing can answer it; it aborts loudly instead.
        args = self._args(yes=False)
        with mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                run_phase1._confirm_proceed(args)
            self.assertEqual(ctx.exception.code, 1)

    def test_confirm_proceed_prompts_and_honors_yes_answer(self):
        args = self._args(yes=False)
        with mock.patch("builtins.input", return_value="y"), \
                mock.patch("sys.stdin.isatty", return_value=True):
            run_phase1._confirm_proceed(args)  # must not raise / exit

    def test_confirm_proceed_prompts_and_aborts_on_non_y_answer(self):
        args = self._args(yes=False)
        with mock.patch("builtins.input", return_value="n"), \
                mock.patch("sys.stdin.isatty", return_value=True):
            with self.assertRaises(SystemExit) as ctx:
                run_phase1._confirm_proceed(args)
            self.assertEqual(ctx.exception.code, 0)

    # -- detect_metashape ----------------------------------------------------

    def test_detect_metashape_raises_instead_of_prompting_under_yes(self):
        os.environ.pop("METASHAPE_PATH", None)
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("run_phase1.METASHAPE_SEARCH_PATHS", []), \
                mock.patch("run_phase1.shutil.which", return_value=None), \
                mock.patch("builtins.input", side_effect=self._refuse_input):
            os.environ.pop("METASHAPE_PATH", None)
            with self.assertRaises(RuntimeError):
                run_phase1.detect_metashape(assume_yes=True)

    def test_detect_metashape_raises_instead_of_prompting_under_non_tty(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("run_phase1.METASHAPE_SEARCH_PATHS", []), \
                mock.patch("run_phase1.shutil.which", return_value=None), \
                mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=False):
            os.environ.pop("METASHAPE_PATH", None)
            with self.assertRaises(RuntimeError):
                run_phase1.detect_metashape(assume_yes=False)

    # -- prompt_inputs (non-TCRMP --input/--project prompt) -----------------

    def test_prompt_inputs_raises_instead_of_prompting_under_yes(self):
        with mock.patch("builtins.input", side_effect=self._refuse_input):
            with self.assertRaises(RuntimeError):
                run_phase1.prompt_inputs(assume_yes=True)

    def test_prompt_inputs_raises_instead_of_prompting_under_non_tty(self):
        with mock.patch("builtins.input", side_effect=self._refuse_input), \
                mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                run_phase1.prompt_inputs(assume_yes=False)


class VicariusRootDefaultTests(unittest.TestCase):
    """Task 18a: run_phase1's VICARIUS_ROOT fallback (used to reach the
    platform's _logging/src package) must resolve to a directory that
    actually exists on this box, or platform process logging silently
    never imports on a bare-shell run with no VICARIUS_ROOT set."""

    def test_default_resolves_to_an_existing_directory(self):
        import importlib
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VICARIUS_ROOT", None)
            importlib.reload(run_phase1)
            resolved = run_phase1.VICARIUS_ROOT
        importlib.reload(run_phase1)  # restore module state for later tests

        self.assertTrue(
            os.path.isdir(resolved),
            f"VICARIUS_ROOT default {resolved!r} is not a directory on this box",
        )


if __name__ == "__main__":
    unittest.main()
