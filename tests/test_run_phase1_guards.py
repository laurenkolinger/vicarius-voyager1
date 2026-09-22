"""Tests for run_phase1.py's run guards: the disk-space failsafe
(processing.min_free_disk_gb), the stale processing.force_rerun clear at the
start of every non-force timepoint, the resume-after-interrupted-run path
(step1_status running + free .processing.lock flock), and
registry_client.update's protect_operator forwarding.

All Metashape/venv/step work is mocked; the registry is a temp root via
VICARIUS_3D_REGISTRY_ROOT, so nothing touches the real platform CSV.
"""
import argparse
import contextlib
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
        "param_pairs": [], "run_id": None, "params_version": None,
        "params_file": None,
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
        project_dir = Path(self.corpus_root) / "MRS_T1_3D"
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
            notes="machine note", processing_folder="MRS_T1_3D")
        row = registry_client.row(self.READABLE_ID)
        # The operator's cell survives; the non-operator cell still updates.
        self.assertEqual(row["notes"], "operator wrote this")
        self.assertEqual(row["processing_folder"], "MRS_T1_3D")

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


# ---------------------------------------------------------------------------
# Voyager 1 run identity: --run-id, --params-version, --params-file
# ---------------------------------------------------------------------------

def _shared_registry():
    """The shared registry module reloaded against the current temp root."""
    lib_dir = os.path.join(
        os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius"), "_METADATA", "3d")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import registry
    return importlib.reload(registry)


def _write_params_file(path, frames=123, marker="# seeded-by-test"):
    """A valid Voyager 1 parameter file: the template with one changed value
    and a marker comment, so a seeded folder can be told from a template copy."""
    text = Path(TEMPLATE_PARAMS).read_text()
    text = run_phase1.set_yaml_path(text, "processing.frames_per_transect", str(frames))
    Path(path).write_text(marker + "\n" + text)
    return Path(path)


class RunIdentityFlagTests(unittest.TestCase):
    """The three new flags are parsed by argparse and validated before any
    work starts: a bad value is a parser error (exit 2), never a crash
    minutes later inside a processing folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.good = _write_params_file(self.tmp / "good.yaml")

    def _parse(self, argv):
        parser = run_phase1.build_parser()
        args = parser.parse_args(argv)
        run_phase1.finalize_args(parser, args)
        return args

    def _refused(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            self._parse(argv)
        self.assertEqual(ctx.exception.code, 2)
        return err.getvalue()

    def test_flags_default_to_none(self):
        args = self._parse(["--yes", "--skip-vim"])
        self.assertIsNone(args.run_id)
        self.assertIsNone(args.params_version)
        self.assertIsNone(args.params_file)

    def test_flags_parse_together(self):
        args = self._parse([
            "--run-id", "TCRMP_3sep26_LO_MRS3_23ann-25pbl",
            "--params-version", "v1.0.0",
            "--params-file", str(self.good), "--yes", "--skip-vim"])
        self.assertEqual(args.run_id, "TCRMP_3sep26_LO_MRS3_23ann-25pbl")
        self.assertEqual(args.params_version, "v1.0.0")
        self.assertEqual(args.params_file, Path(os.path.abspath(self.good)))

    def test_spec_run_id_shapes_are_accepted(self):
        for run_id in ("TCRMP_3sep26_LO_MRS3_23ann-25pbl",
                       "TCRMP_3sep26_LO_MRS3+LBH2_23ann-25pbl",
                       "TCRMP_12dec26_ABCD_LBH1_24ann-24ann", "r1", "a.b"):
            self.assertEqual(run_phase1.validate_run_id(run_id), run_id, run_id)

    def test_hostile_run_ids_are_refused_by_the_parser(self):
        for run_id in ("", "   ", "a b", "x\n", "x\ny", "$(rm -rf /)", "a/b", "../x",
                       "-x", "_x", ".x", "a;b", "a,b", "a\tb", "r" * 129, "x\x00y"):
            message = self._refused(["--run-id", run_id, "--yes", "--skip-vim"])
            self.assertIn("--run-id", message, repr(run_id))

    def test_run_id_wrong_type_is_a_type_error(self):
        with self.assertRaises(TypeError):
            run_phase1.validate_run_id(42)
        self.assertIsNone(run_phase1.validate_run_id(None))

    def test_params_version_shapes_are_accepted(self):
        for ref in ("v1.0.0", "v12.3.44", "branches/coral-2026/v1.0.0",
                    "custom/TCRMP_3sep26_LO_MRS3+LBH2_23ann-25pbl", "default"):
            self.assertEqual(run_phase1.validate_params_version(ref), ref, ref)

    def test_hostile_params_versions_are_refused_by_the_parser(self):
        for ref in ("", "  ", "v1 0", "x\n", "/v1", "-v1", "a b/c", "v" * 129, "v1\x00"):
            message = self._refused(["--params-version", ref, "--yes", "--skip-vim"])
            self.assertIn("--params-version", message, repr(ref))
        with self.assertRaises(TypeError):
            run_phase1.validate_params_version(1.0)

    def test_params_file_missing_is_refused(self):
        message = self._refused(["--params-file", str(self.tmp / "nope.yaml"), "--yes"])
        self.assertIn("--params-file", message)
        self.assertIn("does not exist", message)

    def test_params_file_directory_is_refused(self):
        message = self._refused(["--params-file", str(self.tmp), "--yes"])
        self.assertIn("directory", message)

    def test_params_file_empty_is_refused(self):
        empty = self.tmp / "empty.yaml"
        empty.write_text("")
        message = self._refused(["--params-file", str(empty), "--yes"])
        self.assertIn("empty", message)

    def test_params_file_not_yaml_is_refused(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("processing: [unclosed\n  : : :\n")
        message = self._refused(["--params-file", str(bad), "--yes"])
        self.assertIn("not valid YAML", message)

    def test_params_file_binary_is_refused(self):
        binary = self.tmp / "bin.yaml"
        binary.write_bytes(b"\xff\xfe\x00\x00processing:\x00" + os.urandom(64))
        message = self._refused(["--params-file", str(binary), "--yes"])
        self.assertIn("--params-file", message)

    def test_params_file_without_processing_key_is_refused(self):
        for text in ("project:\n  name: x\n", "- a\n- b\n", "just a string\n",
                     "processing: 5\n", "processing: [1, 2]\n", "processing:\n"):
            path = self.tmp / "shape.yaml"
            path.write_text(text)
            message = self._refused(["--params-file", str(path), "--yes"])
            self.assertIn("processing", message, repr(text))

    def test_params_file_over_the_size_cap_is_refused(self):
        huge = self.tmp / "huge.yaml"
        with open(huge, "wb") as fh:
            fh.write(b"processing:\n  tcrmp: true\n")
            fh.write(b"# " + b"x" * run_phase1.PARAMS_FILE_MAX_BYTES + b"\n")
        message = self._refused(["--params-file", str(huge), "--yes"])
        self.assertIn("larger than", message)

    def test_params_file_wrong_type_is_a_type_error(self):
        with self.assertRaises(TypeError):
            run_phase1.validate_params_file(123)
        self.assertIsNone(run_phase1.validate_params_file(None))


class RunFactsInLoopTests(_TcrmpLoopHarness):
    """prepare_tcrmp_folder writes the run id and params version as voyager1
    facts and seeds a new folder's analysis_params.yaml from --params-file."""

    def _facts(self, readable_id=None):
        registry = _shared_registry()
        return {line["key"]: line
                for line in registry.facts(readable_id or self.READABLE_ID, "voyager1")}

    def _params(self, project_dir=None):
        params_path = Path(project_dir or self._project_dir()) / "analysis_params.yaml"
        return yaml.safe_load(params_path.read_text()), params_path.read_text()

    def test_run_id_and_params_version_become_voyager1_facts(self):
        self._run(_args(run_id="TCRMP_3sep26_LO_MRS3_23ann-25pbl", params_version="v1.0.0"))
        facts = self._facts()
        self.assertEqual(facts["run_id"]["value"], "TCRMP_3sep26_LO_MRS3_23ann-25pbl")
        self.assertEqual(facts["params_version"]["value"], "v1.0.0")
        self.assertEqual(facts["run_id"]["recorded_by"], registry_client.ACTOR)
        self.assertEqual(facts["run_id"]["unit"], "")
        self.assertEqual(facts["run_id"]["link"], "")

    def test_facts_are_written_before_step0_runs(self):
        seen = {}
        self.mock_run_step0.side_effect = lambda p: seen.__setitem__("facts", self._facts())
        self._run(_args(run_id="R1", params_version="v1.0.0"))
        self.assertEqual(seen["facts"]["run_id"]["value"], "R1")

    def test_no_flags_writes_no_facts_file(self):
        self._run(_args())
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))

    def test_only_the_flags_given_are_written(self):
        self._run(_args(run_id="R1"))
        self.assertEqual(list(self._facts()), ["run_id"])
        self._run(_args(params_version="v2.0.0"))
        facts = self._facts()
        self.assertEqual(facts["run_id"]["value"], "R1")
        self.assertEqual(facts["params_version"]["value"], "v2.0.0")

    def test_rerun_with_the_same_flags_keeps_one_line_per_key(self):
        self._run(_args(run_id="R1", params_version="v1.0.0"))
        self._run(_args(run_id="R1", params_version="v1.0.0"))
        registry = _shared_registry()
        keys = [line["key"] for line in registry.facts(self.READABLE_ID, "voyager1")]
        self.assertEqual(sorted(keys), ["params_version", "run_id"])

    def test_facts_go_to_every_selected_row(self):
        video_dir2 = os.path.join(self.corpus_root, "T2_2023_annual")
        os.makedirs(video_dir2)
        registry_client.update(
            "MRS_T2_2023ann", site="MRS", transect="T2", year="2023",
            season_token="ann", process="true", video_location=video_dir2,
            original_videos="TCRMP20231015_3D_MRS_T2.MOV")
        self._run(_args(run_id="R9"))
        self.assertEqual(self._facts()["run_id"]["value"], "R9")
        self.assertEqual(self._facts("MRS_T2_2023ann")["run_id"]["value"], "R9")

    def test_params_file_seeds_a_new_folder(self):
        seed = _write_params_file(Path(self.corpus_root) / "custom.yaml", frames=123)
        out = self._run(_args(params_file=seed))
        params, text = self._params()
        self.assertEqual(params["processing"]["frames_per_transect"], 123)
        self.assertIs(params["processing"]["tcrmp"], True)
        self.assertIn("# seeded-by-test", text)
        self.assertIn(str(seed), out)

    def test_params_file_is_ignored_on_an_existing_folder_with_a_note(self):
        row = registry_client.row(self.READABLE_ID)
        with redirect_stdout(io.StringIO()):
            project_dir = run_phase1.prepare_tcrmp_folder(row)
        before = self._params(project_dir)[1]
        seed = _write_params_file(Path(self.corpus_root) / "custom.yaml", frames=123)
        out = self._run(_args(params_file=seed))
        params, text = self._params(project_dir)
        self.assertEqual(params["processing"]["frames_per_transect"], 1000)
        self.assertNotIn("# seeded-by-test", text)
        self.assertIn("ignored", out)
        self.assertIn(str(seed), out)
        # The run's own force_rerun pin is the only line the loop writes; the
        # ignored seed changed nothing else in the template-seeded file.
        self.assertEqual(text.replace("  force_rerun: false\n", ""), before)

    def test_param_overrides_still_apply_on_top_of_a_seeded_file(self):
        seed = _write_params_file(Path(self.corpus_root) / "custom.yaml", frames=123)
        self._run(_args(params_file=seed,
                        param_pairs=[("processing.frames_per_transect", "77")]))
        params, text = self._params()
        self.assertEqual(params["processing"]["frames_per_transect"], 77)
        self.assertIn("# seeded-by-test", text)

    def test_seed_helper_names_its_source(self):
        project_dir = Path(self.corpus_root) / "fresh_3d"
        project_dir.mkdir()
        seed = _write_params_file(Path(self.corpus_root) / "custom.yaml")
        self.assertEqual(run_phase1.seed_analysis_params(project_dir, seed), seed)
        self.assertIsNone(run_phase1.seed_analysis_params(project_dir, seed))
        other = Path(self.corpus_root) / "other_3d"
        other.mkdir()
        self.assertEqual(run_phase1.seed_analysis_params(other), run_phase1.TEMPLATE_PARAMS)

    def test_seed_helper_refuses_a_vanished_params_file(self):
        project_dir = Path(self.corpus_root) / "fresh_3d"
        project_dir.mkdir()
        with self.assertRaises(RuntimeError) as ctx:
            run_phase1.seed_analysis_params(project_dir, Path(self.corpus_root) / "gone.yaml")
        self.assertIn("gone.yaml", str(ctx.exception))
        self.assertFalse((project_dir / "analysis_params.yaml").exists())

    def test_record_run_facts_outside_tcrmp_mode_writes_nothing(self):
        registry_client.configure({"processing": {"tcrmp": False}})
        self.assertIsNone(run_phase1.record_run_facts(self.READABLE_ID, "R1", "v1.0.0"))
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))

    def test_record_run_facts_with_nothing_to_say_writes_nothing(self):
        self.assertIsNone(run_phase1.record_run_facts(self.READABLE_ID))
        self.assertIsNone(run_phase1.record_run_facts(self.READABLE_ID, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))


