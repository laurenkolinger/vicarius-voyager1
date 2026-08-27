"""Writes the identity-first row of a project's status.csv without importing
config.py at run_phase1.py's own module scope.

config.py reads the project directory from sys.argv[1] and, as an
import-time side effect, loads <project_dir>/analysis_params.yaml and
creates the project's subdirectories. run_phase1.py drives setup for many
projects (one per TCRMP timepoint, or several models in one non-TCRMP
project) within a single process, so config cannot simply be imported once
at the top of the file the way step0.py/step1.py import it. write_identity_row()
instead sets sys.argv[1] to the target project directory for the duration of
the import (or reload, if config was already imported for a different
project earlier in this run), then restores sys.argv.

Callers must ensure <project_dir>/analysis_params.yaml already exists
before calling this (config.py raises FileNotFoundError otherwise).
"""
import importlib
import sys


def write_identity_row(project_dir, original_videos, readable_id):
    """Ensure status.csv in project_dir has a row for readable_id with
    original_videos, readable_id, and Model ID (= readable_id) populated.

    Idempotent: safe to call more than once for the same readable_id (and,
    across a run that touches multiple project dirs, safe to call once per
    project dir in turn). Returns the status.csv path.
    """
    saved_argv = sys.argv[:]
    sys.argv = [saved_argv[0] if saved_argv else "status_rows", str(project_dir)]
    try:
        if "config" in sys.modules:
            config = importlib.reload(sys.modules["config"])
        else:
            import config
    finally:
        sys.argv = saved_argv

    config.initialize_tracking(readable_id)
    config.update_tracking(readable_id, {
        "original_videos": original_videos,
        "readable_id": readable_id,
    })
    return config.TRACKING_FILE
