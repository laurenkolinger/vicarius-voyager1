# 3D Phase 1 (`3D_phase_1`)

First-step 3D photogrammetry for TCRMP transect video: frames out of the video, a scaled and textured model per timepoint, a DEM inside the project, and one Metashape project per site and transect that later timepoints append to. Every timepoint's identity, progress, and numbers are read from and written back to the platform TCRMP 3D registry, so the atlas shows the work as it happens.

- Tags: 3D | Version: 1.0.0 | Author: Lauren K Olinger | Created: 2026-02-13
- Repo: `/mnt/rip/vicarius_drive/vicarius/modules/3D_phase_1/github_repo`
- Registry it reads and writes: `/mnt/rip/vicarius_drive/vicarius/_METADATA/3d/` (contract: [`../../../_METADATA/3d/README.md`](../../../_METADATA/3d/README.md))
- Related study: `S2_3D_structure`

## What it does and why

A transect is filmed once per survey season, and the same ten metres of reef is filmed again the next season, and the season after that. Each of those films has to become a metrically scaled, textured 3D model, and the models of one transect have to stay comparable across years. That is what this module does, and the reason it exists as its own module rather than a script is that the cross-year part cannot be done by a tool that only knows about the folder in front of it.

Concretely, per timepoint, the module:

1. Reads the timepoint's identity from the registry before anything else happens, and writes it into `status.csv`, the registry, and the console log, so traceability exists before a single frame is extracted.
2. Extracts frames from the source video where the video already sits. Video is never copied and never symlinked into the processing folder.
3. Aligns those frames in Metashape at full resolution with capped, re-optimized tie point cleaning, builds Ultra High depth maps and a full mesh, scales the model from two coded-target bars, builds a DEM into the project, decimates and smooths a delivery mesh, and textures it.
4. Appends the finished chunk to the site and transect's psx, renames that bundle to the year range it now covers, and writes the run's numbers, sizes, and a snapshot back to the registry.
5. Marks the timepoint as awaiting a manual edit and prints the gate checklist.

The pieces that matter for comparability are the ones a per-folder script cannot do: one psx per site and transect so every year of a transect opens in the same project, a naming scheme in which alphabetical order is chronological order, a registry that says which timepoints exist and which are already done, and a parameter set that is identical across every run because nothing prompts and nothing is chosen by hand.

Non-TCRMP work (a test cruise, someone else's footage, a rig calibration) uses the same processing with none of the registry machinery: original file names, one project directory you choose, no rows written anywhere.

## Where it sits

- **Upstream, the registry.** Videos enter the TCRMP 3D registry through the atlas module's prep and ingest scripts (`atlasprep.md`, `prep_tools.py`, `atlasingest.py`). This module never guesses a timepoint from a file name on a NAS; it reads rows. A row it will process has `process = true`, a `video_location` that is a local directory that currently exists, and a `step1_status` that is not `complete`.
- **Beside it, the atlas.** The atlas (`/atlas` in vicarius_ui_os) reads the same registry on every request. While this module runs, the atlas's active panel shows the readable id, the stage, and an elapsed clock, and tails the console log of the folder being written. When the module finishes a timepoint the atlas shows it under "Ready for manual editing" with the folder path to sync down.
- **After it, the manual gate.** Step 1 stops at a human. The chunk needs straightening and cropping in the Metashape GUI, and a timepoint whose scale came back `MANUAL_NEEDED` needs its bars placed by hand. The registry cell that carries this is `manual_edit_status = awaiting`.
- **After that, step 2.** Step 2 (`3D_phase_2`) is not built. It will create the `_3doutput` folder beside the processing folder, scan the edited psx, flip `manual_edit_status` to `done`, and export the DEM, the orthomosaic, and the model exports. Everything a step 2 agent needs is written down in `../STEP2_HANDOFF.md`, at the module top level beside `github_repo/`.
- **Predecessors.** `3D_phase1` and `3D_init` are frozen; both move to `vicarius/modules/_archive/` when this module takes over as the default first step. This module is built from the validated `3D_init` work plus the stricter alignment and scaling settings; the parameter chapter below says what changed and why.

## Inputs

### TCRMP mode (default)

The input is a set of registry rows, not a folder. Selection happens in `select_rows` (`src/run_phase1.py`):

- `--ids MRS_T1_2023ann,MRS_T1_2024_pbl` selects named timepoints, in the order given.
- `--site MRS --transect T1` selects one site and transect.
- Neither given selects every pending timepoint.

Whatever the filter, four rules apply. `registry_client.rows_for` only ever returns rows whose `process` cell is `"true"`, so the atlas's process checkbox is the switch that keeps a lit or unlit duplicate, or a bad take, out of every run. Rows are sorted by `naming3d.sort_key` (site, transect number, year, `_pbl` before `ann`), so a psx range only ever grows forward. A row whose `video_location` is not a local directory that currently exists is skipped with a printed note, because the sync driver that makes remote videos local does not exist yet and the module refuses to invent a path. A row whose `step1_status` is already `complete` is skipped unless `--force` is passed.

The video itself is read from `video_location/original_videos`. A row whose `original_videos` still carries a `;`-joined multi-part list has not been through prep; `identity_for` raises, the row is logged as an error, and it is skipped rather than extracted from a nonsense path.

### Non-TCRMP mode

`--no-tcrmp --input <folder> --project <folder>`. The input folder holds either videos or subfolders of pre-extracted frames, and `detect_input_type` decides which by asking ffprobe, never by extension. Videos are read in place: `setup_project` writes the input folder into `processing.video_input_dir` in the project's `analysis_params.yaml` and nothing is copied. Frame subfolders are symlinked read-only into `frames/`. Ids are the original file names with the extension stripped, no multi-part merging and no naming-pattern requirement. No registry row is created, read, or written; `registry_client` short-circuits to `None` on every call because `processing.tcrmp` is `false`.

## Naming rules

These are the contract everything else depends on. They live in one place, `vicarius/_METADATA/3d/naming3d.py`, and both this module and the atlas import them from there.

- **Source video name** (TCRMP convention, unchanged): `TCRMP{YYYYMMDD}_3D_{SITE}_{T#}[_{part}][_Proxy].{ext}`, parsed by `parse_video_name`.
- **Readable id** (per timepoint): `{SITE}_{T#}_{year}{token}` where the token is `ann` for fall annual surveys and `_pbl` for spring post-bleaching surveys, written so alphabetical order is chronological order:

      MRS_T1_2023ann
      MRS_T1_2024_pbl
      MRS_T1_2024ann
      MRS_T1_2025_pbl

  That ordering works because the underscore of `_pbl` (0x5F) sorts before the `a` of `ann` (0x61).
- **Season token rule**: month 1 to 6 is `_pbl`, month 7 to 12 is `ann`, derived once at registry ingest and editable in the atlas. The module never recomputes it; it consumes the registry value.
- **Processing folder** (per site and transect, stable for its life): `{SITE}_{T#}_{earliest year}{earliest token}_3dprocessing`, for example `MRS_T1_2023ann_3dprocessing`. Step 2 later creates the sibling `{same}_3doutput` beside it.
- **psx name** (per file, by the year range of the timepoints it holds): `{SITE}_{T#}_{earliest year}_{latest year}.psx`, for example `MRS_T1_2023_2023.psx` after the first timepoint, renamed on the filesystem (the `.psx` and its `.files` directory together) to `MRS_T1_2023_2025.psx` as later timepoints append. Metashape resolves the bundle by the psx stem, so the rename is a plain move of both halves after a save.
- **Chunk label** is the readable id. **Frames folder** is `frames/{readable id}/`. **Frame files** inherit the source video's base name: `frames/MRS_T1_2023ann/TCRMP20231015_3D_MRS_T1_00001.tiff`. Frames are never renamed.
- **Non-TCRMP**: ids are the original file names with the extension stripped, and folders and psx files are named from them as before (`psx_{N}_{YYYYMMDD}.psx`, or `{id}_{YYYYMMDD}.psx` when `max_chunks_per_psx` is 1).

## Processing folder contract

One processing folder per site and transect, created as a sibling of the folder holding the first-processed timepoint's video (never inside the video folder itself) and reused by every later timepoint of that site and transect. When a transect's season folders share one parent directory, every timepoint resolves to the same processing folder location beside them. `prepare_tcrmp_folder` looks up any sibling row's `processing_location` before creating anything, so the folder is created once even when the four timepoints of a transect sit in four different season folders, and the folder is named for the earliest known timepoint regardless of which one is processed first (`_earliest_date_for` reconstructs a synthetic date from each sibling row's year and season token).

Six things live in that folder, and nothing else:

| Item | What it is |
|---|---|
| `analysis_params.yaml` | Copied from the module template the first time the folder is created, never overwritten afterwards. `processing.tcrmp` is set to `true` in it on every launch. This is the file `config.py` reads. |
| `status.csv` | The per-folder truth, one row per timepoint, identity first. Columns are listed below. |
| `console/` | One log file per step run, `step0_{YYYYMMDD_HHMMSS}.log` and `step1_{YYYYMMDD_HHMMSS}.log`. The atlas tails the newest of these. |
| `frames/` | One subfolder per timepoint, named by readable id, holding frames named after the source video. |
| `reports/` | One `{readable_id}_step1.pdf` per timepoint, written by `chunk.exportReport`. |
| the psx bundle | `{SITE}_{T#}_{first}_{last}.psx` plus its `{SITE}_{T#}_{first}_{last}.files` directory, at the top level of the folder. |

Three hidden runtime files also appear and are not products: `.venv/` (the Python 3.9 environment step 0 runs under, built once per folder), `.processing.lock` (the exclusive flock that stops two steps running against one folder), and `.pause_requested` while a pause is pending.

There is no `_3doutput` in step 1, no DEM raster on disk, and no orthomosaic. The DEM lives inside the psx.

### psx range naming and rename on append

The psx a timepoint goes into is resolved fresh for every timepoint by `current_psx`, in this order:

1. The psx the registry already records for this site and transect (`preferred_psx` prefers this timepoint's own row, then the most recent sibling row, and offers only a path still on disk). It wins outright when it exists and holds fewer than `max_chunks_per_psx` chunks, because the registry knows about bundles a name scan cannot reconstruct. It also wins at the cap when it already holds a chunk with this timepoint's label, which is the forced-rerun case: the stale-chunk swap replaces that chunk instead of adding one, so the count does not grow.
2. Otherwise the newest range-named psx for this site and transect in the folder, when it still holds fewer than the cap.
3. Otherwise a new single-year bundle `{SITE}_{T#}_{year}_{year}.psx`, with a numbered sibling if that name is already taken.

A bundle that cannot be opened counts as zero chunks, so it is reused and then quarantined by `process_model` (both halves moved aside as `.corrupt_{TIMESTAMP}`) rather than blocking the run.

After the final save, `years_in_psx` reads the years out of the chunk labels of chunks that actually hold a model, and `rename_psx_range` moves the bundle to `{SITE}_{T#}_{min}_{max}.psx`. Two guarantees hold there. The range only ever widens: `psx_range_target` unions the computed years with the name's own first and last year, so a document read that comes back short can never rename a bundle out from under the timepoints it still holds. And both halves move together, `.files` first so a failure on the second move can be rolled back; the move is refused outright when either target half already exists, the conflict is written to the registry `notes` of every timepoint in the run, and the bundle keeps its current name, which is what the registry then records. After a successful rename every chunk label's `status.csv` "PSX file" cell and registry `psx_file` cell is updated to the new path.

So a transect processed in order goes: `MRS_T1_2023_2023.psx` after `MRS_T1_2023ann`, `MRS_T1_2023_2024.psx` after `MRS_T1_2024_pbl` (and unchanged after `MRS_T1_2024ann`, which is the same year), `MRS_T1_2023_2025.psx` after `MRS_T1_2025_pbl`. The fifth timepoint of that transect finds four chunks in the bundle, hits the cap, and starts `MRS_T1_2025_2025.psx` beside it.

## Parameters

Every key, its default, the one-line comment it carries in `analysis_params.yaml`, its allowed range, what it changes in Metashape, why the value, and how the module automates what is otherwise a manual GUI procedure. Checked against the Metashape Professional 2.2 User Manual and the Metashape Python API Reference 2.2.2; page numbers are the printed page numbers of those documents. Measured figures come from the full-8K three-way comparison run of 2026-08-20 to 2026-08-22 (two transects, BID_T1 and JKB_T2, 1000 frames each) unless stated otherwise, and are labelled where they could not be traced back to a logged source.

### Frame extraction and input

These keys govern which timepoints Step 0 works on, what it pulls out of each transect video, and what Step 1 hands to Metashape. The first four are curated form fields written through to the processing folder's `analysis_params.yaml` at launch (`ui.params_writethrough` in `module.yaml`); the last three are fixed behaviours of `src/step0.py` and `src/videos.py` that an operator should understand but does not set.

| Key | Default | Comment in `analysis_params.yaml` | Range | What it changes |
|---|---|---|---|---|
| `processing.tcrmp` | true | registry-driven mode: select timepoints from the TCRMP 3D registry | true or false | Whether the run reads its timepoints from the TCRMP 3D registry and writes back to it, or processes a plain input folder by original file name. |
| `processing.frames_per_transect` | 1000 | frames step0 extracts per source video | integer, 1 or more; keep below the source frame count (about 14,400 to 25,200 frames for a 4 to 7 minute transect at 59.94 fps); 100 for a smoke test; 0 marks the transect complete with no frames extracted | Number of still frames FFmpeg writes per timepoint, and therefore the camera count Metashape aligns in that chunk. |
| `processing.max_chunks_per_psx` | 4 | chunks appended to one psx before a new range-named psx starts | integer, 1 or more | How many timepoint chunks one Metashape project (`.psx` pointer plus `.files` bundle) holds before the next timepoint starts a new project. |
| `processing.use_gpu` | true | GPU acceleration for Metashape matching and alignment | true or false | Whether Step 1 sets `Metashape.app.gpu_mask` to every detected device and clears `Metashape.app.cpu_enable` for the GPU-accelerated stages. |
| ffmpeg decode | `-hwaccel cuda` for h264, hevc, av1, and vp9 sources on Linux with an NVIDIA GPU visible; software decode for every other codec, platform, or machine | not a key; decided per file by `videos.hwaccel_args(codec)` | chosen from the ffprobe-reported codec, not operator-set | Which decoder turns the source video into raw frames before the TIFF encoder runs. |
| frame format | 16-bit RGB TIFF, `rgb48le`, uncompressed (`-c:v tiff -pix_fmt rgb48le -compression_level 0`) | not a key; fixed in `src/step0.py` | fixed today; measured alternatives listed below | Pixel depth and on-disk size of every frame Metashape reads. |
| video probing | `ffprobe -show_entries format=duration,format_name -show_entries stream=codec_name,codec_type,width,height,r_frame_rate,nb_frames` | not a key; `videos.probe()` | any container ffprobe opens that carries a video stream | How Step 0 learns each source's container, codec, duration, frame rate, and dimensions, which set the extraction rate and the registry's ingest facts. |

**`processing.frames_per_transect`.** Step 0 converts this count into an FFmpeg `fps` filter rate of `frames_per_transect / duration`, so a 280 s transect at 59.94 fps extracts at 3.57 fps, roughly every 17th source frame, evenly spaced over the whole swim. Metashape's own Import Video tool offers the same idea as a frame step of about 3, 7, or 14 percent of image width (manual, "Video data", pp. 35 to 36); we extract outside Metashape instead so the frames exist as files before any project opens, and Step 1 adds them with `chunk.addPhotos(filenames)` (API, `Chunk.addPhotos`, p. 33), which records links only until processing needs the pixels (manual, "Adding images", p. 23). We use 1000 because a 10 x 1 m swath at 1000 frames gives about 1 cm of forward travel per frame, so every point on the reef sits in dozens of overlapping frames; on the 23 transects of the 2026-07-15 collection every model aligned 1000 of 1000 cameras, and on the full-8K three-way run both transects aligned 1000 of 1000 under all three settings. The cost is superlinear: with pair preselection off, 1000 frames means 499,500 candidate pairs to match, and the full-8K Step 1 runs took 16,300 to 22,700 s per model. Extraction itself is linear and cheap (mean 207 s per transect, range 186 to 260 s). Raising the value past the source frame count makes the `fps` filter duplicate frames, which adds cameras without adding information; lowering it to 100 runs a fast smoke test with sparse overlap; 0 writes no frames and marks the transect `Step 0 complete=True` with `Status: No frames requested`. The UI form and `--param frames_per_transect=N` both write the key; Step 0 skips any transect already marked `Step 0 complete=True`, so changing the value after a run affects only unprocessed transects.

**`processing.max_chunks_per_psx`.** A Metashape project is a list of chunks, each one an independent data set with its own cameras, tie points, mesh, and texture; the manual states that the models in the chunks are not linked with each other (manual, "Using chunks", p. 169). The `.psx` file holds only links and the data sits in the sibling `.files` archive (manual, "Metashape project file (PSX)", p. 74). In registry mode Step 1 opens one `Metashape.Document()` per timepoint on the site and transect's current psx (reopening it if it exists, so earlier chunks are preserved), calls `doc.addChunk()` once, saves with `doc.save(path)` after the model, and clears every reference to the document before the bundle is renamed (API, `Document.addChunk`, p. 82; `Document.save`, p. 85). This key is the cap on how many chunks that bundle accumulates: `current_psx` counts the chunks in the candidate bundle and, at the cap, starts a new single-year psx instead. The value bounds how large one project grows: on the full-8K run a two-chunk project of 1000 cameras each weighed 19 to 20 GB with depth maps deleted after the DEM (32 GB when the older step kept them), so four chunks put a project near 40 GB, which still opens responsively in the GUI for the manual straighten-and-crop gate and keeps a single corrupt bundle from taking more than four timepoints with it. A value of 1 gives every timepoint its own psx, at the cost of more files to open by hand; larger values mean fewer files but slower saves and a bigger blast radius. Metashape does not read this key; it is purely our batching. The launcher writes it to `analysis_params.yaml`; `src/step1.py` falls back to 4 and `src/config.py` to 5 if the key is missing entirely, so leave it in the file.

**`processing.use_gpu`.** Metashape accelerates image matching, depth map reconstruction, depth-map-based mesh, DEM and tiled model generation, texture blending, and photoconsistent mesh refinement on CUDA and OpenCL devices (manual, "GPU recommendations", p. 1). Two application attributes control this from Python: `gpu_mask`, "GPU device bit mask: 1 - use device, 0 - do not use" (API, `Application.gpu_mask`, p. 16), and `cpu_enable`, "Use CPU when GPU is active" (API, `Application.cpu_enable`, p. 14). With the key true, Step 1 calls `Metashape.app.enumGPUDevices()` (API, p. 14), sets one mask bit per device (mask 3 on this box, which reports two RTX 5090 cards), and sets `cpu_enable = False`, following the manual's advice to disable the CPU flag when at least one discrete GPU is enabled (manual, "GPU tab", p. 22, and the note on p. 2). Every measured run in this README used this setting, including the full-8K three-way run; we have not timed a CPU-only run on 8K data and do not recommend one, since alignment and depth maps on 1000 8K frames already take 4.5 to 6.3 h per model with two GPUs. With the key false, Step 1 leaves the mask and CPU flag untouched and Metashape uses whatever the application preferences hold, which is the only reason to flip it (isolating a suspected GPU driver fault). Independently of this key, Step 1 sets `gpu_mask = 0` and `cpu_enable = True` around `buildTexture` because `enable_texture_gpu` defaults to false in `analysis_params.yaml`, then restores the saved values; the manual notes that GPU texture blending on Linux and Windows runs through Vulkan (p. 2), and the CPU path has been the stable choice on this box.

**ffmpeg decode.** Decode is codec-aware: `videos.hwaccel_args(codec)` returns `["-hwaccel", "cuda"]` only when the ffprobe-reported codec is one of `h264`, `hevc`, `av1`, or `vp9`, the platform is Linux, and `nvidia-smi -L` succeeds (the result is cached per process, so a batch of timepoints shells out once). Every other codec, platform, or machine gets an empty argument list and a plain software decode. NVDEC returns decoded frames to system memory, which the CPU-side `fps` filter and TIFF encoder consume; the earlier `-hwaccel_output_format cuda` flag kept frames on the GPU and broke that chain, which is why it is absent. ProRes has no hardware decoder on any NVIDIA card, so ProRes sources take the software path by construction rather than by FFmpeg's own fallback. Measured impact on production footage is nil today, because every production source is 8192 x 4320 ProRes: the 23-transect collection decoded in software at a mean 207 s per 1000-frame transect, roughly 1.3x real time, with no fallback errors. Hardware decode matters only when a season delivers H.264 or HEVC. If the FFmpeg run exits non-zero for any reason, `extract_frames_for_part` logs the FFmpeg stderr as a warning, prints "Hardware-accelerated extraction failed, retrying with software decoding", and reruns the identical command with no hardware-acceleration arguments before giving up on the timepoint.

**frame format.** Each frame is written as an uncompressed 16-bit RGB TIFF (`rgb48le`), about 212 MB at 8192 x 4320 (8192 x 4320 x 3 channels x 2 bytes), so a 1000-frame transect occupies about 212 GB and the 23-transect collection about 4.9 TB of frames. The choice keeps the source's 10-bit range intact; an 8-bit format would truncate it. Metashape reads the full depth: it "uses full color range for image matching operation and does not downsample the color information to 8 bit", and point cloud, orthomosaic, and texture carry the bit depth of the original images (manual, "Adding images", p. 23); TIFF and PNG are both in its input image format list (manual, Appendix B "Supported formats", p. 218), and the manual prefers RAW data losslessly converted to TIFF over JPG because JPG compression may add noise (manual, "Capturing scenarios", p. 9). The tradeoff is disk and I/O: measured on one 8K frame, 8-bit uncompressed TIFF is about 106 MB (half), 16-bit deflate TIFF 177 MB (17 percent smaller, paid in CPU on every write and every Metashape read), 16-bit PNG 177 MB, and JPEG at quality 2 4.5 MB. We have not run a controlled 16-bit versus 8-bit alignment comparison on our data, so the 16-bit choice is a precaution rather than a measured gain, and 8-bit uncompressed TIFF is the first knob to turn if frame storage becomes the constraint. Step 0 hard-codes the encoder arguments; there is no parameter for this yet, and Step 1 accepts `.jpg`, `.jpeg`, `.tif`, or `.tiff` (case-insensitive) when frames are supplied pre-extracted; PNG frames are ignored even though Metashape itself would read them.

**video probing with ffprobe.** Extension is never consulted. `videos.is_video(path)` runs `ffprobe -v error -show_entries format=duration,format_name -show_entries stream=codec_name,codec_type,width,height,r_frame_rate,nb_frames -of json` and accepts the file when the JSON carries at least one stream whose `codec_type` is `video`, so a MOV, an MKV, an MP4, an AVI, an M4V, an MTS, or an MXF all take the same path, a mislabelled non-media file is rejected, and `videos.list_videos(folder)` returns exactly the readable videos in a folder. `videos.probe(path)` returns the container (`format_name`), codec, width, height, frame rate (parsed from `r_frame_rate`), container duration, and `nb_frames` for the first video stream. Duration comes from the container, not from a frame count divided by a frame rate, and it sets the extraction rate above; the summed duration and frame count go to the status columns `Video Length (s)` and `Total Video Frames`, and container, codec, duration, and byte size are written to the registry as `video_format` (`container/codec`, for example `mov,mp4,m4a,3gp,3g2,mj2/prores` shortened to its first container alias), `video_duration_s`, and `video_size_gb` when those cells are still empty. A total duration of zero or less raises and fails the timepoint with a logged error rather than launching FFmpeg with an undefined rate. Multi-part recordings are merged into one file per timepoint before ingest, so the common case is a single part; when several parts do reach Step 0 they are probed separately and each receives a share of `frames_per_transect` proportional to its duration, rounded, numbered continuously into one frame directory by `-start_number`, and a part whose share rounds to 0 is skipped. Metashape never sees the container: it receives only the TIFF list through `addPhotos`, with `strip_extensions=True` by default so camera labels are the frame stems (API, p. 33).

### Alignment

All keys below live in `analysis_params.yaml`. `prepare_tcrmp_folder` copies the repository template into the processing folder the first time that folder is created and never overwrites it afterwards; `step1.py` reads `processing.metashape.defaults` into `METASHAPE_DEFAULTS` and `processing.step1_products` into `products_cfg`, then passes the values straight into the Metashape API. Nothing prompts at the console.

The "Default" column is the value the repository template ships as of v1.0.0 and the value the comparison runs used. A processing folder created by an earlier module carries whatever its own copy holds, so check the folder's file rather than the template when you are reading results back.

| Key | Default | Comment in `analysis_params.yaml` | Range | What it changes |
|---|---|---|---|---|
| `downscale` | `1` (High) | photo downscale for matching; 1 = full resolution | `0` Highest, `1` High, `2` Medium, `4` Low, `8` Lowest | Image resolution used for feature detection and matching in `matchPhotos`. `1` works on the original 8K frame; `2` halves each side; `0` upscales by 4. |
| `keypoint_limit` | `40000` | maximum keypoints detected per image | integer >= 0 (`0` = no limit) | Upper bound on feature points detected per image before matching. |
| `tiepoint_limit` | `0` | maximum tie points kept per image pair; 0 = unlimited | integer >= 0 (`0` = no filtering) | Upper bound on matched points kept per image after matching. `0` keeps every match. |
| `generic_preselection` | `false` | do not use image content to pre-select pairs | `true` / `false` | Whether Metashape first matches at a lower accuracy setting to guess which image pairs overlap, then matches only those pairs at the requested accuracy. |
| `reference_preselection` | `false` | do not use image metadata to pre-select pairs | `true` / `false` | Whether image pairs are chosen from measured camera locations, from a previous alignment, or from frame sequence, according to `reference_preselection_mode`. |
| `filter_stationary_points` | `true` | drop tie points that do not move between cameras | `true` / `false` | Drops tie points that sit at the same pixel location across many frames (sensor dust, lens marks, fixed overlays). |
| `adaptive_fitting` | `false` | do not adaptively fit camera parameters while optimizing | `true` / `false` | Whether `alignCameras` and `optimizeCameras` pick the set of lens parameters to solve automatically, or solve the fixed set listed under the calibration flags. |
| `gradual_selection_mode` | `capped` | capped iterative selection (internal value; code branches on it) | `legacy` or the capped value (`capped`; the pre-rename config token from older `analysis_params.yaml` files is also still accepted) | Which tie point cleaning sequence runs after alignment. `legacy` is one pass per criterion at the raw threshold with a single optimization; the shipped value is the code token for the capped, widening, re-optimized sequence documented here. |
| `reconstruction_uncertainty` | `15` (cap 50 percent) | RU threshold for capped iterative selection | float >= 1, ratio | Threshold for the ReconstructionUncertainty filter. Points above it are removed, never more than half the cloud in one pass. |
| `projection_accuracy` | `5` (cap 50 percent) | PA threshold for capped iterative selection | float > 0, image scale units | Threshold for the ProjectionAccuracy filter. Points above it are removed, never more than half the cloud in one pass. |
| `reprojection_error` | `0.5` px (cap 10 percent) | RE threshold for capped iterative selection | float > 0, normalized pixels | Threshold for the ReprojectionError filter. Points above it are removed, never more than a tenth of the cloud in one pass. |
| widen factor | `1.25` (code constant) | not a key; `cap_adjusted_threshold` in `src/selection_utils.py` | float > 1 | Multiplier applied to a threshold each time the cap would be exceeded; at most 12 probes per criterion. |
| calibration flags | `fit_f`, `fit_cx`, `fit_cy`, `fit_k1`, `fit_k2`, `fit_k3`, `fit_p1`, `fit_p2` on; `fit_b1`, `fit_b2`, `fit_k4` off | only `fit_k4` is a key ("do not fit the 4th radial distortion coefficient", read by the `legacy` path); the rest are written explicitly in `src/step1.py` | boolean per flag | Which interior orientation parameters `optimizeCameras` is allowed to change during each bundle adjustment. |

#### downscale

In the Align Photos dialog this is the Accuracy setting. The manual states that High works on photos at original size, Medium downscales by a factor of 4 (2 per side), Low by 16, Lowest a further 4, and Highest upscales by a factor of 4 and is recommended only for very sharp imagery and mostly for research purposes because of its cost (manual, Aligning photos and laser scans, Accuracy, p. 39). The API encodes those levels as `downscale` 0, 1, 2, 4, 8 (API, `Chunk.matchPhotos`, pp. 64 to 65). We keep `1` because tie point positions are estimated from feature spots found on the source images, and an 8K frame of a 10 by 1 m swath already places roughly 1 mm on a pixel along the long axis; downscaling would throw away the sub-millimetre localization the scale bars depend on, and Highest quadruples the pixel count for no measured gain on these frames. This value was held fixed across every comparison run, so the measured improvements below come from the other keys. `step1.py` passes it verbatim to `chunk.matchPhotos(downscale=...)`.

#### keypoint_limit

The upper limit on feature points Metashape detects on each image; zero lets it find as many as possible at the cost of many less reliable points (manual, Align Photos advanced parameters, Key point limit, p. 40; API `keypoint_limit`, p. 65). We use the API signature default of 40,000 (API, p. 64). Reef texture is feature rich, so 40,000 fills comfortably on an 8K frame, and raising it mostly adds weak points that the capped selection removes again later. It was unchanged across our runs. Passed verbatim to `matchPhotos(keypoint_limit=...)`.

#### tiepoint_limit

The upper limit on matching points kept per image; the manual says a zero value applies no tie point filtering (manual, Tie point limit, p. 40, and Editing chapter, Tie point per photo limit, p. 149). The manual also notes that the parameter is a performance control that does not generally affect model quality, recommends 10,000, and warns that a value that is too high or too low can cause parts of the point cloud to be missed because depth maps are generated only for photo pairs with enough matching points (manual, Note after Adaptive camera model fitting, p. 41). We set `0` rather than the API default of 4,000 (API, p. 64) because the transect video has extreme overlap: each frame shares content with dozens of neighbors, and a per-image cap discards exactly the redundant observations that let the bundle adjustment and the later filters separate good points from bad. Together with the other stricter alignment settings, our comparison runs recorded scale error on the worse transect falling from 4.10 mm to 1.34 mm and RMS reprojection from 2.45 px to 1.95 px, for about 5 percent more runtime. The larger initial cloud (about 3 M points on 8K) is then thinned by the capped selection to about 0.95 M. Passed verbatim to `matchPhotos(tiepoint_limit=...)`.

#### generic_preselection

With Generic preselection on, Metashape selects overlapping pairs by first matching photos at a lower accuracy setting; the option exists to speed up alignment of large photo sets (manual, Generic preselection, p. 39; API `generic_preselection`, p. 65). We turn it off. The low resolution pre-pass on underwater frames, where contrast is soft and the swath is narrow, can miss real overlaps and leave a chunk of the transect unconnected, and 1,000 frames is small enough that exhaustive pairing is affordable. Passed verbatim to `matchPhotos(generic_preselection=...)`.

#### reference_preselection

Reference preselection chooses pairs from measured camera locations (Source mode), from the exterior orientation of an earlier alignment (Estimated mode), or from the sequence number of the images (Sequential mode, which also compares the first image with the last) (manual, Reference preselection, p. 39; API `reference_preselection` and `reference_preselection_mode`, p. 65). Our frames carry no measured positions and no prior alignment, so Source and Estimated have nothing to work from, and Sequential only pairs neighbours in capture order (plus the first and last frame), which would miss the loop closures a diver produces when the camera drifts back over earlier ground. We turn it off and let every pair be tested. Passed verbatim to `matchPhotos(reference_preselection=...)`.

#### filter_stationary_points

Exclude stationary tie points removes tie points that remain stationary across multiple images; the manual gives a static background with a fixed camera and turntable as the primary case and adds that it also helps eliminate false tie points related to camera sensor or lens artefacts (manual, Exclude stationary tie points, p. 40; API `filter_stationary_points`, p. 65). That second case is ours: a housing port scratch, a water droplet, or a sensor blemish shows up in the same pixel on hundreds of frames and would otherwise become a spurious "point" that pins the cameras together wrongly. We set it `true` (also the API default). It is part of the stricter alignment group that produced the 4.10 mm to 1.34 mm scale error improvement in our comparison runs. Passed verbatim to `matchPhotos(filter_stationary_points=...)`.

#### adaptive_fitting

Adaptive camera model fitting lets Metashape choose which interior orientation parameters to adjust based on their reliability estimates; it helps strong geometry (objects photographed from all sides) and prevents divergence of some parameters on weak geometry such as a typical aerial data set. When it is off Metashape refines only focal length, principal point, K1 to K3 and P1 to P2 (manual, Adaptive camera model fitting, pp. 40 to 41; API `Chunk.alignCameras(adaptive_fitting=False)`, p. 34, and `Chunk.optimizeCameras(adaptive_fitting=False)`, p. 66). A nadir transect over a near-flat reef is weak geometry, and letting the solver switch K4, B1 or B2 on and off between passes changes the model from run to run and from criterion to criterion. We set it `false` so every bundle adjustment solves the same fixed set, which is what makes the iterative selection reproducible. `step1.py` passes the value to `chunk.alignCameras(adaptive_fitting=...)` and the capped selection calls `optimizeCameras(adaptive_fitting=False)` explicitly.

#### gradual_selection_mode

After the first `alignCameras` pass, `step1.py` retries `alignCameras(cameras=unaligned, reset_alignment=False)` for any camera without a transform (API `reset_alignment`, p. 34), resets the region, and then runs the tie point cleaning. Metashape's Clean Tie Points tool selects points by one criterion and a threshold (manual, Filtering points based on specified criterion, pp. 146 to 147); the API exposes the same thing as `TiePoints.Filter` with `init(points, criterion)`, `selectPoints(threshold)`, `removePoints(threshold)`, and `resetSelection()` (API, pp. 296 to 297). In `legacy` mode the script calls `removePoints` once per criterion at the raw threshold, in the order ReconstructionUncertainty, optimize, ReprojectionError, ProjectionAccuracy. In the capped mode `src/selection_utils.py` runs ReconstructionUncertainty, then ProjectionAccuracy, then ReprojectionError; for each it probes with `selectPoints`, counts `Point.selected` flags (API, p. 297), widens the threshold while the count exceeds the cap, commits with `chunk.tie_points.removeSelectedPoints()` (API, p. 300), logs threshold, removed and remaining counts, and calls `optimizeCameras` with the fixed calibration set before the next criterion. Removing in bounded fractions and re-adjusting between criteria is what keeps a strict threshold from gutting the cloud on a frame set this dense, and it is the step that turned 3 M tie points into about 0.95 M on 8K while the scale error fell to 1.34 mm in our comparison runs.

#### reconstruction_uncertainty

Reconstruction uncertainty is the ratio of the largest to the smallest semi-axis of the error ellipse of a tie point's triangulated coordinates, computed from the triangulation alone without propagating uncertainties from the interior and exterior orientation parameters (manual, p. 146). In plain words: a point seen only from nearby cameras with a short baseline is well located across the image but poorly located in depth, so its ellipse is long and thin and the ratio is large. The manual says such points can noticeably deviate from the object surface and add noise to the cloud, and that removing them should not affect the accuracy of the optimization. The value has no units and cannot be below 1. We start at 15 with a 50 percent cap. On 8K data the cap widened this to 23.4 before removal, so the effective threshold adapts to the cloud instead of stripping half of it. The key is read from `metashape.defaults` and used as the starting threshold for `TiePoints.Filter.ReconstructionUncertainty`; the earlier `legacy` value of 50 stays valid for that mode.

#### projection_accuracy

Projection accuracy is the average image scale at which a point's projections were measured, summed over its images and divided by the image count (manual, p. 147). A point whose projections were measured at a coarse scale has a large value and a looser pixel position. The manual describes this filter as removing points whose projections were relatively poorly localized because of their bigger size. The value is a positive scale factor. We start at 5 with a 50 percent cap, which on 8K widened to 7.8. Removing these coarse-scale matches before the ReprojectionError pass matters because they inflate reprojection error for their neighbors during the intermediate optimization. Used as the starting threshold for `TiePoints.Filter.ProjectionAccuracy`.

#### reprojection_error

Reprojection error is the distance between the point on the image where a reconstructed 3D point projects and the original projection detected on the photo (manual, Reference pane Cameras section, p. 119). The filter uses the maximum over a point's images of the pixel residual divided by the image scale of each measurement, so the value is in normalized units (manual, p. 146). High values mark poor localization of the projections at matching time or outright false matches, and the manual notes that removing them can improve the accuracy of the subsequent optimization. The threshold is a positive float. We start at 0.5 with a 10 percent cap because by the third criterion the cloud has already been trimmed twice and re-optimized, and a strict pass here is what tightens the camera solution. On 8K the cap widened this to between 0.78 and 0.98 depending on transect, and the final RMS reprojection after the run was 1.95 px versus 2.45 px under the older sequence in our comparison runs. Used as the starting threshold for `TiePoints.Filter.ReprojectionError`. The same notion of per-image reprojection error is what the marker projection pruning step (Scale group) applies to each coded target, where dropping the worst projections brought worst-case target errors from 28 to 47 px down to under 1.2 px in those runs.

#### widen factor

Not a YAML key; `cap_adjusted_threshold` in `selection_utils.py` fixes it at 1.25 with a limit of 12 probes (the starting threshold plus up to 11 widened values). Each probe multiplies the threshold by 1.25 until the selected count fits under the criterion's cap, then that threshold is used. If 12 probes still exceed the cap the script logs the shortfall and removes at the last threshold anyway, so the log for every run records the final thresholds actually applied (the 23.4 / 7.8 / 0.78 to 0.98 values above come from those log lines). The 1.25 step is coarse enough to converge in a handful of probes and fine enough that the effective threshold stays close to the smallest value the cap permits.

#### calibration flags

Optimize Cameras performs a full bundle adjustment that refines exterior orientation, interior orientation and tie point coordinates against all available measurements: tie point and marker projections, camera coordinates, GCP coordinates and scale bar distances (manual, Optimization of camera alignment, pp. 114 to 116). The API flags (API `Chunk.optimizeCameras`, pp. 66 to 67) map to the parameters defined in the camera model (manual, Camera calibration parameters, pp. 96 to 98; Appendix D, Camera models, pp. 242 to 243):

- `fit_f`: focal length in pixels. Always on; it is the primary scale of the image geometry.
- `fit_cx`, `fit_cy`: principal point offset in pixels, where the optical axis meets the sensor plane. On; underwater ports shift this from the sensor centre.
- `fit_k1`, `fit_k2`, `fit_k3`: radial distortion coefficients on r^2, r^4, r^6. On; a dome or flat port behind water produces strong radial distortion that these three describe.
- `fit_k4`: the r^8 radial term. Off; on near-flat nadir geometry it is poorly constrained and trades against K3. It is excluded from the fixed set Metashape refines when adaptive fitting is off, and the API signature default for it is `False`.
- `fit_p1`, `fit_p2`: tangential distortion coefficients for a lens decentred from the sensor. On; small but real on housed cameras.
- `fit_b1`, `fit_b2`: affinity (aspect ratio) and skew (non-orthogonality) coefficients in pixels. Off; modern square-pixel sensors have no measurable skew and freeing them adds two weakly determined unknowns.
- `fit_corrections`: additional coefficients beyond the Brown model, intended for RTK/PPK drone data sets without GCPs (manual, Fit additional corrections, p. 116). Off (API default).
- `tiepoint_covariance`: estimates per-point covariance matrices (manual, Estimate tie point covariance, p. 116). Off (API default); not needed by later steps.

This set is exactly what the manual says Metashape refines when adaptive fitting is unchecked, and it equals the API signature defaults, so every optimization inside the capped selection solves the same eight parameters. The optimize closure in `step1.py` writes all eleven `fit_*` flags plus `adaptive_fitting=False` explicitly rather than relying on defaults, so a future API default change to those cannot silently alter the calibration; `fit_corrections` and `tiepoint_covariance` are left at their API defaults of `False`. The `legacy` path still honours the older `fit_k4` and `adaptive_fitting` keys for A/B comparison.

### Reconstruction

These keys drive the depth-map, mesh, DEM, and delivery-mesh stages that run after camera optimization. The first six are read from `processing.metashape.defaults` in the processing folder's `analysis_params.yaml` (the v1.0.0 template carries all six; `max_neighbors` falls back to 16 in code when a folder's older copy lacks it); `removeComponents` is fixed in code; the last five live under `processing.step1_products`. Step 1 reads them once at launch and passes them straight into the Metashape Python API calls named below. Page numbers cite the printed page numbers of the Metashape 2.2 Professional manual and the Metashape Python Reference 2.2.2. Measured figures come from the full-8K three-way run of 2026-08-20 to 2026-08-22 (two transects, BID_T1 and JKB_T2, 1000 frames each) unless stated otherwise.