class NonTcrmpSeedTests(unittest.TestCase):
    """setup_project (the non-TCRMP folder) seeds from --params-file too, and
    keeps an existing file as before."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        patcher = mock.patch.object(run_phase1, "create_venv")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_project_is_seeded_from_the_params_file(self):
        seed = _write_params_file(self.tmp / "custom.yaml", frames=55)
        project = self.tmp / "project"
        with redirect_stdout(io.StringIO()):
            run_phase1.setup_project(project, self.tmp, "frames", {}, params_file=seed)
        params = yaml.safe_load((project / "analysis_params.yaml").read_text())
        self.assertEqual(params["processing"]["frames_per_transect"], 55)
        self.assertIs(params["processing"]["tcrmp"], False)

    def test_existing_project_file_is_kept(self):
        project = self.tmp / "project"
        project.mkdir()
        (project / "analysis_params.yaml").write_text("processing:\n  frames_per_transect: 9\n")
        seed = _write_params_file(self.tmp / "custom.yaml", frames=55)
        out = io.StringIO()
        with redirect_stdout(out):
            run_phase1.setup_project(project, self.tmp, "frames", {}, params_file=seed)
        params = yaml.safe_load((project / "analysis_params.yaml").read_text())
        self.assertEqual(params["processing"]["frames_per_transect"], 9)
        self.assertIn("ignored", out.getvalue())


# ---------------------------------------------------------------------------
# Step 0 and step 1 fact writes, through the registry_client the steps import
# ---------------------------------------------------------------------------

STEP_PARAMS = """
project:
  name: "fact write test"
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


