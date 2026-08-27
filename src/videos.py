"""ffprobe-based video helpers.

Video-ness is always decided by probing the container with ffprobe, never by
file extension: any container ffmpeg understands is fair game, and a
misleadingly-named non-media file is rejected.
"""
import json
import os
import platform
import subprocess
import sys

_VIDEO_STREAM_FIELDS = "codec_name,codec_type,width,height,r_frame_rate,nb_frames"
_HWACCEL_CODECS = {"h264", "hevc", "av1", "vp9"}

_nvidia_cache = None


def _naming3d():
    root = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius")
    lib_dir = os.path.join(root, "_METADATA", "3d")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import naming3d
    return naming3d


def _stem(name):
    base = os.path.basename(name)
    return base.rsplit(".", 1)[0] if "." in base else base


def _ffprobe_json(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,format_name",
        "-show_entries", f"stream={_VIDEO_STREAM_FIELDS}",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(result.stdout.decode("utf-8"))


def _parse_fps(r_frame_rate):
    if not r_frame_rate or "/" not in r_frame_rate:
        return None
    num, den = r_frame_rate.split("/", 1)
    try:
        num, den = float(num), float(den)
    except ValueError:
        return None
    if den == 0:
        return None
    return num / den


def probe(path):
    """Return {codec, container, width, height, fps, duration_s, nb_frames}
    for the first video stream ffprobe finds. Raises if ffprobe cannot read
    the file at all (subprocess.CalledProcessError).
    """
    data = _ffprobe_json(path)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)

    duration = fmt.get("duration")
    duration_s = float(duration) if duration not in (None, "", "N/A") else None

    codec = width = height = fps = nb_frames = None
    if video_stream:
        codec = video_stream.get("codec_name")
        width = video_stream.get("width")
        height = video_stream.get("height")
        fps = _parse_fps(video_stream.get("r_frame_rate"))
        raw_nb_frames = video_stream.get("nb_frames")
        nb_frames = int(raw_nb_frames) if raw_nb_frames not in (None, "", "N/A") else None

    return {
        "codec": codec,
        "container": fmt.get("format_name"),
        "width": width,
        "height": height,
        "fps": fps,
        "duration_s": duration_s,
        "nb_frames": nb_frames,
    }


def is_video(path):
    """True when ffprobe can read `path` and finds at least one video stream.
    Extension is never consulted."""
    if not os.path.isfile(path):
        return False
    try:
        data = _ffprobe_json(path)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return False
    streams = data.get("streams") or []
    return any(s.get("codec_type") == "video" for s in streams)


def _nvidia_present():
    """True when an NVIDIA GPU is visible via nvidia-smi. Cached per process
    (module global) so repeated hwaccel_args() calls do not shell out
    every time."""
    global _nvidia_cache
    if _nvidia_cache is None:
        try:
            result = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _nvidia_cache = result.returncode == 0
        except OSError:
            _nvidia_cache = False
    return _nvidia_cache


def hwaccel_args(codec):
    """["-hwaccel", "cuda"] for h264/hevc/av1/vp9 on Linux with an NVIDIA
    GPU visible, else []."""
    if codec not in _HWACCEL_CODECS:
        return []
    if platform.system() != "Linux":
        return []
    if not _nvidia_present():
        return []
    return ["-hwaccel", "cuda"]


def list_videos(folder):
    """Sorted names of every file in `folder` that is_video() accepts."""
    names = []
    for entry in os.listdir(folder):
        full = os.path.join(folder, entry)
        if is_video(full):
            names.append(entry)
    return sorted(names)


def group_parts(names, tcrmp=True):
    """Group video filenames into per-timepoint parts, files in part order.

    TCRMP mode (default) parses each name with naming3d.parse_video_name and
    groups by (project, date, site, transect), keyed by the reconstructed
    base stem; within a group, files sort by part number with the no-part
    (None) file first, ties keeping input order. Names that do not match the
    TCRMP pattern fall back to their own single-file group, keyed by their
    stem. Non-TCRMP mode never parses names: every file is its own group,
    keyed by its stem.
    """
    if not tcrmp:
        return {_stem(name): [name] for name in names}

    n3d = _naming3d()
    buckets = {}
    order = []
    for name in names:
        parsed = n3d.parse_video_name(name)
        if parsed is None:
            key, part = _stem(name), None
        else:
            key = f"{parsed['project']}{parsed['date']}_3D_{parsed['site']}_{parsed['transect']}"
            part = parsed["part"]
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((part, name))

    groups = {}
    for key in order:
        items = buckets[key]
        items.sort(key=lambda item: (item[0] is not None, item[0] if item[0] is not None else 0))
        groups[key] = [name for _, name in items]
    return groups