| Key | Default | Comment in `analysis_params.yaml` | Range | What it changes |
|---|---|---|---|---|
| `depth_downscale` | 1 (Ultra High) | depth map downscale; 1 = Ultra High quality | 1, 2, 4, 8, 16 | `buildDepthMaps(downscale=...)`. Image downscale factor used for stereo depth estimation: 1 = original 8K pixels, 2 = High, 4 = Medium, 8 = Low, 16 = Lowest. |
| `depth_filter_mode` | `MildFiltering` | depth map filtering strength | `NoFiltering`, `MildFiltering`, `ModerateFiltering`, `AggressiveFiltering` | `buildDepthMaps(filter_mode=...)`. Size cap of the connected-component outlier filter applied to each raw depth map. |
| `max_neighbors` | 16 | neighboring-image count used to build each depth map | positive integer | `buildDepthMaps(max_neighbors=...)`. Maximum number of overlapping images consulted when computing one camera's depth map. |
| `surface_type` | `Arbitrary` | freeform mesh surface, not a flat height field | `Arbitrary`, `HeightField` | `buildModel(surface_type=...)`. Whether the mesher may reconstruct any 3D shape or assumes a single-valued height surface. |
| `face_count` | `HighFaceCount` | target face count preset for the initial build | `LowFaceCount`, `MediumFaceCount`, `HighFaceCount`, `CustomFaceCount` | `buildModel(face_count=...)`. Polygon budget of the full mesh built from the depth maps. |
| `interpolation` | `EnabledInterpolation` | depth interpolation during buildModel | `DisabledInterpolation`, `EnabledInterpolation`, `Extrapolated` | `buildModel(interpolation=...)`. How much surface the mesher fills between and around observed depth samples. |
| `vertex_colors` | `false` | do not calculate per-vertex mesh colors | `true` / `false` | `buildModel(vertex_colors=...)`. Whether the mesher also samples a colour per vertex; the texture carries colour instead. |
| `removeComponents` | 99 (fixed in code) | not a key; `src/step1.py` | non-negative integer, polygon count | `chunk.model.removeComponents(99)`. Deletes every disconnected mesh fragment with fewer polygons than the threshold. |
| `step1_products.decimation_factor` | 10 | delivery mesh = full mesh faces divided by this factor | integer, 1 or greater (1 = keep full mesh) | `decimateModel(face_count=full_faces // factor, replace_asset=True)`. Divisor between the full mesh and the delivery mesh. |
| `step1_products.smooth_strength` | 4 | smoothing strength applied to the decimated delivery mesh | positive number; the form takes an integer | `smoothModel(strength=..., fix_borders=..., preserve_edges=..., replace_asset=True)`. Laplacian smoothing strength applied to the decimated delivery mesh. |
| `fix_borders` / `preserve_edges` | `true` / `false` | keep the mesh's outer border fixed while smoothing; do not preserve sharp edges while smoothing | `true` / `false` each | The two `smoothModel` flags that decide whether the open transect edges creep inward and whether sharp creases survive the pass. |
| `step1_products.delete_depth_maps` | `true` | remove depth maps from the chunk once the DEM is built | `true`, `false` | `chunk.remove(chunk.depth_maps_sets)` after the DEM is built. Whether the saved psx keeps the Ultra High depth maps. |
| `step1_products.build_dem` / `dem_resolution` | `true` / 0 (native) | build a DEM from the Ultra High depth maps and keep it in the psx; DEM resolution in metres per pixel, 0 = native | boolean / float metres per pixel, 0 = native | `buildDem(source_data=DepthMapsData, interpolation=EnabledInterpolation[, resolution=...])`. Builds the elevation model from the depth maps into the psx; 0 lets Metashape pick the resolution implied by the depth-map quality. |