def _load_step(module_name, project_dir):
    """Import (or re-import) step0 or step1 against `project_dir`, the way
    tests/test_progress_logging.py stages them: a stub Metashape module and
    config.py re-read from sys.argv[1]."""
    import types
    if "Metashape" not in sys.modules:
        sys.modules["Metashape"] = types.ModuleType("Metashape")
    saved_argv = sys.argv[:]
    sys.argv = [module_name, str(project_dir)]
    try:
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])
        else:
            import config  # noqa: F401
        if module_name in sys.modules:
            return importlib.reload(sys.modules[module_name])
        return importlib.import_module(module_name)
    finally:
        sys.argv = saved_argv


class _StepFactHarness(unittest.TestCase):
    """A temp registry root with one row, and step0/step1 loaded against a
    temp processing folder whose analysis_params.yaml says tcrmp: true."""

    READABLE_ID = "MRS_T1_2023ann"

    def setUp(self):
        self.registry_root = tempfile.mkdtemp()
        os.environ["VICARIUS_3D_REGISTRY_ROOT"] = self.registry_root
        self.addCleanup(shutil.rmtree, self.registry_root, True)
        self.addCleanup(os.environ.pop, "VICARIUS_3D_REGISTRY_ROOT", None)
        self.project_dir = tempfile.mkdtemp(prefix="facts_project_")
        self.addCleanup(shutil.rmtree, self.project_dir, True)
        with open(os.path.join(self.project_dir, "analysis_params.yaml"), "w") as fh:
            fh.write(STEP_PARAMS)
        importlib.reload(registry_client)
        registry_client.configure({"processing": {"tcrmp": True}})
        registry_client.update(
            self.READABLE_ID, site="MRS", transect="T1", year="2023",
            season_token="ann", process="true",
            original_videos="TCRMP20231015_3D_MRS_T1.MOV")

    def _facts(self):
        registry = _shared_registry()
        return {line["key"]: line for line in registry.facts(self.READABLE_ID, "voyager1")}

    def _row(self):
        return registry_client.row(self.READABLE_ID)


