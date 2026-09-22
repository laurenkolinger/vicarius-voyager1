#!/usr/bin/env python3
"""
Module:  tests/test_registry_client_speaks.py
Purpose: The module must say WHY it cannot reach the TCRMP 3D registry.
Inputs:  src/registry_client.py, src/step0.py.
Outputs: pass/fail lines; exit 1 on the first failure.

2026-09-06. Lauren's first Voyager 1 run died at the reconstruction with the
single line "No videos to extract frames from." Every video had in fact been
copied and verified. The registry library would not import on the Python 3.9
environment phase 1 builds for Metashape, registry_client caught that
TypeError into `_import_error`, which nothing ever read or logged, `enabled()`
went quietly False, `rows_for()` returned None, step0 found no work items, and
the operator was told the videos were missing.

The import bug is fixed in the library. This file holds the second bug: a
failure that is caught must be said out loud. Finding the real cause took
twenty minutes of reading source because the program knew it and did not say.
"""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import registry_client  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    if ok:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name} {detail}")
        FAILED.append(name)


def _capture(fn):
    """Run fn with logging captured, returning (result, log_text)."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    level, root.level = root.level, logging.DEBUG
    root.addHandler(handler)
    try:
        result = fn()
    finally:
        root.removeHandler(handler)
        root.level = level
    return result, buf.getvalue()


# -- a broken library must be reported, not swallowed -----------------------
def _break_the_library():
    """Point the client at a library directory whose registry.py cannot import."""
    import tempfile
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "registry.py"), "w") as f:
        f.write("raise RuntimeError('deliberately broken for the test')\n")
    with open(os.path.join(d, "naming3d.py"), "w") as f:
        f.write("")
    return d


broken = _break_the_library()
original_library_dir = registry_client._library_dir
registry_client._library_dir = lambda: broken
try:
    _, log = _capture(lambda: registry_client.configure({"processing": {"tcrmp": True}}))
    check("a library that will not import is logged, not swallowed",
          "deliberately broken" in log or "registry" in log.lower(),
          f"log was {log.strip()!r}")
    check("the reason is available to callers",
          registry_client.unavailable_reason() is not None,
          "unavailable_reason() returned None")
    reason = registry_client.unavailable_reason() or ""
    check("the reason names the failure", "deliberately broken" in reason, f"reason was {reason!r}")
    check("enabled() is False while the library is broken", registry_client.enabled() is False)
    check("rows_for returns None, as before", registry_client.rows_for() is None)
finally:
    registry_client._library_dir = original_library_dir

# -- the healthy path still works and reports no reason ---------------------
registry_client.configure({"processing": {"tcrmp": True}})
check("the real library imports", registry_client.enabled() is True,
      f"reason: {registry_client.unavailable_reason()!r}")
check("a healthy client has no reason to report", registry_client.unavailable_reason() is None)

# -- tcrmp off is not a failure ---------------------------------------------
registry_client.configure({"processing": {"tcrmp": False}})
check("registry mode off is not an error", registry_client.unavailable_reason() is None)
check("enabled() is False with tcrmp off", registry_client.enabled() is False)

# -- step0 must name the reason in its own error ----------------------------
step0_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "src", "step0.py")).read()
check("step0 asks for the reason before blaming the videos",
      "unavailable_reason" in step0_src,
      "step0 still reports 'No videos' without checking whether the registry was reachable")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("the registry client speaks when it cannot reach the registry")