#### depth_downscale

The Python API defines `downscale` as the depth-map quality step: 1 is Ultra High, 2 High, 4 Medium, 8 Low, 16 Lowest, and the API default is 4 (Python API 2.2.2, `Chunk.buildDepthMaps`, p. 36). The manual states that Ultra High processes the original photos and each following step downscales the images by a factor of 4 in area, 2 per side (Manual 2.2, Build Model parameters, Quality, p. 51). We hold this at 1 because the reef surface features we care about (branch tips, rugosity at the centimetre scale, the 20-bit circular coded targets) are only a few pixels across even at 8K, and every quality step below Ultra High halves the linear resolution of the depth samples before the mesh ever sees them. On the full-8K run, Ultra High depth maps for 1000 frames took 80 minutes per transect (BID_T1 79.7 min, JKB_T2 80.0 min) and yielded full meshes of 115.5 M and 130.2 M faces; the native DEM resolution that falls out of these depth maps is 0.12 to 0.13 mm per pixel. This is the single largest runtime lever in the module; each step down the quality ladder processes a quarter of the pixels, so dropping to 2 should cut the depth-map stage substantially, at the cost of the fine geometry the DEM and rugosity products depend on (we have not timed the lower settings at 8K). Step 1 passes the value straight into `buildDepthMaps` with `reuse_depth=False` and `subdivide_task=True`; edit it directly in `analysis_params.yaml` (the runner can open the file in vim before processing unless `--skip-vim` is passed).

#### depth_filter_mode

The manual describes depth filtering as a connected-component filter run on each segmented raw depth map, where the preset sets the maximum size of components discarded as outliers (Manual 2.2, Build Model parameters, Depth filtering modes, p. 52). It recommends Mild when the scene holds important small details that are spatially distinct, states that Mild is also required for depth-map-based mesh reconstruction, reserves Aggressive for scenes without meaningful small detail, and advises against disabling the filter because the resulting point cloud can be extremely noisy (same section). The Python API default is `MildFiltering` (`Chunk.buildDepthMaps`, p. 36). We keep Mild because reef transects are exactly the small-detail case: coral branches, sponge lobes, and the 0.75 m scale-bar targets are thin structures that a Moderate or Aggressive preset would strip along with the outliers. Our full-8K meshes came out of Mild filtering with only 0.5 to 0.8 percent of their faces in small disconnected fragments (534 k of 115.5 M on BID_T1; 1.07 M of 130.2 M on JKB_T2), which the `removeComponents` pass below clears in about two minutes, so the stronger presets buy nothing here. Step 1 resolves the string to the `Metashape.FilterMode` enumeration (Python API, p. 89) and passes it into `buildDepthMaps`.

#### max_neighbors

The API defines `max_neighbors` as the maximum number of neighbour images used for generating each depth map, with a default of 16 (`Chunk.buildDepthMaps`, p. 36). Along a swim transect every frame overlaps heavily with its predecessors and successors, so 16 neighbours already spans several seconds of footage on either side of a frame, and raising the cap adds stereo pairs with steadily longer baselines without adding new viewpoints. We hold the API default; the 80-minute depth-map stage above was measured at this value. Each extra neighbour is one more stereo pair per camera, so this is the second runtime lever after `depth_downscale`, although we have not timed other values. The v1.0.0 template carries this key at 16; Step 1 reads `processing.metashape.defaults.max_neighbors` when present and otherwise passes 16 into `buildDepthMaps`, so an older processing folder without the key behaves identically.

#### surface_type

The manual states that Arbitrary makes no assumptions about the object and can model any kind of shape at the cost of higher memory, while Height field is optimized for planar surfaces such as terrains, requires less memory, and suits aerial photography (Manual 2.2, Build Model parameters, Surface type, p. 51). The API default is `Arbitrary` (`Chunk.buildModel`, p. 36). We choose Arbitrary because reef structure has overhangs, undercut colonies, and branching corals that are not single-valued in height; a height field would flatten every undercut to its top surface and bias the rugosity and DEM products. The memory cost is affordable on this box for a 1000-frame transect (both full-8K meshes built in 54 to 58 minutes after the depth maps). Step 1 resolves the string to `Metashape.SurfaceType` (Python API, p. 152) and passes it into `buildModel` with `source_data=DepthMapsData`, the source the manual recommends for Arbitrary reconstruction (Manual 2.2, Build Model parameters, Source data, p. 51).

#### face_count

The manual defines Face count as the maximum number of polygons in the final model, with the High, Medium, and Low presets scaled to the amount of source data (for point-cloud sources the ratios are 1/5, 1/15, and 1/45 of the point count), and warns that custom counts above 10 million polygons can cause visualization problems in external software (Manual 2.2, Build Model parameters, Face count, p. 51). The API default is `HighFaceCount` (`Chunk.buildModel`, p. 36). We take High so that the full mesh keeps as much of the Ultra High depth-map detail as the mesher offers; this mesh is the source for the decimated delivery mesh, and starting from fewer faces would lose detail that decimation can never recover. On the full-8K run High produced 115.5 M faces (BID_T1) and 130.2 M faces (JKB_T2), about 3.6 to 4.0 M faces per square metre of mesh surface (Metashape reported 31.9 and 32.9 m2 of surface area for the two meshes). That mesh is far past the manual's 10 M external-viewer guideline, which is why the module decimates it before texturing rather than shipping it as is. Step 1 resolves the string to `Metashape.FaceCount` (Python API, p. 89) and passes it into `buildModel`.

#### interpolation

The manual explains that Disabled reconstructs only areas that have source points and usually needs manual hole filling; Enabled (the default) interpolates surface within a circle of a certain radius around every source point so some holes close on their own while some may remain; Extrapolated produces a holeless model but can add large areas of extra geometry (Manual 2.2, Build Model parameters, Interpolation, p. 52). The API default is `EnabledInterpolation` (`Chunk.buildModel`, p. 36). We keep Enabled: Disabled leaves pinholes across sand and in shadowed crevices that show up as data gaps in the DEM, while Extrapolated fabricates surface across the transect edges and under overhangs, which would be indistinguishable from real reef in the rugosity metrics. Enabled fills the small gaps with locally supported surface and leaves the large ones honest. The DEM stage uses the same enumeration with the same reasoning (see `build_dem`). Step 1 resolves the string to `Metashape.Interpolation` (Python API, p. 96; not to be confused with `Elevation.Patch.InterpolationType`, which governs DEM fill patches) and passes it into `buildModel`.

#### removeComponents

The API's `Model.removeComponents(size)` removes small connected components, where `size` is a threshold on the polygon count of the components to be removed (Python API 2.2.2, `Model.removeComponents`, p. 112). The GUI equivalent is Tools > Model > Clean Model with the Connected component size criterion, which selects isolated fragments from smallest to largest for deletion (Manual 2.2, Polygon filtering on specified criterion, p. 158); the GUI slider is expressed relative to the model, with the largest component taken as 100 percent, whereas the API takes an absolute polygon count, which is what makes it reproducible across models of different size. We use 99 faces because the fragments left by Mild depth filtering are floating specks of a few dozen polygons (suspended particles, fish, and stray water-column matches), and a 99-face cap removes them while no coral colony at 3.6 to 4.0 M faces per square metre comes anywhere near that size. On the full-8K run the pass took 2.2 to 2.4 minutes and removed 534,154 faces (0.46 percent) from BID_T1 and 1,070,430 faces (0.82 percent) from JKB_T2. Clearing these fragments before the DEM and scale steps keeps them out of the elevation raster and away from the coded-target search. The threshold is fixed in `step1.py` as `chunk.model.removeComponents(99)`; it is not in `analysis_params.yaml`.

#### step1_products.decimation_factor

The API's `Chunk.decimateModel(face_count, ..., replace_asset=False)` decimates the model to the specified face count, with `replace_asset` replacing the source model with the decimated one (Python API 2.2.2, `Chunk.decimateModel`, pp. 43 to 44). The manual notes that decimation discards the texture atlas, so texture must be rebuilt afterwards (Manual 2.2, Decimation tool, pp. 155 to 156), which is why the module textures only after this step. A factor of 10 turns the roughly 115 to 130 M face full mesh into an 11.5 to 12.9 M face delivery mesh, which is close to the roughly 10 M face count of the manually validated reference product and to the manual's 10 M polygon guideline for external viewers. On the full-8K run the decimation itself ran in 61 to 67 seconds per model (114,943,658 to 11,494,365 faces; 129,100,366 to 12,910,036 faces). Because the DEM has already been built from the full-resolution depth maps by this point, the decimation costs nothing in the elevation product; it only trims the mesh that is textured, reported, and shipped. Step 1 reads the full face count from `chunk.model.statistics().faces`, computes `full_faces // decimation_factor`, and calls `decimateModel` with `replace_asset=True`; a value of 1 skips the call and keeps the full mesh. This key is one of the twelve parameters on the launcher form, so the UI form field or a `--param decimation_factor=N` argument writes it through to `processing.step1_products.decimation_factor`.

#### step1_products.smooth_strength

The API's `Chunk.smoothModel` applies Laplacian smoothing with a float `strength` (API default 3), `fix_borders` (default `True`), `preserve_edges` (default `False`), and `replace_asset` (default `False`) (Python API 2.2.2, `Chunk.smoothModel`, p. 71). The manual describes the Smooth tool as removing surface irregularities, with the Strength set by slider and Fix borders preserving vertex positions along open edges (Manual 2.2, Smooth tool, p. 157). We smooth at 4, one step above the API default, and apply it to the decimated mesh rather than the full one: decimation leaves faceted edges at the new coarser triangle size, and a strength-4 pass on 11 to 13 M faces removes that faceting without softening real relief the way the same strength would on a 130 M face mesh. `fix_borders` stays on so the open transect edges do not creep inward. On the full-8K run the pass took 6 to 7 seconds per model on the decimated mesh, compared with 32 seconds for the earlier module's strength-1 pass over a 106.2 M face full mesh. Step 1 calls `smoothModel` with `replace_asset=True` immediately after decimation, passing `fix_borders` and `preserve_edges` from `metashape.defaults`, and then removes any mesh asset that is not the active one, so each chunk ends with exactly one mesh. Launcher form parameter; the UI field or `--param smooth_strength=N` writes it through. A working range of roughly 1 to 10 is convention here, not a limit stated in the API or manual.

#### step1_products.delete_depth_maps

By default `buildModel` stores the depth maps in the chunk (`keep_depth=True`, Python API 2.2.2, `Chunk.buildModel`, p. 36); they are then listed under `Chunk.depth_maps_sets` (p. 44) and can be dropped with `Chunk.remove(items)`, whose accepted item types include `Metashape.DepthMaps` (p. 69). We keep the depth maps through the mesh, the scale step, and the DEM, then delete them, because nothing after the DEM reads them and at Ultra High on 1000 8K frames they are a large share of the PSX bundle. On the full-8K run a two-chunk psx bundle built with these settings came to 19 GB (9.8 GB and 8.8 GB per chunk) against 32 GB (17 GB and 16 GB per chunk) for the same two transects under the older settings, which kept both the depth maps and the undecimated full mesh, so the 13 GB difference reflects both. A later orthomosaic can be built from the DEM plus the mesh, so the depth maps are not needed for that either. Set `false` only when you intend to rebuild a DEM or point cloud at a different setting without recomputing 80 minutes of depth maps. Step 1 calls `chunk.remove` on `chunk.depth_maps_sets` (falling back to `chunk.depth_maps`) right after the DEM is built and logs whether the removal succeeded; edit the key directly in `analysis_params.yaml`.

#### step1_products.build_dem and dem_resolution

The API's `Chunk.buildDem` takes `source_data`, `interpolation` (default `EnabledInterpolation`), and `resolution` in metres with a default of 0 (Python API 2.2.2, `Chunk.buildDem`, p. 35); the resulting `Elevation.resolution` is reported in metres (p. 89). The manual recommends Depth maps as the source when point classification is not required and no point cloud is needed, recommends Enabled interpolation, which computes elevation for every area visible in at least one image, and states that with Depth maps as the source the resolution is recalculated relative to the depth-map quality unless a value is entered manually (Manual 2.2, Build DEM parameters, pp. 66 to 67). It also notes that Build DEM works only for projects saved in PSX format and only for referenced or scaled projects (Manual 2.2, Building digital elevation model, p. 63). We build the DEM from the Ultra High depth maps, before they are deleted, at native resolution so the raster carries the full depth-map detail; on the full-8K run that gave 0.132 mm per pixel (BID_T1) and 0.122 mm per pixel (JKB_T2), with the DEM stage taking 48 to 50 minutes per transect in the run that also exported a GeoTIFF (this module does not). Setting `dem_resolution` above 0 passes an explicit metre value to `buildDem` and is the right lever when a coarser, smaller raster is wanted; leaving it at 0 keeps native. The build runs after the two 0.75 m scale bars are applied, so the elevation is in metres when scaling passed and in local units, flagged `(unscaled units)` in the log line and the `status.csv` Notes cell, when it did not. Step 1 saves the still-empty document to its psx path before any model starts so that project storage exists for `buildDem`, calls `buildDem(source_data=DepthMapsData, interpolation=EnabledInterpolation)` with `resolution` added only when `dem_resolution` is greater than 0, raises if `chunk.elevation` is empty, records `chunk.elevation.resolution * 1000` as the registry's `dem_mm_per_pix`, and keeps the elevation asset in the chunk. **This module exports no raster.** The DEM stays inside the psx so the operator's manual straighten and crop happens before any georeferenced product is written, and step 2 exports the GeoTIFF from the edited project (see `STEP2_HANDOFF.md`). The save-then-verify gate therefore checks the chunk's model and texture in the reopened bundle, not a file on disk. `build_dem` and `dem_resolution` are both edited directly in `analysis_params.yaml`; neither is on the launcher form.