class Step0FactWriteTests(_StepFactHarness):
    """step0.process_timepoint: console_log while extracting, five facts on success."""

    PROBE = {"width": 3840, "height": 2160, "codec": "hevc", "duration_s": 10.0,
             "nb_frames": 300, "container": "mov,mp4"}

    def setUp(self):
        super().setUp()
        self.step0 = _load_step("step0", self.project_dir)
        self.step0.registry_client.configure(self.step0.PARAMS)
        self.assertTrue(self.step0.registry_client.enabled())
        for target, kwargs in [
            ("probe", {"return_value": dict(self.PROBE)}),
        ]:
            patcher = mock.patch.object(self.step0.videos, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.step0, "extract_frames_for_part", return_value=(7, []))
        self.mock_extract = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.step0.manifest, "append_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.step0.os.path, "getsize", return_value=2_000_000_000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row_dict(self):
        return {"readable_id": self.READABLE_ID,
                "original_videos": "TCRMP20231015_3D_MRS_T1.MOV"}

    def _process(self):
        with redirect_stdout(io.StringIO()):
            return self.step0.process_timepoint(
                self.READABLE_ID, ["/fake/TCRMP20231015_3D_MRS_T1.MOV"],
                row=self._row_dict(), tcrmp=True)

    def test_console_log_points_at_the_step0_log_while_extracting(self):
        seen = {}

        def _spy_extract(*a, **k):
            seen["console_log"] = self._row()["console_log"]
            seen["stage"] = self._row()["stage"]
            return 7, []

        self.mock_extract.side_effect = _spy_extract
        self.assertEqual(self._process(), (self.READABLE_ID, True))
        self.assertEqual(seen["console_log"], self.step0.step_log_path("step0"))
        self.assertEqual(seen["stage"], "extracting")

    def test_success_writes_the_five_step0_facts(self):
        self._process()
        facts = self._facts()
        self.assertEqual(sorted(facts), ["console_log_step0", "frames_extracted",
                                         "step0_finished", "step0_seconds", "step0_started"])
        self.assertEqual(facts["frames_extracted"]["value"], "7")
        self.assertEqual(facts["frames_extracted"]["unit"], "count")
        self.assertEqual(facts["step0_seconds"]["unit"], "s")
        self.assertGreaterEqual(float(facts["step0_seconds"]["value"]), 0.0)
        for key in ("step0_started", "step0_finished"):
            self.assertTrue(facts[key]["value"].endswith("-04:00"), facts[key]["value"])
            self.assertRegex(facts[key]["value"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-04:00$")
        self.assertLessEqual(facts["step0_started"]["value"], facts["step0_finished"]["value"])
        log_path = self.step0.step_log_path("step0")
        self.assertEqual(facts["console_log_step0"]["value"], log_path)
        self.assertEqual(facts["console_log_step0"]["link"], log_path)
        for line in facts.values():
            self.assertEqual(line["recorded_by"], registry_client.ACTOR)

    def test_failure_writes_no_facts_and_closes_the_row(self):
        self.mock_extract.side_effect = RuntimeError("ffmpeg died")
        self.assertEqual(self._process(), (self.READABLE_ID, False))
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))
        self.assertEqual(self._row()["stage"], "failed")

    def test_zero_frames_requested_still_records_the_facts(self):
        with mock.patch.object(self.step0, "FRAMES_PER_TRANSECT", 0):
            self._process()
        facts = self._facts()
        self.assertEqual(facts["frames_extracted"]["value"], "0")
        self.assertIn("console_log_step0", facts)

    def test_outside_tcrmp_mode_nothing_is_written(self):
        self.step0.registry_client.configure({"processing": {"tcrmp": False}})
        with mock.patch.object(self.step0.registry_client, "_registry") as fake:
            self._process()
            fake.set_facts.assert_not_called()
            fake.upsert.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))

    def test_a_facts_write_failure_is_logged_not_raised(self):
        with mock.patch.object(self.step0.registry_client, "facts",
                               side_effect=OSError("disk full")):
            with self.assertLogs(level="WARNING") as caught:
                self.assertEqual(self._process(), (self.READABLE_ID, True))
        self.assertTrue(any("step 0 facts" in r.getMessage() for r in caught.records))

    def test_ast_stamp_carries_the_offset(self):
        import datetime
        stamp = self.step0.ast_stamp(datetime.datetime(2026, 9, 4, 7, 5, 9))
        self.assertRegex(stamp, r"^2026-09-04T\d{2}:\d{2}:09-04:00$")


