#!/usr/bin/env python3
"""
Step 0: Frame Extraction

Extracts frames from the source video with FFmpeg (16-bit TIFF, rgb48le),
reading each video from its own local location - never copied, never
symlinked. Containers/codecs are always detected by probing with ffprobe
(src/videos.py), never by file extension.

TCRMP mode (processing.tcrmp: true): the timepoints to extract are the
registry rows whose processing_location is this project folder (a
site/transect's shared processing folder can hold several timepoints, up to
max_chunks_per_psx) - queried fresh from the registry each run, so a second
run_phase1.py pass over the same folder picks up any newly-added sibling
timepoint. Each row's video path is `video_location/original_videos`; a row
still holding a ";"-joined multi-part list is refused (_tcrmp_work_items); a
lone part-numbered file is the whole recording (Lauren, 2026-09-11), noted.

Non-TCRMP mode: videos come from processing.video_input_dir (read in place;
run_phase1.py never copies or symlinks video input into the project). The id
for each video is its file stem - no multi-part merging, no naming-pattern
requirement (src/videos.py's group_parts(tcrmp=False)).

Identity (original_videos, readable_id) is logged to status.csv, the
registry (TCRMP; no-op otherwise), and the console before any ffmpeg call.

Exit code: 0 when at least one timepoint extracted (a partial run prints a
WARNING naming the ones that failed), 1 when there was nothing to extract or
every timepoint failed. A timepoint that fails is closed out in the registry
with stage="failed" and a note, so the atlas stops counting it as live.
"""

import os
import sys
import logging
import subprocess
import datetime
from config import (
    DIRECTORIES,
    FRAMES_PER_TRANSECT,
    PROJECT_NAME,
    PARAMS,
    update_tracking,
    get_transect_status,
    initialize_tracking,
    step_log_path,
)
import manifest
import registry_client
import videos
from step0_naming import effective_frame_count, frame_output_pattern, identity_for, lone_part_note, names_a_single_part

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(step_log_path("step0")),
        logging.StreamHandler()
    ]
)

# Cooperative-pause contract (see step1/step2/step3). A sentinel file in the
# project root requests a pause; we honor it between timepoints (after the
# prior timepoint's frames are extracted and the tracking CSV updated), then
# exit with PAUSE_EXIT_CODE so the orchestrator treats it as a clean pause.
# Re-running resumes (already-extracted timepoints are skipped by the
# per-timepoint status).
PAUSE_EXIT_CODE = 42
PAUSE_SENTINEL = ".pause_requested"


def pause_requested():
    """True when a pause sentinel file exists in the project root."""
    return os.path.exists(os.path.join(DIRECTORIES["base"], PAUSE_SENTINEL))


def checkpoint_pause(where):
    """Between timepoints: if a pause was requested, exit cleanly (no open state)."""
    if not pause_requested():
        return
    logging.info(f"PAUSE requested - stopping cleanly at boundary: {where}")
    logging.info(
        "Paused. Re-run this module on the same project to resume "
        "(already-extracted timepoints are skipped)."
    )
    sys.exit(PAUSE_EXIT_CODE)


# Longest note this step writes into the registry's operator-owned notes
# column (matches step1's cap).
NOTE_MAX_CHARS = 500


def registry_note(readable_id, text):
    """Append a note to the registry row without discarding what the operator
    wrote there. Idempotent and capped. No-op outside TCRMP mode."""
    if not registry_client.enabled():
        return
    existing = (registry_client.row(readable_id) or {}).get("notes", "") or ""
    if text in existing:
        return
    combined = f"{existing}; {text}" if existing else text
    if len(combined) > NOTE_MAX_CHARS:
        combined = combined[-NOTE_MAX_CHARS:]
    registry_client.update(readable_id, notes=combined)


def registry_failure(readable_id, message):
    """Close a failed timepoint out in the registry and record why.

    Without this the stage set at the top of process_timepoint ("extracting")
    stays on the row, and the atlas shows a run that never ends.
    """
    if not registry_client.enabled():
        return
    registry_client.update(readable_id, stage="failed")
    registry_note(readable_id, message)


