"""
Step 1: reconstruction, one psx per site and transect

Each timepoint is one folder under frames/ (named by its readable id in
TCRMP mode, by the video's file stem otherwise) and becomes one chunk
labeled with that id.

TCRMP mode: the chunk is appended to the site and transect's current psx - a
range-named bundle {SITE}_{T#}_{first year}_{last year}.psx that is renamed
on disk as its range grows - until it holds processing.max_chunks_per_psx
chunks, after which the next timepoint starts a new psx. Timepoints are
processed chronologically so the range grows forward. Non-TCRMP mode keeps
the original batching and psx_{N}_{date}.psx naming.

One document is opened per psx per timepoint: open or create, append the
chunk, process, report, save, verify, rename to the range it now covers,
then write the run's numbers back to the registry (a no-op outside TCRMP
mode). Nothing flips a status row to complete until the save has been
reopened and verified on disk.

Scale never blocks the run: PASS when at least two bars agree under the
threshold, MANUAL_NEEDED otherwise, with the mean error recorded in both mm
and ppm and the timepoint listed at the manual gate. The DEM is built into
the psx and stays there - step 1 exports no raster.
"""

import os
import gc
import logging
import Metashape
import datetime
import math
import re
import sys
import time
import traceback
import fcntl
import shutil
import socket
from config import (
    DIRECTORIES,
    PROJECT_NAME,
    METASHAPE_DEFAULTS,
    FRAMES_PER_TRANSECT,
    USE_GPU,
    PARAMS,
    update_tracking,
    get_transect_status,
    step_log_path,
    TIMESTAMP
)
import registry_client
import scale_utils
import selection_utils
import manifest

# Minimum free space on the temp volume (TMPDIR or /tmp) before we start.
# Metashape spills depth_maps_pyramids here; running out mid-build is the
# original FLC T6 failure mode (see _DOCS/archive/incidents/INCIDENT_2026-05_parallel_step1_flc_t6.md).
MIN_TEMP_FREE_GB = int(os.environ.get("STEP1_MIN_TEMP_FREE_GB", "50"))

# The project directory is the processing folder (flat layout); config
# created these on import.
print(f"Step 1 for project {PROJECT_NAME}")
for _key, _path in DIRECTORIES.items():
    print(f"  {_key}: {_path}")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(step_log_path("step1")),
        logging.StreamHandler()
    ]
)

# Maximum number of chunks per PSX file
MAX_CHUNKS_PER_PSX = PARAMS['processing'].get('max_chunks_per_psx', 4)

# Longest note this module writes into the registry's operator-owned notes
# column (see registry_note).
NOTE_MAX_CHARS = 500

# Metashape's quality presets, keyed by the downscale factor the parameters
# carry, for the registry's params_summary line.
MATCH_QUALITY = {0: "Highest", 1: "High", 2: "Medium", 4: "Low", 8: "Lowest"}
DEPTH_QUALITY = {1: "UltraHigh", 2: "High", 4: "Medium", 8: "Low", 16: "Lowest"}

# Cooperative-pause contract (shared across step1/step2/step3 + run_phaseN.py +
# the VICARIUS runner). When the UI / queue requests a pause it drops a sentinel
# file in the project root; we check it only at safe model/batch boundaries
# (after the tracking CSV is updated and the PSX is saved), finish nothing
# mid-flight, and exit with PAUSE_EXIT_CODE so the orchestrator knows this was a
# clean pause (not a failure). Re-running the module resumes: completed models
# are skipped (idempotent). The exclusive project lock is released automatically
# when the process exits, so a paused run frees the GPU + the lock.
PAUSE_EXIT_CODE = 42
PAUSE_SENTINEL = ".pause_requested"


# --- shared naming/registry library -----------------------------------------
# naming3d and registry live at $VICARIUS_ROOT/_METADATA/3d and are reached by
# the same sys.path insert registry_client uses. Imported on first use so a
# non-TCRMP run never needs them.

_naming3d_module = None
_registry_module = None


def _library_dir():
    root = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius")
    return os.path.join(root, "_METADATA", "3d")


def _on_library_path():
    lib_dir = _library_dir()
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def naming3d():
    """The shared naming3d module (readable-id parsing and psx range names)."""
    global _naming3d_module
    if _naming3d_module is None:
        _on_library_path()
        import naming3d as _n
        _naming3d_module = _n
    return _naming3d_module


def registry():
    """The shared registry module. Only tally_size_gb is used here; every
    registry write goes through registry_client, which owns the module's
    configuration (call it only after registry_client.configure())."""
    global _registry_module
    if _registry_module is None:
        _on_library_path()
        import registry as _r
        _registry_module = _r
    return _registry_module


# --- psx naming (pure) ------------------------------------------------------

PSX_RANGE_PATTERN = re.compile(
    r"^(?P<site>[A-Za-z0-9]+)_(?P<transect>T\d+)_(?P<first>\d{4})_(?P<last>\d{4})\.psx$")


def psx_range_parts(psx_name):
    """(site, transect, first year, last year) for a range-named psx file
    name, or None when the name is not one: a non-TCRMP psx_1_20260826.psx, a
    quarantined .corrupt_ bundle, a numbered collision sibling, anything else.
    """
    match = PSX_RANGE_PATTERN.match(os.path.basename(psx_name))
    if not match:
        return None
    return (match.group("site").upper(), match.group("transect").upper(),
            int(match.group("first")), int(match.group("last")))


def range_psx_paths(project_dir, site, transect):
    """Every range-named psx for this site and transect in project_dir,
    oldest first by (first year, last year)."""
    try:
        names = os.listdir(project_dir)
    except OSError:
        return []
    found = []
    for name in names:
        parts = psx_range_parts(name)
        if parts and parts[0] == site.upper() and parts[1] == transect.upper():
            found.append((parts[2], parts[3], os.path.join(project_dir, name)))
    found.sort()
    return [path for _first, _last, path in found]


def current_psx(project_dir, site, transect, year, cap, count_chunks,
                preferred=None, label=None, has_label=None):
    """Resolve the psx a site and transect's next timepoint belongs in.

    Returns (path, is_new). `preferred` is the psx the registry already
    records for this site and transect: when it exists on disk and still
    holds fewer than `cap` chunks it wins outright, because the registry
    knows about bundles a name scan cannot reconstruct (a numbered collision
    sibling, a bundle whose range rename was refused). A preferred bundle at
    the cap still wins when it already holds a chunk labeled `label`: that is
    a forced rerun of a timepoint whose chunk is already in there, and the
    stale-chunk swap replaces it rather than adding one, so the count does
    not grow. `has_label(path, label) -> bool` is injected for the same
    reason `count_chunks` is, and defaults to reading the bundle. Otherwise
    the newest
    range-named psx for the site and transect is reused when it still holds
    fewer than `cap` chunks;
    count_chunks(path) -> int is injected so this stays testable without
    Metashape and so a bundle that cannot be opened (counted as 0) is reused
    and handled by process_model's quarantine branch. At the cap, or with no
    range-named psx in the folder, the path is a new single-year psx
    {SITE}_{T#}_{year}_{year}.psx. A name already taken by a different bundle
    (two timepoints of one year with a cap of 1) gets a numbered sibling
    rather than being opened and appended to.
    """
    if preferred and os.path.exists(preferred):
        if count_chunks(preferred) < cap:
            return preferred, False
        if label and (has_label or psx_has_chunk_label)(preferred, label):
            return preferred, False

    existing = range_psx_paths(project_dir, site, transect)
    if existing:
        newest = existing[-1]
        if count_chunks(newest) < cap:
            return newest, False

    new_name = naming3d().psx_range_name(site, transect, [year])
    path = os.path.join(project_dir, new_name)
    stem = new_name[:-len(".psx")]
    suffix = 1
    while os.path.exists(path):
        suffix += 1
        path = os.path.join(project_dir, f"{stem}_{suffix}.psx")
    return path, True


def psx_range_target(path, years):
    """Where a bundle should live for `years`, or None when no move is due:
    the name is not a range name (non-TCRMP psx files are left alone), no
    years were given, or the name is already correct.

    The range only ever widens. The name's own first and last year join the
    computed years, so a document read that comes back short - a chunk whose
    model failed and no longer counts, a hand-deleted chunk - can never move
    the first year later or the last year earlier and quietly rename a
    bundle out from under the timepoints it still holds.
    """
    parts = psx_range_parts(path)
    if parts is None or not years:
        return None
    span = set(int(y) for y in years) | {parts[2], parts[3]}
    target = os.path.join(os.path.dirname(path),
                          naming3d().psx_range_name(parts[0], parts[1], span))
    if os.path.abspath(target) == os.path.abspath(path):
        return None
    return target