class Step1FactWriteTests(_StepFactHarness):
    """step1.registry_success: the report path and the step 1 console log."""

    def setUp(self):
        super().setUp()
        self.step1 = _load_step("step1", self.project_dir)
        self.step1.registry_client.configure(self.step1.PARAMS)
        self.assertTrue(self.step1.registry_client.enabled())
        patcher = mock.patch.object(self.step1.registry_client, "snapshot")
        self.mock_snapshot = patcher.start()
        self.addCleanup(patcher.stop)

    def _facts_dict(self, **extra):
        facts = {
            "seconds": 1234.5, "tie_points": 100000, "faces_full": 5000000,
            "faces_delivery": 500000, "texture_pages": 4, "dem_mm_per_pix": 0.7,
            "params_summary": "frames=10", "scale_status": "PASS",
            "scale_error_m": 0.002, "scale_bars": 2,
            "end_time": "2026-09-04 07:05:09",
            "report_file": os.path.join(self.project_dir, "reports", f"{self.READABLE_ID}_step1.pdf"),
        }
        facts.update(extra)
        return facts

    def test_success_writes_report_and_console_facts(self):
        psx = os.path.join(self.project_dir, "MRS_T1_2023_2023.psx")
        self.step1.registry_success(self.READABLE_ID, self._facts_dict(), psx)
        facts = self._facts()
        report = os.path.join(self.project_dir, "reports", f"{self.READABLE_ID}_step1.pdf")
        self.assertEqual(sorted(facts), ["console_log_step1", "step1_report"])
        self.assertEqual(facts["step1_report"]["value"], report)
        self.assertEqual(facts["step1_report"]["link"], report)
        log_path = self.step1.step_log_path("step1")
        self.assertEqual(facts["console_log_step1"]["value"], log_path)
        self.assertEqual(facts["console_log_step1"]["link"], log_path)
        row = self._row()
        self.assertEqual(row["step1_status"], "complete")
        self.assertEqual(row["manual_edit_status"], "awaiting")

    def test_missing_report_writes_only_the_console_fact(self):
        psx = os.path.join(self.project_dir, "MRS_T1_2023_2023.psx")
        self.step1.registry_success(self.READABLE_ID, self._facts_dict(report_file=""), psx)
        self.assertEqual(sorted(self._facts()), ["console_log_step1"])

    def test_outside_tcrmp_mode_nothing_is_written(self):
        self.step1.registry_client.configure({"processing": {"tcrmp": False}})
        psx = os.path.join(self.project_dir, "MRS_T1_2023_2023.psx")
        with mock.patch.object(self.step1.registry_client, "_registry") as fake:
            self.step1.registry_success(self.READABLE_ID, self._facts_dict(), psx)
            fake.set_facts.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.registry_root, "row_facts.csv")))

    def test_a_facts_write_failure_is_logged_and_the_row_still_completes(self):
        psx = os.path.join(self.project_dir, "MRS_T1_2023_2023.psx")
        with mock.patch.object(self.step1.registry_client, "facts",
                               side_effect=OSError("disk full")):
            with self.assertLogs(level="WARNING") as caught:
                self.step1.registry_success(self.READABLE_ID, self._facts_dict(), psx)
        self.assertTrue(any("step 1 facts" in r.getMessage() for r in caught.records))
        self.assertEqual(self._row()["step1_status"], "complete")
        self.mock_snapshot.assert_called_once()