# Atlantic Standard Time, the platform's clock for every new timestamp.
AST = datetime.timezone(datetime.timedelta(hours=-4))


def ast_stamp(moment):
    """ISO 8601 with the -04:00 offset for a naive local datetime, the form
    the registry sidecars carry (for example 2026-09-04T07:05:09-04:00).
    The naive value is read as this machine's local time and converted, so
    the stamp is right even on a box whose clock is not set to AST."""
    return moment.astimezone(AST).isoformat(timespec="seconds")


def record_step0_facts(readable_id, start_time, end_time, frames_extracted):
    """Write the step 0 facts the atlas drop-down shows for this timepoint
    into the registry's row_facts.csv (section voyager1): when extraction
    started and finished, how long it took, how many frames it wrote, and
    where its console log is. No-op outside TCRMP mode. A failure to write
    the facts is logged and never fails the extraction: the frames and the
    registry row are already on disk and are the work that matters.
    """
    if not registry_client.enabled():
        return
    log_path = step_log_path("step0")
    facts = {
        "step0_started": ast_stamp(start_time),
        "step0_finished": ast_stamp(end_time),
        "step0_seconds": round((end_time - start_time).total_seconds(), 1),
        "frames_extracted": int(frames_extracted),
        "console_log_step0": log_path,
    }
    try:
        registry_client.facts(
            readable_id, registry_client.VOYAGER1_SECTION, facts,
            links={"console_log_step0": log_path},
            units={"step0_seconds": "s", "frames_extracted": "count"},
        )
    except Exception as exc:
        logging.warning(f"Could not record the step 0 facts for {readable_id}: {exc}")


# How often the extraction loop reports progress while ffmpeg runs, in
# seconds. The report is one cheap directory listing per interval, so it
# never slows the extraction itself.
EXTRACT_PROGRESS_INTERVAL_S = 30


def _extraction_progress(video_path, output_dir, existing_files):
    """A callback that logs how many new frames this video has written so
    far: "extracting <video name>: <N> frames written". Counting is one
    os.listdir of the output directory; any error is swallowed so a progress
    line can never take down the extraction."""
    video_name = os.path.basename(video_path)

    def report():
        try:
            written = sum(
                1 for f in os.listdir(output_dir)
                if f.endswith(".tiff") and f not in existing_files
            )
            logging.info(f"extracting {video_name}: {written} frames written")
        except Exception:  # silent-ok: counting frames written so far, only for a progress line
            pass

    return report


def _run_ffmpeg(video_path, out_pattern, rate, start_number, hwaccel_args,
                progress_cb=None):
    """Run one ffmpeg extraction. With progress_cb set, the wait is a
    timeout loop that calls progress_cb about every
    EXTRACT_PROGRESS_INTERVAL_S seconds while ffmpeg keeps working;
    subprocess.communicate(timeout=...) leaves the child running and keeps
    the partial pipe output, so the loop just retries until ffmpeg exits."""
    cmd = (
        ["ffmpeg"] + hwaccel_args + [
            "-i", video_path,
            "-vf", f"fps={rate}",
            "-c:v", "tiff",
            "-pix_fmt", "rgb48le",
            "-compression_level", "0",
            "-start_number", str(start_number),
            out_pattern,
        ]
    )
    if progress_cb is None:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=EXTRACT_PROGRESS_INTERVAL_S)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            try:
                progress_cb()
            except Exception:  # silent-ok: firing the progress callback between ffmpeg polls
                pass