def files_dir_for(psx_path):
    """The sibling .files directory Metashape addresses by the psx stem."""
    return psx_path[:-len(".psx")] + ".files"


def rename_psx_range(path, years):
    """Move a range-named psx bundle to the year range its chunks now cover.

    Both halves move together: the .psx file and its sibling .files
    directory, which Metashape addresses by the project's own stem, so a
    bundle split across two names is unopenable. The move therefore refuses
    outright when EITHER target half already exists (nothing is clobbered,
    the caller records the conflict), and moves the .files directory first so
    a failure on the smaller second move can be rolled back.

    Returns the path the bundle lives at afterwards: the target on success,
    the original path when no move was due or the move was refused. Raises
    RuntimeError when the .psx move fails after the .files move landed - the
    .files move is rolled back first, so the bundle is left intact.
    """
    target = psx_range_target(path, years)
    if target is None:
        return path

    files_dir = files_dir_for(path)
    target_files = files_dir_for(target)
    taken = [p for p in (target, target_files) if os.path.exists(p)]
    if taken:
        logging.error(
            f"Cannot rename {os.path.basename(path)} to {os.path.basename(target)}: "
            f"{', '.join(os.path.basename(t) for t in taken)} already exists; "
            f"leaving the bundle where it is"
        )
        return path

    moved_files_dir = False
    if os.path.isdir(files_dir):
        os.rename(files_dir, target_files)
        moved_files_dir = True
    try:
        os.rename(path, target)
    except OSError as exc:
        if moved_files_dir:
            try:
                os.rename(target_files, files_dir)
            except OSError as rollback_exc:
                raise RuntimeError(
                    f"Could not rename {path} to {target} ({exc}), and rolling the "
                    f"project data back from {target_files} to {files_dir} also failed "
                    f"({rollback_exc}). The bundle is split across two names and must "
                    f"be repaired by hand before this timepoint is rerun."
                ) from exc
        raise RuntimeError(
            f"Could not rename {path} to {target} ({exc}); the project data was moved "
            f"back to {files_dir} and the bundle is unchanged."
        ) from exc
    logging.info(f"Renamed psx {os.path.basename(path)} to {os.path.basename(target)}")
    return target


def years_in_psx(doc):
    """Sorted unique years of the timepoints a document holds, read from its
    chunk labels (readable ids). Only chunks that actually hold a model
    count: a chunk whose reconstruction failed is still in the document, and
    letting it widen the range would name the bundle after a year it does
    not contain. Labels that are not readable ids - a hand-added "Chunk 1",
    a non-TCRMP file stem - are ignored, and so is a whole document when the
    shared naming library is not reachable: a non-TCRMP run never needs it
    and must not fail on it."""
    try:
        naming = naming3d()
    except ImportError as exc:
        logging.warning(f"Shared naming library unavailable, skipping psx range naming: {exc}")
        return []
    years = set()
    for chunk in getattr(doc, "chunks", []):
        if getattr(chunk, "model", None) is None:
            continue
        try:
            years.add(naming.id_parts(chunk.label)["year"])
        except (ValueError, AttributeError, TypeError):
            continue
    return sorted(years)


def order_models(labels, tcrmp):
    """Processing order for the pending timepoints: chronological by readable
    id in TCRMP mode (so a psx range only ever grows forward), folder order
    otherwise. A name that is not a readable id sorts last, by name."""
    if not tcrmp:
        return list(labels)
    naming = naming3d()

    def key(label):
        try:
            return (0, naming.sort_key(label), "")
        except ValueError:
            return (1, ("", 0, 0, 0), label)

    return sorted(labels, key=key)


def params_summary(texture_pages):
    """One line recording the settings this run used, for the registry."""
    products_cfg = PARAMS.get("processing", {}).get("step1_products", {}) or {}
    downscale = METASHAPE_DEFAULTS.get("downscale", 1)
    depth_downscale = METASHAPE_DEFAULTS.get("depth_downscale", 1)
    match = MATCH_QUALITY.get(downscale, f"downscale{downscale}")
    depth = DEPTH_QUALITY.get(depth_downscale, f"downscale{depth_downscale}")
    return (
        f"frames={FRAMES_PER_TRANSECT} match={match} "
        f"kp={METASHAPE_DEFAULTS.get('keypoint_limit')} "
        f"tp={METASHAPE_DEFAULTS.get('tiepoint_limit')} "
        f"sel=RU{METASHAPE_DEFAULTS.get('reconstruction_uncertainty')}"
        f"/PA{METASHAPE_DEFAULTS.get('projection_accuracy')}"
        f"/RE{METASHAPE_DEFAULTS.get('reprojection_error')} "
        f"depth={depth} decim=/{products_cfg.get('decimation_factor', 10)} "
        f"smooth={products_cfg.get('smooth_strength', 4)} tex=8192x{texture_pages}"
    )


def mean_bar_length(model_cfg):
    """Mean declared scale-bar length in metres: the ppm denominator."""
    lengths = []
    for bar in model_cfg.get("scale_bars", []) or []:
        try:
            length = float(bar.get("distance", 0) or 0)
        except (TypeError, ValueError):
            continue
        if length > 0:
            lengths.append(length)
    return sum(lengths) / len(lengths) if lengths else 0.0


def registry_scale_fields(status, error_m, bars, bar_length_m=None):
    """The four registry cells a scaling attempt produces.

    SENTINEL_ERROR is a "nothing was measured" marker, not a 999 metre bar,
    so it reaches the registry as blank error cells rather than 999000 mm and
    a nine-digit ppm that would sort and chart as a real reading. The status
    and the bar count are always written: MANUAL_NEEDED with no bars is the
    fact the atlas needs. status.csv keeps the sentinel itself, so the
    operator still sees what the scaling pass returned.

    bar_length_m defaults to the mean declared bar length in the loaded
    parameters (the ppm denominator).
    """
    if bar_length_m is None:
        bar_length_m = mean_bar_length(
            PARAMS.get("processing", {}).get("model_processing", {}) or {})
    measured = error_m is not None and float(error_m) < scale_utils.SENTINEL_ERROR
    return {
        "scale_status": status,
        "scale_error_mm": round(float(error_m) * 1000, 2) if measured else "",
        "scale_error_ppm": scale_utils.ppm(error_m, bar_length_m) if measured else "",
        "scale_bars": bars,
    }


def print_boxed(message):
    """Print one message inside a box so it survives a long console scroll."""
    rule = "+" + "-" * (len(message) + 2) + "+"
    print(rule)
    print(f"| {message} |")
    print(rule)


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _progress_logger(label, min_interval_s=30):
    """A throttled progress callback for Metashape's long build calls.

    Returns callback(pct) that logs "<label>: <pct rounded>%" at INFO level
    at most once per min_interval_s (measured on time.monotonic, so a system
    clock change cannot mute or flood it), plus exactly once more when pct
    reaches 100 so every operation's log visibly ends. The first call always
    logs, so the tailed log shows the operation the moment it starts moving.

    The callback runs inside Metashape's C++ task loop, where a raised
    exception can abort the whole build, so it swallows every exception:
    worst case a progress line is lost, never the reconstruction.
    """
    state = {"last": None, "logged_done": False}

    def callback(pct):
        try:
            if pct >= 100:
                if state["logged_done"]:
                    return
                state["logged_done"] = True
                state["last"] = time.monotonic()
                logging.info(f"{label}: 100%")
                return
            now = time.monotonic()
            if state["last"] is not None and (now - state["last"]) < min_interval_s:
                return
            state["last"] = now
            # Clamp to 99 so "100%" only ever comes from the terminal branch
            # above (a pct like 99.6 would otherwise round up and print 100%
            # twice).
            logging.info(f"{label}: {min(round(pct), 99)}%")
        except Exception:
            pass

    return callback


# --- registry write-back ----------------------------------------------------


def status_note(transect_id, text):
    """The status.csv Notes cell for a row with `text` appended rather than
    substituted, so recording a failure does not erase what an earlier step
    wrote there. Idempotent (a note already present is not repeated) and
    capped, so repeated reruns cannot grow the cell without bound."""
    existing = (get_transect_status(transect_id).get("Notes") or "").strip()
    if not existing:
        return text
    if text in existing:
        return existing
    combined = f"{existing}; {text}"
    return combined[-NOTE_MAX_CHARS:] if len(combined) > NOTE_MAX_CHARS else combined