# ---------------------------------------------------------------------------
# print_manual_instructions reads the shared checklist; output unchanged
# ---------------------------------------------------------------------------

MANUAL_PROJECT_DIR = Path("/data/MRS_T1_3D")
PREVIOUS_MANUAL_TEXT = "\n".join([
    "",
    "=" * 60,
    "  PHASE 1 COMPLETE - MANUAL STEP REQUIRED",
    "=" * 60,
    "",
    "  Before running Phase 2, you must manually straighten and prepare",
    "  each model in the Metashape GUI.",
    "",
    f"  PSX file location: {MANUAL_PROJECT_DIR}/",
    "",
    "  For each chunk in each PSX file:",
    "",
    "  STRAIGHTENING (always required):",
    "    1. Load the textured model",
    "    2. Auto-adjust brightness/contrast on an image to improve texture",
    "    3. Switch to rotate model view",
    "    4. Rotate the model so it aligns horizontally at the top of the view",
    "    5. Use 'Model > Region > Rotate Region to View' to set alignment",
    "    6. Resize the region to crop to the model area (use top XY & side views)",
    "    7. Use the rectangular crop tool to crop within the region bounds",
    "",
    "  SCALING PREPARATION:",
    "    1. Ensure coded targets are visible and properly positioned",
    "    2. Verify at least 2 scale bars worth of targets are clearly visible",
    "",
    "  3. Save the project and quit Metashape",
    "",
    "  When done with all models, continue with the step 2 module for",
    "  automatic scaling and export (see STEP2_HANDOFF.md at the",
    "  module top level).",
    "",
    "=" * 60,
]) + "\n"


