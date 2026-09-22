#!/usr/bin/env python3
"""
run_phase1.py - Interactive CLI runner for 3D Phase 1 processing.

Two modes:

  TCRMP (default, --tcrmp): rows come from the shared TCRMP 3D registry
  (vicarius/_METADATA/3d), selected by --ids / --site+--transect / or, with
  none of those given, every pending timepoint. Each timepoint's processing
  folder lives as a SIBLING of its video's folder (never inside it; the
  video is never copied, never symlinked); all timepoints of the same
  site+transect share one folder and one growing psx, so the folder is
  only created once (beside the video folder of whichever timepoint is
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
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

import gpu_claim
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

# The manual edit checklist is one file with two readers: the manual edit
# module's checklist popout and this runner's console print. The reader
# (manualedit.checklist) lives in the manual edit module's clone.
MANUAL_EDIT_REPO = Path(VICARIUS_ROOT) / "modules" / "manual_edit" / "github_repo"
CHECKLIST_PATH = GITHUB_REPO_DIR / "manual_edit_checklist.yaml"

# Run identity flags (--run-id, --params-version, --params-file), validated
# at entry so a bad value is refused by the parser and never reaches a
# processing folder. The run id grammar matches the Carousel's (letters,
# digits, _ + -) plus the dot the voyagerparams custom set names allow; a
# params version is a voyagerparams ref such as v1.2.0, branches/<slug>/v1.2.0
# or custom/<run_id>, so it also allows a slash.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
RUN_ID_MAX_CHARS = 128
PARAMS_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./+-]*")
PARAMS_VERSION_MAX_CHARS = 128
PARAMS_FILE_MAX_BYTES = 16 * 1024 * 1024

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


def _interactive() -> bool:
    """True only when this process can safely block on a console prompt: an
    interactive terminal is attached to stdin. False under the VICARIUS UI
    launcher and any other non-terminal invocation (piped stdin, a cron job,
    a subprocess with stdin redirected from /dev/null), where input() would
    raise EOFError instead of waiting. Every input() call in this module is
    gated on this (directly, or via --yes) so no code path launched from the
    UI ever waits on a prompt.
    """
    try:
        return sys.stdin.isatty()
    except Exception:  # silent-ok: stdin.isatty() on a closed or exotic stdin; False is the answer and logging is not up yet
        return False


DEFAULT_PURPOSE = "3D Phase 1 processing"


def resolve_purpose(args, default: str = DEFAULT_PURPOSE) -> str:
    """Resolve the run purpose (Commandment VI) without ever calling input()
    when this process cannot safely wait on one.

    --purpose (the VICARIUS UI form field) always wins. Otherwise, under
    --yes or a non-interactive stdin, fall back to `default` instead of
    prompting - a launch with neither a purpose nor a keyboard behind it must
    still complete, not die with EOFError. Only prompts when a human is
    actually at the keyboard and did not pass --yes.
    """
    if getattr(args, "purpose", None):
        purpose = args.purpose.strip()
        print(f"  Purpose (from --purpose): {purpose}")
        return purpose or default

    if getattr(args, "yes", False) or not _interactive():
        print(f"  Purpose (default, no prompt under --yes/non-interactive stdin): {default}")
        return default

    purpose = input("  Why are you running this? ").strip()
    return purpose or default


def _confirm_proceed(args) -> None:
    """Gate the SUMMARY confirmation the same way every prompt in this module
    is gated: --yes proceeds outright; with neither --yes nor an interactive
    stdin, abort with a clear message instead of calling input() and dying
    with EOFError. Only prompts when a human is actually at the keyboard.
    """
    if args.yes:
        print("  Proceeding (--yes).")
        return
    if not _interactive():
        print(
            "  Aborted: no --yes and stdin is not a terminal, so the SUMMARY "
            "confirmation cannot be prompted. Pass --yes to run hands-free."
        )
        sys.exit(1)
    confirm = input("  Proceed? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)


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


# ---------------------------------------------------------------------------
# Run identity flags: --run-id, --params-version, --params-file
# ---------------------------------------------------------------------------


def _validate_token(value, flag: str, pattern, max_chars: int, allowed: str):
    """Shared check for --run-id and --params-version: None stays None; a
    string must be non-blank, at most `max_chars` long, and match `pattern`
    in full, surrounding whitespace included (a trailing newline is a sign
    of a malformed launch, not something to trim). `allowed` names the
    punctuation the pattern accepts, for the error message. Raises TypeError
    for a non-string and ValueError (naming the flag) for anything else."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{flag} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{flag} is blank")
    if len(value) > max_chars:
        raise ValueError(f"{flag} is longer than {max_chars} characters")
    if not pattern.fullmatch(value):
        raise ValueError(
            f"{flag} must start with a letter or digit and hold only letters, "
            f"digits and {allowed}; got {value!r}"
        )
    return value