def extract_frames_for_part(video_path, output_dir, frames_for_part, duration_s, codec, start_number=1):
    """Extract `frames_for_part` frames from one video (or video part) into
    `output_dir`, named after that video's own base name (frame_output_pattern).
    Tries hardware decode (videos.hwaccel_args) first, falls back to software
    decode on failure. Returns (frames_extracted, extracted_frame_paths).
    """
    if frames_for_part <= 0:
        raise ValueError(f"frames_for_part must be > 0. Found {frames_for_part}")
    if not duration_s or duration_s <= 0:
        raise ValueError(
            f"Video duration must be > 0 seconds to extract {frames_for_part} frames. "
            f"Found {duration_s} for {video_path}"
        )

    os.makedirs(output_dir, exist_ok=True)
    existing_files = set(os.listdir(output_dir))

    rate = frames_for_part / duration_s
    out_pattern = frame_output_pattern(video_path, output_dir)

    logging.info(f"Extracting {frames_for_part} frames from {video_path} (fps={rate:.6f})")
    print(f"Extracting {frames_for_part} 16-bit TIFF frames from {os.path.basename(video_path)} (fps={rate:.6f})...")

    progress_cb = _extraction_progress(video_path, output_dir, existing_files)
    hw_args = videos.hwaccel_args(codec)
    result = _run_ffmpeg(video_path, out_pattern, rate, start_number, hw_args,
                         progress_cb=progress_cb)

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        logging.warning(f"Hardware-accelerated ffmpeg failed for {video_path}: {stderr[-1000:]}")
        print("Hardware-accelerated extraction failed, retrying with software decoding...")
        result = _run_ffmpeg(video_path, out_pattern, rate, start_number, [],
                             progress_cb=progress_cb)
        if result.returncode != 0:
            stderr2 = (result.stderr or b"").decode(errors="replace").strip()
            logging.error(f"Software ffmpeg extraction also failed for {video_path}: {stderr2[-1000:]}")
            print(f"ERROR: ffmpeg failed for {os.path.basename(video_path)}: {stderr2[-1000:]}")
            return 0, []

    extracted_frame_paths = sorted([
        os.path.join(output_dir, f) for f in os.listdir(output_dir)
        if f.endswith(".tiff") and f not in existing_files
    ])
    frames_extracted = len(extracted_frame_paths)

    if frames_extracted == 0:
        logging.error(f"No frames were extracted from {video_path}")
        print(f"ERROR: No frames were extracted from {os.path.basename(video_path)}")
    else:
        size_mb = os.path.getsize(extracted_frame_paths[0]) / (1024 * 1024)
        logging.info(f"Extracted {frames_extracted} frames from {video_path}")
        print(f"SUCCESS: Extracted {frames_extracted} 16-bit TIFF frames "
              f"(first frame {size_mb:.2f} MB) to {output_dir}")

    return frames_extracted, extracted_frame_paths