### Scale and texture

Every key below lives in the processing folder's `analysis_params.yaml` unless noted. Metashape references are to the Metashape Professional 2.2 User Manual (chapter and section, printed page number) and the Metashape Python API Reference 2.2.2 (class and method). Scale in this module never blocks a run: the two outcomes are `PASS` and `MANUAL_NEEDED`, and processing continues either way.

| Key | Default | Comment in `analysis_params.yaml` | Range | What it changes |
|---|---|---|---|---|
| `step1_products.scale_in_step1` | `true` | detect coded targets and apply the scale bars after the model builds | `true` / `false` | Whether the scale stage runs at all in step 1. `false` leaves the status `MANUAL_NEEDED` with the sentinel error and no detection cost. |
| `model_processing.has_coded_scales` | `true` | the model carries coded scale-bar targets | `true` / `false` | Whether `apply_scale` runs target detection and scale-bar construction. `false` skips the whole scale stage and the model stays in arbitrary chunk units. |
| `model_processing.scale_bars` | `target 1000` to `target 1010` = 0.75 m; `target 1020` to `target 1030` = 0.75 m | the two 0.75 m bars used to apply real-world scale | list of `{start_marker, end_marker, distance}`; distance > 0 m; marker labels as Metashape writes them (`target NNNN`) | The two scale bars Metashape builds from detected markers (`Chunk.addScalebar(point1, point2)`, then `Scalebar.reference.distance`). |
| `model_processing.remove_unlisted_markers` | `true` | remove any detected target not listed in scale_bars | `true` / `false` | Deletes every detected marker whose label is not an endpoint of a listed bar (`Chunk.remove([marker])`). |
| `model_processing.marker_projection_prune` | `true` | prune each marker's worst-error projection until under threshold | `true` / `false`; fixed sub-parameters 0.8 px ceiling, 5 projection floor | Per marker, drops the worst-error image projection, worst first, until the worst remaining projection is under 0.8 px or only 5 projections remain. |
| `model_processing.scale_error_threshold` | `0.009` m | maximum acceptable scale error in metres (9 mm) | > 0 m; 0.005 conservative, 0.010 relaxed | The ceiling on the mean absolute scale-bar residual that, together with the two-bar requirement, decides PASS versus MANUAL_NEEDED. |
| detectMarkers target type and tolerance | `CircularTarget20bit`, `tolerance=50` | not a key; hard-coded in `src/scale_utils.py` | tolerance 0 to 100 (`Chunk.detectMarkers`) | Which coded-target family the detector decodes, and how far a target may deviate from the ideal pattern and still be accepted. |
| Scale-bar reference accuracy | `0.001` m (code constant `SCALEBAR_ACCURACY`) | not a key; `src/scale_utils.py` | > 0 m | `Scalebar.reference.accuracy`, the assumed accuracy of the 0.75 m bar length, consulted by any later Optimize Camera Alignment run. |
| Scale decision | PASS / MANUAL_NEEDED | not a key; derived by `scale_utils.decide` | n/a (derived) | Two or more bars built AND a mean absolute residual strictly under threshold gives PASS. No bars, one bar, or two bars whose mean residual reaches the threshold gives MANUAL_NEEDED. Processing continues either way; the texture page count falls back. |
| Scale error report | metres, millimetres, parts per million | not a key; derived by `scale_utils.ppm` | n/a (derived) | Mean absolute residual over the bars built. `status.csv` carries `Scale Error (m)` to six decimals and `Scale Error (ppm)`; the registry carries `scale_error_mm` and `scale_error_ppm` (`error_m / bar_length_m x 1e6`, rounded to a whole ppm). |
| `step1_products.texture_pixel_size` | `0.0005` m | target metres per texel for scaled models; sets page count | > 0 m; 0.0002 to 0.002 practical | Target size of one texel on the scaled mesh; drives the texture page count. |
| Texture page size | `8192` px (fixed in code) | not a key; `src/step1.py` | power of two; 4096 to 16384 | `texture_size` for `Chunk.buildUV` and `Chunk.buildTexture`, the width and height of one atlas page. |
| Texture page count | `ceil(area / texel^2 / 8192^2)` | not a key; `scale_utils.compute_texture_pages` | integer >= 1 | `page_count` for `Chunk.buildUV`, how many 8192 px pages the atlas is split into. |
| `step1_products.unscaled_page_count` | `4` | fixed 8192-pixel texture pages when Scale is not PASS | integer >= 1 | Fixed page count used when the scale decision is not PASS (mesh area is then in arbitrary units). |
| `metashape.defaults.mapping_mode` | `GenericMapping` | UV mapping mode used by buildUV | `GenericMapping`, `OrthophotoMapping`, `AdaptiveOrthophotoMapping`, `SphericalMapping`, `CameraMapping`, `KeepUV` | `buildUV(mapping_mode=...)`. Generic is the mode that supports multi-page atlases. |
| `metashape.defaults.texture_type` | `DiffuseMap` | texture channel produced by buildTexture | `DiffuseMap`, `NormalMap`, `OcclusionMap` | `buildTexture(texture_type=Metashape.Model.<value>)`. Which channel the atlas holds. |
| `metashape.defaults.blending_mode` | `MosaicBlending` | how overlapping images blend into the texture | `MosaicBlending`, `AverageBlending`, `MaxBlending`, `MinBlending`, `DisabledBlending` | How colour from overlapping frames is combined into each texel. |
| `metashape.defaults.fill_holes` | `true` | fill small holes left in the texture | `true` / `false` | Fills texels no camera sees. |
| `metashape.defaults.ghosting_filter` | `true` | filter ghosting artifacts caused by moving subjects | `true` / `false` | Suppresses moving or unreconstructed thin objects that would leave ghosts in the texture. |
| `metashape.defaults.enable_texture_gpu` | `false` | build the texture on CPU; GPU is freed during this step | `true` / `false` | Whether the GPU is left enabled during texture blending. `false` blends on CPU only. |

#### `step1_products.scale_in_step1` and `model_processing.has_coded_scales`

Together these are the master switch for the scale stage. `scale_in_step1` decides whether `step1.py` calls the scale stage at all; `has_coded_scales` decides whether `apply_scale` runs detection once called. With both `true`, Step 1 detects coded targets, builds the listed bars, applies the transform, and records the decision. With either `false`, or with `scale_bars` empty, the stage logs "Scaling skipped" and the timepoint keeps `MANUAL_NEEDED` with the sentinel error value 999.0 (written to `status.csv` as `999.000000`), so the DEM is built in unscaled units, the texture uses `unscaled_page_count`, and the registry's `scale_error_mm` and `scale_error_ppm` cells are left blank rather than carrying a nine-digit non-measurement. Both default to `true` because every transect in this workflow carries two printed target bars. Automation: `scale_utils.apply_scale` reads `has_coded_scales` together with `scale_bars` before calling `Chunk.detectMarkers`, so a project with no targets never pays for detection.

#### `model_processing.scale_bars`

Each entry names two marker labels and the known distance between their centres. The labels are the ones Metashape assigns on detection (`target NNNN`, where NNNN is the decoded target ID), and the distance is the manufactured centre-to-centre length of the bar. Metashape treats a scale bar as the representation of any known distance within the scene; it is not enough on its own to set a coordinate system, but it constrains scale and is useful where ground control points cannot be placed across the scene (Manual, Reference and calibration, Scale bar based optimization, p. 117), which is exactly what a headless reef transect needs: metric size without GCPs. Our two bars are 0.75 m each and sit at opposite ends of the 10 m swath, so any residual scale gradient along the transect shows up as disagreement between the two bars. Automation: `add_scale_bars` looks up both markers by label, calls `chunk.addScalebar(start, end)` (API, `Chunk.addScalebar`, endpoints may be markers or cameras), sets `scalebar.reference.distance`, `.accuracy`, and `.enabled = True` (API, `Scalebar.Reference`), then `chunk.updateTransform()` updates the chunk transformation from the reference data (API, `Chunk.updateTransform`). A bar whose markers were not both detected is logged as a warning and skipped; the decision below is then made on whichever bars were built.

#### `model_processing.remove_unlisted_markers`

Target detection returns every target the decoder finds, including stray ones from neighbouring transects and false decodes on high-frequency reef texture. Markers that are not bar endpoints add nothing to scale and would be exported into downstream products and reports. With this key `true`, every marker whose label is not an endpoint of a listed bar is removed with `chunk.remove([marker])` (API, `Chunk.remove`, which accepts `Metashape.Marker` items). Default `true`; set `false` only when you intend to inspect the extra markers in the GUI. Automation: runs immediately after `detectMarkers` and before pruning, and the log lists the removed labels.

#### `model_processing.marker_projection_prune`

A coded target is seen in many of the 1000 frames, and each image projection contributes to the marker's triangulated position. Metashape's marker `Error (pix)` is the root mean square reprojection error over all images where the marker projection is placed (Manual, Reference and calibration, What do the errors in the Reference pane mean, Markers section, p. 119), and reprojection error is the distance between where the reconstructed 3D point projects on the image and the original projection detected on that image (same section, Cameras section). Detected projections are placed automatically and carry a per-projection `pinned` flag (API, `Marker.Projection.pinned`; an unpinned projection shows as a blue flag in the GUI, Manual, Placing markers on the images, p. 110), and Metashape reports no per-projection warning at the bar level, so a motion-blurred or partially occluded frame can place a target centre many pixels off unnoticed. Pruning computes each projection's own pixel error once, as the distance between `camera.project(marker.position)` (API, `Camera.project`, returns 2D image coordinates; `Marker.position`) and `marker.projections[camera].coord` (API, `Marker.Projection.coord`, pixels), then repeatedly clears the worst projection from that set (`marker.projections[camera] = None`) while the worst remaining error is above 0.8 px and more than 5 projections remain. Errors are not recomputed between removals within a pass. Cameras with no transform are skipped and do not count toward the floor. The 0.8 px ceiling is set at roughly the marker residual observed on clean frames; the floor of 5 keeps every target over-determined (the manual requires projections on at least 2 images to triangulate a marker, Manual, Placing markers on the images, p. 108; 5 keeps a redundant solution after pruning). The v1.0.0 template sets the key `true`; set it `false` to keep every detected projection, which is what the earlier module did. The code's own fallback when the key is missing entirely is `false`, so an older processing folder without the key does not silently gain the pass. Automation: `prune_marker_projections` runs after unlisted markers are removed and before bars are built; cameras with no transform are skipped and do not count toward the floor, and any exception on one marker is logged as a warning and that marker is skipped so the rest of the pass continues.

#### `model_processing.scale_error_threshold`

After the transform is applied, each bar's residual is the absolute difference between its measured length and the reference length, which is what the Reference pane reports as scale-bar `Error (m)` (Manual, Reference and calibration, What do the errors in the Reference pane mean, Scale Bars section, p. 119; also Processing report, Scale Bars, p. 91). Our code computes it directly from `chunk.transform.matrix.mulp()` of the two marker positions (API, `Matrix.mulp`), then averages over the bars that were built. The threshold is 9 mm on a 0.75 m bar, which is 1.2 percent, or 12,000 ppm. It is deliberately loose relative to good runs, so a mean residual over 9 mm indicates a bad target projection or a scale gradient rather than normal noise. Tighten to 0.005 m if you want the gate to catch marginal runs; loosen to 0.010 m only for low-resolution proxies. Automation: read by `apply_scale`; the comparison is a strict less-than on the mean absolute residual, and the result is written to `status.csv` as `Scale`, `Scale Error (m)`, `Scale Error (ppm)`, and `Scale Bars`, and to the registry as `scale_status`, `scale_error_mm`, `scale_error_ppm`, and `scale_bars`. This is the one scale key on the launcher form.

#### detectMarkers: `CircularTarget20bit`, `tolerance=50`

`Chunk.detectMarkers(target_type, tolerance, filter_mask, inverted, noparity, maximum_residual, minimum_size, minimum_dist, merge_markers, ...)` creates markers from coded targets (API, `Chunk.detectMarkers`). `target_type` is a `Metashape.TargetType`; the circular coded families are `CircularTarget12bit`, `CircularTarget14bit`, `CircularTarget16bit`, and `CircularTarget20bit`, and the enum also lists non-coded circle and cross targets and the AprilTag families (API, `Metashape.TargetType`). The 20-bit family is the one printed on our bars and gives the largest ID space; the manual notes that the 12-bit pattern is considered to decode more precisely while 14, 16, and 20-bit allow more distinct targets in one project (Manual, Reference and calibration, Coded targets advantages and limitations, p. 111). Tolerance (0 to 100, API default 50) is not a size parameter; it sets how far a target on an oblique image may differ from the ideal pattern and still be accepted (Manual, Non-coded targets implementation, p. 112). We keep the default 50 because underwater frames are near-nadir and target contrast is high; raising it is expected to admit more false decodes on reef texture, lowering it to drop detections on the outermost frames where the bar is at the image edge. `filter_mask=False` because no image masks exist in this pipeline. Automation: called once per chunk by `apply_scale` with these values hard-coded in `scale_utils.py`; they are not exposed in the YAML.

#### Scale-bar reference accuracy: 0.001 m

`Scalebar.reference.accuracy` is the scale bar length accuracy (API, `Scalebar.Reference.accuracy`), and the manual describes it as the assumed accuracy of measured scale bar lengths, set in the Reference Settings dialog before running scale bar based optimization, or per item in the Accuracy column of the Reference pane (Manual, Reference and calibration, Scale bar based optimization, p. 118; Optimization, p. 116). Optimize Camera Alignment consults it when scale bars are checked for optimization. In this stage the bar is applied with `chunk.updateTransform()`, which updates the chunk transformation from reference data without re-running the camera optimization, so the value is stored for any later optimization rather than consumed here. 1 mm matches the print and mounting tolerance of the bars, and it is the same value the earlier scaling workflow used, so results stay comparable. Automation: the constant `SCALEBAR_ACCURACY` in `scale_utils.py` is written to every bar; change it there.

#### Scale decision: PASS versus MANUAL_NEEDED