class ManualInstructionTests(unittest.TestCase):
    """The printed checklist is byte-identical to the text the runner printed
    before manual_edit_checklist.yaml existed, whether it comes from the
    shared reader or from the built-in fallback."""

    def _capture(self):
        out = io.StringIO()
        with redirect_stdout(out):
            run_phase1.print_manual_instructions(MANUAL_PROJECT_DIR)
        return out.getvalue()

    def _without_manualedit(self):
        """Hide the manual edit module: its clone off sys.path, its modules
        out of sys.modules, the repo constant pointed at an empty folder."""
        saved_path = sys.path[:]
        saved_modules = {name: sys.modules.pop(name) for name in list(sys.modules)
                         if name == "manualedit" or name.startswith("manualedit.")}
        sys.path[:] = [p for p in sys.path if not p.rstrip("/").endswith("manual_edit/github_repo")]
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)

        def _restore():
            sys.path[:] = saved_path
            sys.modules.update(saved_modules)

        self.addCleanup(_restore)
        return mock.patch.object(run_phase1, "MANUAL_EDIT_REPO", Path(empty))

    def test_output_is_byte_identical_to_the_previous_text(self):
        self.assertEqual(self._capture(), PREVIOUS_MANUAL_TEXT)

    def test_output_comes_from_the_shared_checklist(self):
        with mock.patch.object(run_phase1, "_checklist_console",
                               wraps=run_phase1._checklist_console) as spy:
            text = self._capture()
        spy.assert_called_once()
        self.assertEqual(text, PREVIOUS_MANUAL_TEXT)
        self.assertIn("manualedit.checklist", sys.modules)

    def test_checklist_path_is_the_clone_file(self):
        self.assertEqual(run_phase1.CHECKLIST_PATH,
                         run_phase1.GITHUB_REPO_DIR / "manual_edit_checklist.yaml")
        self.assertTrue(run_phase1.CHECKLIST_PATH.is_file())

    def _assert_fallback(self, text, *fragments):
        """One note line, then exactly the previous text (which opens with
        the blank line banner() prints)."""
        note, _, rest = text.partition("\n")
        self.assertIn("built-in", note)
        self.assertIn("could not be read", note)
        for fragment in fragments:
            self.assertIn(fragment, note)
        self.assertEqual(rest, PREVIOUS_MANUAL_TEXT)

    def test_fallback_when_the_module_cannot_be_imported(self):
        with self._without_manualedit():
            text = self._capture()
        self._assert_fallback(text, "manualedit")

    def test_fallback_when_the_yaml_is_missing(self):
        with mock.patch.object(run_phase1, "CHECKLIST_PATH", Path("/nonexistent/checklist.yaml")):
            text = self._capture()
        self._assert_fallback(text, "/nonexistent/checklist.yaml")

    def test_fallback_when_the_yaml_is_malformed(self):
        bad = Path(tempfile.mkdtemp()) / "checklist.yaml"
        self.addCleanup(shutil.rmtree, str(bad.parent), True)
        bad.write_text("console: [\nsections: nope\n")
        with mock.patch.object(run_phase1, "CHECKLIST_PATH", bad):
            text = self._capture()
        self._assert_fallback(text, str(bad))

    def test_builtin_text_matches_the_shared_rendering(self):
        out = io.StringIO()
        with redirect_stdout(out):
            run_phase1._print_builtin_manual_instructions(MANUAL_PROJECT_DIR)
        self.assertEqual(out.getvalue(), PREVIOUS_MANUAL_TEXT)
        self.assertEqual(run_phase1._checklist_console(MANUAL_PROJECT_DIR), PREVIOUS_MANUAL_TEXT)


if __name__ == "__main__":
    unittest.main()