def registry_note(transect_id, text):
    """Append a note to the registry row without discarding what the operator
    wrote there (notes is an operator-owned column). Idempotent: a note
    already present is not repeated, and the column is capped so repeated
    reruns cannot grow it without bound. No-op outside TCRMP mode.
    """
    if not registry_client.enabled():
        return
    existing = (registry_client.row(transect_id) or {}).get("notes", "") or ""
    if text in existing:
        return
    combined = f"{existing}; {text}" if existing else text
    if len(combined) > NOTE_MAX_CHARS:
        combined = combined[-NOTE_MAX_CHARS:]
    registry_client.update(transect_id, notes=combined)


def force_rerun_requested():
    """True when run_phase1 launched this step under --force.

    run_phase1 writes processing.force_rerun into the processing folder's
    analysis_params.yaml right before step 1 starts and clears it right
    after, so the step can tell a forced rebuild from an ordinary rerun
    without a second channel.
    """
    return bool(PARAMS.get("processing", {}).get("force_rerun", False))


MANUAL_EDIT_SKIP_NOTE = "skipped: manual edits present; use --force to rebuild"


def stale_chunk_decision(registry_enabled, manual_edit_status, force):
    """"replace" or "skip" for a chunk already labelled with this timepoint.

    Removing a stale chunk destroys whatever straightening, cropping or hand
    scaling the operator did at the manual gate, and nothing else on the
    platform holds a copy of that work yet. So a timepoint the registry
    records as manually edited is skipped rather than rebuilt, unless the
    operator asked for the rebuild with --force. Outside TCRMP mode there is
    no registry to consult and the original swap stands.
    """
    if not registry_enabled or force:
        return "replace"
    if (manual_edit_status or "").strip().lower() == "done":
        return "skip"
    return "replace"


def registry_failure(transect_id, message):
    """Mark the timepoint failed in the registry and record why."""
    registry_client.update(transect_id, step1_status="failed", stage="failed")
    registry_note(transect_id, message)


def registry_success(transect_id, facts, psx_path):
    """Write every number this run produced back to the registry, then
    capture the snapshot the atlas reads when the folder is gone."""
    if not registry_client.enabled():
        return
    project_dir = DIRECTORIES["base"]
    numbers = {
        "step1_seconds": facts["seconds"],
        "tie_points": facts["tie_points"],
        "faces_full": facts["faces_full"],
        "faces_delivery": facts["faces_delivery"],
        "texture_pages": facts["texture_pages"],
        "dem_mm_per_pix": facts["dem_mm_per_pix"],
        "psx_file": psx_path,
        "params_summary": facts["params_summary"],
    }
    numbers.update(registry_scale_fields(
        facts["scale_status"], facts["scale_error_m"], facts["scale_bars"]))

    # Walking the folder is the one part of this that touches the whole tree,
    # so a failure there records the size as unknown rather than costing the
    # timepoint every other number it earned.
    try:
        numbers["processing_size_gb"] = registry().tally_size_gb(project_dir)
        numbers["sizes_verified"] = now_stamp()
    except Exception as exc:
        logging.warning(f"Could not tally the processing folder for {transect_id}: {exc}")

    registry_client.update(
        transect_id,
        step1_status="complete",
        step1_finished=facts["end_time"],
        manual_edit_status="awaiting",
        stage="done",
        **numbers,
    )

    # The snapshot is a copy of facts already written above, so a failure
    # here is logged and the row still stands.
    try:
        registry_client.snapshot(
            transect_id,
            project_dir,
            dict(numbers),
            report_pdf=facts.get("report_file") or None,
            params_yaml=os.path.join(project_dir, "analysis_params.yaml"),
        )
    except Exception as exc:
        logging.warning(f"Could not capture the registry snapshot for {transect_id}: {exc}")


# --- run guards -------------------------------------------------------------


def pause_requested():
    """True when a pause sentinel file exists in the project root."""
    return os.path.exists(os.path.join(DIRECTORIES["base"], PAUSE_SENTINEL))


def checkpoint_pause(where, save_fn=None):
    """At a safe boundary: if a pause was requested, persist + exit cleanly.

    save_fn (optional) is called to flush in-memory document state to disk
    BEFORE exiting, so the work matching the tracking CSV is durable. The
    project lock (if held) is released by process exit.
    """
    if not pause_requested():
        return
    logging.info(f"PAUSE requested - stopping cleanly at boundary: {where}")
    if save_fn is not None:
        try:
            save_fn()
        except Exception as exc:  # pragma: no cover - best-effort flush
            logging.warning(f"pause: save before exit failed: {exc}")
    logging.info(
        "Paused. Re-run this module on the same project to resume "
        "(completed models are skipped)."
    )
    sys.exit(PAUSE_EXIT_CODE)


def acquire_project_lock(step_name):
    """Take an exclusive flock on <project>/.processing.lock.

    Refuses to start if another step is already running for this project.
    Caller must keep the returned file object alive: closing it releases
    the lock. Stamps the file with PID, step name, hostname, and start time
    so the holder is visible if a future run is blocked.
    """
    lock_path = os.path.join(DIRECTORIES["base"], ".processing.lock")
    lock_fp = open(lock_path, "w")
    # A registry/atlas liveness probe briefly touches this lock with a shared
    # flock; a couple of quick retries make sure a probe in flight can never
    # abort a legitimate launch. A real holder keeps its exclusive lock for
    # the whole run, so retries never get past one.
    acquired = False
    for attempt in range(3):
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except BlockingIOError:
            if attempt < 2:
                time.sleep(0.2)
    if not acquired:
        lock_fp.close()
        try:
            with open(lock_path, "r") as f:
                holder = f.read().strip() or "(unknown holder)"
        except Exception:
            holder = "(unknown holder)"
        raise RuntimeError(
            f"Another VICARIUS 3D step is already running for this project.\n"
            f"  Lock file: {lock_path}\n"
            f"  Held by:   {holder}\n"
            f"  Refusing to launch {step_name}. If you are certain no other run is "
            f"active, delete the lock file and retry."
        )
    stamp = (
        f"pid={os.getpid()} step={step_name} "
        f"host={socket.gethostname()} "
        f"started={now_stamp()}\n"
    )
    lock_fp.write(stamp)
    lock_fp.flush()
    logging.info(f"Acquired project lock at {lock_path} ({stamp.strip()})")
    return lock_fp


def check_temp_free_space(min_gb=None):
    """Refuse to start if TMPDIR (or /tmp) has less than min_gb free.

    Metashape's depth_maps_pyramids intermediates land in TMPDIR. Running
    out of space mid-build leaves a half-saved PSX (the original 2026-05
    incident). This is a coarse preflight only, and does not guarantee
    enough space for the full run, just that we are not starting empty.
    """
    if min_gb is None:
        min_gb = MIN_TEMP_FREE_GB
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    try:
        free_bytes = shutil.disk_usage(tmpdir).free
    except OSError as e:
        raise RuntimeError(f"Cannot stat TMPDIR={tmpdir}: {e}")
    free_gb = free_bytes / (1024 ** 3)
    logging.info(f"Temp volume free space: {free_gb:.1f} GB at {tmpdir}")
    if free_gb < min_gb:
        raise RuntimeError(
            f"Refusing to start: only {free_gb:.1f} GB free at {tmpdir}, "
            f"need at least {min_gb} GB for Metashape intermediates. "
            f"Free space or point TMPDIR at a larger volume "
            f"(export TMPDIR=/path/with/space before launching)."
        )


# --- document inspection ----------------------------------------------------


def count_chunks_in_psx(psx_path):
    """How many chunks an existing psx holds, read in a throwaway read-only
    Document. A bundle that is missing or unopenable counts as 0: the run
    then reuses that path and process_model's quarantine branch deals with
    the unusable bytes."""
    if not os.path.exists(psx_path):
        return 0
    try:
        probe_doc = Metashape.Document()
        probe_doc.open(psx_path, read_only=True, ignore_lock=True)
        count = len(probe_doc.chunks)
        probe_doc = None
        return count
    except Exception as e:
        logging.warning(f"Could not count chunks in {psx_path}: {e}")
        return 0


def psx_has_chunk_label(psx_path, label):
    """True when an existing psx already holds a chunk with this label, read
    in a throwaway read-only Document. A bundle that is missing or unopenable
    answers False, which sends the caller down the same path as an empty
    one."""
    if not os.path.exists(psx_path):
        return False
    try:
        probe_doc = Metashape.Document()
        probe_doc.open(psx_path, read_only=True, ignore_lock=True)
        found = any(c.label == label for c in probe_doc.chunks)
        probe_doc = None
        return found
    except Exception as e:
        logging.warning(f"Could not read chunk labels from {psx_path}: {e}")
        return False