def validate_run_id(value):
    """The run id written as a voyager1 fact (for example
    TCRMP_3sep26_LO_MRS3_23ann-25pbl), or None when the flag was not given.
    Raises ValueError naming --run-id for a blank, over-long or ill-formed id."""
    return _validate_token(value, "--run-id", RUN_ID_PATTERN, RUN_ID_MAX_CHARS,
                           "the characters _ . + -")


def validate_params_version(value):
    """The voyagerparams version ref written as a voyager1 fact (for example
    v1.2.0 or branches/coral/v1.2.0), or None when the flag was not given.
    Raises ValueError naming --params-version for a blank, over-long or
    ill-formed ref."""
    return _validate_token(value, "--params-version", PARAMS_VERSION_PATTERN,
                           PARAMS_VERSION_MAX_CHARS, "the characters _ . / + -")


def _read_params_file_text(path: Path) -> str:
    """The text of a candidate parameter file, refused (ValueError naming
    --params-file) when it is missing, a directory, empty, over the size cap,
    unreadable, or not UTF-8."""
    if not path.exists():
        raise ValueError(f"--params-file {path} does not exist")
    if path.is_dir():
        raise ValueError(f"--params-file {path} is a directory, not a YAML file")
    if not path.is_file():
        raise ValueError(f"--params-file {path} is not a regular file")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"--params-file {path} is empty")
    if size > PARAMS_FILE_MAX_BYTES:
        raise ValueError(
            f"--params-file {path} is larger than {PARAMS_FILE_MAX_BYTES} bytes "
            f"({size} bytes); a parameter file is a few kilobytes"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"--params-file {path} is not UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"--params-file {path} cannot be read: {exc}") from exc


def validate_params_file(value):
    """The absolute path of a parameter file that can seed a new processing
    folder's analysis_params.yaml, or None when the flag was not given.

    The file must exist, be a regular UTF-8 file under PARAMS_FILE_MAX_BYTES,
    parse as YAML, and hold a top-level mapping whose `processing` key is a
    mapping (the block config.py reads). Raises TypeError for a value that
    is not a path and ValueError naming --params-file for everything else.
    """
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"--params-file must be a path, got {type(value).__name__}")
    path = Path(os.path.abspath(Path(value).expanduser()))
    text = _read_params_file_text(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"--params-file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"--params-file {path} must hold a mapping with a processing key at the top level"
        )
    if not isinstance(data.get("processing"), dict):
        raise ValueError(
            f"--params-file {path} has no processing mapping "
            "(the block analysis_params.yaml keeps every reconstruction setting in)"
        )
    return path


def seed_analysis_params(project_dir: Path, params_file=None):
    """Create <project_dir>/analysis_params.yaml when it is missing, from
    `params_file` (a validated --params-file) or else the module template.

    Returns the source path that was copied, or None when the folder already
    had the file (nothing is written then: a folder's parameters belong to
    the folder once it exists, and the caller prints that the seed was
    ignored). Raises RuntimeError naming both paths when the copy fails.
    """
    params_dst = Path(project_dir) / "analysis_params.yaml"
    if params_dst.exists():
        return None
    source = Path(params_file) if params_file is not None else TEMPLATE_PARAMS
    try:
        shutil.copy2(str(source), str(params_dst))
    except OSError as exc:
        raise RuntimeError(
            f"cannot seed {params_dst} from {source}: {exc}"
        ) from exc
    return source


def seed_folder_params(project_dir: Path, params_file=None, indent: str = "    ") -> bool:
    """seed_analysis_params plus the console line that says what happened:
    where the new file came from, or that an existing file kept its place
    and the --params-file was ignored for this folder. Returns True when the
    file was created. Silent when the folder already had the file and no
    seed was offered (the caller may say so in its own words)."""
    seeded_from = seed_analysis_params(project_dir, params_file)
    if seeded_from is not None:
        origin = f" from {seeded_from}" if params_file is not None else ""
        print(f"{indent}Copied analysis_params.yaml to {project_dir}{origin}")
        return True
    if params_file is not None:
        print(
            f"{indent}analysis_params.yaml already exists in {project_dir}; "
            f"--params-file {params_file} ignored for this folder"
        )
    return False