`scale_utils.decide(bars_added, mean_error_m, threshold_m)` needs both halves of the evidence: at least **two** bars actually built, because one bar cannot corroborate itself, and a mean absolute residual strictly under the threshold. Everything else is MANUAL_NEEDED: no bars (targets missing, the scale stage switched off, `scale_bars` empty, or detection returned nothing), one bar only (a marker of the other pair was not detected, which the log records as a warning), or two bars whose mean residual reaches the threshold. MANUAL_NEEDED never halts the run: the DEM is still built into the psx (its resolution note is flagged "(unscaled units)"), the mesh is still decimated, smoothed, and textured, and the texture uses the fixed `unscaled_page_count` because mesh area in arbitrary units cannot size pages. Step 1 prints the boxed line `MANUAL SCALE NEEDED for <readable id>: <n> bar(s) found, error <e>`, appends the same sentence to the registry's `notes` cell, and lists the timepoint again under MANUAL SCALE at the manual gate. The operator then opens the chunk in the GUI, checks the targets in the Reference pane, fixes or re-places projections, and re-applies the bars (Manual, Scale bar based optimization, To add a scale bar between markers, p. 117). Automation: `step1.py` reads the returned status, writes it to `status.csv` and the registry, and branches the DEM units note and the texture page count on it. If `apply_scale` raises, `step1.py` logs the exception as a warning and the timepoint keeps MANUAL_NEEDED with the sentinel error.

#### Scale error report: metres, millimetres, and ppm

`status.csv` stores the mean absolute residual in metres to six decimals (`Scale Error (m)`) alongside `Scale Error (ppm)` and `Scale Bars`; the sentinel `999.000000` means no bar was measured. The registry stores the same reading as `scale_error_mm` (metres times 1000, two decimals) and `scale_error_ppm`, and `registry_scale_fields` leaves both blank when the value is the sentinel, so a non-measurement never sorts or charts as a 999,000 mm bar. Millimetres are what a diver understands; ppm (`error_m / bar_length_m x 1e6`, where the denominator is the mean declared bar length in `scale_bars`) carries across bar lengths and lets a 0.75 m bar result be compared with a 1 m bar or with the overall transect length (for example 1.34 mm on a 0.75 m bar is 1,787 ppm, which over a 10 m transect is 18 mm end to end). Automation: `mean_scale_bar_error` and `ppm` in `scale_utils.py`; no key to set.

#### `step1_products.texture_pixel_size`

This is the texel size, in metres, that the scaled mesh should carry. Metashape exposes the same idea as Pixel size (m) in the Build Texture dialog, calculated automatically but overridable (Manual, General workflow, Building model texture, Texture generation parameters, p. 57) and as `pixel_size` on `Chunk.buildUV` (API, "Texture resolution in meters"). We do not pass `pixel_size` to Metashape; instead we convert it to a page count ourselves (next entry) so that the unscaled fallback can use a fixed count and the atlas size is deterministic. 0.5 mm per texel is on the order of the ground sampling distance of an 8K frame at about 1 m altitude, so the texture neither oversamples the imagery nor throws away much detail. Raising it to 0.001 m halves the linear resolution and quarters the texel count. Automation: one of the twelve launcher form parameters; set it on the form or with `--param texture_pixel_size=0.0005`, which the launcher writes to `processing.step1_products.texture_pixel_size`.

#### Texture page size: 8192 px

Texture size specifies the width and height of the texture atlas in pixels (Manual, Building model texture, Texture size, p. 57), and `texture_size` defaults to 8192 on both `Chunk.buildUV` and `Chunk.buildTexture` (API). We fix it at 8192 in `step1.py`; there is no `texture_size` key in the v1.0.0 template. The manual notes that exporting a high-resolution texture to a single file can fail due to RAM limitations and that splitting across pages avoids this (Manual, Page count, p. 57); a 16384 page holds four times the texels of an 8192 page, and at 0.5 mm texels the extra page size brings no added detail, only memory. Automation: hard-coded in the `buildUV` and `buildTexture` calls.

#### Texture page count: `ceil(area / texel^2 / 8192^2)`

Page count sets the number of texture files the atlas is split across; multi-page atlases are supported in Generic mapping mode (and Keep UV), and Generic is the mode this workflow sets (`metashape.defaults.mapping_mode: GenericMapping`) (Manual, Building model texture, Page count and Mapping mode, p. 56 to 57; API, `Chunk.buildUV(page_count=...)`). We compute the count from `chunk.model.area()` (API, `Model.area`) in square metres at the requested texel size: one 8192 page at 0.5 mm covers 4.096 m by 4.096 m, or 16.8 m2, so a 10 by 1 m transect mesh with modest relief resolves to one page, and a wider or more rugose mesh grows to two or three pages without any operator input. Zero or negative area, or a failed `area()` call, yields one page. Automation: `scale_utils.compute_texture_pages` runs after decimation and smoothing, and the log prints the page count and the area it came from.

#### `step1_products.unscaled_page_count`

When the scale decision is not PASS the mesh area is in chunk units and the formula above is meaningless, so the workflow uses this fixed count. Four 8192 pages give 268 megatexels, which for a typical transect is above the scaled result (one page) and keeps the unscaled texture from being coarser than the scaled one would have been; the operator can rebuild the texture after manual scaling if they want the exact count. Range is any positive integer; 1 to 8 is practical. Automation: read by `compute_texture_pages` only on the fallback branch; edit it in the YAML directly, it is not on the launcher form.

#### `metashape.defaults.blending_mode`: `MosaicBlending`

Mosaic is a two-step blend: the low-frequency component is a weighted average across overlapping images to avoid seamlines, while the high-frequency component that carries picture detail is taken from a single image, the one with good resolution for the area whose view is almost along the surface normal (Manual, Building model texture, Blending mode, p. 56 to 57). Average uses a weighted average of all pixels, and Max Intensity or Min Intensity select the image with the brightest or darkest corresponding pixel. For reef imagery, where the point is to read individual corallites and colony margins, Mosaic keeps the sharpest frame's detail and hides the exposure drift between frames. It is the API default (`Chunk.buildTexture(blending_mode=MosaicBlending)`). Automation: the YAML string is resolved with `getattr(Metashape, ...)` and passed straight through.

#### `metashape.defaults.fill_holes`: `true`

Hole filling is on by default in Metashape because it helps avoid a salt-and-pepper effect on complicated surfaces with numerous tiny parts shading other parts of the model (Manual, Building model texture, Enable hole filling, p. 57). Branching corals are exactly that case: texels on the underside of branches are seen by no camera and would otherwise render as gaps. API default `fill_holes=True` (`Chunk.buildTexture`). Automation: passed through from the YAML; leave it on unless you are diagnosing coverage gaps and want the unfilled texels visible.

#### `metashape.defaults.ghosting_filter`: `true`

The ghosting filter is meant for scenes with thin structures or moving objects that failed to reconstruct as part of the polygonal model, which otherwise produce a ghosting effect in the texture (Manual, Building model texture, Enable ghosting filter, p. 57). Fish, drifting particulate, and swaying gorgonian tips are all in that class, so we keep it on. API default `ghosting_filter=True` (`Chunk.buildTexture`). Because it discounts image content that does not agree with the mesh, it may soften genuine thin features such as branch tips that the mesh only partly captured; if a transect has none of the moving-object problem and you want the last bit of sharpness on thin branches, set it `false` and compare. Automation: passed through from the YAML.

#### `metashape.defaults.enable_texture_gpu`: `false`

Metashape blends textures on the GPU through Vulkan on Linux and Windows for frame and fisheye cameras on supported NVIDIA and AMD hardware and driver versions (Manual, Installation and Activation, GPU recommendations, p. 1 to 2). With this key `false`, the workflow saves `Metashape.app.gpu_mask` and `Metashape.app.cpu_enable`, sets `gpu_mask = 0` and `cpu_enable = True` immediately before `buildTexture`, and restores both afterwards (API, `Application.gpu_mask`, a device bit mask; `Application.cpu_enable`). Every other GPU-capable stage keeps whatever `use_gpu` set up. CPU blending is the conservative default because GPU texture blending has a documented history of driver-version sensitivity; it costs extra minutes per transect on a one-page atlas and does not depend on the installed driver. GPU blending can be re-enabled by setting this key `true`, but do it only after a canary test on one transect with the current driver: confirm the texture builds, that `chunk.model.textures` is non-empty (the code raises if it is empty), and that the page looks the same as the CPU result before switching a batch over. Automation: read by `step1.py` at texture time; no other stage consults it.

## Outputs and dictionary entries

`module.yaml` declares two outputs, each with a data-dictionary dataset and an EML sidecar:

| Dataset | Path template | What it is |
|---|---|---|
| `3D_phase_1_psx` | `${project_dir}/{SITE}_{TRANSECT}_{FIRST_YEAR}_{LAST_YEAR}.psx` | The site and transect's Metashape project bundle, one chunk per timepoint, each chunk carrying aligned cameras, tie points, an elevation model, one delivery mesh, and its texture. The sibling `.files` directory holds the data. |
| `3D_phase_1_step1_reports` | `${project_dir}/reports/{READABLE_ID}_step1.pdf` | Metashape's own processing report for that chunk, exported with `chunk.exportReport`. |

Two more artifacts are written but are not declared outputs, because they are records rather than products: `frames/{readable id}/` (16-bit TIFF frames, roughly 212 MB each at 8192 x 4320, so about 212 GB per 1000-frame timepoint) and `status.csv`. The registry snapshot at `vicarius/_METADATA/3d/snapshots/{readable_id}/` holds the params file as used, the report PDF, and a `snapshot.yaml` with the run's numbers plus a file inventory of the whole processing folder, so the atlas can show a timepoint after its folder has been synced away.

The module-central artifact log `vicarius/modules/3D_phase_1/manifest.csv` gains one append-only row per artifact event (`frames created`, `dem created`, `report created`, `psx updated`, `psx renamed`), each with timestamp, module version, project, model id, path, and details.

## `status.csv` columns

One row per timepoint, written by `config.initialize_tracking` and `config.update_tracking`. Identity comes first: column 1 is `original_videos` and column 2 is `readable_id`, both written before any frame is extracted. The full header, in order:

    1  original_videos                 12 Step 0 end time                23 Report file
    2  readable_id                     13 Step 0 processing time (s)     24 Step 1 error time
    3  Model ID                        14 Frames directory               25 Step 2 complete
    4  Status                          15 Step 0 error time              26 Step 2 site
    5  Step 0 complete                 16 Step 1 complete                27 Step 2 consolidation time
    6  Video Length (s)                17 Step 1 start time              28 Step 3 complete
    7  Total Video Frames              18 Step 1 end time                29 Step 3 scale method
    8  Frames Extracted                19 Step 1 processing time (s)     30 Step 3 scale applied
    9  Video Source                    20 Aligned cameras                31 Step 3 ortho exported
    10 Extraction Timestamp            21 Total cameras                  32 Step 3 model exported
    11 Step 0 start time               22 PSX file                       33 Step 3 processing time

    34 Step 4 complete                 38 Step 4 processing time         42 Scale Bars
    35 Step 4 web published            39 Scale                          43 Cameras Removed
    36 Sketchfab URL                   40 Scale Error (m)                44 Notes
    37 Step 4 high-res exported        41 Scale Error (ppm)

`Model ID` repeats the readable id and is what the row is looked up by. Columns 25 to 38 are the inherited step 2 to step 4 shape and stay blank in this module. `Notes` is appended to rather than replaced (`status_note`), capped at 500 characters, so recording a failure does not erase what an earlier step wrote there.

## How to run

Launch through VICARIUS. Do not call `step0.py` or `step1.py` directly.

### From the UI

1. Open the 3D Phase 1 module page from the module launcher.
2. The page offers only "Run in terminal", because `module.yaml` sets `ui.run_mode: terminal`. A multi-hour Metashape run should not be tied to a browser tab.
3. For a TCRMP run, leave both directory fields blank; they are labelled "(non-TCRMP mode only)" for that reason. Fill in the run purpose, then set the parameters you want. To scope the run, put readable ids in `ids`, or a code in `site` and `transect`. To reprocess a finished timepoint, tick `force`.
4. For a non-TCRMP run, untick `tcrmp`, then fill "Video or frame source directory" and "Project workspace directory".
5. Click "Run in terminal". The runner assembles the command from `cli.flag_map` so the script never prompts.
6. Pause and Resume are available (`ui.supports_pause: true`). Pause finishes the current timepoint, saves, releases the GPU and the project lock, and exits with code 42, which the runner records as paused rather than failed. Resume relaunches; the idempotent steps skip what is done.