def verify_psx_chunk(psx_path, transect_id):
    """Open psx_path in a fresh Document and confirm the chunk is real.

    Returns True only when a chunk with label==transect_id exists with a
    built model and at least one texture. Anything less means the save did
    not actually land. We do this in a throwaway Document so the active one
    is untouched. The DEM lives inside the psx (step 1 exports no raster),
    so it is not checked as a file here.
    """
    if not os.path.exists(psx_path):
        logging.error(f"Verify: PSX file does not exist: {psx_path}")
        return False
    try:
        verify_doc = Metashape.Document()
        try:
            verify_doc.open(psx_path, read_only=True, ignore_lock=True)
        except Exception as e:
            logging.error(f"Verify: could not reopen {psx_path}: {e}")
            return False
        try:
            chunks_by_label = {c.label: c for c in verify_doc.chunks}
            chunk = chunks_by_label.get(transect_id)
            if chunk is None:
                logging.error(
                    f"Verify: no chunk labeled {transect_id} in {psx_path}. "
                    f"Found: {sorted(chunks_by_label.keys())}"
                )
                return False
            if not chunk.model:
                logging.error(f"Verify: chunk {transect_id} has no model in {psx_path}")
                return False
            if not chunk.model.textures:
                logging.error(f"Verify: chunk {transect_id} has model but no textures in {psx_path}")
                return False
            logging.info(
                f"Verify: {transect_id} confirmed in {psx_path} "
                f"({len(chunk.model.faces)} faces, {len(chunk.model.textures)} texture(s))"
            )
            return True
        finally:
            verify_doc = None
    except Exception as e:
        logging.error(f"Verify: unexpected error checking {psx_path} for {transect_id}: {e}")
        return False


def enumerate_gpus():
    """
    Enumerate available GPUs and log their details.

    Returns:
        list: List of available GPU devices
    """
    logging.info("Enumerating available GPU devices...")
    gpu_devices = Metashape.app.enumGPUDevices()

    if not gpu_devices:
        logging.warning("No GPU devices detected by Metashape")
        return []

    for i, device in enumerate(gpu_devices):
        if isinstance(device, dict):
            device_info = []
            for key, value in device.items():
                device_info.append(f"{key}: {value}")
            logging.info(f"GPU {i}: {', '.join(device_info)}")
        else:
            logging.info(f"GPU {i}: {device}")

    return gpu_devices


def setup_gpu(gpu_devices=None):
    """
    Configure GPU processing based on available devices.

    Args:
        gpu_devices (list, optional): List of available GPU devices

    Returns:
        bool: Whether GPU processing was successfully enabled
    """
    if not USE_GPU:
        logging.info("GPU processing disabled in config")
        return False

    # Enumerate GPUs if not provided
    if gpu_devices is None:
        gpu_devices = enumerate_gpus()

    if not gpu_devices:
        logging.warning("GPU processing requested but no devices available")
        return False

    # Set GPU mask to enable all available GPUs
    # Each bit in the mask corresponds to a GPU
    gpu_mask = 0
    for i in range(len(gpu_devices)):
        gpu_mask |= (1 << i)  # Set the corresponding bit

    Metashape.app.gpu_mask = gpu_mask

    # Enable GPU for depth maps and mesh generation
    Metashape.app.cpu_enable = False

    logging.info(f"GPU acceleration enabled with mask: {gpu_mask} (binary: {bin(gpu_mask)})")
    logging.info(f"Using {len(gpu_devices)} GPU device(s)")

    return True


