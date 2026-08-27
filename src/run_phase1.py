#!/usr/bin/env python3
"""
run_phase1.py - Interactive CLI runner for 3D Phase 1 processing.

Two modes:

  TCRMP (default, --tcrmp): rows come from the shared TCRMP 3D registry
  (vicarius/_METADATA/3d), selected by --ids / --site+--transect / or, with
  none of those given, every pending timepoint. Each timepoint's processing
  folder lives NEXT TO its own video (never copied, never symlinked); all
  timepoints of the same site+transect share one folder and one growing
  psx, so the folder is only created once (next to whichever video is
  processed first) and every later timepoint reuses it.

  Plain (--no-tcrmp): the original --input/--project pair, minus copying.
  Video input is read in place from --input; frame-folder input is
  symlinked (read-only) into the project's frames/ directory. Non-TCRMP ids
  are the original file name without extension - no multi-part merging, no
  naming-pattern validation.

Parameter overrides arrive as repeatable `--param dotted.path=value` and are
written into the project's analysis_params.yaml before step 0 runs (TCRMP:
right after the processing folder is prepared; plain: right after project
setup). That is how the VICARIUS UI form reaches a TCRMP run, whose
processing folder does not exist yet at launch time.

Usage:
    python src/run_phase1.py                          # TCRMP, every pending timepoint
    python src/run_phase1.py --site MRS --transect T1  # TCRMP, one site/transect
    python src/run_phase1.py --param processing.frames_per_transect=300
    python src/run_phase1.py --no-tcrmp --input /path/to/input --project /path/to/project
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import registry_client
import status_rows
import videos as videos_mod

# ---------------------------------------------------------------------------
# VICARIUS logging integration
# ---------------------------------------------------------------------------
VICARIUS_ROOT = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius")
sys.path.insert(0, os.path.join(VICARIUS_ROOT, "_logging", "src"))
try:
    from vicarius_log import get_log

    VICARIUS_LOGGING = True
except ImportError as exc:
    VICARIUS_LOGGING = False
    logging.warning(
        f"VICARIUS platform logging unavailable (could not import vicarius_log "
        f"from {VICARIUS_ROOT}/_logging/src): {exc}. Run events will not be "
        f"recorded to the platform log."
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SRC_DIR = Path(__file__).resolve().parent  # github_repo/src/
GITHUB_REPO_DIR = SRC_DIR.parent  # github_repo/
MODULE_DIR = GITHUB_REPO_DIR.parent  # modules/<module_name>/
MODULE_NAME = os.path.basename(MODULE_DIR)  # e.g. "3D_phase_1"; used for provenance/logging
TEMPLATE_PARAMS = GITHUB_REPO_DIR / "analysis_params.yaml"

# Metashape detection paths (ordered)
METASHAPE_SEARCH_PATHS = [
    "/home/bizon/applications/metashape-pro_2_2_2_amd64/metashape-pro/metashape",
]

# Cooperative-pause contract. A step (step0/step1) that sees the pause sentinel
# exits with this code; we translate that into a PipelinePaused so main() can
# stop the pipeline gracefully and propagate PAUSE_EXIT_CODE to the VICARIUS
# runner (which records the run as "paused", not "failed"). Re-running resumes
# - already-extracted/reconstructed timepoints are skipped by step0/step1.
PAUSE_EXIT_CODE = 42


class PipelinePaused(Exception):
    """Raised when a step exits because a pause was requested."""


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tiff", ".tif", ".png"}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def banner(text: str) -> None:
    """Print a section banner."""
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def _naming3d():
    """Lazy import of the shared vicarius/_METADATA/3d naming rules (same
    sys.path convention already used by registry_client.py and videos.py)."""
    root = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius")
    lib_dir = os.path.join(root, "_METADATA", "3d")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import naming3d

    return naming3d


# ---------------------------------------------------------------------------
# Section-aware analysis_params.yaml editing
# ---------------------------------------------------------------------------
# A targeted text edit, not a YAML load/dump round trip, so the comments in
# the human-editable file (the one `open_params_for_editing` hands the user in
# vim) survive untouched, along with key order and every key nobody asked to
# change. Every lookup is scoped to the block its parent key opens, so
# processing.metashape.defaults.smooth_strength and
# processing.step1_products.smooth_strength are different keys and setting one
# never touches the other.

_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def yaml_scalar(value) -> str:
    """The YAML literal for a value that arrived as a command-line string.

    true/false and plain numbers are written bare, so config.py reads them
    back as booleans and numbers rather than strings. Everything else is
    JSON-quoted, so a path with a space, a colon or a "#" cannot break the
    file it is written into.
    """
    text = str(value).strip()
    if text.lower() in ("true", "false"):
        return text.lower()
    if _NUMBER_RE.match(text):
        return text
    return json.dumps(text)


def _significant(line: str) -> bool:
    """False for a blank line or a whole-line comment: either can sit at any
    indent, so neither may be mistaken for a key when bounding a block."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_trailing_comment(value_text: str):
    """(value, comment) for the text after "key:" on one line. A "#" inside a
    quoted scalar is part of the value, not the start of a comment."""
    quote = None
    for i, ch in enumerate(value_text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (i == 0 or value_text[i - 1] in " \t"):
            return value_text[:i], value_text[i:]
    return value_text, ""


def _find_key(lines, start: int, end: int, key: str):
    """(index, indent) of the line opening `key` at the top level of the block
    spanning [start, end), or (None, None). The block's top level is its
    shallowest significant indent, so a nested key of the same name deeper in
    the block is never matched."""
    base = None
    for i in range(start, end):
        if not _significant(lines[i]):
            continue
        indent = _indent_of(lines[i])
        if base is None or indent < base:
            base = indent
    if base is None:
        return None, None
    opener = re.compile(rf"^\s*{re.escape(key)}\s*:(\s|$)")
    for i in range(start, end):
        if not _significant(lines[i]):
            continue
        if _indent_of(lines[i]) != base:
            continue
        if opener.match(lines[i]):
            return i, base
    return None, None


def _block_end(lines, key_index: int, indent: int, end: int) -> int:
    """End (exclusive) of the block the key at `key_index` owns."""
    for i in range(key_index + 1, end):
        if not _significant(lines[i]):
            continue
        if _indent_of(lines[i]) <= indent:
            return i
    return end


def set_yaml_path(text: str, dotted_path: str, value: str) -> str:
    """Return `text` with `dotted_path` set to `value`.

    `value` must already be a valid YAML scalar (see yaml_scalar). A key that
    is missing is created, together with any missing parents, at the top of
    its parent's block; a key that exists has its value replaced in place,
    keeping its trailing comment.
    """
    lines = text.split("\n")
    parts = [p for p in dotted_path.split(".") if p]
    if not parts:
        raise ValueError(f"empty parameter path: {dotted_path!r}")

    start, end, parent_indent = 0, len(lines), -1
    for depth, key in enumerate(parts):
        index, key_indent = _find_key(lines, start, end, key)

        if index is None:
            child_indent = parent_indent + 2 if parent_indent >= 0 else 0
            created = []
            indent = child_indent
            for missing in parts[depth:-1]:
                created.append(" " * indent + missing + ":")
                indent += 2
            created.append(" " * indent + parts[-1] + ": " + value)
            if parent_indent < 0:
                # A missing top-level section goes at the end of the document,
                # after the sections that are already there.
                while lines and not lines[-1].strip():
                    lines.pop()
                lines.extend([""] + created + [""])
            else:
                lines[start:start] = created
            return "\n".join(lines)

        if depth == len(parts) - 1:
            head, _, tail = lines[index].partition(":")
            _, comment = _split_trailing_comment(tail)
            replaced = f"{' ' * key_indent}{key}: {value}"
            if comment.strip():
                replaced += " " + comment.strip()
            lines[index] = replaced
            return "\n".join(lines)

        parent_indent = key_indent
        start = index + 1
        end = _block_end(lines, index, key_indent, end)

    return "\n".join(lines)


def set_params_path(params_path: Path, dotted_path: str, value: str) -> None:
    """Apply set_yaml_path to a project's analysis_params.yaml on disk."""
    params_path.write_text(set_yaml_path(params_path.read_text(), dotted_path, value))


def parse_param_pairs(pairs):
    """[(dotted_path, yaml scalar)] from repeatable --param dotted.path=value."""
    parsed = []
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--param must be dotted.path=value, got: {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--param has an empty key: {pair!r}")
        parsed.append((key, yaml_scalar(raw)))
    return parsed


def apply_param_overrides(project_dir: Path, pairs) -> None:
    """Write every --param override into this project's analysis_params.yaml.

    This is how the VICARIUS UI form reaches a TCRMP run: the processing
    folder is minted mid-run from the module template, so the runner cannot
    edit a file that does not exist yet at launch and sends the values on the
    command line instead.
    """
    if not pairs:
        return
    params_path = Path(project_dir) / "analysis_params.yaml"
    for dotted_path, value in pairs:
        set_params_path(params_path, dotted_path, value)
        print(f"    Set {dotted_path}: {value} in {params_path.name}")


def _set_force_rerun(project_dir: Path, on: bool) -> None:
    """Record whether step 1 is running under --force, in the one file step 1
    reads. step1's stale-chunk swap consults it before removing a chunk from a
    timepoint the registry records as manually edited, so the operator's
    straightening and cropping is only rebuilt when the rebuild was asked for.
    Set right before step 1 launches and cleared right after, so the flag can
    never outlive the run that set it.
    """
    params_path = Path(project_dir) / "analysis_params.yaml"
    if not params_path.exists():
        return
    set_params_path(params_path, "processing.force_rerun", "true" if on else "false")


def detect_metashape() -> str:
    """Find the Metashape executable. Returns path or raises RuntimeError."""
    # 1. Environment variable override
    env_path = os.environ.get("METASHAPE_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path

    # 2. Known paths
    for path in METASHAPE_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # 3. Search $PATH
    found = shutil.which("metashape")
    if found:
        return found

    # 4. Prompt user
    print("\nMetashape Pro executable not found automatically.")
    print("Set $METASHAPE_PATH or provide the path below.")
    user_path = input("Metashape executable path: ").strip().strip("'\"")
    if user_path and os.path.isfile(user_path):
        return user_path

    raise RuntimeError(
        "Metashape Pro not found. Install it or set $METASHAPE_PATH."
    )


def create_venv(project_dir: Path) -> None:
    """Create Python 3.9 venv and install requirements."""
    venv_dir = project_dir / ".venv"
    if (venv_dir / "bin" / "python").exists():
        print("  venv present, reusing.")
        return

    python39 = shutil.which("python3.9")
    if not python39:
        raise RuntimeError(
            "python3.9 not found on PATH.\n"
            "Install with: sudo apt install python3.9 python3.9-venv python3.9-dev"
        )

    print(f"  Creating venv with {python39}...")
    subprocess.run([python39, "-m", "venv", str(venv_dir)], check=True)

    requirements_file = GITHUB_REPO_DIR / "requirements.txt"
    pip = venv_dir / "bin" / "pip"

    print("  Installing requirements...")
    subprocess.run([str(pip), "install", "-r", str(requirements_file)], check=True)


def ensure_project_ready(project_dir: Path) -> None:
    """One-time-per-folder setup a processing folder needs before step0/step1
    can run: the .venv. Checks for <project>/.venv/bin/python itself, ahead
    of calling create_venv, so a TCRMP folder shared across several
    timepoints in one run only pays for venv setup once - the check has to
    live here rather than solely inside create_venv so it still holds when
    create_venv is swapped out (e.g. tests)."""
    if (project_dir / ".venv" / "bin" / "python").exists():
        return
    create_venv(project_dir)


def open_params_for_editing(project_dir: Path) -> None:
    """Open analysis_params.yaml in vim with instructions."""
    params_file = project_dir / "analysis_params.yaml"

    banner("EDIT ANALYSIS PARAMETERS")
    print()
    print(f"  File: {params_file}")
    print()
    print("  Key settings to review/edit:")
    print("    processing.frames_per_transect  (default: 1000)")
    print("    processing.max_chunks_per_psx   (default: 4)")
    print("    processing.use_gpu              (default: true)")
    print("    processing.tcrmp                (default: true)")
    print("    model_processing.scale_bars     (set your scale bar markers/distances)")
    print()
    print("  Vim quick reference:")
    print("    i          - Enter insert mode (to edit text)")
    print("    Esc        - Exit insert mode")
    print("    :wq Enter  - Save and quit")
    print("    :q! Enter  - Quit without saving")
    print("    /text      - Search for 'text'")
    print()

    input("  Press Enter to open in vim...")
    subprocess.run(["vim", str(params_file)])


def run_step0(project_dir: Path) -> None:
    """Run frame extraction (step 0)."""
    banner("STEP 0: Frame Extraction")
    print()

    venv_python = project_dir / ".venv" / "bin" / "python"
    step0_script = SRC_DIR / "step0.py"

    cmd = [str(venv_python), str(step0_script), str(project_dir)]
    print(f"  Running: {' '.join(cmd)}")
    print()

    process = subprocess.run(cmd, cwd=str(GITHUB_REPO_DIR))

    if process.returncode == PAUSE_EXIT_CODE:
        raise PipelinePaused("Step 0 paused at a transect boundary")
    if process.returncode != 0:
        raise RuntimeError(f"Step 0 failed with return code {process.returncode}")

    print("\n  Step 0 complete.")


def run_step1(project_dir: Path, metashape_path: str) -> None:
    """Run initial 3D processing via Metashape (step 1)."""
    banner("STEP 1: Initial 3D Processing (Metashape)")
    print()

    venv_packages = project_dir / ".venv" / "lib" / "python3.9" / "site-packages"
    step1_script = SRC_DIR / "step1.py"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(venv_packages)

    cmd = [metashape_path, "-r", str(step1_script), str(project_dir)]
    print(f"  Running: {' '.join(cmd)}")
    print()

    process = subprocess.run(cmd, env=env, cwd=str(GITHUB_REPO_DIR))

    if process.returncode == PAUSE_EXIT_CODE:
        raise PipelinePaused("Step 1 paused at a model boundary")
    if process.returncode != 0:
        raise RuntimeError(f"Step 1 failed with return code {process.returncode}")

    print("\n  Step 1 complete.")


def print_manual_instructions(project_dir: Path) -> None:
    """Print instructions for the manual GUI step."""
    banner("PHASE 1 COMPLETE - MANUAL STEP REQUIRED")
    print()
    print("  Before running Phase 2, you must manually straighten and prepare")
    print("  each model in the Metashape GUI.")
    print()
    print(f"  PSX file location: {project_dir}/")
    print()
    print("  For each chunk in each PSX file:")
    print()
    print("  STRAIGHTENING (always required):")
    print("    1. Load the textured model")
    print("    2. Auto-adjust brightness/contrast on an image to improve texture")
    print("    3. Switch to rotate model view")
    print("    4. Rotate the model so it aligns horizontally at the top of the view")
    print("    5. Use 'Model > Region > Rotate Region to View' to set alignment")
    print("    6. Resize the region to crop to the model area (use top XY & side views)")
    print("    7. Use the rectangular crop tool to crop within the region bounds")
    print()
    print("  SCALING PREPARATION:")
    print("    1. Ensure coded targets are visible and properly positioned")
    print("    2. Verify at least 2 scale bars worth of targets are clearly visible")
    print()
    print("  3. Save the project and quit Metashape")
    print()
    print("  When done with all models, continue with the step 2 module for")
    print("  automatic scaling and export (see STEP2_HANDOFF.md at the")
    print("  module top level).")
    print()
    print("=" * 60)


def create_vicarius_run(purpose: str, study: str) -> Path:
    """Create a VICARIUS run in inprocess/ for metadata tracking."""
    sys.path.insert(0, str(SRC_DIR))
    from init_run import init_run

    run_name = f"run_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = init_run(
        run_name=run_name,
        module_path=MODULE_DIR,
        purpose=purpose,
        study=study,
    )
    return run_dir


def _link_output(vicarius_run_dir: Path, name: str, target: Path) -> None:
    """Best-effort symlink of a project dir into the VICARIUS run's outputs/."""
    if not vicarius_run_dir:
        return
    try:
        outputs_dir = vicarius_run_dir / "outputs"
        link = outputs_dir / name
        if not link.exists():
            os.symlink(str(target), str(link))
    except Exception:
        pass  # Non-critical


# ---------------------------------------------------------------------------
# TCRMP mode: registry-driven, processing folder next to the video
# ---------------------------------------------------------------------------


def _earliest_date_for(site: str, transect: str) -> str:
    """Synthetic YYYYMMDD for the earliest known registry timepoint of a
    site/transect (year + season_token -> YYYY0401 for _pbl, YYYY1001 for
    ann), so the shared processing folder is always named for the earliest
    timepoint even when a later one is processed first."""
    rows = registry_client.rows_for(site=site, transect=transect) or []
    dates = []
    for r in rows:
        year, token = r.get("year"), r.get("season_token")
        if not year or token not in ("_pbl", "ann"):
            continue
        dates.append(f"{year}0401" if token == "_pbl" else f"{year}1001")
    if not dates:
        raise RuntimeError(f"No dated registry rows found for {site} {transect}")
    return min(dates)


def prepare_tcrmp_folder(row: dict) -> Path:
    """Ensure the processing folder for a TCRMP registry row exists next to
    its video, has a ready .venv, and has its location recorded back into
    the registry - all before step 0 runs. Returns the project directory.

    All timepoints of the same site+transect share one processing folder
    and one growing psx: if any row of that site/transect already has a
    processing_location, this reuses it (even if it differs from THIS row's
    own video_location); a new folder is only created, next to THIS row's
    video, when no row of that site/transect has one yet - named for the
    earliest known timepoint regardless of processing order. venv setup
    (ensure_project_ready) is per-folder, not per-row, so a shared folder
    only pays for it once even when several timepoints are processed in the
    same run.
    """
    registry_client.configure({"processing": {"tcrmp": True}})
    readable_id = row["readable_id"]
    site, transect = row["site"], row["transect"]
    video_location = row.get("video_location") or ""
    if not os.path.isdir(video_location):
        raise RuntimeError(
            f"{readable_id}: video_location is not a local directory: {video_location!r}"
        )

    sibling_rows = registry_client.rows_for(site=site, transect=transect) or []
    existing = next((r["processing_location"] for r in sibling_rows if r.get("processing_location")), None)

    if existing:
        project_dir = Path(existing)
    else:
        earliest_date = _earliest_date_for(site, transect)
        folder_name = _naming3d().processing_folder_name(site, transect, earliest_date)
        project_dir = Path(video_location) / folder_name

    for sub in ("console", "frames", "reports"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    params_dst = project_dir / "analysis_params.yaml"
    if not params_dst.exists():
        shutil.copy2(str(TEMPLATE_PARAMS), str(params_dst))
        print(f"    Copied analysis_params.yaml to {project_dir}")
    set_params_path(params_dst, "processing.tcrmp", "true")

    registry_client.update(
        readable_id,
        processing_folder=project_dir.name,
        processing_location=str(project_dir),
        console_log=str(project_dir / "console"),
    )
    registry_client.stage(readable_id, 1, "starting")

    status_rows.write_identity_row(project_dir, row.get("original_videos", ""), readable_id)

    print("  Ensuring Python environment...")
    ensure_project_ready(project_dir)

    return project_dir


def select_rows(args) -> list:
    """Select the TCRMP registry rows this run should process, in
    chronological order grouped by site/transect (the order the shared psx
    needs, per naming3d.sort_key).

    Reads only through registry_client, so process=false rows are already
    excluded (registry_client.rows_for filters to process=="true"). --ids
    (comma-separated) or --site/--transect narrow the selection; with none
    of those given, every pending row is eligible. A row is skipped (with a
    console note, never a prompt) when its video_location is not a local
    directory that currently exists, or when its step1_status is already
    "complete" and --force was not passed.
    """
    registry_client.configure({"processing": {"tcrmp": True}})
    if not registry_client.enabled():
        raise RuntimeError(
            "TCRMP registry unavailable (shared library import failed at "
            f"{os.environ.get('VICARIUS_ROOT', '/mnt/rip/vicarius_drive/vicarius')}/_METADATA/3d). "
            "Pass --no-tcrmp to process a plain --input/--project pair instead."
        )

    ids = None
    if getattr(args, "ids", None):
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]

    rows = registry_client.rows_for(
        site=getattr(args, "site", None), transect=getattr(args, "transect", None), ids=ids
    ) or []
    n3d = _naming3d()
    rows = sorted(rows, key=lambda r: n3d.sort_key(r["readable_id"]))

    force = bool(getattr(args, "force", False))
    selected = []
    for r in rows:
        video_location = r.get("video_location") or ""
        if not os.path.isdir(video_location):
            print(f"    Skipping {r['readable_id']}: video_location is not a local directory ({video_location!r})")
            continue
        if r.get("step1_status") == "complete" and not force:
            print(f"    Skipping {r['readable_id']}: step1_status already complete (pass --force to reprocess)")
            continue
        selected.append(r)
    return selected


def run_tcrmp_mode(args, metashape_path: str) -> None:
    start_time = time.time()

    print("\nSelecting TCRMP timepoints from the registry...")
    rows = select_rows(args)
    if not rows:
        print("  No TCRMP timepoints selected (nothing pending, or the filters matched nothing).")
        return
    print(f"  Selected {len(rows)} timepoint(s):")
    for r in rows:
        print(f"    {r['readable_id']}  ({r.get('original_videos', '')})")

    banner("PURPOSE (Commandment VI)")
    if args.purpose:
        purpose = args.purpose.strip()
        print(f"  Purpose (from --purpose): {purpose}")
    else:
        purpose = input("  Why are you running this? ").strip()
    if not purpose:
        purpose = "3D Phase 1 processing (TCRMP)"

    vicarius_run_dir = None
    try:
        vicarius_run_dir = create_vicarius_run(purpose, "")
        print(f"\n  VICARIUS run created: {vicarius_run_dir.name}")
    except Exception as e:
        print(f"\n  Warning: Could not create VICARIUS run: {e}")
        print("  Continuing without VICARIUS run tracking.")

    start_event = None
    if VICARIUS_LOGGING:
        try:
            log = get_log()
            start_event = log.process_start(
                module=MODULE_NAME,
                purpose=purpose,
                inputs=[r["readable_id"] for r in rows],
            )
        except Exception as e:
            print(f"  Warning: VICARIUS logging failed: {e}")

    banner("SUMMARY")
    print(f"  Mode:        TCRMP registry")
    print(f"  Timepoints:  {len(rows)}")
    for r in rows:
        print(f"    {r['readable_id']}")
    print(f"  Purpose:     {purpose}")
    print()
    if args.yes:
        print("  Proceeding (--yes).")
    else:
        confirm = input("  Proceed? (y/n): ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    opened_params_for = set()
    processed_dirs = []
    try:
        for row in rows:
            readable_id = row["readable_id"]
            banner(f"TIMEPOINT: {readable_id}")
            project_dir = prepare_tcrmp_folder(row)
            apply_param_overrides(project_dir, getattr(args, "param_pairs", None))
            print(f"  Processing folder: {project_dir}")
            _link_output(vicarius_run_dir, readable_id, project_dir)

            if not args.skip_vim and project_dir not in opened_params_for:
                open_params_for_editing(project_dir)
            opened_params_for.add(project_dir)

            run_step0(project_dir)
            if getattr(args, "force", False):
                # select_rows let this row through on step1_status alone;
                # step1.py skips on its own status.csv cell, so clear that
                # too or the forced rerun would do nothing.
                status_rows.reset_step1(project_dir, readable_id)
                print(f"  --force: cleared the step 1 verdict for {readable_id} in status.csv")
            _set_force_rerun(project_dir, bool(getattr(args, "force", False)))
            try:
                run_step1(project_dir, metashape_path)
            finally:
                _set_force_rerun(project_dir, False)
            print_manual_instructions(project_dir)
            processed_dirs.append(project_dir)

    except PipelinePaused as e:
        banner("PHASE 1 PAUSED")
        print()
        print(f"  {e}")
        print(
            f"  Re-run {MODULE_NAME} with the same selection to resume "
            "(extracted / reconstructed timepoints are skipped)."
        )
        print()
        if VICARIUS_LOGGING and start_event:
            try:
                log = get_log()
                log.process_end(
                    module=MODULE_NAME,
                    status="paused",
                    duration_sec=time.time() - start_time,
                    parent_event_id=start_event,
                    notes=str(e),
                )
            except Exception:
                pass
        sys.exit(PAUSE_EXIT_CODE)

    except RuntimeError as e:
        elapsed = time.time() - start_time
        print(f"\nERROR: {e}")

        if VICARIUS_LOGGING and start_event:
            try:
                log = get_log()
                log.process_end(
                    module=MODULE_NAME,
                    status="failed",
                    duration_sec=elapsed,
                    parent_event_id=start_event,
                    notes=str(e),
                )
            except Exception:
                pass

        sys.exit(1)

    elapsed = time.time() - start_time
    if VICARIUS_LOGGING and start_event:
        try:
            log = get_log()
            log.process_end(
                module=MODULE_NAME,
                status="success",
                duration_sec=elapsed,
                outputs=[str(p) for p in processed_dirs],
                parent_event_id=start_event,
                notes=f"Processed {len(processed_dirs)} TCRMP timepoint(s)",
            )
        except Exception:
            pass

    hours = elapsed / 3600
    if hours >= 1:
        print(f"\n  Total runtime: {hours:.1f} hours")
    else:
        minutes = elapsed / 60
        print(f"\n  Total runtime: {minutes:.1f} minutes")


# ---------------------------------------------------------------------------
# Non-TCRMP mode: plain --input/--project, no registry, no copying
# ---------------------------------------------------------------------------


def prompt_inputs() -> tuple:
    """Interactive prompts for input_path and project_dir (non-TCRMP only)."""
    banner("3D Phase 1 - Setup + Frame Extraction + Initial 3D Processing")
    print()
    print("This tool will guide you through:")
    print("  1. Detecting and validating your input files")
    print("  2. Setting up the project directory structure")
    print("  3. Running frame extraction (if video input)")
    print("  4. Running initial 3D processing in Metashape")
    print()

    input_path = input("Input folder path (videos OR frame folders): ").strip().strip("'\"")
    if not input_path:
        print("Error: Input path required.")
        sys.exit(1)

    project_dir = input("Project directory path (will be created if needed): ").strip().strip("'\"")
    if not project_dir:
        print("Error: Project directory required.")
        sys.exit(1)

    return Path(input_path).expanduser().resolve(), Path(project_dir).expanduser().resolve()


def detect_input_type(input_path: Path) -> str:
    """'video' when input_path holds video files (ffprobe-verified via
    videos.is_video - extension is never consulted), 'frames' when it holds
    subdirectories of images."""
    if videos_mod.list_videos(str(input_path)):
        return "video"

    for subdir in sorted(input_path.iterdir()):
        if subdir.is_dir() and not subdir.name.startswith("."):
            images = [f for f in subdir.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS]
            if images:
                return "frames"

    raise ValueError(
        f"No video files or frame directories found in {input_path}\n"
        f"Frame directories need images with extensions: {', '.join(sorted(IMAGE_EXTENSIONS))}"
    )


def collect_input_ids(input_path: Path, input_type: str) -> dict:
    """Non-TCRMP ids = the original file name without extension (video, one
    id per file - no multi-part merging) or the subdirectory name as-is
    (frames). Returns {id: [source name(s)]}."""
    if input_type == "video":
        names = videos_mod.list_videos(str(input_path))
        return videos_mod.group_parts(names, tcrmp=False)
    return {
        d.name: [d.name]
        for d in sorted(input_path.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    }


def setup_project(project_dir: Path, input_path: Path, input_type: str, ids: dict) -> None:
    """Create the project workspace. Frame-folder input is symlinked
    (read-only) into frames/; video input is read in place from
    input_path - nothing is copied or linked into the project for video
    input, so step0 (Task 10) reads processing.video_input_dir from
    analysis_params.yaml to find the source videos.
    """
    banner("PROJECT SETUP")

    print("  Creating directory structure...")
    for sub in ("frames", "reports", "console"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    print("  Setting up Python environment...")
    create_venv(project_dir)

    params_dst = project_dir / "analysis_params.yaml"
    if not params_dst.exists():
        shutil.copy2(str(TEMPLATE_PARAMS), str(params_dst))
        print(f"  Copied analysis_params.yaml to {project_dir}")
    else:
        print("  analysis_params.yaml already exists, keeping existing.")
    set_params_path(params_dst, "processing.tcrmp", "false")

    if input_type == "frames":
        frames_dir = project_dir / "frames"
        print("  Linking frame directories into frames/ (read-only input)...")
        for name in ids:
            src = input_path / name
            dst = frames_dir / name
            if dst.exists():
                print(f"    {name} already exists, skipping.")
                continue
            print(f"    Linking {name}/...")
            os.symlink(str(src.resolve()), str(dst))
    else:
        set_params_path(params_dst, "processing.video_input_dir", json.dumps(str(input_path)))
        print(f"  Videos will be read in place from {input_path} (no copy, no link).")

    print("  Project setup complete.")


def run_non_tcrmp_mode(args, metashape_path: str) -> None:
    start_time = time.time()

    if args.input and args.project:
        input_path = args.input.expanduser().resolve()
        project_dir = args.project.expanduser().resolve()
    else:
        input_path, project_dir = prompt_inputs()

    if not input_path.exists() or not input_path.is_dir():
        print(f"Error: Input path does not exist or is not a directory: {input_path}")
        sys.exit(1)

    print("\nDetecting input type...")
    input_type = detect_input_type(input_path)
    print(f"  Detected: {input_type}")
    if input_type == "video":
        print("  Will run: Step 0 (frame extraction) -> Step 1 (3D processing)")
    else:
        print("  Will run: Step 1 (3D processing) [skipping Step 0]")
    print("  Videos/frames are read in place; nothing is copied or symlinked into the project"
          if input_type == "video" else
          "  Frame directories are symlinked into the project (read-only input).")

    ids = collect_input_ids(input_path, input_type)
    print(f"\n  Found {len(ids)} model(s):")
    for model_id in ids:
        print(f"    {model_id}")

    banner("PURPOSE (Commandment VI)")
    if args.purpose:
        purpose = args.purpose.strip()
        print(f"  Purpose (from --purpose): {purpose}")
    else:
        purpose = input("  Why are you running this? ").strip()
    if not purpose:
        purpose = "3D Phase 1 processing"

    vicarius_run_dir = None
    try:
        vicarius_run_dir = create_vicarius_run(purpose, "")
        print(f"\n  VICARIUS run created: {vicarius_run_dir.name}")
    except Exception as e:
        print(f"\n  Warning: Could not create VICARIUS run: {e}")
        print("  Continuing without VICARIUS run tracking.")

    start_event = None
    if VICARIUS_LOGGING:
        try:
            log = get_log()
            start_event = log.process_start(
                module=MODULE_NAME,
                purpose=purpose,
                inputs=[str(input_path)],
            )
        except Exception as e:
            print(f"  Warning: VICARIUS logging failed: {e}")

    banner("SUMMARY")
    print(f"  Input:       {input_path}")
    print(f"  Input type:  {input_type}")
    print(f"  Models:      {len(ids)}")
    print(f"  Project dir: {project_dir}")
    print(f"  Purpose:     {purpose}")
    print()
    if args.yes:
        print("  Proceeding (--yes).")
    else:
        confirm = input("  Proceed? (y/n): ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    try:
        setup_project(project_dir, input_path, input_type, ids)
        apply_param_overrides(project_dir, getattr(args, "param_pairs", None))
    except Exception as e:
        print(f"\nError during project setup: {e}")
        sys.exit(1)

    _link_output(vicarius_run_dir, "project", project_dir)

    for model_id, sources in ids.items():
        original_videos = ", ".join(sources) if input_type == "video" else ""
        status_rows.write_identity_row(project_dir, original_videos, model_id)

    if not args.skip_vim:
        open_params_for_editing(project_dir)

    try:
        if input_type == "video":
            run_step0(project_dir)

        _set_force_rerun(project_dir, bool(getattr(args, "force", False)))
        try:
            run_step1(project_dir, metashape_path)
        finally:
            _set_force_rerun(project_dir, False)

    except PipelinePaused as e:
        banner("PHASE 1 PAUSED")
        print()
        print(f"  {e}")
        print(
            f"  Re-run {MODULE_NAME} on the same project to resume "
            "(extracted / reconstructed models are skipped)."
        )
        print()
        if VICARIUS_LOGGING and start_event:
            try:
                log = get_log()
                log.process_end(
                    module=MODULE_NAME,
                    status="paused",
                    duration_sec=time.time() - start_time,
                    parent_event_id=start_event,
                    notes=str(e),
                )
            except Exception:
                pass
        sys.exit(PAUSE_EXIT_CODE)

    except RuntimeError as e:
        elapsed = time.time() - start_time
        print(f"\nERROR: {e}")

        if VICARIUS_LOGGING and start_event:
            try:
                log = get_log()
                log.process_end(
                    module=MODULE_NAME,
                    status="failed",
                    duration_sec=elapsed,
                    parent_event_id=start_event,
                    notes=str(e),
                )
            except Exception:
                pass

        sys.exit(1)

    print_manual_instructions(project_dir)

    elapsed = time.time() - start_time
    if VICARIUS_LOGGING and start_event:
        try:
            log = get_log()
            log.process_end(
                module=MODULE_NAME,
                status="success",
                duration_sec=elapsed,
                outputs=[str(project_dir)],
                parent_event_id=start_event,
                notes=f"Processed {len(ids)} model(s), input_type={input_type}",
            )
        except Exception:
            pass

    hours = elapsed / 3600
    if hours >= 1:
        print(f"\n  Total runtime: {hours:.1f} hours")
    else:
        minutes = elapsed / 60
        print(f"\n  Total runtime: {minutes:.1f} minutes")


def main():
    parser = argparse.ArgumentParser(
        description="3D Phase 1: Setup + Frame Extraction + Initial 3D Processing"
    )
    parser.add_argument(
        "--tcrmp", dest="tcrmp", action="store_true", default=True,
        help="Registry-driven mode (default): select TCRMP timepoints from the shared "
        "3D registry and process each next to its own video. No --input/--project needed.",
    )
    parser.add_argument(
        "--no-tcrmp", dest="tcrmp", action="store_false",
        help="Process a plain --input/--project pair instead (original names, no registry).",
    )
    parser.add_argument(
        "--ids", type=str, default=None,
        help="TCRMP mode: comma-separated readable ids to process, e.g. "
        "MRS_T1_2023ann,MRS_T1_2024_pbl. Unset selects by --site/--transect, or, if those "
        "are also unset, every pending timepoint.",
    )
    parser.add_argument("--site", type=str, default=None, help="TCRMP mode: registry site code to select, e.g. MRS.")
    parser.add_argument(
        "--transect", type=str, default=None, help="TCRMP mode: registry transect code to select, e.g. T1."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="TCRMP mode: reprocess timepoints whose step1_status is already complete.",
    )
    parser.add_argument(
        "--input", "-i", type=Path, default=None,
        help="Non-TCRMP mode: input folder path (videos OR frame folders).",
    )
    parser.add_argument(
        "--project", "-p", type=Path, default=None,
        help="Non-TCRMP mode: project directory path (created if needed). Ignored in TCRMP "
        "mode, where each timepoint's folder is derived from the registry.",
    )
    parser.add_argument(
        "--skip-vim",
        action="store_true",
        help="Skip opening analysis_params.yaml in vim",
    )
    parser.add_argument(
        "--purpose",
        type=str,
        default=None,
        help="Run purpose (Commandment VI). When set, the interactive purpose "
        "prompt is skipped - used by the VICARIUS UI which collects this in "
        "the form.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the SUMMARY confirmation prompt. Used by the VICARIUS UI "
        "to run non-interactively when all inputs come from the form.",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="DOTTED.PATH=VALUE",
        help="Set one key in the project's analysis_params.yaml before the steps "
        "read it, e.g. --param processing.frames_per_transect=300. Repeatable, and "
        "section aware: processing.step1_products.smooth_strength and "
        "processing.metashape.defaults.smooth_strength are different keys. This is "
        "how the VICARIUS UI form reaches a TCRMP run, whose processing folder does "
        "not exist yet at launch.",
    )
    args = parser.parse_args()
    try:
        args.param_pairs = parse_param_pairs(args.param)
    except ValueError as exc:
        parser.error(str(exc))

    print("\nDetecting Metashape installation...")
    metashape_path = detect_metashape()
    print(f"  Found: {metashape_path}")

    if args.tcrmp:
        run_tcrmp_mode(args, metashape_path)
    else:
        run_non_tcrmp_mode(args, metashape_path)


if __name__ == "__main__":
    main()