The argv the runner builds, in `flag_map` order:

    python3 src/run_phase1.py [--input <in>] [--project <out>] --purpose "<purpose>" \
      --yes --skip-vim [--tcrmp | --no-tcrmp] [--ids <a,b>] [--site <S>] \
      [--transect <T#>] [--force]

`--input` and `--project` are emitted only when the corresponding field is non-empty. When `--project` is empty (the TCRMP case) the runner also appends one `--param <analysis_path>=<value>` per parameter that carries an `analysis_path` and has a value, after the flags above; `param_flag: "--param"` in `cli.flag_map` is what turns that on. `tcrmp` is a boolean with both a `tcrmp_flag` and a `tcrmp_off_flag`, so it always emits one of `--tcrmp` or `--no-tcrmp`. `force` has no off flag; unticked, it emits nothing. `ids`, `site`, and `transect` emit nothing when blank. `--yes` skips the summary confirmation and `--skip-vim` skips the editor step. Every prompt in `run_phase1.py` (the purpose question, the summary confirmation, the vim edit step, the Metashape path, and the non-TCRMP input/project fallback) also checks `sys.stdin.isatty()` and falls back to a default or a clear error instead of calling `input()` whenever stdin is not a terminal, which it never is for a UI-launched run. So no code path launched from the UI ever waits on a console prompt, even if a flag such as `--purpose` were missing.

Twelve parameters appear on the form. The eight that carry an `analysis_path` (`tcrmp`, `frames_per_transect`, `max_chunks_per_psx`, `use_gpu`, `scale_error_threshold`, `decimation_factor`, `smooth_strength`, `texture_pixel_size`) reach the project's `analysis_params.yaml` (`ui.params_writethrough: true`), by one of two routes depending on whether the project directory exists at launch:

- **Non-TCRMP run** (both directory fields filled). The runner writes the values straight into `<project>/analysis_params.yaml` before the script starts, seeding the file from this module's template when the project is new.
- **TCRMP run** (both directory fields blank). There is no project directory yet: each timepoint's processing folder is minted mid-run from the template, beside its own video's folder. So the runner appends one `--param <analysis_path>=<value>` per parameter to the command line instead (the flag it uses is `cli.flag_map.param_flag`, which is how a module tells the runner its script accepts one), and `run_phase1.py` applies each one into the folder's `analysis_params.yaml` immediately after preparing it, before step 0 reads anything. The write is section aware (`set_yaml_path`), so `processing.step1_products.smooth_strength` and `processing.metashape.defaults.smooth_strength` are different keys, and every comment and every other key in the file survives.

The other four (`force`, `ids`, `site`, `transect`) are launch-time selection only and reach the script as flags.

### From the CLI

For a TCRMP run, call the script directly, because `vicarius run` requires `--input` and `--output` and the registry path needs neither:

    cd /mnt/rip/vicarius_drive/vicarius/modules/3D_phase_1/github_repo
    python3 src/run_phase1.py --ids MRS_T1_2023ann \
      --purpose "MRS T1 2023 annual, first reconstruction" --yes --skip-vim

    python3 src/run_phase1.py --site MRS --transect T1 \
      --purpose "MRS T1 backfill" --yes --skip-vim

    python3 src/run_phase1.py --purpose "every pending timepoint" --yes --skip-vim

Add `--param` (repeatable) to change a parameter for this run without editing the template: `--param processing.frames_per_transect=300 --param processing.step1_products.decimation_factor=20`. The path is the dotted key in `analysis_params.yaml`, matched section by section, and the value is written into each processing folder's copy of the file before step 0 reads it.

For a non-TCRMP run, the launcher route works because both directories are real:

    vicarius run 3D_phase_1 \
      --input  /path/to/videos \
      --output /path/to/workspace \
      --purpose "rig calibration footage" \
      --param  frames_per_transect=100

`vicarius run` forwards each repeatable `--param key=value` into the project's `analysis_params.yaml`, and forwards the module's `cli.flag_map` flags exactly as the UI does. Add `--param tcrmp=false` for the non-TCRMP path, or call the script directly with `--no-tcrmp --input ... --project ...`.

Every flag is optional in a bare shell: `python3 src/run_phase1.py` with no arguments, run at an actual terminal, runs in TCRMP mode over every pending timepoint, prompts for the purpose, opens `analysis_params.yaml` in vim, and asks for confirmation before starting. Run the same bare command with stdin not a terminal (piped, redirected from `/dev/null`, or under `--yes`) and it uses the default purpose, skips the vim step, and, for the summary confirmation, aborts rather than blocking; nothing calls `input()`.

## What happens inside

Per launch, in order. The stage name in brackets is what the module writes into the registry's `stage` column, and what the atlas shows.

1. **Detect Metashape.** `METASHAPE_PATH`, then `/home/bizon/applications/metashape-pro_2_2_2_amd64/metashape-pro/metashape`, then `metashape` on `PATH`, then, only at an interactive terminal and without `--yes`, a prompt for the path by hand; under `--yes` or a non-interactive stdin it raises `Metashape Pro not found` instead. Nothing is downloaded or installed.
2. **Select rows** from the registry (TCRMP) or read the input folder (non-TCRMP), and print what was selected and what was skipped, with the reason.
3. **Open a VICARIUS run** under `vicarius/modules/3D_phase_1/inprocess/run_{YYYYMMDD_HHMMSS}/`, with each processing folder symlinked into its `outputs/`, and log a `process_start` event.
4. Then, per timepoint:
5. **Prepare the processing folder** `[starting]`. Find or create the site and transect's folder beside the video's folder (a sibling of it, never inside it), create `console/`, `frames/`, `reports/`, copy `analysis_params.yaml` if absent, set `processing.tcrmp: true` in it, build the `.venv` if the folder does not have one yet, write `processing_folder`, `processing_location`, and `console_log` to the registry, and write the identity row into `status.csv`. Identity is on record before anything else runs.
6. **Step 0, frame extraction** `[extracting]`. For every registry row whose `processing_location` is this folder and whose `Step 0 complete` is not `True`: write identity and the stage, probe the video with ffprobe, extract `frames_per_transect` frames at `frames_per_transect / duration` fps as uncompressed 16-bit `rgb48le` TIFFs into `frames/{readable id}/` named after the source video, then write the timings, the frame count, and the video's size, duration, and container/codec facts back to `status.csv` and the registry.
7. **Step 1 preflight.** Refuse to start when `TMPDIR` (or `/tmp`) has less than 50 GB free (`STEP1_MIN_TEMP_FREE_GB`), then take an exclusive `flock` on `.processing.lock` stamped with pid, step, host, and start time. A second step against the same folder is refused with the holder printed.
8. **Resolve the psx** for this timepoint (registry-preferred, then newest range-named, then a new single-year bundle), open or create the document, save the still-empty document so project storage exists for the DEM, and drop any stale chunk already carrying this label.
9. **Add photos and match** `[matching]`. `addPhotos` on the frame list, then `matchPhotos` at `downscale 1`, 40,000 keypoints, no tie point limit, no preselection, stationary points filtered.
10. **Align** `[aligning]`. `alignCameras` with adaptive fitting off, then a second pass over any camera that still has no transform, then `resetRegion`.
11. **Clean tie points** `[filtering]`. The capped iterative selection: ReconstructionUncertainty at 15 capped at 50 percent of the cloud, then ProjectionAccuracy at 5 capped at 50 percent, then ReprojectionError at 0.5 capped at 10 percent. Each threshold widens by 1.25 until the selection fits under its cap (at most 12 probes), and `optimizeCameras` runs over the fixed calibration set after each criterion.
12. **Rotate the region** to the bounding box, preserving the current scale.
13. **Depth maps** `[depth_maps]`. Ultra High (`downscale 1`), Mild filtering, 16 neighbours, `reuse_depth=False`, `subdivide_task=True`.
14. **Mesh** `[mesh]`. `buildModel` from the depth maps, Arbitrary surface, HighFaceCount, Enabled interpolation. A chunk that comes back with no model raises and the timepoint fails rather than advancing. Then `removeComponents(99)` clears floating specks.
15. **Scale** `[scaling]`. Detect `CircularTarget20bit` targets, remove markers not named in `scale_bars`, prune each marker's worst projections, build the two 0.75 m bars, `updateTransform`, and compare the mean absolute bar residual against 0.009 m. See the if/then below.
16. **DEM** `[dem]`. `buildDem` from the still-present depth maps at native resolution, kept inside the psx. No raster is exported.
17. **Delete depth maps** `[delete_depth_maps]`, once the DEM exists.
18. **Delivery mesh** `[decimating]`. Decimate to full faces divided by 10 with `replace_asset=True`, smooth at strength 4 with borders fixed, then remove any mesh asset that is not the active one so the chunk holds exactly one mesh.
19. **Texture** `[texturing]`. Compute the page count from mesh area at 0.5 mm per texel (or use the fixed fallback), `buildUV` and `buildTexture` at 8192 with the GPU freed for the blend. A chunk that comes back with no texture raises.
20. **Report** `[report]`. `chunk.exportReport` to `reports/{readable_id}_step1.pdf`.
21. **Save** `[saving]` and **verify** `[verifying]`. Save the document, then reopen the bundle in a throwaway read-only document and confirm the chunk exists with a model and at least one texture. Only then does `Step 1 complete` flip to `True`. A verification that fails leaves the row at `False` with the reason in `Notes` and marks the registry row failed.
22. **Finalize.** Final save, read the chunk labels and years, release every reference to the document, rename the bundle to its new year range, update every `PSX file` and `psx_file` cell that moved, then write the run's numbers to the registry `[done]`: `step1_status=complete`, timings, tie points, full and delivery face counts, texture pages, DEM mm per pixel, scale status and error in mm and ppm, bar count, `params_summary`, processing folder size in GB, `manual_edit_status=awaiting`, and a snapshot. A failure anywhere in finalization is recorded against the timepoint `[failed]` and does not take the run down, because the reconstruction is already saved and verified on disk.
23. **Completeness sweep.** Walk `status.csv` once more and re-verify every row claiming `Step 1 complete`; any row that no longer verifies on disk is reset to needs-rerun with a note.
24. **Manual gate.** Print the checklist and exit 0.

The stage values the registry ever holds are, in order: `starting`, `extracting`, then the thirteen step 1 stages `matching`, `aligning`, `filtering`, `depth_maps`, `mesh`, `scaling`, `dem`, `delete_depth_maps`, `decimating`, `texturing`, `report`, `saving`, `verifying`, and finally `done` or `failed`. Each write also stamps `step` and `stage_started`, which is what the atlas's elapsed clock counts from.

## Scale: if, then, and the fallback

`scale_utils.apply_scale` returns `(status, mean_abs_error_m, bars_added)` and never raises out of step 1; an exception inside it is logged as a warning and the timepoint keeps the failed-scale branch.

| If | Then |
|---|---|
| Two or more bars built and the mean absolute bar residual is strictly under `scale_error_threshold` (0.009 m) | `PASS`. The chunk is in metres. The DEM resolution is reported in real units. The texture page count is computed from mesh area at `texture_pixel_size`. The registry gets `scale_status=PASS`, `scale_error_mm`, `scale_error_ppm`, and `scale_bars`. |
| One bar built, or two bars whose mean residual reaches the threshold | `MANUAL_NEEDED` with the measured numbers recorded in mm and ppm. |
| No bars built: targets absent, detection returned nothing, `scale_bars` empty, `has_coded_scales` false, or `scale_in_step1` false | `MANUAL_NEEDED` with the sentinel error 999.0. `status.csv` keeps `999.000000` so the operator can see what the pass returned; the registry's `scale_error_mm` and `scale_error_ppm` are left blank so a non-measurement never charts as a real reading. |

In every `MANUAL_NEEDED` case the run continues to completion. Three things change:

1. **Texture falls back to a fixed page count.** Mesh area in arbitrary chunk units cannot size an atlas, so `compute_texture_pages` returns `unscaled_page_count` (4 pages of 8192) instead of the computed value. Four pages is above what a typical scaled transect resolves to, so the unscaled texture is never coarser than the scaled one would have been.
2. **The DEM is built anyway**, in local units, and its resolution line is flagged `(unscaled units)` in the log and in the `status.csv` `Notes` cell.
3. **The timepoint is announced twice.** Step 1 prints a boxed `MANUAL SCALE NEEDED` line at the moment of the decision and appends the same sentence to the registry's `notes` column, and the manual gate lists it again under MANUAL SCALE with the bar count and the error in metres. The atlas surfaces it from `scale_status`.

Scale never stops a run and never blocks the next timepoint.

## Manual gate

Step 1 ends by writing `manual_edit_status = awaiting` to every timepoint it completed and printing two blocks. The first, from `print_manual_gate` in `step1.py`, is the per-timepoint summary:

    ======================================================================
     STEP 1 COMPLETE - MANUAL EDIT GATE
    ======================================================================
      Folder: /path/to/MRS_T1_2023ann_3dprocessing
        MRS_T1_2023ann
          psx:   MRS_T1_2023_2023.psx
          scale: PASS (2 bar(s), 1787 ppm)

      MANUAL SCALE:
        MRS_T1_2024ann: 1 bar(s), error 999.000 m
    ======================================================================

The second, from `print_manual_instructions` in `run_phase1.py`, is the checklist. Per chunk in the psx:

**Straightening and cropping (always required)**

1. Load the textured model.
2. Auto-adjust brightness and contrast on an image to improve the texture.
3. Switch to the rotate model view.
4. Rotate the model so it aligns horizontally at the top of the view.
5. Use Model > Region > Rotate Region to View to set the alignment.
6. Resize the region to crop to the model area, using the top XY and side views.
7. Use the rectangular crop tool to crop within the region bounds.

**Scaling check**

1. Confirm the coded targets are visible and correctly positioned.
2. Confirm two scale bars' worth of targets are clearly visible. If the timepoint came back `MANUAL_NEEDED`, place or repair the bars by hand now and re-apply the scale.

**Finish**

1. Save the project and quit Metashape.

The atlas shows every awaiting timepoint under "Ready for manual editing" with a one-click copy of the folder path. Step 2 clears the notice by flipping `manual_edit_status` to `done` with the edit time taken from the psx modification time.

## Gotchas

- **`frames_per_transect` must stay below the source frame count.** Step 0 turns the count into an ffmpeg `fps` filter rate of `frames_per_transect / duration`. Ask for more frames than the source holds and ffmpeg duplicates frames to fill the rate, which adds cameras without adding information and quietly degrades the alignment. A 4 to 7 minute transect at 59.94 fps holds about 14,400 to 25,200 frames, so the default 1000 has enormous headroom; the trap is a short clip, a proxy, or a test video. Set `0` to mark a timepoint complete with no frames at all.
- **Edit the registry CSV in a spreadsheet only while nothing is running.** `registry.py` takes an exclusive lock and replaces the file atomically, which protects the programmatic writers from each other. It cannot protect anything from a spreadsheet application that saves the whole file over the top after the lock is released, silently dropping whatever a running module wrote in between. Use the atlas for edits during a run; it goes through the same library and holds the same lock.
- **`--force` clears two cells, not the whole timepoint.** `select_rows` lets a `step1_status = complete` row through, then `status_rows.reset_step1` sets `Step 1 complete` to `False` and `Status` to `Forced rerun` in `status.csv`, because step 1 skips on its own status cell and would otherwise do nothing. Everything else is left for the rerun to overwrite, so a forced rerun that fails does not erase the record of the run that succeeded. `--force` does not re-extract frames: step 0 still skips on `Step 0 complete = True`, so delete the timepoint's `frames/` subfolder and clear that cell if you want new frames. The forced chunk goes back into the bundle it already lives in (`preferred_psx` puts this timepoint's own row first) and the stale-chunk swap replaces it, so the chunk count does not grow and the psx range does not move.
- **`--force` is also the recovery for a registry row marked `failed` whose reconstruction actually succeeded.** Step 1 verifies the saved psx on disk before flipping `Step 1 complete`, but a failure in the finalization that follows (the range rename, the write-back, the snapshot) marks the registry row `failed` while `status.csv` already says complete. The bytes are good; only the record disagrees. Rerun that timepoint with `--force`, which clears the `status.csv` verdict and lets step 1 rewrite the registry row from the rebuilt chunk.
- **A chunk with manual edits is not rebuilt without `--force`.** When the registry says `manual_edit_status = done` and the psx already holds a chunk labelled with that timepoint, step 1 leaves it alone, prints `skipped: manual edits present; use --force to rebuild`, and writes the same line into the registry notes. Straightening, cropping and hand scaling live only in that chunk, so the swap that would rebuild it has to be asked for.
- **A `status.csv` whose header does not match the current schema is migrated, not truncated.** Point this module at a folder an older 3D module processed and `initialize_tracking` copies the file to `status.csv.pre_<TIMESTAMP>.bak`, carries every row onto the new header by column name (new columns blank, columns only the old header had appended to `Notes` as `migrated: <column>=<value>`), and logs a WARNING naming the backup.
- **A row with a non-local `video_location` is skipped, not fetched.** The sync driver does not exist yet. Until it does, make the video local and correct `video_location` in the atlas.
- **Multi-part recordings must be merged before ingest.** A `;`-joined `original_videos` cell fails the row at step 0 with a pointer to `atlasprep.md`.
- **One step per processing folder at a time.** `.processing.lock` refuses a second step 0 or step 1 against the same folder and prints who holds it. Delete the lock file only when you are certain no run is active.
- **Step 1 refuses to start with less than 50 GB free on the temp volume.** Metashape spills depth map pyramids there. `export TMPDIR=/path/with/space` before launching, or raise the floor with `STEP1_MIN_TEMP_FREE_GB`.
- **A psx rename can be refused.** If either half of the target name already exists, the move is refused, the bundle keeps its current name, and the conflict is written into the registry `notes` of every timepoint in that run. That is a name collision to resolve by hand, not a failed reconstruction.
- **Renaming a `readable_id` in the atlas renames nothing on disk.** Folders, psx files, frame directories, and chunk labels keep the old id. Rename before processing, or accept the mismatch.
- **VICARIUS process logging needs `VICARIUS_ROOT`.** `run_phase1.py` looks for the logging library under `$VICARIUS_ROOT/_logging/src` with a built-in default that does not exist on this box, so without the variable exported the import fails quietly and the run is not written to the platform log. The registry and `status.csv` are unaffected. Export `VICARIUS_ROOT=/mnt/rip/vicarius_drive/vicarius` to get the log events; the UI launcher already does.
- **`open_params_for_editing` lists a `processing.chunk_size` key that no longer exists.** Ignore that line in the vim banner; the key was dropped from the v1.0.0 parameter set.

