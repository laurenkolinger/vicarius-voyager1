"""Tests for run_phase1.py's run guards: the disk-space failsafe
(processing.min_free_disk_gb), the stale processing.force_rerun clear at the
start of every non-force timepoint, the resume-after-interrupted-run path
(step1_status running + free .processing.lock flock), and
registry_client.update's protect_operator forwarding.

All Metashape/venv/step work is mocked; the registry is a temp root via
VICARIUS_3D_REGISTRY_ROOT, so nothing touches the real platform CSV.
"""
import argparse
import fcntl
import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import registry_client  # noqa: E402
import run_phase1  # noqa: E402

TEMPLATE_PARAMS = os.path.join(os.path.dirname(HERE), "analysis_params.yaml")


def _statvfs_result(free_gb):
    """A stand-in os.statvfs result with free_gb GB available
    (f_bavail * f_frsize = free_gb * 1024 * 1 MiB)."""
    return mock.Mock(f_bavail=int(free_gb * 1024), f_frsize=1024 ** 2)


def _args(**kw):
    base = {
        "ids": None, "site": None, "transect": None, "force": False,
        "skip_vim": True, "purpose": "guard tests", "yes": True,
        "param_pairs": [],
    }
    base.update(kw)
    return argparse.Namespace(**base)


class DiskCheckUnitTests(unittest.TestCase):
    """check_free_disk / read_min_free_disk_gb with a mocked statvfs."""

    def setUp(self):
        self.project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.project_dir), True)
        shutil.copy(TEMPLATE_PARAMS, str(self.project_dir / "analysis_params.yaml"))

    def test_template_carries_the_default_threshold(self):
        self.assertEqual(run_phase1.read_min_free_disk_gb(self.project_dir), 200.0)

    def test_default_is_200_when_the_params_file_is_missing(self):
        bare = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(bare), True)
        self.assertEqual(run_phase1.read_min_free_disk_gb(bare), 200.0)
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(199)):
            message = run_phase1.check_free_disk(bare)
        self.assertIsNotNone(message)
        self.assertIn("199.0 GB free", message)
        self.assertIn("at least 200 GB required", message)

    def test_passes_when_enough_space(self):
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(500)) as sv:
            self.assertIsNone(run_phase1.check_free_disk(self.project_dir))
        sv.assert_called_once_with(str(self.project_dir))

    def test_fails_below_threshold_with_plain_message(self):
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(50)):
            message = run_phase1.check_free_disk(self.project_dir)
        self.assertEqual(
            message,
            f"not enough free disk space for {self.project_dir}: 50.0 GB "
            "free, at least 200 GB required (processing.min_free_disk_gb)",
        )

    def test_unreachable_folder_returns_a_message_instead_of_raising(self):
        with mock.patch.object(run_phase1.os, "statvfs",
                               side_effect=OSError("No such file or directory")):
            message = run_phase1.check_free_disk(self.project_dir)
        self.assertIn("cannot check free disk space", message)
        self.assertIn(str(self.project_dir), message)

    def test_zero_disables_the_check_without_calling_statvfs(self):
        run_phase1.set_params_path(
            self.project_dir / "analysis_params.yaml",
            "processing.min_free_disk_gb", "0")
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(0.001)) as sv:
            self.assertIsNone(run_phase1.check_free_disk(self.project_dir))
        sv.assert_not_called()

    def test_threshold_is_read_from_the_project_params(self):
        run_phase1.set_params_path(
            self.project_dir / "analysis_params.yaml",
            "processing.min_free_disk_gb", "400")
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(300)):
            message = run_phase1.check_free_disk(self.project_dir)
        self.assertIn("at least 400 GB required", message)
        with mock.patch.object(run_phase1.os, "statvfs",
                               return_value=_statvfs_result(450)):
            self.assertIsNone(run_phase1.check_free_disk(self.project_dir))