def record_run_facts(readable_id: str, run_id=None, params_version=None):
    """Write the run identity (voyager1 facts run_id and params_version) for
    one registry row, so the atlas can name the run and link its parameter
    version. Only the values given are written; with neither, nothing is
    written. Returns what registry_client.facts returns (None outside TCRMP
    mode)."""
    facts = {}
    if run_id:
        facts["run_id"] = run_id
    if params_version:
        facts["params_version"] = params_version
    if not facts:
        return None
    return registry_client.facts(readable_id, registry_client.VOYAGER1_SECTION, facts)


# Longest note this driver writes into the registry's operator-owned notes
# column (matches step0/step1's cap).
NOTE_MAX_CHARS = 500


def registry_note(readable_id: str, text: str) -> None:
    """Append a note to the registry row without discarding what the operator
    wrote there (notes is an operator-owned column). Idempotent: a note
    already present is not repeated, and the column is capped so repeated
    reruns cannot grow it without bound. No-op outside TCRMP mode. Same
    contract as step0/step1's registry_note.
    """
    if not registry_client.enabled():
        return
    existing = (registry_client.row(readable_id) or {}).get("notes", "") or ""
    if text in existing:
        return
    combined = f"{existing}; {text}" if existing else text
    if len(combined) > NOTE_MAX_CHARS:
        combined = combined[-NOTE_MAX_CHARS:]
    registry_client.update(readable_id, notes=combined)


def processing_lock_held(processing_location) -> bool:
    """True when some process currently holds the flock on
    <processing_location>/.processing.lock, i.e. a step is really running in
    that folder right now.

    Probes without ever creating the lock file and without blocking. A
    missing location or missing lock file means nothing is running there. A
    hard-killed run leaves the file behind with the flock already released
    by the OS, so the file merely existing never counts as held: only a
    currently held flock does (stage active + lock held = RUNNING; stage
    active + lock free = INTERRUPTED).
    """
    if not processing_location:
        return False
    lock_path = os.path.join(str(processing_location), ".processing.lock")
    try:
        fp = open(lock_path, "r")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fp.close()


