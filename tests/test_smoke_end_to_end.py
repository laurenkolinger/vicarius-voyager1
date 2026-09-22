#!/usr/bin/env python3
"""
Module:  tests/test_smoke_end_to_end.py
Purpose: Run the real registry-driven frame extraction, on the real Python 3.9,
         from a real registry row to real frames on disk. The test that would
         have caught the 2026-09-06 failure before Lauren ever pressed RUN.
Inputs:  python3.9, ffmpeg, the shared registry library, src/step0.py.
Outputs: pass/fail lines; exit 1 on the first failure. Uses a temporary
         registry root and a temporary project folder; touches no real data,
         no NAS and no Metashape.

On 2026-09-06 a real run copied 228 GB of video correctly, then died at the
reconstruction saying "No videos to extract frames from." Every unit test
passed. The gap was that nothing ever ran this module's own code on this
module's own interpreter against a real registry: phase 1 builds a Python 3.9
environment because that is what Metashape ships, the shared registry library
used syntax that only parses on 3.10 and newer, and the import error was
caught into a variable nothing read.

A test that stubs the registry cannot catch that. A test that runs on the
harness interpreter cannot catch it either. This one runs the actual step 0
under python3.9 and asserts frames come out, so the same class of break
cannot reach an operator again.
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY = "/mnt/rip/vicarius_drive/vicarius/_METADATA/3d"
OLDEST = "python3.9"          # what phase 1 builds, because Metashape ships it

FAILED = []


def check(name, ok, detail=""):
    if ok:
        print("ok   " + name)
    else:
        print("FAIL " + name + " " + str(detail)[:600])
        FAILED.append(name)


def _need(tool):
    path = shutil.which(tool)
    if not path:
        print("SKIP: " + tool + " is not installed; this smoke test needs it")
        sys.exit(0)
    return path


def _make_video(path, seconds=2):
    """A tiny real video, so frame extraction has something true to do."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [_need("ffmpeg"), "-y", "-f", "lavfi", "-i",
         "testsrc=size=320x240:rate=10:duration=" + str(seconds),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
        capture_output=True, check=True, timeout=120)


def _params(video_dir, frames):
    """The analysis_params.yaml a run works from: the module's own committed
    template with only the frame count and the video folder changed, so this
    test cannot drift from what a real run is actually handed."""
    import yaml
    with open(os.path.join(REPO, "analysis_params.yaml")) as f:
        params = yaml.safe_load(f)
    params.setdefault("project", {})["name"] = "smoke test"
    proc = params.setdefault("processing", {})
    proc["tcrmp"] = True
    proc["frames_per_transect"] = frames
    proc["video_input_dir"] = video_dir
    proc["use_gpu"] = False
    proc["min_free_disk_gb"] = 1
    return yaml.safe_dump(params, sort_keys=False)