## Resume from cold

A person with no prior context can set up, run, and verify this module from this section alone.

### Environment

Step 0 and step 1 run under two different interpreters.

- **Step 0** runs under the processing folder's own `.venv`, built from system Python 3.9. Install `python3.9`, `python3.9-venv`, and `python3.9-dev` first; the module creates `<folder>/.venv` and installs `requirements.txt` into it. Step 0 also shells out to `ffmpeg` and `ffprobe`, so both must be on `PATH`. Hardware decode is used for h264, hevc, av1, and vp9 sources when `nvidia-smi -L` succeeds, and software decode otherwise or on any failure.
- **Step 1** runs under the bundled Python of Agisoft Metashape Pro, invoked as `metashape -r src/step1.py <folder>` with the venv's `site-packages` on `PYTHONPATH`. Metashape Pro is commercial software sold by Agisoft (agisoft.com) and this module never downloads, installs, or activates it. `detect_metashape()` only locates a copy that is already installed and license-activated, in this order: `$METASHAPE_PATH`, then `/home/bizon/applications/metashape-pro_2_2_2_amd64/metashape-pro/metashape`, then `metashape` on `PATH`, then, only at an interactive terminal and without `--yes`, an interactive prompt for the path. With no executable, or under `--yes` or a non-interactive stdin, it raises `Metashape Pro not found` and step 1 never starts.
- Set Metashape up once: obtain a Professional license from Agisoft, install version 2.1.1 or newer (older versions use an incompatible Python API), and activate the license so the program launches without a prompt. Then leave the executable at the default path above, point `METASHAPE_PATH` at it, or put it on `PATH`. A GPU is required.
- **The shared registry library** must be reachable. `registry_client`, `videos`, and `step1.py` add `$VICARIUS_ROOT/_METADATA/3d` to `sys.path`, where `VICARIUS_ROOT` defaults to `/mnt/rip/vicarius_drive/vicarius`. When the import fails, a TCRMP launch stops with a message telling you to pass `--no-tcrmp` instead. Tests and the fabricated corpus point `VICARIUS_3D_REGISTRY_ROOT` at a scratch directory so nothing touches the real CSV.
- **Export `VICARIUS_ROOT=/mnt/rip/vicarius_drive/vicarius`** before a bare-shell run if you want the run written to the platform process log.

### Inputs and outputs

For a TCRMP run there is nothing to prepare beyond the registry: the rows carry the video paths, and the processing folder is created beside the video's folder the first time. For a non-TCRMP run the input is a read-only directory of videos or of frame subfolders, and the output is the project directory you pass to `--project`; everything the module writes goes inside it, laid out exactly as the folder contract above describes. Either way the module also writes a provenance run folder under `vicarius/modules/3D_phase_1/inprocess/run_{YYYYMMDD_HHMMSS}/`, with each processing folder symlinked into its `outputs/`.

### Run

    # TCRMP, one timepoint, hands-free
    cd /mnt/rip/vicarius_drive/vicarius/modules/3D_phase_1/github_repo
    python3 src/run_phase1.py --ids MRS_T1_2023ann --purpose "why" --yes --skip-vim

    # Non-TCRMP smoke test, 100 frames
    vicarius run 3D_phase_1 --input /path/to/videos --output /path/to/workspace \
      --purpose "smoke test" --param tcrmp=false --param frames_per_transect=100

### Verify success

The run succeeded when all of these hold:

1. The terminal printed `STEP 1 COMPLETE - MANUAL EDIT GATE`, then `PHASE 1 COMPLETE - MANUAL STEP REQUIRED`, and the process exited 0.
2. In the folder's `status.csv`, every timepoint row shows `Step 1 complete` = `True` and `Status` = `Step 1 complete`. No row shows `Step 1 save verification failed` or `Step 1 sweep failed`.
3. The folder holds one range-named psx whose name spans the years of the chunks in it, plus a `reports/{readable_id}_step1.pdf` per timepoint and a `console/step1_*.log` for the run.
4. In the registry, each timepoint shows `step1_status = complete`, `stage = done`, `manual_edit_status = awaiting`, a `psx_file` that exists on disk, non-empty `tie_points`, `faces_full`, `faces_delivery`, `texture_pages`, `dem_mm_per_pix`, `processing_size_gb`, and a `snapshot_dir` that holds `snapshot.yaml`, the params file, and the report.
5. Opening the psx in the Metashape GUI shows one chunk per timepoint, each labelled with its readable id, each with exactly one mesh, a texture, and an elevation, and no depth maps.

A paused run exits 42 and is recorded as paused. Relaunch with the same selection to resume: step 0 skips a timepoint whose `Step 0 complete` is `True`, step 1 skips one whose `Step 1 complete` is `True`, so both are idempotent per timepoint.

Phase 1 is complete only after the manual gate. Open the psx, straighten and crop every chunk, confirm the targets, fix any `MANUAL_NEEDED` scale by hand, and save.

## Field Methods Guide

Carried over from the `3D_vicarius` project documentation, Part 2, and edited for currency. This is what happens before the module ever sees a file: the camera system, the equipment, the pre-dive preparation, the in-water sequence, and the maintenance that keeps the footage usable.

### Required Materials

- Camera system:

  - Camera with lights
  - Memory card (CF Express)
  - Camera housing
  - External battery pack
  - Strobe light batteries
  - Camera lens
  - Cinema camera gear
  - Handle with clips and rope
- Field equipment:

  - Scale bars (2)
  - Field box containing:
    - Extra towels
    - O-ring grease
    - Cleaning materials
    - Dry towels
  - Slate
  - Vacuum device for housing seal check

### Camera Setup and Maintenance

#### Regular Maintenance

- Camera cinema gear maintenance
- Camera settings verification
- Programmable button configuration
- Housing maintenance (every few weeks or if leaks detected):
  - O-ring greasing

#### Pre-Dive Preparation

1. Day before:

   - Check housing and o-rings
   - Charge camera
   - Charge external battery pack
   - Charge strobe light batteries
   - Initialize media on memory card
2. Morning of:

   - Camera sealing procedure:

     1. Install battery and memory card
     2. Attach lens and verify autofocus is on
     3. Remove lens cap and check for smudges
     4. Prepare housing for camera insertion
     5. Seat camera in housing using cinema camera gear
     6. Connect external battery
     7. Final housing checks:
        - Turn on alarm
        - Check for smudges on housing lens
        - Verify o-ring condition
        - Close housing
        - Use vacuum device until light turns green
   - Equipment verification:

     - Camera and memory card
     - Housing
     - Field box with supplies
     - Slate
     - Scale bars (2)
     - Handle with clips and rope
   - Camera settings verification:

     - CP file: C2 (Canon log 3 / C.Gamut Color matrix neutral)
     - Sensor mode: full frame
     - Frequency: 59.94hz
     - Recording: RAW LT
     - Destination: CFexpress
     - Frame rate: 59.94 fps

### In-Water Procedures

#### Start of Dive

1. **B**uttons: Press all buttons to prime them
2. **P**ower:
   - Turn on camera and lights (for Kraken lights, hold in/out buttons 1s, press middle button)
   - Put lights to sleep (hold center 2s)
3. **L**eaks: Monitor green light. If it turns red, return to boat

#### Transect and Camera Setup

1. **S**cale bars:

   - Place at each end of transect
   - One scale bar should be at roughly a 45 degree angle to the transect, the other parallel to it. Set both where they will not move or wobble at all.
   - Ensure circular targets are **visible** in footage and that scale bars **never move** during filming
   - If scale bars move or get moved before filming ends, the film is useless and needs to be redone (restart filming right away if time and gas allow).
2. **T**ime code: Reset (Mode button)

   - This resets the time code to zero, so timing stays consistent.
3. **A**rms: Extend to position lights as far apart as possible
4. **L**ights: Turn on (for Kraken lights, hold Center Button 2 sec)
5. **W**hite balance: Press Button 13, hold camera over white part of scale bar
6. **E**xposure:

   - You should see the waveform monitor (WFM) on the screen. If not, press "Disp" to cycle through the menu, or press button 6 to open the WFM
   - Use the ISO dial (top of camera, next to the vacuum valve) to slightly overexpose (peaks should barely exceed 100% on the WFM). ISO should ideally be under 10000 to avoid noisy footage
7. **A**ltitude:

   - Position camera so the viewfinder covers the length of the scale bar
   - Note the altitude (height off bottom) of the camera when viewing the entire viewfinder (should be about 70 cm)
   - Maintain this altitude throughout filming
8. **R**ecord:

   - Press Record button
   - Show transect number

#### Filming Sequence (4-Pass Method)

Each pass should be approximately 10 meters long and take about 1 minute, maintaining consistent altitude.

1. **Pass 1**:

   - Start at one end
   - Camera facing straight down
   - Transect line visible in left quarter of viewfinder
2. **Pass 2**:

   - Turn around
   - Camera facing straight down
   - Position slightly away from transect line
   - Viewfinder should see 1 m distance from transect
   - Maintain about 0.5 m overlap with Pass 1
   - Position approximately arm's length from transect
3. **Pass 3 and 4**:

   - Move about 20 cm from the pass 1/2 position
   - Tilt camera 45 degrees
   - Capture angled view of transect from either side

After filming a transect, if using Krakens, press the center button on each light for 2 s to put the lights to sleep. If using Keldans, turn the dial to off.
After the dive, turn off the Krakens (hold inner and outer buttons for 2 s) or, for Keldans, turn the dial to off and lock. Turn off the camera.

### Offloading the memory card each day

Not yet written down.

### Encoding CRAW video

Not yet written down. Whatever the encode produces, the module reads it: containers and codecs are probed with ffprobe, never assumed from the extension, and hardware decode is chosen per codec.

## Provenance and links

- Repo: `vicarius/modules/3D_phase_1/github_repo` (branch `main`, no remote). Upstream lineage: `github.com/laurenkolinger/3D_vicarius`, then the module `3D_phase1`, then `3D_init`, both frozen and archived under `vicarius/modules/_archive/`.
- Design spec: [`docs/superpowers/specs/2026-08-26-3d-phase-1-registry-atlas-design.md`](../../../../docs/superpowers/specs/2026-08-26-3d-phase-1-registry-atlas-design.md)
- Implementation plan: [`docs/superpowers/plans/2026-08-26-3d-phase-1-registry-atlas.md`](../../../../docs/superpowers/plans/2026-08-26-3d-phase-1-registry-atlas.md)
- Registry contract: [`vicarius/_METADATA/3d/README.md`](../../../_METADATA/3d/README.md)
- Atlas module: [`vicarius/modules/tcrmp_3d_atlas/github_repo/README.md`](../../tcrmp_3d_atlas/github_repo/README.md), served at `/atlas` in vicarius_ui_os
- Appending future timepoints: [`../APPENDING.md`](../APPENDING.md)
- Step 2 handoff: [`../STEP2_HANDOFF.md`](../STEP2_HANDOFF.md)
- Related studies: `S2_3D_structure`
- Related docs: the 2026-05 FLC T6 incident write-up at `vicarius/_DOCS/archive/incidents/INCIDENT_2026-05_parallel_step1_flc_t6.md` (the reason for the temp-space preflight, the project lock, the save-then-verify gate, and the completeness sweep), and the module system overview at `vicarius/_DOCS/reference/MODULE_REGISTRY_GUIDE.md`.
- Data-dictionary descriptors: `3D_phase_1_psx`, `3D_phase_1_step1_reports`.
- Parameter references: Agisoft Metashape Professional 2.2 User Manual and Metashape Python API Reference 2.2.2, cited by printed page throughout the parameter chapter.
- Tests: `python3 -m unittest discover -s tests -q` from `github_repo/` covers the pure logic (naming, psx range naming and rename, ffprobe helpers, scale decisions and ppm, capped selection, registry client, folder setup, manifest, config layout). Metashape-touching code is exercised by the quarter-resolution integration corpus under `/mnt/tear/temp/3D_phase_1_test/`.

## Changelog

- v1.0.0 (2026-08-27): First release of `3D_phase_1`, built from the validated `3D_init` work. Registry-driven TCRMP mode: timepoints are selected from `vicarius/_METADATA/3d`, processed in a folder beside their video's folder with nothing copied or symlinked, and every stage, number, size, and snapshot is written back. One psx per site and transect, range-named and renamed on the filesystem as timepoints append, with a chunk cap of 4. Identity-first `status.csv` (`original_videos`, `readable_id`) written before any frame is extracted. Container and codec detection with ffprobe and codec-aware CUDA decode, replacing extension matching and blanket hardware acceleration. Stricter alignment (full-resolution matching, 40,000 keypoints, no tie point limit, stationary points filtered, adaptive fitting off, capped iterative tie point selection with re-optimization after each criterion, fixed eight-parameter calibration). Scale never blocks: `PASS` or `MANUAL_NEEDED`, mean absolute bar error recorded in mm and ppm, texture falling back to four fixed pages when the scale is unverified. DEM built into the psx with no raster export and no orthomosaic; depth maps deleted after it. One delivery mesh per chunk, decimated by 10 and smoothed at strength 4. Manual gate writes `manual_edit_status = awaiting` and prints the per-chunk checklist. Non-TCRMP path preserved with original names and no registry writes.
- Predecessor history: `3D_phase1` v0.1.0 (2026-02-13, initial VICARIUS integration) and v0.2.0 (2026-05-15, temp free-space preflight, exclusive project lock, save-then-verify, raise-on-missing-model, completeness sweep) are recorded in the archived module's own README.