def acquire_processing_lock(project_dir):
    """Hold <project_dir>/.processing.lock for the prepare-and-extract window.

    step1.py takes this same exclusive flock for the Metashape build, but
    nothing held it during folder preparation and frame extraction, so for
    that whole window (minutes to over an hour) an actively running
    timepoint read as interrupted: the atlas would have allowed edits and a
    concurrent driver would not have skipped it. This closes that window:
    the driver holds the lock from right after the folder exists until just
    before step 1 launches (step 1, a separate process, takes its own).

    Non-blocking, with two quick retries so a liveness probe touching the
    lock at the same moment can never fail the acquisition. Returns the open
    file object (closing it releases the lock), or None when another process
    genuinely holds it. Mirrors step1.acquire_project_lock's stamp so a
    blocked run can name the holder.
    """
    lock_path = os.path.join(str(project_dir), ".processing.lock")
    lock_fp = open(lock_path, "a")
    for attempt in range(3):
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if attempt == 2:
                lock_fp.close()
                return None
            time.sleep(0.2)
    lock_fp.truncate(0)
    lock_fp.write(
        f"pid={os.getpid()} step=prepare+step0 "
        f"host={os.uname().nodename} "
        f"started={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    lock_fp.flush()
    return lock_fp


def release_processing_lock(lock_fp) -> None:
    """Release a lock from acquire_processing_lock (safe on None)."""
    if lock_fp is None:
        return
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    lock_fp.close()


# ---------------------------------------------------------------------------
# Disk-space failsafe
# ---------------------------------------------------------------------------
# A third-party sync driver is expected to manage space as the primary
# mechanism; this is only the last line of defense so a step never starts on
# a drive about to fill (a full drive mid-project usually forces restarting
# the whole project). One statvfs call per check point, no polling, no
# waiting, and a skipped timepoint never stalls the rest of the run.

DEFAULT_MIN_FREE_DISK_GB = 200


def read_min_free_disk_gb(project_dir: Path) -> float:
    """The processing.min_free_disk_gb threshold for this project, read from
    its analysis_params.yaml. Default 200 when the file or the key is missing
    or unreadable; 0 disables the disk failsafe."""
    params_path = Path(project_dir) / "analysis_params.yaml"
    try:
        params = yaml.safe_load(params_path.read_text()) or {}
        value = (params.get("processing") or {}).get(
            "min_free_disk_gb", DEFAULT_MIN_FREE_DISK_GB
        )
        return float(value)
    except Exception as exc:
        # Say which floor is actually in force. A params file that cannot be
        # read silently reverted to the default before, so a run configured to
        # stop at 500 GB would happily fill the disk to the built-in figure
        # instead. Same shape as the 2026-09-06 registry failure: a caught
        # error became a wrong answer with nobody told.
        logging.warning("The disk floor could not be read from %s (%s: %s), so this run uses the "
                        "built-in default of %s GB.", params_path, type(exc).__name__, exc,
                        DEFAULT_MIN_FREE_DISK_GB)
        return float(DEFAULT_MIN_FREE_DISK_GB)


def check_free_disk(project_dir: Path):
    """Disk failsafe: a single os.statvfs call on the processing folder.

    Returns None when there is enough free space (or the check is disabled
    with processing.min_free_disk_gb 0), else the message describing the
    shortfall. The caller decides what to do with the message (TCRMP mode
    records it in the registry and skips the row; plain mode stops the run).
    """
    min_gb = read_min_free_disk_gb(project_dir)
    if min_gb <= 0:
        return None
    try:
        st = os.statvfs(str(project_dir))
    except OSError as exc:
        # An unreachable folder (unmounted drive, permissions) is exactly the
        # situation the failsafe exists for: refuse to start rather than
        # write into the void.
        return f"cannot check free disk space for {project_dir}: {exc}"
    free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
    if free_gb >= min_gb:
        return None
    return (
        f"not enough free disk space for {project_dir}: {free_gb:.1f} GB "
        f"free, at least {min_gb:g} GB required (processing.min_free_disk_gb)"
    )


def _reconcile_stage_after_step1(readable_id: str) -> None:
    """Never leave a finished row wearing an active stage word.

    Step 1 normally ends a row at stage "done" (success) or "failed". But a
    run can end with nothing for step 1 to do (for example a forced rerun of
    a timepoint whose frames subfolder no longer exists): the registry then
    keeps prepare's "starting" while step1_status stays "complete", and
    because the lock is now free the atlas would badge the row interrupted
    forever. When step 1 exits cleanly and the row reads complete but its
    stage is still an active word, settle the stage to "done"."""
    try:
        row = registry_client.row(readable_id) or {}
    except Exception as exc:
        # An unreachable registry here left the row badged interrupted forever
        # with no word anywhere. Report it: on 2026-09-06 exactly this silence,
        # one layer down, cost an evening.
        logging.warning("The stage of %s could not be settled because the registry could not be "
                        "read (%s: %s); the row may read interrupted until the next run.",
                        readable_id, type(exc).__name__, exc)
        return
    stage = (row.get("stage") or "").strip()
    if stage not in ("", "done", "failed") and row.get("step1_status") == "complete":
        registry_client.stage(readable_id, 1, "done")


def _skip_for_low_disk(readable_id: str, message: str) -> None:
    """Console + registry record for a TCRMP timepoint the disk failsafe
    skipped: the row is marked failed with the reason in its notes, and the
    run moves on to the remaining timepoints."""
    print(f"  {message}")
    print(
        f"  Skipping {readable_id} and continuing with the remaining "
        "timepoints. Free space on the processing drive (or lower "
        "processing.min_free_disk_gb) and run again."
    )
    registry_note(readable_id, message)
    registry_client.stage(readable_id, 1, "failed")
    # set_stage writes only step/stage/stage_started; without this, a
    # previously interrupted row would keep step1_status "running" and the
    # next run would wrongly try to resume a row that failed on disk space.
    registry_client.update(readable_id, step1_status="failed")


def detect_metashape(assume_yes: bool = False) -> str:
    """Find the Metashape executable. Returns path or raises RuntimeError.

    `assume_yes` is the run's --yes flag: paired with a non-interactive
    stdin check, it keeps step 4 (prompt for the path by hand) from ever
    running under --yes or off a terminal, where input() would raise
    EOFError instead of waiting - a clear RuntimeError takes its place.
    """
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

    # 4. Prompt user - only when a human is actually at the keyboard.
    if assume_yes or not _interactive():
        raise RuntimeError(
            "Metashape Pro not found. Install it, set $METASHAPE_PATH, or add it "
            "to $PATH. Cannot prompt for its path under --yes or a "
            "non-interactive stdin."
        )
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
    """Open analysis_params.yaml in vim with instructions.

    Callers already skip this entirely under --skip-vim (the flag the UI
    always sends). This is the second line of defense: a bare-shell call
    that forgot --skip-vim off a non-interactive stdin gets the file path
    printed and moves on instead of dying with EOFError on the "Press
    Enter" prompt.
    """
    params_file = project_dir / "analysis_params.yaml"

    if not _interactive():
        print("  Stdin is not a terminal; skipping the interactive vim edit step.")
        print(f"  Edit {params_file} directly if you need to change parameters.")
        return

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


def _checklist_console(project_dir: Path) -> str:
    """The manual edit checklist rendered for the console by the manual edit
    module's reader (manualedit.checklist in MANUAL_EDIT_REPO) from
    CHECKLIST_PATH, the one file both readers share. Raises ImportError when
    the reader is not installed, FileNotFoundError when the YAML is missing,
    and ValueError (ChecklistError) when it is malformed."""
    repo = str(MANUAL_EDIT_REPO)
    if repo not in sys.path:
        sys.path.append(repo)
    from manualedit import checklist

    return checklist.render_console(project_dir, path=CHECKLIST_PATH)


def print_manual_instructions(project_dir: Path) -> None:
    """Print the manual edit checklist for the Metashape GUI step.

    The text comes from manual_edit_checklist.yaml through the manual edit
    module's reader, so the console and the checklist popout can never
    drift apart. When that reader or the file is unavailable, a note says
    so and the built-in copy of the same text is printed instead.
    """
    try:
        text = _checklist_console(project_dir)
    except (ImportError, OSError, ValueError) as exc:
        reason = " ".join(str(exc).split())
        print(
            f"  Note: the shared manual edit checklist could not be read "
            f"({reason}); printing the built-in copy."
        )
        _print_builtin_manual_instructions(project_dir)
        return
    print(text, end="")


def _print_builtin_manual_instructions(project_dir: Path) -> None:
    """The checklist text as this runner printed it before the shared YAML
    existed; the fallback when the shared reader is unavailable."""
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
    except Exception:  # silent-ok: a convenience symlink into the outputs folder; its absence changes nothing
        pass  # Non-critical


# ---------------------------------------------------------------------------
# TCRMP mode: registry-driven, processing folder beside the video's folder
# ---------------------------------------------------------------------------


def _warn_run_not_logged(outcome, exc):
    """Say that the VICARIUS processing log could not record how this run ended.

    Parameters:
        outcome: the run's own outcome, "paused", "failed" or "success".
        exc: whatever the log write raised.

    The run is over either way, so a log write failing never changes the exit
    code. It must not be silent either: a run missing from the processing log
    is indistinguishable from a run that never happened, and this module's
    whole audit trail is that log.
    """
    logging.warning(
        "This run ended %s, but the VICARIUS processing log could not record it "
        "(%s: %s). The run itself is unaffected; its row in the log is missing.",
        outcome, type(exc).__name__, exc)


def prepare_tcrmp_folder(row: dict, run_id=None, params_version=None, params_file=None) -> Path:
    """Ensure the processing folder for a TCRMP registry row exists as a
    SIBLING of its video's folder, has a ready .venv, and has its location
    recorded back into the registry - all before step 0 runs. Returns the
    project directory.

    `run_id` and `params_version` (the validated --run-id and
    --params-version) are written as voyager1 facts for the row. `params_file`
    (the validated --params-file) seeds a folder created here in place of
    the module template; a folder that already has analysis_params.yaml
    keeps it and a printed note says the file was ignored.

    Placement rule: the folder is created beside the folder that holds the
    video (video_location's parent), never inside it, so video folders and
    processing folders stay cleanly separated. When a transect's season
    folders (2023_annual/, 2024_pbl/, ...) share one parent, every
    timepoint resolves to the same spot beside them.

    All timepoints of the same site+transect share one processing folder
    and one growing psx: if any row of that site/transect already has a
    processing_location, this reuses it (even if THIS row's video sits
    under a different parent); a new folder is only created, beside THIS
    row's video folder, when no row of that site/transect has one yet -
    named {SITE}_{T#}_3d, which carries no season and so does not depend
    on which timepoint is processed first. venv setup
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
        folder_name = _naming3d().processing_folder_name(site, transect)
        # abspath, not resolve(): normalize without following symlinks, so
        # the folder is placed beside the video folder as the operator sees
        # it (consistent with step0's abspath comparisons).
        video_dir = Path(os.path.abspath(video_location))
        if video_dir.parent == video_dir:
            raise RuntimeError(
                f"{readable_id}: video_location {video_location!r} has no parent "
                "directory to place the processing folder beside it"
            )
        project_dir = video_dir.parent / folder_name

    try:
        for sub in ("console", "frames", "reports"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(
            f"{readable_id}: cannot create processing folder {project_dir} "
            f"(is the directory above the video folder writable?): {exc}"
        ) from exc

    params_dst = project_dir / "analysis_params.yaml"
    seed_folder_params(project_dir, params_file)
    set_params_path(params_dst, "processing.tcrmp", "true")

    # protect_operator keeps any value a person or the sync driver already
    # put in an operator cell (processing_location is one): the module fills
    # blanks and otherwise leaves the operator's word alone, which is the
    # contract STEP2_HANDOFF holds step 2 to as well.
    registry_client.update(
        readable_id,
        protect_operator=True,
        processing_folder=project_dir.name,
        processing_location=str(project_dir),
        console_log=str(project_dir / "console"),
    )
    registry_client.stage(readable_id, 1, "starting")
    record_run_facts(readable_id, run_id, params_version)

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
    purpose = resolve_purpose(args)

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
    _confirm_proceed(args)

    opened_params_for = set()
    processed_dirs = []
    disk_failed = []
    try:
        for row in rows:
            readable_id = row["readable_id"]
            banner(f"TIMEPOINT: {readable_id}")

            pending_resume_note = None
            if row.get("step1_status") == "running":
                # The registry thinks this row is mid-run. The flock on the
                # processing folder tells the truth: held means another
                # process really is working there (skip, never queue behind
                # it); free means the previous run was hard-killed and this
                # run resumes it from the latest stopping point.
                if processing_lock_held(row.get("processing_location")):
                    print(
                        f"  Skipping {readable_id}: another process is working "
                        "on this timepoint right now (its processing folder's "
                        "lock is held). This run will not wait for it; run "
                        "again later if the timepoint still needs processing."
                    )
                    continue
                stage = row.get("stage") or "unknown stage"
                stage_started = row.get("stage_started") or "unknown time"
                print(
                    f"  {readable_id}: the previous run was interrupted "
                    f"(stopped during {stage}, {stage_started}) and its lock "
                    "is free. Resuming; finished work is kept and processing "
                    "continues from the latest stopping point."
                )
                # Recorded only once processing actually starts (after the
                # disk check), so the registry never says "resumed" about a
                # run that was skipped before doing anything.
                pending_resume_note = (
                    f"resumed after interrupted run (stopped during {stage}, {stage_started})"
                )

            project_dir = prepare_tcrmp_folder(
                row,
                run_id=getattr(args, "run_id", None),
                params_version=getattr(args, "params_version", None),
                params_file=getattr(args, "params_file", None),
            )

            # Hold the processing lock from here until step 1 launches, so
            # the whole prepare-and-extract window reads as RUNNING to the
            # atlas and to any concurrent driver (step 1 takes its own lock
            # in its own process).
            proc_lock = acquire_processing_lock(project_dir)
            if proc_lock is None:
                print(
                    f"  Skipping {readable_id}: another process is working "
                    "on this timepoint right now (its processing folder's "
                    "lock is held)."
                )
                continue
            try:
                apply_param_overrides(project_dir, getattr(args, "param_pairs", None))
                print(f"  Processing folder: {project_dir}")
                _link_output(vicarius_run_dir, readable_id, project_dir)

                if not getattr(args, "force", False):
                    # A hard-killed --force run never reaches the finally that
                    # clears processing.force_rerun, so a stale true could make
                    # this normal run rebuild a manually edited chunk. Pin it
                    # false at the start of every non-force timepoint.
                    _set_force_rerun(project_dir, False)

                if not args.skip_vim and project_dir not in opened_params_for:
                    open_params_for_editing(project_dir)
                opened_params_for.add(project_dir)

                free_message = check_free_disk(project_dir)
                if free_message:
                    _skip_for_low_disk(readable_id, free_message)
                    disk_failed.append(readable_id)
                    continue

                if pending_resume_note:
                    registry_note(readable_id, pending_resume_note)

                run_step0(project_dir)
                if getattr(args, "force", False):
                    # select_rows let this row through on step1_status alone;
                    # step1.py skips on its own status.csv cell, so clear that
                    # too or the forced rerun would do nothing.
                    status_rows.reset_step1(project_dir, readable_id)
                    print(f"  --force: cleared the step 1 verdict for {readable_id} in status.csv")

                free_message = check_free_disk(project_dir)
                if free_message:
                    _skip_for_low_disk(readable_id, free_message)
                    disk_failed.append(readable_id)
                    continue

                _set_force_rerun(project_dir, bool(getattr(args, "force", False)))
            finally:
                release_processing_lock(proc_lock)

            try:
                run_step1(project_dir, metashape_path)
            finally:
                _set_force_rerun(project_dir, False)
            _reconcile_stage_after_step1(readable_id)
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
            except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
                _warn_run_not_logged("paused", exc)
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
            except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
                _warn_run_not_logged("failed", exc)

        sys.exit(1)

    elapsed = time.time() - start_time
    if VICARIUS_LOGGING and start_event:
        try:
            log = get_log()
            run_notes = f"Processed {len(processed_dirs)} TCRMP timepoint(s)"
            if disk_failed:
                run_notes += (
                    f"; skipped {len(disk_failed)} for low disk space: "
                    + ", ".join(disk_failed)
                )
            log.process_end(
                module=MODULE_NAME,
                status="failed" if disk_failed else "success",
                duration_sec=elapsed,
                outputs=[str(p) for p in processed_dirs],
                parent_event_id=start_event,
                notes=run_notes,
            )
        except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
            _warn_run_not_logged("failed" if disk_failed else "success", exc)

    hours = elapsed / 3600
    if hours >= 1:
        print(f"\n  Total runtime: {hours:.1f} hours")
    else:
        minutes = elapsed / 60
        print(f"\n  Total runtime: {minutes:.1f} minutes")

    if disk_failed:
        print(
            f"\n  {len(disk_failed)} timepoint(s) were skipped because the "
            f"processing drive is low on space: {', '.join(disk_failed)}. "
            "Free disk space (or lower processing.min_free_disk_gb) and run "
            "again to process them."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Non-TCRMP mode: plain --input/--project, no registry, no copying
# ---------------------------------------------------------------------------


def prompt_inputs(assume_yes: bool = False) -> tuple:
    """Interactive prompts for input_path and project_dir (non-TCRMP only).

    Only reached when --input/--project were not both given. `assume_yes` is
    the run's --yes flag: paired with a non-interactive stdin check, it
    turns what would otherwise be an EOFError into a clear error explaining
    the missing flags, so a misconfigured hands-free launch fails fast
    instead of hanging.
    """
    if assume_yes or not _interactive():
        raise RuntimeError(
            "Non-TCRMP mode needs both --input and --project; neither can be "
            "prompted for under --yes or a non-interactive stdin."
        )

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


def setup_project(project_dir: Path, input_path: Path, input_type: str, ids: dict,
                  params_file=None) -> None:
    """Create the project workspace. Frame-folder input is symlinked
    (read-only) into frames/; video input is read in place from
    input_path - nothing is copied or linked into the project for video
    input, so step0 (Task 10) reads processing.video_input_dir from
    analysis_params.yaml to find the source videos. `params_file` (the
    validated --params-file) seeds a new project's analysis_params.yaml in
    place of the module template; an existing file is kept as before.
    """
    banner("PROJECT SETUP")

    print("  Creating directory structure...")
    for sub in ("frames", "reports", "console"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    print("  Setting up Python environment...")
    create_venv(project_dir)

    params_dst = project_dir / "analysis_params.yaml"
    if not seed_folder_params(project_dir, params_file, indent="  ") and params_file is None:
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
        input_path, project_dir = prompt_inputs(args.yes)

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
    purpose = resolve_purpose(args)

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
    _confirm_proceed(args)

    try:
        setup_project(project_dir, input_path, input_type, ids,
                      params_file=getattr(args, "params_file", None))
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
        if not getattr(args, "force", False):
            # A hard-killed --force run never reaches the finally that clears
            # processing.force_rerun, so a stale true could make this normal
            # run rebuild a manually edited chunk. Pin it false at the start.
            _set_force_rerun(project_dir, False)

        # Same lock window as the TCRMP loop: held through extraction,
        # released before step 1 (which takes its own in its own process).
        proc_lock = acquire_processing_lock(project_dir)
        if proc_lock is None:
            raise RuntimeError(
                "Another process is working in this project folder right now "
                "(its .processing.lock is held). Run again once it finishes."
            )
        try:
            if input_type == "video":
                free_message = check_free_disk(project_dir)
                if free_message:
                    raise RuntimeError(
                        f"{free_message}. Free disk space (or lower "
                        "processing.min_free_disk_gb) and run again."
                    )
                run_step0(project_dir)

            free_message = check_free_disk(project_dir)
            if free_message:
                raise RuntimeError(
                    f"{free_message}. Free disk space (or lower "
                    "processing.min_free_disk_gb) and run again."
                )
        finally:
            release_processing_lock(proc_lock)

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
            except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
                _warn_run_not_logged("paused", exc)
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
            except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
                _warn_run_not_logged("failed", exc)

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
        except Exception as exc:  # silent-ok: best-effort processing-log write while already exiting; the real reason is reported by the raiser
            _warn_run_not_logged("success", exc)

    hours = elapsed / 3600
    if hours >= 1:
        print(f"\n  Total runtime: {hours:.1f} hours")
    else:
        minutes = elapsed / 60
        print(f"\n  Total runtime: {minutes:.1f} minutes")


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser: every flag the VICARIUS runner's flag_map
    emits, plus the run identity flags the Voyager 1 setup page sends."""
    parser = argparse.ArgumentParser(
        description="3D Phase 1: Setup + Frame Extraction + Initial 3D Processing"
    )
    parser.add_argument(
        "--tcrmp", dest="tcrmp", action="store_true", default=True,
        help="Registry-driven mode (default): select TCRMP timepoints from the shared "
        "3D registry and process each in a folder beside its video's folder. No --input/--project needed.",
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
    parser.add_argument(
        "--run-id", dest="run_id", type=str, default=None,
        help="TCRMP mode: the run id the Voyager 1 setup page minted (for example "
        "TCRMP_3sep26_LO_MRS3_23ann-25pbl), recorded as a voyager1 fact on every "
        "selected registry row so the atlas can name the run.",
    )
    parser.add_argument(
        "--params-version", dest="params_version", type=str, default=None,
        help="TCRMP mode: the voyagerparams version the parameters came from (for "
        "example v1.2.0 or custom/<run id>), recorded as a voyager1 fact beside the run id.",
    )
    parser.add_argument(
        "--params-file", dest="params_file", type=str, default=None,
        metavar="PATH",
        help="Seed a NEW processing folder's analysis_params.yaml from this YAML file "
        "instead of the module template. The file must exist and hold a processing "
        "mapping. A folder that already has analysis_params.yaml keeps it and the "
        "file is ignored with a printed note; --param overrides apply either way.",
    )
    return parser


def finalize_args(parser: argparse.ArgumentParser, args) -> None:
    """Parse the --param pairs and validate the run identity flags in place,
    turning any ValueError into a parser error (exit 2) before any work."""
    try:
        args.param_pairs = parse_param_pairs(args.param)
        args.run_id = validate_run_id(args.run_id)
        args.params_version = validate_params_version(args.params_version)
        args.params_file = validate_params_file(args.params_file)
    except ValueError as exc:
        parser.error(str(exc))


def _check_gpu_right_of_way() -> None:
    """Ask VICARIUS whether VOYAGER may touch a card right now, and stop if not.

    Parameters: none.
    Returns:
        None. Returns silently on a "granted" or "unregistered" decision:
        port 5090 being unreachable is fail-open by design (gpu_claim.py
        has already warned once on stderr for that case), and the run
        proceeds unregistered, showing up as an unclaimed holder in the
        GPU monitor once the desktop UI comes back.

    Called once, right after the command line is parsed and validated and
    before Metashape is ever touched (spec docs/superpowers/specs/2026-09-
    17-gpu-right-of-way-design.md, "Gates"), so a launch from a terminal
    that bypasses the desktop UI's runner still refuses to collide with
    another model-tier run. This is advisory only and claims nothing:
    vicarius_ui_os/runner.py's start_job() already made the binding claim,
    with this process's own pid, before spawning it; a second claim here
    would be a second writer of state this process does not own (design
    semantics item 6, "one writer").

    Raises:
        SystemExit: the gate refuses the start (decision "refused" or
            "needs_pin"), after printing the gate's own message to stderr
            and exiting with status 1.
    """
    # The helper is contracted never to raise, and a bug that broke that
    # contract must still never be the reason a multi-day reconstruction
    # dies. The whole point of this system is protecting long runs, so it
    # fails open on its own failure: warn on stderr, and let the science
    # proceed. Found 2026-09-17 by the VOYAGER 2 gate test, which had no
    # VOYAGER 1 equivalent until the same day.
    try:
        result = gpu_claim.check(module="3D_phase_1", by="LO")
    except Exception as exc:  # noqa: BLE001 - see the comment above
        print(
            "gpu_claim: the GPU right-of-way check itself failed "
            "(" + str(exc) + "); proceeding unregistered",
            file=sys.stderr,
        )
        return
    if result.get("decision") in ("refused", "needs_pin"):
        print(
            result.get("message")
            or "GPU right of way: VOYAGER may not start right now.",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    """Parse and validate the command line, find Metashape, then run the
    TCRMP (registry) mode or the plain --input/--project mode."""
    parser = build_parser()
    args = parser.parse_args()
    finalize_args(parser, args)

    _check_gpu_right_of_way()

    print("\nDetecting Metashape installation...")
    metashape_path = detect_metashape(args.yes)
    print(f"  Found: {metashape_path}")

    if args.tcrmp:
        run_tcrmp_mode(args, metashape_path)
    else:
        run_non_tcrmp_mode(args, metashape_path)


if __name__ == "__main__":
    main()