python39 = _need(OLDEST)
_need("ffmpeg")
work = tempfile.mkdtemp(prefix="phase1_smoke_")
try:
    reg_root = os.path.join(work, "registry")
    os.makedirs(reg_root)

    # -- a real registry row, written by the real library ------------------
    env = dict(os.environ)
    env["VICARIUS_3D_REGISTRY_ROOT"] = reg_root
    seed = (
        "import sys; sys.path.insert(0, " + repr(LIBRARY) + ")\n"
        "import registry\n"
        "registry.upsert('MRS_T1_2025ann', {\n"
        "  'process': 'true', 'site': 'MRS', 'transect': 'T1', 'year': '2025',\n"
        "  'season_token': 'ann', 'original_videos': 'TCRMP20250101_3D_MRS_T1.mp4',\n"
        "  'video_location': " + repr(os.path.join(work, "videos")) + ",\n"
        "  'processing_folder': 'MRS_T1_3D',\n"
        "  'processing_location': " + repr(os.path.join(work, "MRS_T1_3D")) + ",\n"
        "}, actor='test')\n"
        "print('seeded', len(registry.load()))\n"
    )
    proc = subprocess.run([python39, "-c", seed], capture_output=True, text=True, env=env, timeout=120)
    check("the registry library writes a row under python3.9", proc.returncode == 0,
          proc.stderr.strip()[-500:])
    if proc.returncode != 0:
        print("\n1 FAILED: the library does not even import; nothing else can pass")
        sys.exit(1)

    # -- a real video where the row says it is -----------------------------
    video = os.path.join(work, "videos", "TCRMP20250101_3D_MRS_T1.mp4")
    _make_video(video)
    check("the video exists where the registry row points", os.path.exists(video))

    # -- the project folder a run works in ---------------------------------
    project = os.path.join(work, "MRS_T1_3D")
    os.makedirs(project)
    with open(os.path.join(project, "analysis_params.yaml"), "w") as f:
        f.write(_params(os.path.join(work, "videos"), 5))

    # -- run the REAL step 0 on the REAL 3.9 interpreter --------------------
    env2 = dict(env)
    env2["PYTHONPATH"] = os.path.join(REPO, "src")
    run = subprocess.run([python39, os.path.join(REPO, "src", "step0.py"), project],
                         capture_output=True, text=True, env=env2, timeout=600)
    out = (run.stdout or "") + (run.stderr or "")

    check("step 0 does not report missing videos when the videos are there",
          "No videos to extract frames from" not in out,
          "step 0 said the videos were missing; this is the 2026-09-06 failure")
    check("step 0 exits cleanly", run.returncode == 0, out.strip()[-900:])

    # The module writes 16-bit TIFF, which is what Metashape wants; assert on
    # what it really produces rather than on an assumed format.
    frames_dir = os.path.join(project, "frames")
    frames = []
    for root, _dirs, files in os.walk(frames_dir):
        frames += [os.path.join(root, f) for f in files
                   if f.lower().endswith((".tif", ".tiff", ".jpg", ".jpeg", ".png"))]
    check("frames were actually extracted", len(frames) > 0,
          "the frames folder held " + str(len(frames)) + " images")
    check("the frame count is the number asked for", len(frames) == 5,
          "asked for 5, produced " + str(len(frames)))
    check("the frames are not empty files",
          all(os.path.getsize(f) > 1024 for f in frames),
          "at least one frame is under 1 KB")
    check("the frames are named after the source video",
          all(os.path.basename(f).startswith("TCRMP20250101_3D_MRS_T1_") for f in frames),
          sorted(os.path.basename(f) for f in frames)[:3])
    check("the frames sit under the timepoint's own folder",
          os.path.isdir(os.path.join(frames_dir, "MRS_T1_2025ann")),
          sorted(os.listdir(frames_dir)) if os.path.isdir(frames_dir) else "no frames folder")
    check("a console log was written for the step",
          any(f.startswith("step0_") for f in os.listdir(os.path.join(project, "console"))),
          "no step0 log in the console folder")
    check("status.csv records the timepoint",
          "MRS_T1_2025ann" in open(os.path.join(project, "status.csv")).read())

    # -- and the registry saw the work -------------------------------------
    readback = (
        "import sys; sys.path.insert(0, " + repr(LIBRARY) + ")\n"
        "import registry\n"
        "r = [x for x in registry.load() if x['readable_id'] == 'MRS_T1_2025ann'][0]\n"
        "print(repr(r.get('step1_status')), repr(r.get('stage')))\n"
    )
    rb = subprocess.run([python39, "-c", readback], capture_output=True, text=True, env=env, timeout=120)
    check("the registry is still readable after the run", rb.returncode == 0, rb.stderr.strip()[-400:])

finally:
    shutil.rmtree(work, ignore_errors=True)

print()
if FAILED:
    print(str(len(FAILED)) + " FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("end to end: a registry row became frames on the interpreter phase 1 actually uses")