def process_timepoint(readable_id, video_paths, row=None, tcrmp=True):
    """Process one timepoint (one or more video parts sharing a single
    readable id). Identity is logged - to status.csv, the registry (no-op
    when not TCRMP), and the console - before any ffmpeg call. Already-
    extracted timepoints (Step 0 complete == True in status.csv) are skipped
    before identity is re-logged, so a rerun over a shared processing folder
    only touches timepoints that still need frames.
    """
    output_dir_final = os.path.join(DIRECTORIES["frames"], readable_id)

    initialize_tracking(readable_id)

    status = get_transect_status(readable_id)
    if status.get("Step 0 complete", "False") == "True":
        logging.info(f"{readable_id} already has frames extracted, skipping...")
        return readable_id, True

    try:
        start_time = datetime.datetime.now()

        if tcrmp:
            original_videos, _ = identity_for(row=row, tcrmp=True)
        else:
            original_videos, _ = identity_for(video_path=video_paths[0], tcrmp=False)

        # Identity first: tracking + registry writes happen before we even
        # probe the video, so identity is on record whether or not the
        # probe/extraction that follows succeeds.
        update_tracking(readable_id, {
            "original_videos": original_videos,
            "readable_id": readable_id,
            "Status": "Extracting frames",
        })
        registry_client.stage(readable_id, 0, "extracting")
        # The atlas tails whatever console_log holds; pointing it at this
        # step's own log file makes the extraction output visible while it
        # runs (step 1 repoints the cell at its own log when it starts).
        registry_client.update(readable_id, console_log=step_log_path("step0"))

        first_path = video_paths[0]
        probe_info = videos.probe(first_path)

        width, height = probe_info.get("width"), probe_info.get("height")
        codec = probe_info.get("codec")
        duration = probe_info.get("duration_s")
        duration_str = f"{duration:.1f}" if duration is not None else "unknown"
        identity_line = (
            f"Timepoint {readable_id} from {os.path.basename(first_path)} "
            f"({width}x{height} {codec} {duration_str} s)"
        )
        logging.info(identity_line)
        print(identity_line)

        # Probe every part (the common case is a single part; the
        # allocation-by-duration split below only matters if more than one
        # part ever reaches this step - see module docstring).
        part_details = []
        total_duration = 0.0
        for path in video_paths:
            info = probe_info if path == first_path else videos.probe(path)
            part_duration = info.get("duration_s") or 0.0
            total_duration += part_duration
            part_details.append({
                "path": path,
                "duration": part_duration,
                "codec": info.get("codec"),
                "nb_frames": info.get("nb_frames"),
            })

        if FRAMES_PER_TRANSECT <= 0:
            logging.info(f"FRAMES_PER_TRANSECT is {FRAMES_PER_TRANSECT}. No frames extracted for {readable_id}.")
            os.makedirs(output_dir_final, exist_ok=True)
            end_time = datetime.datetime.now()
            nb_frames_values = [p["nb_frames"] for p in part_details if p["nb_frames"] is not None]
            update_tracking(readable_id, {
                "Status": "No frames requested",
                "Step 0 complete": "True",
                "Video Length (s)": f"{total_duration:.2f}",
                "Total Video Frames": str(sum(nb_frames_values)) if nb_frames_values else "",
                "Frames Extracted": "0",
                "Video Source": original_videos,
                "Extraction Timestamp": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Step 0 start time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Step 0 end time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "Step 0 processing time (s)": str((end_time - start_time).total_seconds()),
                "Frames directory": output_dir_final,
                "Notes": f"FRAMES_PER_TRANSECT set to {FRAMES_PER_TRANSECT}. No frames extracted.",
            })
            record_step0_facts(readable_id, start_time, end_time, 0)
            return readable_id, True

        if total_duration <= 0:
            raise ValueError(
                f"Total video duration for {readable_id} is zero or negative "
                f"({total_duration:.2f}s). Cannot extract frames."
            )

        os.makedirs(output_dir_final, exist_ok=True)
        global_start_number = 1
        cumulative_frames_extracted = 0
        multi_part = len(part_details) > 1

        for part in part_details:
            frames_for_part = (
                round(FRAMES_PER_TRANSECT * (part["duration"] / total_duration))
                if multi_part else FRAMES_PER_TRANSECT
            )
            if frames_for_part <= 0:
                logging.info(f"Skipping {part['path']}: 0 frames allocated by duration proportion.")
                continue

            clamped_frames_for_part = effective_frame_count(frames_for_part, part["nb_frames"])
            if clamped_frames_for_part < frames_for_part:
                logging.warning(
                    f"{readable_id}: requested {frames_for_part} frames from "
                    f"{os.path.basename(part['path'])} but the source video "
                    f"reports only {part['nb_frames']} frames; ffmpeg would "
                    "duplicate frames to reach the requested count. Clamping "
                    f"extraction to {clamped_frames_for_part} frames."
                )
                frames_for_part = clamped_frames_for_part

            num_extracted, _ = extract_frames_for_part(
                part["path"], output_dir_final, frames_for_part,
                part["duration"], part["codec"], start_number=global_start_number,
            )
            if num_extracted > 0:
                global_start_number += num_extracted
            cumulative_frames_extracted += num_extracted

        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()
        nb_frames_values = [p["nb_frames"] for p in part_details if p["nb_frames"] is not None]

        update_tracking(readable_id, {
            "Status": "Frames extracted",
            "Step 0 complete": "True",
            "Video Length (s)": f"{total_duration:.2f}",
            "Total Video Frames": str(sum(nb_frames_values)) if nb_frames_values else "",
            "Frames Extracted": str(cumulative_frames_extracted),
            "Video Source": original_videos,
            "Extraction Timestamp": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Step 0 start time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Step 0 end time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Step 0 processing time (s)": f"{processing_time:.2f}",
            "Frames directory": output_dir_final,
            "Notes": f"Extracted {cumulative_frames_extracted} frames from {total_duration:.2f}s "
                     f"of video across {len(part_details)} part(s).",
        })

        manifest.append_event(PROJECT_NAME, readable_id, "frames", "created",
                               output_dir_final,
                               details=f"{cumulative_frames_extracted} frames from {original_videos}")

        # Registry facts, only where the registry doesn't already have them
        # (no-op when not TCRMP - registry_client.row()/update() return None).
        existing_row = registry_client.row(readable_id) or {}
        registry_fields = {}
        if not existing_row.get("video_size_gb"):
            try:
                size_gb = sum(os.path.getsize(p["path"]) for p in part_details) / 1e9
                registry_fields["video_size_gb"] = round(size_gb, 3)
            except OSError:
                pass
        if not existing_row.get("video_duration_s") and total_duration:
            registry_fields["video_duration_s"] = round(total_duration, 1)
        if not existing_row.get("video_format"):
            container = (probe_info.get("container") or "").split(",")[0]
            registry_fields["video_format"] = f"{container}/{codec}" if codec else container
        if registry_fields:
            registry_client.update(readable_id, **registry_fields)
        record_step0_facts(readable_id, start_time, end_time, cumulative_frames_extracted)

        logging.info(
            f"Successfully extracted {cumulative_frames_extracted} frames for {readable_id} "
            f"(total duration: {total_duration:.2f}s) in {processing_time:.1f} seconds. "
            f"Frames saved to {output_dir_final}"
        )
        return readable_id, True

    except Exception as e:
        error_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_msg = f"Error processing timepoint {readable_id}: {str(e)}"
        logging.error(error_msg, exc_info=True)

        update_tracking(readable_id, {
            "Status": "Error in frame extraction",
            "Step 0 complete": "False",
            "Step 0 error time": error_time,
            "Notes": f"Error: {str(e)}"
        })
        # The registry already says stage="extracting" from the top of this
        # function. Left there, the atlas counts this row as a live run for
        # ever. Writing the failure closes it out; a no-op outside TCRMP mode.
        registry_failure(readable_id, f"step 0 failed: {str(e)}")
        return readable_id, False