def process_transect(transect_id, chunk, doc, psx_path):
    """
    Process a single timepoint through initial 3D reconstruction.

    Args:
        transect_id (str): The timepoint identifier (chunk label)
        chunk (Metashape.Chunk): The chunk to process
        doc (Metashape.Document): The document containing the chunk
        psx_path (str): The path the document is saved to

    Returns:
        dict: the run's facts (timings, scale, counts) on success, or None on
        failure. The tracking row is written either way; "Step 1 complete"
        stays False until the caller has verified the save on disk.
    """
    try:
        start_time = datetime.datetime.now()

        registry_client.update(
            transect_id,
            step1_status="running",
            step1_started=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            console_log=step_log_path("step1"),
        )

        # Set up GPU processing
        gpu_devices = enumerate_gpus()
        setup_gpu(gpu_devices)

        # Set chunk label
        chunk.label = transect_id

        products_cfg = PARAMS.get("processing", {}).get("step1_products", {}) or {}
        model_cfg = PARAMS.get("processing", {}).get("model_processing", {}) or {}

        # Add photos from frames directory
        frames_dir = os.path.join(DIRECTORIES["frames"], transect_id)
        if not os.path.exists(frames_dir):
            raise ValueError(f"Frames directory not found: {frames_dir}")

        # Get list of frame files
        frame_files = [f for f in os.listdir(frames_dir) if f.lower().endswith(('.jpg', '.jpeg', '.tif', '.tiff'))]
        if not frame_files:
            raise ValueError(f"No image files found in {frames_dir}")

        # Add photos to chunk
        logging.info(f"Adding {len(frame_files)} photos for model {transect_id}")
        chunk.addPhotos([os.path.join(frames_dir, f) for f in frame_files])

        # Match photos and align cameras
        logging.info(f"Matching photos for model {transect_id}")
        registry_client.stage(transect_id, 1, "matching")
        chunk.matchPhotos(
            downscale=METASHAPE_DEFAULTS["downscale"],
            keypoint_limit=METASHAPE_DEFAULTS["keypoint_limit"],
            tiepoint_limit=METASHAPE_DEFAULTS["tiepoint_limit"],
            generic_preselection=METASHAPE_DEFAULTS["generic_preselection"],
            reference_preselection=METASHAPE_DEFAULTS["reference_preselection"],
            filter_stationary_points=METASHAPE_DEFAULTS["filter_stationary_points"],
            progress=_progress_logger(f"Matching photos for {transect_id}")
        )
        logging.info(f"Aligning cameras for model {transect_id}")
        registry_client.stage(transect_id, 1, "aligning")
        chunk.alignCameras(
            adaptive_fitting=METASHAPE_DEFAULTS["adaptive_fitting"],
            progress=_progress_logger(f"Aligning cameras for {transect_id}")
        )

        # Attempt to align any unaligned cameras
        unaligned_cameras = [camera for camera in chunk.cameras if not camera.transform]
        for camera in unaligned_cameras:
            camera.transform = None
        chunk.alignCameras(
            cameras=unaligned_cameras, reset_alignment=False,
            progress=_progress_logger(f"Aligning remaining cameras for {transect_id}")
        )

        # Fail clearly, right here, when alignment produced nothing to
        # filter (e.g. a degenerate video whose extracted frames are mostly
        # duplicates, see step0's frames_per_transect clamp). Left
        # unguarded, the legacy path's Filter.removePoints and the capped
        # path's selection_utils.capped_gradual_selection both eventually
        # hit an uninformative TypeError deep inside tie-point filtering
        # instead of naming the actual cause.
        if chunk.tie_points is None or not chunk.tie_points.points:
            raise RuntimeError(
                f"no tie points after alignment for {chunk.label}: alignment "
                "produced nothing to filter; check that the frames are distinct "
                "and the video is longer than frames_per_transect / extraction rate"
            )

        # Reset the region
        chunk.resetRegion()

        # Filter points and optimize cameras. "legacy" is the original
        # RU -> optimize -> RE -> PA sequence, byte-identical to before.
        # "capped" runs the capped iterative gradual-selection scheme
        # (RU -> PA -> RE, each capped and re-optimized) from
        # selection_utils; it is activated via
        # products_cfg.gradual_selection_mode. The pre-rename config token
        # is also accepted here (same branch), for back-compat with
        # existing analysis_params.yaml files.
        registry_client.stage(transect_id, 1, "filtering")
        gradual_selection_mode = products_cfg.get("gradual_selection_mode", "legacy")
        if gradual_selection_mode in ("capped", "noaa"):
            logging.info("Filtering points and optimizing cameras (capped iterative selection)")

            def capped_optimize():
                chunk.optimizeCameras(
                    fit_f=True, fit_cx=True, fit_cy=True,
                    fit_b1=False, fit_b2=False,
                    fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
                    fit_p1=True, fit_p2=True,
                    adaptive_fitting=False,
                )

            selection_utils.capped_gradual_selection(
                Metashape, chunk, METASHAPE_DEFAULTS, logging.info, capped_optimize
            )
        else:
            logging.info("Filtering points and optimizing cameras")
            f1 = Metashape.TiePoints.Filter()
            f1.init(chunk, Metashape.TiePoints.Filter.ReconstructionUncertainty)
            f1.removePoints(METASHAPE_DEFAULTS["reconstruction_uncertainty"])

            chunk.optimizeCameras(
                fit_k4=METASHAPE_DEFAULTS["fit_k4"],
                adaptive_fitting=METASHAPE_DEFAULTS["adaptive_fitting"]
            )

            f2 = Metashape.TiePoints.Filter()
            f2.init(chunk, Metashape.TiePoints.Filter.ReprojectionError)
            f2.removePoints(METASHAPE_DEFAULTS["reprojection_error"])

            f3 = Metashape.TiePoints.Filter()
            f3.init(chunk, Metashape.TiePoints.Filter.ProjectionAccuracy)
            f3.removePoints(METASHAPE_DEFAULTS["projection_accuracy"])

        try:
            tie_points = len(chunk.tie_points.points)
        except (AttributeError, TypeError) as exc:
            logging.warning(f"Could not count tie points for {transect_id}: {exc}")
            tie_points = 0

        # Rotate coordinate system to bounding box
        logging.info("Rotating coordinate system to bounding box")
        R = chunk.region.rot     # Bounding box rotation matrix
        C = chunk.region.center  # Bounding box center vector

        if chunk.transform.matrix:
            T = chunk.transform.matrix
            s = math.sqrt(T[0, 0] ** 2 + T[0, 1] ** 2 + T[0, 2] ** 2)  # scaling
            S = Metashape.Matrix().Diag([s, s, s, 1])                  # scale matrix
        else:
            S = Metashape.Matrix().Diag([1, 1, 1, 1])

        T = Metashape.Matrix([[R[0, 0], R[0, 1], R[0, 2], C[0]],
                             [R[1, 0], R[1, 1], R[1, 2], C[1]],
                             [R[2, 0], R[2, 1], R[2, 2], C[2]],
                             [     0,      0,      0,    1]])

        chunk.transform.matrix = S * T.inv()  # resulting chunk transformation matrix

        # Build depth maps
        logging.info(f"Building depth maps for model {transect_id}")
        registry_client.stage(transect_id, 1, "depth_maps")
        chunk.buildDepthMaps(
            downscale=METASHAPE_DEFAULTS["depth_downscale"],
            filter_mode=getattr(Metashape, METASHAPE_DEFAULTS["depth_filter_mode"]),
            reuse_depth=False,
            max_neighbors=METASHAPE_DEFAULTS.get("max_neighbors", 16),
            subdivide_task=True,  # Split into subtasks for better GPU utilization
            progress=_progress_logger(f"Building depth maps for {transect_id}")
        )

        # Build model
        logging.info(f"Building model for {transect_id}")
        registry_client.stage(transect_id, 1, "mesh")
        chunk.buildModel(
            source_data=Metashape.DepthMapsData,
            surface_type=getattr(Metashape, METASHAPE_DEFAULTS["surface_type"]),
            face_count=getattr(Metashape, METASHAPE_DEFAULTS["face_count"]),
            interpolation=getattr(Metashape, METASHAPE_DEFAULTS["interpolation"]),
            vertex_colors=METASHAPE_DEFAULTS["vertex_colors"],
            subdivide_task=True,  # Split into subtasks for better GPU utilization
            progress=_progress_logger(f"Building mesh for {transect_id}")
        )

        # Verify model exists. We raise here (rather than just logging)
        # because the old "log and continue" path let step 1 mark a chunk
        # as complete with no usable mesh. See the 2026-05 FLC T6 incident.
        if not chunk.model:
            raise RuntimeError(
                f"buildModel returned but chunk.model is None for {transect_id}. "
                f"Treating as failed and refusing to advance."
            )
        logging.info(f"Model built successfully with {len(chunk.model.faces)} faces.")

        # Remove small disconnected mesh components (Phase 2 step2 did this
        # before marker work; the validated flow keeps the cleanup in the chain)
        chunk.model.removeComponents(99)
        logging.info("Removed small disconnected mesh components (fewer than 99 faces)")

        # Automatic scaling (flow item 5): after the model builds, as in the
        # validated manual KGC T2 workflow. Scale never blocks: MANUAL_NEEDED
        # continues the build with the unscaled branches applied downstream
        # and the timepoint listed at the manual gate.
        scale_status = scale_utils.MANUAL_NEEDED
        scale_error = scale_utils.SENTINEL_ERROR
        scale_bars = 0
        if products_cfg.get("scale_in_step1", True):
            logging.info(f"Scaling {transect_id} from coded targets")
            registry_client.stage(transect_id, 1, "scaling")
            try:
                scale_status, scale_error, scale_bars = scale_utils.apply_scale(
                    Metashape, chunk, model_cfg, logging.info)
            except Exception as exc:
                logging.warning(f"Scaling raised {exc}; continuing without verified scale")
            scale_ppm = scale_utils.ppm(scale_error, mean_bar_length(model_cfg))
            update_tracking(transect_id, {
                "Scale": scale_status,
                "Scale Error (m)": f"{scale_error:.6f}",
                "Scale Error (ppm)": str(scale_ppm),
                "Scale Bars": str(scale_bars),
            })
            if scale_status != scale_utils.PASS:
                bar_word = "bar" if scale_bars == 1 else "bars"
                message = (
                    f"MANUAL SCALE NEEDED for {transect_id}: {scale_bars} {bar_word} found, "
                    f"error {scale_error:.3f}"
                )
                logging.warning(message)
                print_boxed(message)
                registry_note(transect_id, message)
        else:
            scale_ppm = scale_utils.ppm(scale_error, mean_bar_length(model_cfg))

        # DEM (flow item 6), from the EXISTING Ultra High depth maps, before
        # they are deleted. The DEM stays in the psx: no raster export here,
        # and no orthomosaic (that is built later from this DEM, after the
        # manual edits).
        notes_bits = []
        dem_mm_per_pix = ""
        if products_cfg.get("build_dem", True):
            units_note = "" if scale_status == scale_utils.PASS else " (unscaled units)"
            logging.info("Building DEM from depth maps")
            registry_client.stage(transect_id, 1, "dem")
            dem_kwargs = {
                "source_data": Metashape.DepthMapsData,
                "interpolation": Metashape.EnabledInterpolation,
                "progress": _progress_logger(f"Building DEM for {transect_id}"),
            }
            dem_resolution = float(products_cfg.get("dem_resolution", 0))
            if dem_resolution > 0:
                dem_kwargs["resolution"] = dem_resolution
            chunk.buildDem(**dem_kwargs)
            if chunk.elevation is None:
                raise RuntimeError(f"DEM build produced no elevation for {transect_id}")
            dem_mm_per_pix = round(chunk.elevation.resolution * 1000, 3)
            logging.info(f"DEM built in the project at {dem_mm_per_pix:.3f} mm/pix{units_note}")
            notes_bits.append(f"DEM {dem_mm_per_pix:.2f} mm/pix{units_note}")
            manifest.append_event(PROJECT_NAME, transect_id, "dem", "created", psx_path,
                                  details=f"{dem_mm_per_pix:.3f} mm/pix{units_note}, kept in the project")

        # Delete depth maps (flow item 7): ~12 GB per chunk at full res and
        # nothing downstream needs them. Removal attribute per probe_results.
        if products_cfg.get("delete_depth_maps", True):
            logging.info(f"Deleting depth maps for model {transect_id}")
            registry_client.stage(transect_id, 1, "delete_depth_maps")
            removed = False
            for attr in ("depth_maps_sets", "depth_maps"):
                assets = getattr(chunk, attr, None)
                if not assets:
                    continue
                try:
                    chunk.remove(assets if isinstance(assets, list) else [assets])
                    removed = True
                    break
                except (AttributeError, TypeError) as exc:
                    logging.warning(f"Depth-map removal via {attr} failed: {exc}")
            if removed:
                logging.info("Depth maps deleted from the chunk")
            else:
                logging.warning("Could not delete depth maps; PSX keeps them")

        # Flow items 8 to 10: decimate to the delivery mesh, smooth it,
        # texture it. Exactly one mesh asset must remain in the chunk.
        faces_full = chunk.model.statistics().faces
        decimation_factor = int(products_cfg.get("decimation_factor", 10))
        if decimation_factor > 1:
            registry_client.stage(transect_id, 1, "decimating")
            target_faces = max(1, faces_full // decimation_factor)
            logging.info(f"Decimating mesh {faces_full:,} to {target_faces:,} faces (factor {decimation_factor})")
            chunk.decimateModel(
                face_count=target_faces,
                apply_to_selection=False,
                replace_asset=True,
            )
            logging.info(f"Active mesh now {chunk.model.statistics().faces:,} faces")

        smooth_strength = int(products_cfg.get("smooth_strength", 4))
        logging.info(f"Smoothing delivery mesh at strength {smooth_strength}")
        chunk.smoothModel(
            strength=smooth_strength,
            apply_to_selection=False,
            fix_borders=METASHAPE_DEFAULTS.get("fix_borders", True),
            preserve_edges=METASHAPE_DEFAULTS.get("preserve_edges", False),
            replace_asset=True,
        )

        # Second-mesh guard: remove any non-active mesh assets regardless of
        # what replace_asset did.
        try:
            stale = [m for m in chunk.models if m.key != chunk.model.key]
            if stale:
                chunk.remove(stale)
                logging.info(f"Removed {len(stale)} stale mesh asset(s)")
        except (AttributeError, TypeError) as exc:
            logging.warning(f"Could not check for stale mesh assets: {exc}")

        faces_delivery = chunk.model.statistics().faces

        # Texture sizing (flow item 10): computed pages at 8192 from mesh
        # area when scaled; fixed pages when the scale needs manual work.
        registry_client.stage(transect_id, 1, "texturing")
        try:
            area_m2 = float(chunk.model.area())
        except Exception:
            area_m2 = 0.0
        page_count = scale_utils.compute_texture_pages(
            area_m2,
            float(products_cfg.get("texture_pixel_size", 0.0005)),
            8192,
            scale_status == scale_utils.PASS,
            int(products_cfg.get("unscaled_page_count", 4)),
        )
        logging.info(f"Building UV and texture: {page_count} page(s) of 8192 (area {area_m2:.1f})")
        chunk.buildUV(
            mapping_mode=getattr(Metashape, METASHAPE_DEFAULTS["mapping_mode"]),
            texture_size=8192,
            page_count=page_count,
            progress=_progress_logger(f"Building UV for {transect_id}"),
        )

        # Build texture
        logging.info(f"Building texture for model {transect_id}")

        # Check if we should use GPU for texture generation
        enable_texture_gpu = METASHAPE_DEFAULTS.get("enable_texture_gpu", False)

        if not enable_texture_gpu:
            # Save current GPU state
            saved_gpu_mask = Metashape.app.gpu_mask
            saved_cpu_enable = Metashape.app.cpu_enable

            # Temporarily disable GPU for texture building
            Metashape.app.gpu_mask = 0
            Metashape.app.cpu_enable = True
            logging.info("GPU disabled for texture building (using CPU only)")

        # Build texture without gpu_mask parameter
        chunk.buildTexture(
            texture_size=8192,
            texture_type=getattr(Metashape.Model, METASHAPE_DEFAULTS["texture_type"]),
            blending_mode=getattr(Metashape, METASHAPE_DEFAULTS["blending_mode"]),
            ghosting_filter=METASHAPE_DEFAULTS.get("ghosting_filter", True),
            fill_holes=METASHAPE_DEFAULTS.get("fill_holes", True),
            progress=_progress_logger(f"Building texture for {transect_id}")
        )

        if not enable_texture_gpu:
            # Restore GPU state for subsequent operations
            Metashape.app.gpu_mask = saved_gpu_mask
            Metashape.app.cpu_enable = saved_cpu_enable
            logging.info("GPU re-enabled after texture building")

        # Verify texture exists. Same reasoning as the model check above:
        # a textureless chunk is not a usable Step 1 output. Raise rather
        # than silently marking Step 1 complete.
        if not chunk.model:
            raise RuntimeError(f"Texture build skipped: model missing for {transect_id}")
        if not chunk.model.textures:
            raise RuntimeError(
                f"buildTexture completed but chunk.model.textures is empty for {transect_id}"
            )
        logging.info(f"Texture built successfully with {len(chunk.model.textures)} texture(s).")

        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()

        # Record the build-time facts now, but DO NOT mark Step 1 complete
        # here: that flag flips only after the PSX save is verified on disk
        # (see process_model). The 2026-05 FLC T6 incident wrote complete=True
        # before doc.save() landed, leaving an unrecoverable orphan chunk.
        tracking_data = {
            "Status": "Step 1 build complete (awaiting save verification)",
            "Step 1 start time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Step 1 end time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Step 1 processing time (s)": str(processing_time),
            "Aligned cameras": str(len([c for c in chunk.cameras if c.transform])),
            "Total cameras": str(len(chunk.cameras))
        }
        if notes_bits:
            tracking_data["Notes"] = "; ".join(notes_bits)
        update_tracking(transect_id, tracking_data)

        logging.info(f"Successfully processed model {transect_id} in {processing_time:.1f} seconds")
        Metashape.app.update()  # Added update after model build
        return {
            # started_at stays a datetime: process_model restamps end_time
            # and seconds once the save, the verification and the psx rename
            # are done, so the registry carries the timepoint's wall time
            # rather than the reconstruction time alone.
            "started_at": start_time,
            "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round(processing_time, 1),
            "scale_status": scale_status,
            "scale_error_m": scale_error,
            "scale_error_ppm": scale_ppm,
            "scale_bars": scale_bars,
            "tie_points": tie_points,
            "faces_full": faces_full,
            "faces_delivery": faces_delivery,
            "texture_pages": page_count,
            "dem_mm_per_pix": dem_mm_per_pix,
            "params_summary": params_summary(page_count),
            "report_file": "",
        }

    except Exception as e:
        error_time = now_stamp()
        error_msg = f"Error processing model {transect_id}: {str(e)}"
        logging.error(error_msg)
        traceback.print_exc()
        update_tracking(transect_id, {
            "Status": "Error in Step 1",
            "Step 1 complete": "False",
            "Step 1 error time": error_time,
            "Notes": status_note(transect_id, error_msg),
        })
        registry_failure(transect_id, error_msg)
        return None


def process_model(transect_ids, psx_path):
    """
    Process timepoints into one psx and close it before returning.

    Opens (or creates) the bundle at psx_path, appends and processes each
    pending timepoint, saves and verifies each one, then renames the bundle
    to the year range its chunks now cover and writes the run's numbers back
    to the registry.

    Args:
        transect_ids (list): timepoint ids to process into this psx
        psx_path (str): where the psx lives before any range rename

    Returns:
        tuple: (psx path after the rename, {transect_id: facts})
    """
    if not transect_ids:
        return psx_path, {}

    os.makedirs(os.path.dirname(psx_path) or ".", exist_ok=True)

    # Create a new document for this psx. If psx_path already exists
    # (a rerun, a resume, or the site/transect's growing bundle), open it
    # instead of overwriting it: a fresh doc.save() here would erase any
    # chunks already completed and verified in an earlier run.
    doc = Metashape.Document()
    if os.path.exists(psx_path):
        try:
            doc.open(psx_path, read_only=False, ignore_lock=True)
            logging.info(f"Opened existing project {psx_path} ({len(doc.chunks)} chunk(s) preserved)")
        except (RuntimeError, OSError) as e:
            # A prior run that crashed mid-save can leave a truncated or
            # zero-byte .psx (Metashape raises RuntimeError: "Empty XML
            # data" on open). Rather than let that exception kill the whole
            # Step 1 run, quarantine the unopenable bundle -- both the .psx
            # and its sibling .files directory, which share ownership of
            # the project -- and start fresh, same as if psx_path never
            # existed.
            logging.error(f"Could not open existing project {psx_path}, quarantining and starting fresh: {e}")
            stem, _ = os.path.splitext(psx_path)
            files_dir = f"{stem}.files"
            quarantined_psx = f"{psx_path}.corrupt_{TIMESTAMP}"
            os.rename(psx_path, quarantined_psx)
            logging.error(f"Quarantined unopenable project file to {quarantined_psx}")
            if os.path.exists(files_dir):
                quarantined_files_dir = f"{files_dir}.corrupt_{TIMESTAMP}"
                os.rename(files_dir, quarantined_files_dir)
                logging.error(f"Quarantined unopenable project data to {quarantined_files_dir}")
            doc = Metashape.Document()
            doc.save(psx_path)
            logging.info(f"Initial project save to {psx_path} (project storage needed for DEM builds)")
    else:
        # Initial save: buildDem() later writes elevation data into PROJECT
        # storage, which only exists once the document has been saved to a
        # .psx path at least once. Without this, the first model in a fresh
        # bundle fails inside chunk.buildDem with Metashape error
        # "Empty frame path". Save the still-empty document now so project
        # storage exists before any model processes.
        doc.save(psx_path)
        logging.info(f"Initial project save to {psx_path} (project storage needed for DEM builds)")

    # Results tracking
    results = {}

    # Bound to Metashape objects inside the loop and cleared before the
    # document is released: a live chunk (or the stale-chunk list) keeps the
    # Document alive through the collector, and a Document that is still
    # open must not be renamed on disk.
    chunk = None
    stale_chunks = None

    for i, transect_id in enumerate(transect_ids):
        # Pause boundary: stop before starting a new model. Prior models
        # were already saved+verified per-timepoint below, so we flush the
        # doc only if it actually holds processed chunks.
        checkpoint_pause(
            f"before model {transect_id} in {os.path.basename(psx_path)}",
            save_fn=(lambda: doc.save(psx_path)) if results else None,
        )

        # Skip if already processed
        status = get_transect_status(transect_id)
        if status.get("Step 1 complete", "False") == "True":
            logging.info(f"Model {transect_id} already processed, skipping...")
            continue

        logging.info(f"Processing model {transect_id} ({i+1}/{len(transect_ids)}) "
                     f"into {os.path.basename(psx_path)}")

        # A chunk already labeled transect_id in an opened-existing document
        # is by definition incomplete or superseded (the complete case was
        # caught by the skip above); drop it before reprocessing so we
        # don't accumulate duplicate labels in the psx. The one exception is
        # a timepoint the registry records as manually edited: that chunk
        # holds work nothing else has a copy of, so it is left alone unless
        # the operator asked for the rebuild with --force.
        stale_chunks = [c for c in doc.chunks if c.label == transect_id]
        if stale_chunks:
            row = registry_client.row(transect_id) or {}
            decision = stale_chunk_decision(
                registry_client.enabled(),
                row.get("manual_edit_status"),
                force_rerun_requested(),
            )
            if decision == "skip":
                stale_chunks = None
                logging.warning(
                    f"{transect_id}: the registry records manual edits as done and this "
                    f"psx already holds a chunk with that label. Leaving it untouched."
                )
                print_boxed(f"{transect_id} {MANUAL_EDIT_SKIP_NOTE}")
                registry_note(transect_id, MANUAL_EDIT_SKIP_NOTE)
                continue
            doc.remove(stale_chunks)
            logging.info(f"Removed {len(stale_chunks)} stale chunk(s) labeled {transect_id} before reprocessing")

        # Create a new chunk for this timepoint
        chunk = doc.addChunk()

        facts = process_transect(transect_id, chunk, doc, psx_path)

        if facts is not None:
            update_tracking(transect_id, {"PSX file": psx_path})

            # Create the step 1 report for this timepoint
            logging.info(f"Generating step 1 report for {transect_id}")
            registry_client.stage(transect_id, 1, "report")
            try:
                reports_dir = DIRECTORIES["reports"]
                os.makedirs(reports_dir, exist_ok=True)

                report_file_path = os.path.join(reports_dir, f"{transect_id}_step1.pdf")
                chunk.exportReport(report_file_path, title=f"Model {transect_id} - Step 1 Report")

                update_tracking(transect_id, {"Report file": report_file_path})
                facts["report_file"] = report_file_path

                logging.info(f"Report generated: {report_file_path}")
                manifest.append_event(PROJECT_NAME, transect_id, "report", "created", report_file_path)
            except Exception as e:
                logging.error(f"Error generating report for {transect_id}: {str(e)}")

            # Save the document after this chunk, then re-open it in a
            # throwaway Document to confirm the chunk landed with model +
            # texture before flipping "Step 1 complete" to True. Without
            # this gate, a doc.save() that fails mid-write (e.g. parallel
            # process, full disk, crash) leaves a tracking row that says
            # "complete" but points at unusable bytes.
            logging.info(f"Saving document to {psx_path} after processing {transect_id}")
            registry_client.stage(transect_id, 1, "saving")
            Metashape.app.update()
            doc.save(psx_path)

            logging.info(f"Verifying saved chunk {transect_id} in {os.path.basename(psx_path)}")
            registry_client.stage(transect_id, 1, "verifying")
            if verify_psx_chunk(psx_path, transect_id):
                update_tracking(transect_id, {
                    "Status": "Step 1 complete",
                    "Step 1 complete": "True",
                })
                results[transect_id] = facts
                logging.info(f"Step 1 verified for {transect_id} in {psx_path}")
                manifest.append_event(PROJECT_NAME, transect_id, "psx", "updated", psx_path,
                                      details="step1 complete, verified")
            else:
                error_time = now_stamp()
                note = (
                    f"doc.save returned but reopen could not find chunk "
                    f"{transect_id} with model+texture in {psx_path}. "
                    f"Treat as needs-rerun."
                )
                update_tracking(transect_id, {
                    "Status": "Step 1 save verification failed",
                    "Step 1 complete": "False",
                    "Step 1 error time": error_time,
                    "Notes": status_note(transect_id, note),
                })
                registry_failure(transect_id, note)
                logging.error(
                    f"Step 1 save verification FAILED for {transect_id} - "
                    f"row left as Step 1 complete=False"
                )
        else:
            # process_transect already marked the row (and the registry) as
            # failed; just save the doc so any partial chunk artifacts are
            # persisted for forensic inspection.
            logging.info(f"Saving document to {psx_path} after FAILED processing of {transect_id}")
            Metashape.app.update()
            doc.save(psx_path)

    # Final save, then read the labels the bundle now holds before the
    # document is released: the range rename below needs them, and so do the
    # rows of timepoints processed in earlier runs whose "PSX file" cell
    # points at the pre-rename name.
    logging.info(f"Final save of {os.path.basename(psx_path)}")
    Metashape.app.update()  # Keep update BEFORE the final save
    doc.save(psx_path)
    chunk_labels = [c.label for c in doc.chunks]
    years = years_in_psx(doc)

    # Clear EVERY reference to the document before the bundle is renamed on
    # disk: chunk and stale_chunks are Metashape objects that own the
    # Document, so dropping doc alone would not release it.
    chunk = None
    stale_chunks = None
    doc = None
    gc.collect()

    # Finalization: rename the bundle to the range it now covers, then write
    # the run's numbers back to the registry. The reconstruction above is
    # already saved and verified on disk, so nothing here may take the run
    # down: a failure is recorded against the timepoint and the caller's
    # completeness sweep still runs.
    final_psx_path = psx_path
    try:
        rename_target = psx_range_target(psx_path, years)
        final_psx_path = rename_psx_range(psx_path, years)

        if rename_target is not None and final_psx_path == psx_path:
            # The rename was refused (a name collision). The bundle keeps its
            # current name, so that is what the registry must record.
            conflict = (
                f"psx name conflict: {os.path.basename(psx_path)} could not become "
                f"{os.path.basename(rename_target)}"
            )
            logging.error(conflict)
            for transect_id in results:
                registry_note(transect_id, conflict)

        if final_psx_path != psx_path:
            for label in chunk_labels:
                if not get_transect_status(label):
                    continue
                update_tracking(label, {"PSX file": final_psx_path})
                registry_client.update(label, psx_file=final_psx_path)
            manifest.append_event(PROJECT_NAME, ", ".join(chunk_labels), "psx", "renamed",
                                  final_psx_path, details=f"was {os.path.basename(psx_path)}")

        # Wall time for the timepoint, measured here so the atlas sees the
        # save, the verification and the rename, not the reconstruction alone.
        finished = datetime.datetime.now()
        for transect_id, facts in results.items():
            facts["end_time"] = finished.strftime("%Y-%m-%d %H:%M:%S")
            facts["seconds"] = round((finished - facts["started_at"]).total_seconds(), 1)
            registry_success(transect_id, facts, final_psx_path)

    except Exception as exc:
        logging.error(
            f"Step 1 finalization failed for {os.path.basename(psx_path)}: {exc}")
        traceback.print_exc()
        for transect_id in results:
            try:
                registry_failure(
                    transect_id,
                    f"reconstruction completed and verified on disk, but step 1 "
                    f"finalization (psx rename or registry write-back) failed: {exc}",
                )
            except Exception as note_exc:
                logging.error(
                    f"Could not record the finalization failure for {transect_id}: {note_exc}")

    return final_psx_path, results


def preferred_psx(site, transect, readable_id=None):
    """The psx the registry already associates with this site and transect.

    This timepoint's own row comes first, so a forced rerun goes back into
    the bundle its chunk already lives in and the stale-chunk swap can
    replace it; otherwise the most recent sibling row's psx_file wins. Only
    a path that is still on disk is offered. It outranks a scan of
    range-named files in current_psx, so a bundle the name scan cannot
    reconstruct (a numbered collision sibling, a bundle whose rename was
    refused) is still the one appended to. None outside TCRMP mode.
    """
    rows = registry_client.rows_for(site=site, transect=transect) or []
    ordered = list(reversed(rows))
    if readable_id:
        ordered.sort(key=lambda r: 0 if r.get("readable_id") == readable_id else 1)
    for row in ordered:
        candidate = (row.get("psx_file") or "").strip()
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def frame_model_dirs():
    """Every timepoint folder under frames/ (one folder per model)."""
    frames_dir = DIRECTORIES["frames"]
    if not os.path.exists(frames_dir):
        return []
    return [d for d in os.listdir(frames_dir)
            if os.path.isdir(os.path.join(frames_dir, d))]


def non_tcrmp_batches(transect_ids, timestamp):
    """(psx path, [ids]) batches for a non-TCRMP run: the original naming,
    psx_{N}_{date}.psx in the project folder, or {id}_{date}.psx when the cap
    is one chunk per psx."""
    batches = []
    current_batch = []
    for transect_id in transect_ids:
        if len(current_batch) >= MAX_CHUNKS_PER_PSX:
            batches.append(current_batch)
            current_batch = []
        current_batch.append(transect_id)
    if current_batch:
        batches.append(current_batch)

    work = []
    for i, batch in enumerate(batches):
        batch_num = i + 1  # Start with batch 1
        if len(batch) == 1 and MAX_CHUNKS_PER_PSX == 1:
            psx_filename = f"{batch[0]}_{timestamp}.psx"
        else:
            psx_filename = f"psx_{batch_num}_{timestamp}.psx"
        work.append((os.path.join(DIRECTORIES["psx_dir"], psx_filename), batch))
    return work


def print_manual_gate(processed):
    """List every timepoint this run produced, with the folder to open and
    the psx it now lives in, plus the ones whose scale needs manual work."""
    if not processed:
        return
    project_dir = DIRECTORIES["base"]
    print()
    print("=" * 70)
    print(" STEP 1 COMPLETE - MANUAL EDIT GATE")
    print("=" * 70)
    print(f"  Folder: {project_dir}")
    for transect_id, psx_path, facts in processed:
        print(f"    {transect_id}")
        print(f"      psx:   {os.path.basename(psx_path)}")
        print(f"      scale: {facts['scale_status']} "
              f"({facts['scale_bars']} bar(s), {facts['scale_error_ppm']} ppm)")
    manual = [(t, f) for t, _p, f in processed if f["scale_status"] != scale_utils.PASS]
    if manual:
        print()
        print("  MANUAL SCALE:")
        for transect_id, facts in manual:
            print(f"    {transect_id}: {facts['scale_bars']} bar(s), "
                  f"error {facts['scale_error_m']:.3f} m")
    print("=" * 70)
    print()


def main():
    """Reconstruct every pending timepoint in this processing folder."""
    # Preflight: confirm temp volume has room for Metashape intermediates,
    # then take an exclusive project lock so a second step1/step2 cannot
    # race against this one (the 2026-05 FLC T6 incident root cause).
    # The lock_fp must stay open for the lifetime of main(); we bind it
    # to a local so it is released on return / exception.
    check_temp_free_space()
    lock_fp = acquire_project_lock("step1")  # noqa: F841 -- holds the flock

    tcrmp = bool(PARAMS.get("processing", {}).get("tcrmp", False))
    registry_client.configure(PARAMS)

    transect_dirs = frame_model_dirs()
    if not transect_dirs:
        logging.error(f"No model directories found in {DIRECTORIES['frames']}")
        print(f"ERROR: no model directories found in {DIRECTORIES['frames']}; nothing to reconstruct.")
        sys.exit(1)

    # Filter for unprocessed timepoints
    unprocessed_transects = []
    for transect_id in transect_dirs:
        status = get_transect_status(transect_id)
        if status.get("Step 1 complete", "False") != "True":
            unprocessed_transects.append(transect_id)

    if not unprocessed_transects:
        logging.info("All models have already been processed")
        return

    unprocessed_transects = order_models(unprocessed_transects, tcrmp)
    logging.info(f"Found {len(unprocessed_transects)} model(s) to process: "
                 f"{', '.join(unprocessed_transects)}")

    processed = []

    if tcrmp:
        # One document per timepoint: the site and transect's current psx is
        # resolved fresh each time, so a chunk appended (and a bundle renamed)
        # by the previous timepoint is seen by the next one.
        project_dir = DIRECTORIES["psx_dir"]
        naming = naming3d()
        for transect_id in unprocessed_transects:
            checkpoint_pause(f"before timepoint {transect_id}")
            try:
                parts = naming.id_parts(transect_id)
            except ValueError as e:
                logging.error(f"Skipping {transect_id}: {e}")
                continue
            psx_path, is_new = current_psx(
                project_dir, parts["site"], parts["transect"], parts["year"],
                MAX_CHUNKS_PER_PSX, count_chunks_in_psx,
                preferred=preferred_psx(parts["site"], parts["transect"], transect_id),
                label=transect_id,
            )
            logging.info(
                f"{transect_id}: {'starting' if is_new else 'appending to'} "
                f"{os.path.basename(psx_path)}"
            )
            final_psx_path, results = process_model([transect_id], psx_path)
            for done_id, facts in results.items():
                processed.append((done_id, final_psx_path, facts))
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d")
        for psx_path, batch in non_tcrmp_batches(unprocessed_transects, timestamp):
            checkpoint_pause(f"before {os.path.basename(psx_path)}")
            logging.info(f"Starting {os.path.basename(psx_path)} with {len(batch)} model(s)")
            final_psx_path, results = process_model(batch, psx_path)
            for done_id, facts in results.items():
                processed.append((done_id, final_psx_path, facts))

    # Post-run sweep: walk the tracking CSV one more time and re-verify
    # every row that claims Step 1 complete. This catches any drift between
    # the CSV and the PSX files on disk (the 2026-05 FLC T6 mode).
    logging.info("Running Step 1 completeness sweep against tracking CSV...")
    sweep_failures = []
    for transect_id in transect_dirs:
        status = get_transect_status(transect_id)
        if status.get("Step 1 complete", "False") != "True":
            continue
        psx_path = (status.get("PSX file") or "").strip()
        if not psx_path:
            sweep_failures.append((transect_id, "no PSX file recorded"))
            continue
        if not verify_psx_chunk(psx_path, transect_id):
            note = (
                f"Post-run sweep could not verify {transect_id} in "
                f"{psx_path}; row reset to needs-rerun."
            )
            sweep_failures.append((transect_id, f"verify failed for {psx_path}"))
            update_tracking(transect_id, {
                "Status": "Step 1 sweep failed",
                "Step 1 complete": "False",
                "Notes": status_note(transect_id, note),
            })
            registry_failure(transect_id, note)
    if sweep_failures:
        logging.error(
            f"Step 1 completeness sweep flagged {len(sweep_failures)} model(s); "
            f"their tracking rows were reset:"
        )
        for tid, reason in sweep_failures:
            logging.error(f"  {tid}: {reason}")
    else:
        logging.info("Step 1 completeness sweep: all complete rows verified on disk")

    logging.info("Step 1 processing complete")
    print_manual_gate(processed)

    # Exit code is the only signal run_phase1 (and the VICARIUS runner behind
    # it) reads. Reconstructing nothing that was queued is a failed run, not a
    # quiet success with an empty manual gate; a partial run is reported and
    # still succeeds, because the timepoints that did finish are real work.
    queued = len(unprocessed_transects)
    if queued and not processed:
        logging.error(f"Step 1 completed 0 of {queued} queued model(s); nothing was reconstructed.")
        print(f"ERROR: step 1 completed 0 of {queued} queued model(s). See the errors above.")
        sys.exit(1)
    if len(processed) != queued:
        logging.warning(
            f"Step 1 completed {len(processed)} of {queued} queued model(s); "
            f"the rest failed or were skipped. See the messages above."
        )


if __name__ == "__main__":
    main()
