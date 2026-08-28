"""Pure naming and frame-count helpers for step0.py (frame extraction). No
config import, no I/O, so tests can exercise these without Metashape or a
project directory on disk (config.py has import-time side effects: it reads
sys.argv and creates directories).
"""
import os


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


def identity_for(row=None, video_path=None, tcrmp=True):
    """Return (original_videos, readable_id) for one timepoint.

    TCRMP mode (tcrmp=True): identity comes straight from the registry row
    (`row["original_videos"]`, `row["readable_id"]`). Multi-part rows are
    merged by prep before they ever reach the registry; if `original_videos`
    still carries a ";"-joined list, this raises ValueError so the caller can
    log and skip the row instead of extracting from a bogus path.

    Non-TCRMP mode (tcrmp=False): identity comes from the video file itself
    - `original_videos` is its base name (with extension), `readable_id` is
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