def _tcrmp_work_items():
    """The registry rows this processing folder is responsible for, split into
    the ones that can be worked and the ones that cannot.

    Returns:
        (items, refused). `items` is [(readable_id, [video_path], row)] for
        every row whose identity resolves, including a row that names a single
        part-numbered file: a lone part is the whole recording (Lauren,
        2026-09-11), so the row is worked, and the part number goes to this
        step's log as a WARNING and to the console as a NOTE line so the fact
        is never lost. `refused` is [(readable_id, reason)] for every row whose
        identity does not resolve: a row still holding several video parts
        joined by ";" that were never merged.

    Rows are queried fresh from the registry every run, so a later sibling
    timepoint (same site and transect, added after this folder was first
    created) is picked up on a rerun.

    A refused row is returned, not dropped. Until 2026-09-08 it was skipped
    with `continue`, which took it out of the run's own denominator: a run
    given four timepoints, one of them unmerged, extracted three, reported
    "3/3", exited zero, and the Carousel moved on. Everything downstream had
    to believe it, because nothing anywhere counted the fourth.
    """
    rows = registry_client.rows_for() or []
    project_dir = os.path.abspath(DIRECTORIES["base"])

    items, refused = [], []
    for row in rows:
        location = os.path.abspath(row.get("processing_location") or "")
        if location != project_dir:
            continue
        try:
            original_videos, readable_id = identity_for(row=row, tcrmp=True)
        except ValueError as e:
            logging.error(str(e))
            print(f"ERROR: {e}")
            refused.append((row.get("readable_id") or "", str(e)))
            continue
        if names_a_single_part(original_videos):
            note = lone_part_note(original_videos, readable_id)
            logging.warning(note)
            print(f"NOTE: {note}")
        video_path = os.path.join(row.get("video_location") or "", original_videos)
        items.append((readable_id, [video_path], row))
    return items, refused


