"""Module-central artifact manifest for 3D_phase_1.

One append-only CSV at the module root records every artifact event:
what was made or moved, where it lives, and when. Read by the module
dashboard; written best-effort so processing never fails on logging.
No imports from config (keeps this unit-testable and side-effect free).
"""
import csv
import fcntl
import logging
import os
from datetime import datetime

MODULE_VERSION = "1.1.0"
HEADERS = ["timestamp", "module_version", "project", "model_id",
           "artifact", "action", "path", "details"]


def default_manifest_path():
    github_repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(github_repo_dir), "manifest.csv")


def append_event(project, model_id, artifact, action, path, details="", manifest_path=None):
    """Append one artifact event. Never raises."""
    target = manifest_path or default_manifest_path()
    try:
        write_header = not os.path.exists(target)
        with open(target, "a", newline="") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(HEADERS)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                MODULE_VERSION, project, model_id, artifact, action, path, details,
            ])
    except Exception as exc:
        logging.warning(f"manifest append failed ({target}): {exc}")
