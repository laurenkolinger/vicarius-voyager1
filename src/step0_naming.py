"""Pure naming and frame-count helpers for step0.py (frame extraction). No
config import, no I/O, so tests can exercise these without Metashape or a
project directory on disk (config.py has import-time side effects: it reads
sys.argv and creates directories).
"""
import os
import re


def effective_frame_count(requested, nb_frames):
    """Clamp a requested extraction count to the source video's own frame count.

    `requested` is how many frames step0 is about to ask ffmpeg for (either
    FRAMES_PER_TRANSECT directly, or its per-part share of it for a
    multi-part video). `nb_frames` is what ffprobe reported for that part
    (videos.probe's "nb_frames"); it is None when ffprobe could not report a
    count for the container/codec, in which case there is nothing reliable
    to clamp against and `requested` passes through unchanged.

    Asking ffmpeg for more frames than a video has does not fail: ffmpeg
    raises its output fps above the source's native rate and duplicates
    frames to fill the gap. Metashape then has nothing distinct to align
    those duplicates on, which surfaces much later as an opaque tie-point
    failure (see selection_utils.capped_gradual_selection's guard and
    step1.py's post-alignment guard) instead of here, where the actual cause
    is knowable up front.

    The caller is responsible for logging a WARNING naming both numbers when
    clamping actually happens; this function only computes the clamped value.
    """
    if nb_frames is None:
        return requested
    return min(requested, nb_frames)


def frame_output_pattern(video_path, frames_dir):
    """Return the ffmpeg output pattern for frames extracted from
    `video_path` into `frames_dir`: `<frames_dir>/<source base name>_%05d.tiff`.
    Frames keep the source video's own base name (never the readable id),
    so multi-part recordings that still reach this step end up with each
    part's frames distinguishable by file name.
    """
    stem = os.path.splitext(os.path.basename(video_path))[0]
    return os.path.join(frames_dir, f"{stem}_%05d.tiff")


# A file name ending in a part number, however the diver spelled it: "_2",
# "_part2", "_pt2". The archive uses all three.
# Parts count from 1, as in naming3d.MIN_PART; "_0" is not a part.
PART_SUFFIX = re.compile(r"_(?:part|pt)?(?P<part>[1-9][0-9]*)(?:_proxy)?$", re.IGNORECASE)

# Who decided that a lone part is the whole recording, and when. Quoted in
# every note so the reader of a log can trace the rule.
LONE_PART_RULING = "Lauren, 2026-09-11"


def names_a_single_part(original_videos):
    """True when `original_videos` names one file that carries a part number.

    Parameters:
        original_videos: the registry cell, one file name (a ";"-joined list is
            the caller's separate refusal).

    Returns:
        True for "TCRMP20240307_demo_SHR_T5_pt1.MP4", False for
        "TCRMP20240307_3D_SHR_T5.MP4".

    This decides whether the fact is worth noting, not whether the row is
    worked. A lone part is the whole recording (Lauren, 2026-09-11): prep
    renames such a file to the standard name before a Carousel run, so a row
    that still names a part comes from a run outside the Carousel or a cell
    prep never rewrote. The caller writes lone_part_note for it so the part
    number is never lost once the file is processed under its timepoint.
    """
    return part_number(original_videos) is not None


def part_number(original_videos):
    """The part number a file name ends in, or None for a standard name.

    Parameters:
        original_videos: the registry cell, one file name.

    Returns:
        3 for "TCRMP20240422_demo_MRS_T1_3.MP4", 1 for "..._pt1.MP4" and
        "..._part1.MP4", 2 for "..._2_Proxy.MOV", None for "..._3D_SHR_T5.MP4".
    """
    stem = os.path.splitext((original_videos or "").strip())[0]
    match = PART_SUFFIX.search(stem)
    return int(match.group("part")) if match else None


def lone_part_note(original_videos, readable_id):
    """One plain sentence recording that a lone part was taken as the whole
    recording.

    Parameters:
        original_videos: the registry cell, one part-numbered file name.
        readable_id: the timepoint the row belongs to.

    Returns:
        A single line for the step 0 log and the console, so the part number
        stays on record. Example:

        >>> lone_part_note("TCRMP20231207_demo_MRS_T3_part1.MP4", "MRS_T3_2023ann")
        'MRS_T3_2023ann: TCRMP20231207_demo_MRS_T3_part1.MP4 is a lone part and is taken as the whole recording (Lauren, 2026-09-11)'

    Raises:
        ValueError when `original_videos` does not carry a part number: a
        note for a standard-named file would record a fact that is not true.
    """
    if not names_a_single_part(original_videos):
        raise ValueError(
            f"{readable_id}: {original_videos!r} does not name a lone part; "
            "there is no part number to note."
        )
    return (f"{readable_id}: {original_videos.strip()} is lone part "
            f"{part_number(original_videos)}, taken as the whole recording ({LONE_PART_RULING})")


def identity_for(row=None, video_path=None, tcrmp=True):
    """Return (original_videos, readable_id) for one timepoint.

    TCRMP mode (tcrmp=True): identity comes straight from the registry row
    (`row["original_videos"]`, `row["readable_id"]`). A row still carrying a
    ";"-joined list of parts raises ValueError so the caller can count and
    close the row out instead of extracting from a bogus path; a set of parts
    is merged by prep before it reaches a Carousel run.

    A lone part is the whole recording (Lauren, 2026-09-11): a single
    part-numbered file with nothing else of its recording beside it is the
    recording. Prep renames such a file to the standard name before a
    Carousel run, so step 0 normally sees the standard name here. A row that
    still names a part (a run outside the Carousel, or a cell prep never
    rewrote) is accepted as it is, and the caller notes the fact with
    lone_part_note. Until 2026-09-11 step 0 refused such a row too. The
    refusal that paused the first Carousel batch was prep's, at the ingest
    stage before step 0 ran: one lone part held four timepoints of one
    transect (Meri Shoal transect 3) and three more turns behind them.

    Non-TCRMP mode (tcrmp=False): identity comes from the video file itself:
    `original_videos` is its base name (with extension), `readable_id` is
    that name without the extension. No merging, no naming-pattern
    requirement (see videos.group_parts(tcrmp=False)).
    """
    if tcrmp:
        if row is None:
            raise ValueError("identity_for(tcrmp=True) requires a registry row")
        original_videos = row["original_videos"]
        readable_id = row["readable_id"]
        if ";" in original_videos:
            raise ValueError(
                f"{readable_id}: multi-part original_videos not supported at step0 "
                f"({original_videos!r}); merge parts first (see atlasprep.md)."
            )
        return original_videos, readable_id

    if video_path is None:
        raise ValueError("identity_for(tcrmp=False) requires video_path")
    base = os.path.basename(video_path)
    readable_id = os.path.splitext(base)[0]
    return base, readable_id