def _non_tcrmp_work_items():
    """(readable_id, [video_path, ...], None) for every video group under
    processing.video_input_dir. id = file stem, no multi-part merging
    (videos.group_parts(tcrmp=False))."""
    video_input_dir = PARAMS.get("processing", {}).get("video_input_dir")
    if not video_input_dir:
        logging.error("processing.video_input_dir is not set in analysis_params.yaml; nothing to extract.")
        return []
    if not os.path.isdir(video_input_dir):
        logging.error(f"processing.video_input_dir does not exist: {video_input_dir}")
        return []

    names = videos.list_videos(video_input_dir)
    if not names:
        logging.error(f"No video files found in {video_input_dir}")
        return []

    groups = videos.group_parts(names, tcrmp=False)
    return [
        (readable_id, [os.path.join(video_input_dir, n) for n in group_names], None)
        for readable_id, group_names in groups.items()
    ]


def main():
    """Extract frames for every pending timepoint: registry rows sharing this
    processing folder (TCRMP) or videos under processing.video_input_dir
    (non-TCRMP)."""
    tcrmp = bool(PARAMS.get("processing", {}).get("tcrmp", False))
    registry_client.configure(PARAMS)

    if tcrmp:
        work_items, refused = _tcrmp_work_items()
    else:
        work_items, refused = _non_tcrmp_work_items(), []

    if not work_items and not refused:
        # Never blame the videos without first checking whether the registry
        # could be read at all. On 2026-09-06 the registry library would not
        # import on this module's Python 3.9 environment, every row went
        # invisible, and the run reported missing videos while all of them sat
        # on the disk. The reason comes first because it is the actual fault.
        reason = registry_client.unavailable_reason() if tcrmp else None
        if reason:
            logging.error("%s No timepoint could be read, so there is nothing to extract. "
                          "The videos themselves were not checked.", reason)
            print(f"ERROR: {reason}")
            print("ERROR: no registry rows could be read, so no timepoint was processed. "
                  "This is not a missing video.")
            sys.exit(1)
        where = ("this processing folder's registry rows" if tcrmp
                 else PARAMS.get("processing", {}).get("video_input_dir") or "the video input folder")
        logging.error("No videos to extract frames from (looked in %s).", where)
        print(f"ERROR: no videos to extract frames from in {where}; nothing was done.")
        sys.exit(1)

    total = len(work_items) + len(refused)
    logging.info(f"Found {total} timepoint(s) to potentially process.")

    results = []
    # Refused rows are counted and closed out first, so the registry stops
    # showing them as live and the run's own total matches what it was given.
    for readable_id, reason in refused:
        if readable_id:
            registry_failure(readable_id, f"step 0 could not start: {reason}")
        results.append((readable_id or "(row with no readable id)", False))

    for idx, (readable_id, video_paths, row) in enumerate(work_items, start=len(refused) + 1):
        # Pause boundary: the previous timepoint's frames are fully extracted
        # and its tracking row updated. Stop here before starting the next.
        checkpoint_pause(f"before timepoint {readable_id}")

        logging.info(f"Processing timepoint {idx}/{total}: {readable_id} "
                     f"with {len(video_paths)} part(s)")
        processed_id, success = process_timepoint(readable_id, video_paths, row=row, tcrmp=tcrmp)
        results.append((processed_id, success))

    successful = sum(1 for _, success in results if success)
    logging.info(f"Frame extraction run complete. Successfully processed {successful}/{total} timepoint(s).")

    # Exit code is the only signal run_phase1 (and the VICARIUS runner behind
    # it) reads. Extracting nothing that was queued is a failed run; a partial
    # run is reported and still succeeds, because the timepoints that did
    # extract are real work step 1 can pick up.
    failed = [readable_id for readable_id, success in results if not success]
    if successful == 0:
        logging.error(
            f"Frame extraction failed for every timepoint: {', '.join(failed)}"
        )
        print(f"ERROR: frame extraction failed for all {total} timepoint(s).")
        sys.exit(1)
    if failed:
        logging.warning(f"Failed to process the following timepoint(s): {', '.join(failed)}")
        print(f"WARNING: {len(failed)} of {total} timepoint(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