class ProcessingLockProbeTests(unittest.TestCase):
    """processing_lock_held: non-blocking, never creates the lock file."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.lock_path = os.path.join(self.folder, ".processing.lock")

    def test_empty_location_is_not_held(self):
        self.assertFalse(run_phase1.processing_lock_held(""))
        self.assertFalse(run_phase1.processing_lock_held(None))

    def test_missing_lock_file_is_not_held_and_is_not_created(self):
        self.assertFalse(run_phase1.processing_lock_held(self.folder))
        self.assertFalse(os.path.exists(self.lock_path))

    def test_unheld_lock_file_is_not_held(self):
        # The hard-kill situation: the file remains but the OS released the
        # flock when the process died.
        with open(self.lock_path, "w") as fh:
            fh.write("pid=12345 step=step1\n")
        self.assertFalse(run_phase1.processing_lock_held(self.folder))

    def test_held_flock_is_reported_held(self):
        holder = open(self.lock_path, "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(run_phase1.processing_lock_held(self.folder))


class _TcrmpLoopHarness(unittest.TestCase):
    """Shared setup: temp registry with one selectable row, all heavy work
    (venv, steps, run tracking, platform logging) mocked out."""

    READABLE_ID = "MRS_T1_2023ann"

    def setUp(self):
        self.registry_root = tempfile.mkdtemp()
        os.environ["VICARIUS_3D_REGISTRY_ROOT"] = self.registry_root
        importlib.reload(registry_client)
        registry_client.configure({"processing": {"tcrmp": True}})
        self.assertTrue(registry_client.enabled())

        self.corpus_root = tempfile.mkdtemp()
        self.video_dir = os.path.join(self.corpus_root, "2023_annual")
        os.makedirs(self.video_dir)
        registry_client.update(
            self.READABLE_ID, site="MRS", transect="T1", year="2023",
            season_token="ann", process="true", video_location=self.video_dir,
            original_videos="TCRMP20231015_3D_MRS_T1.MOV",
        )

        def _fake_create_venv(project_dir):
            venv_bin = Path(project_dir) / ".venv" / "bin"
            venv_bin.mkdir(parents=True, exist_ok=True)
            (venv_bin / "python").touch()

        for target, kwargs in [
            ("create_venv", {"side_effect": _fake_create_venv}),
            ("create_vicarius_run", {"side_effect": RuntimeError("no run tracking in tests")}),
            ("run_step0", {}),
            ("run_step1", {}),
        ]:
            patcher = mock.patch.object(run_phase1, target, **kwargs)
            setattr(self, "mock_" + target, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(run_phase1, "VICARIUS_LOGGING", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.registry_root, ignore_errors=True)
        shutil.rmtree(self.corpus_root, ignore_errors=True)
        os.environ.pop("VICARIUS_3D_REGISTRY_ROOT", None)

    def _run(self, args, statvfs_free_gb=10000):
        """run_tcrmp_mode with a quiet console; returns captured output.
        statvfs_free_gb may be a number (constant) or a list (side_effect)."""
        if isinstance(statvfs_free_gb, (list, tuple)):
            sv = mock.patch.object(
                run_phase1.os, "statvfs",
                side_effect=[_statvfs_result(g) for g in statvfs_free_gb])
        else:
            sv = mock.patch.object(
                run_phase1.os, "statvfs",
                return_value=_statvfs_result(statvfs_free_gb))
        out = io.StringIO()
        with sv, redirect_stdout(out):
            run_phase1.run_tcrmp_mode(args, "/fake/metashape")
        return out.getvalue()

    def _project_dir(self):
        return Path(registry_client.row(self.READABLE_ID)["processing_location"])

    def _force_rerun_value(self, project_dir):
        params = yaml.safe_load((Path(project_dir) / "analysis_params.yaml").read_text())
        return params["processing"].get("force_rerun")


class ForceStickinessTests(_TcrmpLoopHarness):
    """A stale processing.force_rerun=true from a hard-killed --force run
    must never survive into a later normal run."""

    def _plant_stale_force_true(self):
        # Mimic a killed --force run: folder exists, params carry
        # force_rerun: true, and nothing ever cleared it.
        row = registry_client.row(self.READABLE_ID)
        with redirect_stdout(io.StringIO()):
            project_dir = run_phase1.prepare_tcrmp_folder(row)
        run_phase1.set_params_path(
            project_dir / "analysis_params.yaml", "processing.force_rerun", "true")
        self.assertIs(self._force_rerun_value(project_dir), True)
        return project_dir

    def test_stale_force_true_is_cleared_at_the_start_of_a_non_force_run(self):
        project_dir = self._plant_stale_force_true()
        seen = {}
        self.mock_run_step0.side_effect = (
            lambda p: seen.__setitem__("at_step0", self._force_rerun_value(p)))
        self.mock_run_step1.side_effect = (
            lambda p, m: seen.__setitem__("at_step1", self._force_rerun_value(p)))

        self._run(_args(force=False))

        # Cleared before step 0 even runs, false through step 1, false after.
        self.assertIs(seen["at_step0"], False)
        self.assertIs(seen["at_step1"], False)
        self.assertIs(self._force_rerun_value(project_dir), False)

    def test_force_run_still_sets_true_for_step1_and_clears_after(self):
        seen = {}
        self.mock_run_step1.side_effect = (
            lambda p, m: seen.__setitem__("at_step1", self._force_rerun_value(p)))

        self._run(_args(force=True))

        project_dir = self._project_dir()
        self.assertIs(seen["at_step1"], True)
        self.assertIs(self._force_rerun_value(project_dir), False)


class ResumeAfterInterruptionTests(_TcrmpLoopHarness):
    """step1_status running: a free flock means the previous run died (note +
    resume); a held flock means live work (skip, never queue behind it)."""

    def _mark_running(self):
        registry_client.update(
            self.READABLE_ID, step1_status="running", stage="aligning",
            stage_started="2026-08-30 14:22:10")
        row = registry_client.row(self.READABLE_ID)
        with redirect_stdout(io.StringIO()):
            project_dir = run_phase1.prepare_tcrmp_folder(row)
        # prepare_tcrmp_folder stamps stage starting; restore the state a
        # hard kill leaves behind.
        registry_client.update(self.READABLE_ID, stage="aligning",
                               stage_started="2026-08-30 14:22:10")
        lock_path = project_dir / ".processing.lock"
        lock_path.write_text("pid=99999 step=step1\n")
        return project_dir, lock_path

    def test_free_lock_resumes_and_writes_the_note(self):
        self._mark_running()
        out = self._run(_args())

        note = ("resumed after interrupted run (stopped during aligning, "
                "2026-08-30 14:22:10)")
        self.assertIn(note, registry_client.row(self.READABLE_ID)["notes"])
        self.assertIn("previous run was interrupted", out)
        self.assertEqual(self.mock_run_step0.call_count, 1)
        self.assertEqual(self.mock_run_step1.call_count, 1)

    def test_free_lock_resume_does_not_need_force(self):
        self._mark_running()
        args = _args(force=False)
        self._run(args)
        self.assertEqual(self.mock_run_step1.call_count, 1)

    def test_free_lock_resume_keeps_existing_operator_notes(self):
        registry_client.update(self.READABLE_ID, notes="operator wrote this")
        self._mark_running()
        self._run(_args())
        notes = registry_client.row(self.READABLE_ID)["notes"]
        self.assertIn("operator wrote this", notes)
        self.assertIn("resumed after interrupted run", notes)

    def test_held_lock_skips_the_row_without_touching_it(self):
        project_dir, lock_path = self._mark_running()
        holder = open(lock_path, "r+")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        out = self._run(_args())

        self.assertIn("another process is working on this timepoint", out)
        self.assertEqual(self.mock_run_step0.call_count, 0)
        self.assertEqual(self.mock_run_step1.call_count, 0)
        row = registry_client.row(self.READABLE_ID)
        self.assertNotIn("resumed after interrupted run", row["notes"])
        # The live run's registry row was not clobbered back to "starting".
        self.assertEqual(row["stage"], "aligning")


class DiskFailsafeLoopTests(_TcrmpLoopHarness):
    """The TCRMP loop's disk failsafe: skip + registry record + non-zero
    exit, never a stall and never an aborted run for the other rows."""

    SECOND_ID = "MRS_T2_2023ann"

    def _add_second_row(self):
        video_dir2 = os.path.join(self.corpus_root, "T2_2023_annual")
        os.makedirs(video_dir2)
        registry_client.update(
            self.SECOND_ID, site="MRS", transect="T2", year="2023",
            season_token="ann", process="true", video_location=video_dir2,
            original_videos="TCRMP20231015_3D_MRS_T2.MOV",
        )

    def test_low_disk_before_step0_skips_all_rows_and_exits_nonzero(self):
        self._add_second_row()
        with self.assertRaises(SystemExit) as ctx:
            self._run(_args(), statvfs_free_gb=50)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.mock_run_step0.call_count, 0)
        self.assertEqual(self.mock_run_step1.call_count, 0)
        for rid in (self.READABLE_ID, self.SECOND_ID):
            row = registry_client.row(rid)
            self.assertEqual(row["stage"], "failed", rid)
            self.assertIn("not enough free disk space for", row["notes"])
            self.assertIn("50.0 GB free", row["notes"])
            self.assertEqual(row["step1_status"], "failed")
            self.assertIn("processing.min_free_disk_gb", row["notes"])

    def test_low_disk_before_step1_skips_after_step0_and_exits_nonzero(self):
        # First check (before step 0) sees plenty; the second (before the
        # step 1 launch) sees a nearly full drive.
        with self.assertRaises(SystemExit) as ctx:
            self._run(_args(), statvfs_free_gb=[10000, 50])
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.mock_run_step0.call_count, 1)
        self.assertEqual(self.mock_run_step1.call_count, 0)
        row = registry_client.row(self.READABLE_ID)
        self.assertEqual(row["stage"], "failed")
        self.assertIn("not enough free disk space", row["notes"])

    def test_enough_disk_runs_both_steps_and_exits_cleanly(self):
        self._run(_args(), statvfs_free_gb=10000)  # returns, no SystemExit
        self.assertEqual(self.mock_run_step0.call_count, 1)
        self.assertEqual(self.mock_run_step1.call_count, 1)
        row = registry_client.row(self.READABLE_ID)
        self.assertNotIn("not enough free disk space", row["notes"] or "")
        self.assertNotEqual(row["stage"], "failed")

    def test_param_override_reaches_the_threshold(self):
        # The UI path: --param processing.min_free_disk_gb=0 disables the
        # failsafe for this run even on a nearly full drive.
        args = _args(param_pairs=[("processing.min_free_disk_gb", "0")])
        self._run(args, statvfs_free_gb=1)
        self.assertEqual(self.mock_run_step1.call_count, 1)


class ProcessingLockWindowTests(_TcrmpLoopHarness):
    """The driver holds the processing lock through the prepare-and-extract
    window and releases it before step 1 launches (step 1, a separate
    process, takes its own), so the whole run reads as live to the atlas
    with no gap during extraction."""

    def test_lock_held_during_step0_released_before_step1(self):
        seen = {}

        def _spy_step0(project_dir):
            seen["during_step0"] = run_phase1.processing_lock_held(str(project_dir))

        def _spy_step1(project_dir, metashape_path):
            seen["during_step1_launch"] = run_phase1.processing_lock_held(str(project_dir))

        self.mock_run_step0.side_effect = _spy_step0
        self.mock_run_step1.side_effect = _spy_step1
        self._run(_args())
        self.assertIs(seen["during_step0"], True)
        self.assertIs(seen["during_step1_launch"], False)

    def test_lock_released_even_when_step0_raises(self):
        self.mock_run_step0.side_effect = RuntimeError("extraction blew up")
        with self.assertRaises(SystemExit):
            self._run(_args())
        self.assertFalse(
            run_phase1.processing_lock_held(str(self._project_dir()))
        )

    def test_held_lock_from_another_process_skips_the_row(self):
        # Pre-create the folder and hold its lock the way another driver
        # would; the loop must skip the row without touching the steps.
        project_dir = Path(self.corpus_root) / "MRS_T1_2023ann_3dprocessing"
        project_dir.mkdir()
        holder = open(project_dir / ".processing.lock", "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        out = self._run(_args())
        self.assertIn("another process is working", out)
        self.assertEqual(self.mock_run_step0.call_count, 0)
        self.assertEqual(self.mock_run_step1.call_count, 0)

    def test_stage_reconciled_when_step1_had_nothing_to_do(self):
        # A clean step 1 exit that never touched the row (for example a
        # forced rerun whose frames subfolder vanished) must not leave the
        # stage on an active word: prepare wrote "starting", and with the
        # lock now free the atlas would badge the row interrupted forever.
        registry_client.update(self.READABLE_ID, step1_status="complete")
        self._run(_args(force=True))
        row = registry_client.row(self.READABLE_ID)
        self.assertEqual(row["stage"], "done")


class ProtectOperatorForwardingTests(unittest.TestCase):
    """registry_client.update(protect_operator=...) forwards to the shared
    upsert and keeps operator-owned cells when asked."""

    READABLE_ID = "MRS_T1_2023ann"

    def setUp(self):
        self.registry_root = tempfile.mkdtemp()
        os.environ["VICARIUS_3D_REGISTRY_ROOT"] = self.registry_root
        importlib.reload(registry_client)
        registry_client.configure({"processing": {"tcrmp": True}})
        self.assertTrue(registry_client.enabled())

    def tearDown(self):
        shutil.rmtree(self.registry_root, ignore_errors=True)
        os.environ.pop("VICARIUS_3D_REGISTRY_ROOT", None)

    def test_protect_operator_keeps_operator_cells(self):
        registry_client.update(
            self.READABLE_ID, site="MRS", transect="T1", year="2023",
            season_token="ann", notes="operator wrote this")
        registry_client.update(
            self.READABLE_ID, protect_operator=True,
            notes="machine note", processing_folder="MRS_T1_2023ann_3dprocessing")
        row = registry_client.row(self.READABLE_ID)
        # The operator's cell survives; the non-operator cell still updates.
        self.assertEqual(row["notes"], "operator wrote this")
        self.assertEqual(row["processing_folder"], "MRS_T1_2023ann_3dprocessing")

    def test_protect_operator_defaults_off(self):
        registry_client.update(self.READABLE_ID, site="MRS", transect="T1",
                               year="2023", season_token="ann", notes="old")
        registry_client.update(self.READABLE_ID, notes="new")
        self.assertEqual(registry_client.row(self.READABLE_ID)["notes"], "new")

    def test_protect_operator_fills_empty_operator_cells(self):
        registry_client.update(self.READABLE_ID, site="MRS", transect="T1",
                               year="2023", season_token="ann")
        registry_client.update(self.READABLE_ID, protect_operator=True,
                               notes="first note")
        self.assertEqual(registry_client.row(self.READABLE_ID)["notes"], "first note")

    def test_update_forwards_protect_operator_to_upsert(self):
        with mock.patch.object(registry_client, "_registry") as fake:
            registry_client.update("X_T1_2020ann", protect_operator=True, site="MRS")
            fake.upsert.assert_called_once_with(
                "X_T1_2020ann", {"site": "MRS"}, actor=registry_client.ACTOR,
                protect_operator=True)
            fake.reset_mock()
            registry_client.update("X_T1_2020ann", site="MRS")
            fake.upsert.assert_called_once_with(
                "X_T1_2020ann", {"site": "MRS"}, actor=registry_client.ACTOR,
                protect_operator=False)


if __name__ == "__main__":
    unittest.main()
