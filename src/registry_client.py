"""Thin wrapper over the shared vicarius/_METADATA/3d registry.

No-op when the module is not running in TCRMP mode: step scripts call
`configure(config.PARAMS)` once after config.py loads, and every other
function returns None until that has happened with
`PARAMS["processing"]["tcrmp"]` true. config.py has import-time side effects
(reads sys.argv, creates directories), so this module never imports it --
callers pass the already-loaded PARAMS dict in.

The shared library (registry.py, naming3d.py) lives at
`$VICARIUS_ROOT/_METADATA/3d` (VICARIUS_ROOT defaults to
/mnt/rip/vicarius_drive/vicarius) and is reached by adding that directory to
sys.path. registry.py itself reads VICARIUS_3D_REGISTRY_ROOT to decide where
to read/write the CSVs; tests override that to a temp directory.
"""
import importlib
import logging
import os
import sys

ACTOR = "3D_phase_1"

# The row_facts.csv section this module writes (registry.FACT_SECTIONS names
# the writers; this one is the Voyager 1 reconstruction module).
VOYAGER1_SECTION = "voyager1"

_params = None
_registry = None
_naming3d = None
_import_error = None


def _library_dir():
    root = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius")
    return os.path.join(root, "_METADATA", "3d")


def _import_library():
    """Import (or re-import) registry/naming3d from the shared library dir.

    Reloads registry.py every call so a VICARIUS_3D_REGISTRY_ROOT set just
    before configure() takes effect, mirroring the shared library's own test
    convention.
    """
    global _registry, _naming3d, _import_error
    lib_dir = _library_dir()
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        import registry as _r
        import naming3d as _n
        _r = importlib.reload(_r)
        _registry, _naming3d = _r, _n
        _import_error = None
    except Exception as exc:
        # Say it. This used to be caught into _import_error and never read: on
        # 2026-09-06 the library would not import on the Python 3.9 environment
        # phase 1 builds for Metashape, and the only thing the operator saw was
        # "No videos to extract frames from" while every video sat on the disk.
        # A caught failure that nothing reports is how twenty minutes go.
        _registry, _naming3d = None, None
        _import_error = exc
        logging.error("The TCRMP 3D registry library at %s could not be imported, so this run "
                      "cannot see any registry rows: %s: %s",
                      lib_dir, type(exc).__name__, exc)


def _tcrmp_flag():
    if not _params:
        return False
    return bool(_params.get("processing", {}).get("tcrmp", False))


def configure(params):
    """Step scripts call this once with config.PARAMS after config loads."""
    global _params, _registry, _naming3d, _import_error
    _params = params or {}
    _registry, _naming3d, _import_error = None, None, None
    if _tcrmp_flag():
        _import_library()


def enabled():
    return _tcrmp_flag() and _registry is not None


def unavailable_reason():
    """Why the registry cannot be read, in one sentence, or None when it can.

    Returns None when registry mode is off (which is a choice, not a failure)
    and when the library imported cleanly. Otherwise a sentence naming the
    library directory and the underlying error, for a caller to put in front
    of an operator instead of guessing at a cause.

    Example:
        >>> unavailable_reason()
        "The TCRMP 3D registry library at /x/3d could not be imported: TypeError: ..."
    """
    if not _tcrmp_flag():
        return None
    if _registry is not None:
        return None
    if _import_error is None:
        return f"The TCRMP 3D registry library at {_library_dir()} was never loaded."
    return (f"The TCRMP 3D registry library at {_library_dir()} could not be imported: "
            f"{type(_import_error).__name__}: {_import_error}")


def stage(readable_id, step, stage):
    if not enabled():
        return None
    return _registry.set_stage(readable_id, step, stage, actor=ACTOR)


def update(readable_id, protect_operator=False, **fields):
    """Write fields into the registry row through the shared upsert.

    protect_operator=True keeps any value the operator (a person or the
    sync driver) already put in a cell.
    """
    if not enabled():
        return None
    return _registry.upsert(readable_id, fields, actor=ACTOR,
                            protect_operator=protect_operator)


def snapshot(readable_id, folder, extra, report_pdf=None, params_yaml=None):
    if not enabled():
        return None
    return _registry.capture_snapshot(readable_id, folder, extra,
                                       report_pdf=report_pdf, params_yaml=params_yaml)


def facts(readable_id, section, facts, links=None, units=None):
    """Record facts for one row and section in the registry's row_facts.csv
    sidecar through the shared set_facts, signed with this module's actor.

    Parameters:
        readable_id: an existing registry row.
        section: one of registry.FACT_SECTIONS (VOYAGER1_SECTION here).
        facts: {key: value}; the keys given replace their old lines, other
            keys of the section stay.
        links: {key: path or URL} the atlas renders as a link, or None.
        units: {key: "" | "s" | "GB" | "count"}, or None.

    Returns:
        None outside TCRMP mode (nothing is written); otherwise True when
        the sidecar changed, False when every key already matched.

    Raises:
        TypeError, ValueError, KeyError: from the shared validation (an
        unknown section, a bad key or value, a link or unit for a key not
        given, a row that does not exist); nothing is written then.
    """
    if not enabled():
        return None
    return _registry.set_facts(readable_id, section, facts, actor=ACTOR,
                               links=links, units=units)


def row(readable_id):
    if not enabled():
        return None
    return _registry.get(readable_id)


def rows_for(site=None, transect=None, ids=None):
    if not enabled():
        return None
    rows = [r for r in _registry.load() if r.get("process") == "true"]
    if site is not None:
        rows = [r for r in rows if r.get("site") == site]
    if transect is not None:
        rows = [r for r in rows if r.get("transect") == transect]
    rows.sort(key=lambda r: _naming3d.sort_key(r["readable_id"]))
    if ids is not None:
        by_id = {r["readable_id"]: r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KeyError(f"unknown readable_id(s): {', '.join(missing)}")
        return [by_id[i] for i in ids]
    return rows
