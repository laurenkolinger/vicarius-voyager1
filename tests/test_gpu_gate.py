"""Tests for run_phase1.py's GPU right-of-way gate (plan Task 8,
docs/superpowers/plans/2026-09-17-gpu-right-of-way.md; spec docs/superpowers/
specs/2026-09-17-gpu-right-of-way-design.md).

_check_gpu_right_of_way() is called once in main(), immediately after the
command line is parsed and validated and before Metashape is ever touched,
so a launch from a terminal that bypasses the desktop UI's runner still
refuses to collide with another model-tier run. It is advisory only: it
never claims (runner.py's start_job already made the binding claim, with
this process's own pid, before spawning it).

CRITICAL TEST SAFETY: a real VOYAGER reconstruction may be running on this
box right now (this module's own run_phase1.py, pid found by
pgrep -af "metashape|run_phase1"). subprocess.Popen and subprocess.run are
stubbed at module level, before run_phase1 is imported. This is safe to do
at module level in THIS house-test convention specifically because
tests/run_all_tests.sh runs every tests/test_*.py file as its own separate
python3 process (`for t in tests/test_*.py; do "$PY" "$t"; done`), so a
module-level stub here cannot leak into any other test file the way it
would under pytest's one-shared-process model (a mistake made and reverted
while building the desktop UI's half of this same task). Every test below
also mocks run_phase1.gpu_claim.check directly, so no test in this file ever
makes a real HTTP call to port 5090 or touches the real claims registry.
"""
import io
import os
import sys
import unittest
from unittest import mock

import subprocess

subprocess.Popen = mock.MagicMock(name="subprocess.Popen (stubbed for GPU gate tests)")
subprocess.run = mock.MagicMock(name="subprocess.run (stubbed for GPU gate tests)")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import run_phase1  # noqa: E402


class ReachedMetashape(Exception):
    """Sentinel raised by a stubbed detect_metashape() to prove main() got
    past the GPU gate without actually detecting or touching Metashape."""


class CheckGpuRightOfWayUnitTests(unittest.TestCase):
    """_check_gpu_right_of_way() in isolation: every decision the gate can
    return, and what this function does with each one."""

    def test_granted_returns_silently(self):
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "granted"}) as check:
            run_phase1._check_gpu_right_of_way()
        check.assert_called_once_with(module="3D_phase_1", by="LO")

    def test_unregistered_returns_silently(self):
        """Port 5090 unreachable is fail-open: the run proceeds."""
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "unregistered", "reason": "no route"}):
            run_phase1._check_gpu_right_of_way()  # must not raise

    def test_refused_exits_nonzero_with_the_message_on_stderr(self):
        message = "GPU right of way: VEGA holds the cards (r1, since 10:00 AST)."
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "refused", "message": message}):
            with self.assertRaises(SystemExit) as ctx:
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    run_phase1._check_gpu_right_of_way()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(message, err.getvalue())

    def test_needs_pin_also_exits_nonzero(self):
        """A human-tier module never launches through run_phase1.py, but the
        gate still has to fail closed on needs_pin rather than proceed:
        there is no PIN-bypass path at this layer."""
        message = "GPU right of way: VOYAGER is running (r1, since 10:00 AST)."
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "needs_pin", "message": message}):
            with self.assertRaises(SystemExit) as ctx:
                run_phase1._check_gpu_right_of_way()
        self.assertEqual(ctx.exception.code, 1)

    def test_refusal_with_no_message_still_exits_with_a_plain_one(self):
        """A hostile/malformed reply (refused, but no 'message' key) must
        still block the start rather than crash on a missing key."""
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "refused"}):
            with self.assertRaises(SystemExit) as ctx:
                with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                    run_phase1._check_gpu_right_of_way()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("VOYAGER", err.getvalue())

    def test_never_claims_itself(self):
        """runner.py's start_job already made the binding claim before
        spawning this process; a second claim here would be a second writer
        of the registry (design semantics item 6, "one writer")."""
        with mock.patch.object(run_phase1.gpu_claim, "check",
                                return_value={"decision": "granted"}):
            with mock.patch.object(run_phase1.gpu_claim, "claim") as claim:
                run_phase1._check_gpu_right_of_way()
        claim.assert_not_called()


class MainOrderingTests(unittest.TestCase):
    """main() calls the gate right after parsing/validating the command
    line and before Metashape is ever touched (detect_metashape())."""

    def _run_main_with(self, argv, check_result):
        """Run main() with sys.argv and gpu_claim.check() both controlled.

        Parameters:
            argv: the argv list main() should see (argv[0] is the program
                name; parse_args() reads argv[1:]).
            check_result: the dict run_phase1.gpu_claim.check() should
                return for this call.
        Returns:
            None normally; raises whatever main() raises (SystemExit on a
            refusal, ReachedMetashape once detect_metashape() is reached on
            a granted or unregistered decision).
        """
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(run_phase1.gpu_claim, "check", return_value=check_result), \
             mock.patch.object(run_phase1, "detect_metashape",
                                side_effect=ReachedMetashape("main() reached Metashape detection")):
            run_phase1.main()

    def test_refused_exits_before_detecting_metashape(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_main_with(
                ["run_phase1.py"],
                {"decision": "refused", "message": "GPU right of way: VEGA holds the cards"},
            )
        self.assertEqual(ctx.exception.code, 1)

    def test_granted_proceeds_to_detect_metashape(self):
        with self.assertRaises(ReachedMetashape):
            self._run_main_with(["run_phase1.py"], {"decision": "granted"})

    def test_unregistered_proceeds_to_detect_metashape(self):
        with self.assertRaises(ReachedMetashape):
            self._run_main_with(
                ["run_phase1.py"],
                {"decision": "unregistered", "reason": "connection refused"},
            )


class RaisingHelperTests(unittest.TestCase):
    """A safety system must never be the reason a multi-day run dies.

    Added 2026-09-17 after an independent re-review pointed out that
    3D_phase2's gate test covered this case and 3D_phase_1's did not, even
    though both scripts carry the identical guard. The guard was written
    because the bug was real in both: before it, a gpu_claim helper that
    raised would propagate straight out of main() and end the run.
    """

    def test_a_raising_helper_never_stops_the_science(self):
        with mock.patch.object(run_phase1.gpu_claim, "check",
                               side_effect=RuntimeError("helper blew up")):
            try:
                run_phase1._check_gpu_right_of_way()
            except SystemExit:
                self.fail("a raising helper must not stop the run")
            except RuntimeError:
                self.fail("a raising helper must be caught, not propagated")

    def test_the_gate_never_claims(self):
        """Advisory only: runner.start_job already made the binding claim."""
        with mock.patch.object(run_phase1.gpu_claim, "check",
                               return_value={"decision": "granted"}):
            with mock.patch.object(run_phase1.gpu_claim, "claim") as claimed:
                with mock.patch.object(run_phase1.gpu_claim, "release") as released:
                    run_phase1._check_gpu_right_of_way()
        claimed.assert_not_called()
        released.assert_not_called()


if __name__ == "__main__":
    unittest.main()


