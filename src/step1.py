"""
Step 1: Isolated 3D Processing

Each batch is completely processed and saved before moving to the next batch.
Documents are properly closed between batches to prevent interference.
"""

import os
import logging
import Metashape
import datetime
import math
import pandas as pd
import time
import sys
import traceback
import fcntl
import shutil
import socket
from config import (
    DIRECTORIES,
    PROJECT_NAME,
    METASHAPE_DEFAULTS,
    USE_GPU,
    PARAMS,
    update_tracking,
    get_transect_status,
    TIMESTAMP
)
import scale_utils
import selection_utils
import manifest

# Minimum free space on the temp volume (TMPDIR or /tmp) before we start.
# Metashape spills depth_maps_pyramids here; running out mid-build is the
# original FLC T6 failure mode (see _DOCS/archive/incidents/INCIDENT_2026-05_parallel_step1_flc_t6.md).
MIN_TEMP_FREE_GB = int(os.environ.get("STEP1_MIN_TEMP_FREE_GB", "50"))

# Print all directory paths for debugging
print("DEBUG: Directory paths:")
for key, path in DIRECTORIES.items():
    print(f"  {key}: {path}")
    # Check if directory exists
    if os.path.exists(path):
        print(f"    [EXISTS]")
    else:
        print(f"    [DOES NOT EXIST]")
        try:
            os.makedirs(path, exist_ok=True)
            print(f"    [CREATED]")
        except Exception as e:
            print(f"    [FAILED TO CREATE: {str(e)}]")

# Try to create each directory explicitly
print("DEBUG: Attempting to create all directories:")
for key, path in DIRECTORIES.items():
    try:
        print(f"Creating directory: {path}")
        os.makedirs(path, exist_ok=True)
        print(f"  Success!")
    except Exception as e:
        print(f"  Error creating {path}: {str(e)}")
        traceback.print_exc()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(DIRECTORIES["logs"], f"step1_isolated_{PROJECT_NAME}_{TIMESTAMP}.log")),
        logging.StreamHandler()
    ]
)

# Maximum number of chunks per PSX file
MAX_CHUNKS_PER_PSX = PARAMS['processing'].get('max_chunks_per_psx', 5)

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
    Caller must keep the returned file object alive — closing it releases
    the lock. Stamps the file with PID, step name, hostname, and start time
    so the holder is visible if a future run is blocked.
    """
    lock_path = os.path.join(DIRECTORIES["base"], ".processing.lock")
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
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
        f"started={datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    lock_fp.write(stamp)
    lock_fp.flush()
    logging.info(f"Acquired project lock at {lock_path} ({stamp.strip()})")
    return lock_fp


def check_temp_free_space(min_gb=None):
    """Refuse to start if TMPDIR (or /tmp) has less than min_gb free.

    Metashape's depth_maps_pyramids intermediates land in TMPDIR. Running
    out of space mid-build leaves a half-saved PSX (the original 2026-05
    incident). This is a coarse preflight only — it does not guarantee
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


def verify_psx_chunk(psx_path, transect_id):
    """Open psx_path in a fresh Document and confirm the chunk is real.

    Returns True only when a chunk with label==transect_id exists with a
    built model, at least one texture, AND (when step1_products.build_dem
    is enabled, the default) a DEM export file on disk at the path
    process_transect writes it to. Anything less means the save did not
    actually land. We do this in a throwaway Document so the active one is
    untouched.
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
            products_cfg = PARAMS.get("processing", {}).get("step1_products", {}) or {}
            if products_cfg.get("build_dem", True):
                dem_path = os.path.join(DIRECTORIES["dems_output"], transect_id, f"{transect_id}_dem.tif")
                if not os.path.isfile(dem_path):
                    logging.error(f"Verify: missing DEM export {dem_path} for {transect_id}")
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
    Process a single transect through initial 3D reconstruction.
    
    Args:
        transect_id (str): The transect identifier
        chunk (Metashape.Chunk): The chunk to process
        doc (Metashape.Document): The document containing the chunk
        psx_path (str): The path to save the PSX file
        
    Returns:
        bool: Success or failure
    """
    try:
        start_time = datetime.datetime.now()
        
        # Set up GPU processing
        gpu_devices = enumerate_gpus()
        gpu_enabled = setup_gpu(gpu_devices)
        
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
        chunk.matchPhotos(
            downscale=METASHAPE_DEFAULTS["downscale"],
            keypoint_limit=METASHAPE_DEFAULTS["keypoint_limit"],
            tiepoint_limit=METASHAPE_DEFAULTS["tiepoint_limit"],
            generic_preselection=METASHAPE_DEFAULTS["generic_preselection"],
            reference_preselection=METASHAPE_DEFAULTS["reference_preselection"],
            filter_stationary_points=METASHAPE_DEFAULTS["filter_stationary_points"]
        )
        chunk.alignCameras(adaptive_fitting=METASHAPE_DEFAULTS["adaptive_fitting"])
        
        # Attempt to align any unaligned cameras
        unaligned_cameras = [camera for camera in chunk.cameras if not camera.transform]
        for camera in unaligned_cameras:
            camera.transform = None
        chunk.alignCameras(cameras=unaligned_cameras, reset_alignment=False)
        
        # Reset the region
        chunk.resetRegion()
        
        # Filter points and optimize cameras. "legacy" is the original
        # RU -> optimize -> RE -> PA sequence, byte-identical to before.
        # "noaa" runs the NOAA TM NMFS-PIFSC-159 gradual-selection scheme
        # (RU -> PA -> RE, each capped and re-optimized) from
        # selection_utils; it is an A/B alternative activated only via
        # products_cfg.gradual_selection_mode.
        gradual_selection_mode = products_cfg.get("gradual_selection_mode", "legacy")
        if gradual_selection_mode == "noaa":
            logging.info("Filtering points and optimizing cameras (NOAA gradual selection)")

            def noaa_optimize():
                chunk.optimizeCameras(
                    fit_f=True, fit_cx=True, fit_cy=True,
                    fit_b1=False, fit_b2=False,
                    fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
                    fit_p1=True, fit_p2=True,
                    adaptive_fitting=False,
                )

            selection_utils.noaa_gradual_selection(
                Metashape, chunk, METASHAPE_DEFAULTS, logging.info, noaa_optimize
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
        chunk.buildDepthMaps(
            downscale=METASHAPE_DEFAULTS["depth_downscale"],
            filter_mode=getattr(Metashape, METASHAPE_DEFAULTS["depth_filter_mode"]),
            reuse_depth=False,
            max_neighbors=METASHAPE_DEFAULTS.get("max_neighbors", 16),
            subdivide_task=True  # Split into subtasks for better GPU utilization
        )
        
        # Build model
        logging.info(f"Building model for {transect_id}")
        chunk.buildModel(
            source_data=Metashape.DepthMapsData,
            surface_type=getattr(Metashape, METASHAPE_DEFAULTS["surface_type"]),
            face_count=getattr(Metashape, METASHAPE_DEFAULTS["face_count"]),
            interpolation=getattr(Metashape, METASHAPE_DEFAULTS["interpolation"]),
            vertex_colors=METASHAPE_DEFAULTS["vertex_colors"],
            subdivide_task=True  # Split into subtasks for better GPU utilization
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
        # before marker work; 3D_init keeps the cleanup in the chain)
        chunk.model.removeComponents(99)
        logging.info("Removed small disconnected mesh components (fewer than 99 faces)")

        # Automatic scaling (flow item 5): after the model builds, as in the
        # validated manual KGC T2 workflow. On FAIL the build continues;
        # unscaled branches apply downstream and manual scaling happens at
        # the existing gate.
        scale_status, scale_error = "FAIL", scale_utils.SENTINEL_ERROR
        if products_cfg.get("scale_in_step1", True):
            logging.info(f"Scaling {transect_id} from coded targets")
            try:
                scale_status, scale_error = scale_utils.apply_scale(
                    Metashape, chunk, model_cfg, logging.info)
            except Exception as exc:
                logging.warning(f"Scaling raised {exc}; continuing without verified scale")
            update_tracking(transect_id, {
                "Scale": scale_status,
                "Scale Error (m)": f"{scale_error:.6f}",
            })

        # DEM (flow item 6), from the EXISTING Ultra High depth maps, before
        # they are deleted. No orthomosaic here: it is built later from this
        # DEM, after manual edits.
        notes_bits = []
        if products_cfg.get("build_dem", True):
            units_note = "" if scale_status == "PASS" else " (unscaled units)"
            logging.info("Building DEM from depth maps")
            dem_kwargs = {
                "source_data": Metashape.DepthMapsData,
                "interpolation": Metashape.EnabledInterpolation,
            }
            dem_resolution = float(products_cfg.get("dem_resolution", 0))
            if dem_resolution > 0:
                dem_kwargs["resolution"] = dem_resolution
            chunk.buildDem(**dem_kwargs)
            if chunk.elevation is None:
                raise RuntimeError(f"DEM build produced no elevation for {transect_id}")
            dem_dir = os.path.join(DIRECTORIES["dems_output"], transect_id)
            os.makedirs(dem_dir, exist_ok=True)
            dem_path = os.path.join(dem_dir, f"{transect_id}_dem.tif")
            dem_compression = Metashape.ImageCompression()
            dem_compression.tiff_big = True
            dem_compression.tiff_tiled = True
            dem_compression.tiff_overviews = True
            dem_compression.tiff_compression = Metashape.ImageCompression.TiffCompressionLZW
            chunk.exportRaster(
                path=dem_path,
                source_data=Metashape.ElevationData,
                image_format=Metashape.ImageFormatTIFF,
                image_compression=dem_compression,
                save_world=True,
            )
            logging.info(f"DEM exported to {dem_path} ({chunk.elevation.resolution * 1000:.3f} mm/pix{units_note})")
            notes_bits.append(f"DEM {chunk.elevation.resolution * 1000:.2f} mm/pix{units_note}")
            manifest.append_event(PROJECT_NAME, transect_id, "dem", "created", dem_path,
                                  details=f"{chunk.elevation.resolution * 1000:.3f} mm/pix{units_note}")

        # Delete depth maps (flow item 7): ~12 GB per chunk at full res and
        # nothing downstream needs them. Removal attribute per probe_results.
        if products_cfg.get("delete_depth_maps", True):
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
        decimation_factor = int(products_cfg.get("decimation_factor", 10))
        if decimation_factor > 1:
            full_faces = chunk.model.statistics().faces
            target_faces = max(1, full_faces // decimation_factor)
            logging.info(f"Decimating mesh {full_faces:,} to {target_faces:,} faces (factor {decimation_factor})")
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

        # Texture sizing (flow item 10): computed pages at 8192 from mesh
        # area when scaled; fixed pages when unscaled.
        try:
            area_m2 = float(chunk.model.area())
        except Exception:
            area_m2 = 0.0
        page_count = scale_utils.compute_texture_pages(
            area_m2,
            float(products_cfg.get("texture_pixel_size", 0.0005)),
            8192,
            scale_status == "PASS",
            int(products_cfg.get("unscaled_page_count", 4)),
        )
        logging.info(f"Building UV and texture: {page_count} page(s) of 8192 (area {area_m2:.1f})")
        chunk.buildUV(
            mapping_mode=getattr(Metashape, METASHAPE_DEFAULTS["mapping_mode"]),
            texture_size=8192,
            page_count=page_count,
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
            fill_holes=METASHAPE_DEFAULTS.get("fill_holes", True)
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
        # here — that flag flips only after the PSX save is verified on disk
        # (see process_batch). The 2026-05 FLC T6 incident wrote complete=True
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
        Metashape.app.update() # Added update after model build
        return True
        
    except Exception as e:
        error_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_msg = f"Error processing model {transect_id}: {str(e)}"
        logging.error(error_msg)
        update_tracking(transect_id, {
            "Status": "Error in Step 1",
            "Step 1 complete": "False",
            "Step 1 error time": error_time,
            "Notes": error_msg
        })
        return False

def process_batch(transects, batch_num, timestamp):
    """
    Process a single batch of transects and completely close it before returning.
    
    Args:
        transects (list): List of transect IDs to process
        batch_num (int): Batch number
        timestamp (str): Timestamp string
        
    Returns:
        dict: Mapping of processed transects to their PSX file
    """
    if not transects:
        return {}
    
    # Create psxraw directory if it doesn't exist
    os.makedirs(DIRECTORIES["psxraw"], exist_ok=True)
    
    # Use transect name as filename if only 1 transect per PSX
    if len(transects) == 1 and MAX_CHUNKS_PER_PSX == 1:
        psx_filename = f"{transects[0]}_{timestamp}.psx"
    else:
        psx_filename = f"psx_{batch_num}_{timestamp}.psx"
    
    # Create PSX file path
    psx_path = os.path.join(DIRECTORIES["psxraw"], psx_filename)
    
    # Create a new document for this batch. If psx_path already exists
    # (a same-day rerun or resume reusing this batch's filename), open it
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
            # the project -- and start this batch fresh, same as if
            # psx_path never existed.
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
        # batch fails inside chunk.buildDem with Metashape error
        # "Empty frame path" (the second model succeeds because the failure
        # path below already saved the doc). Save the still-empty document
        # now so project storage exists before any model processes.
        doc.save(psx_path)
        logging.info(f"Initial project save to {psx_path} (project storage needed for DEM builds)")

    # Results tracking
    results = {}
    
    # Process each transect in the batch
    for i, transect_id in enumerate(transects):
        # Pause boundary: stop before starting a new model. Prior models in
        # this batch were already saved+verified per-transect below, so we
        # flush the doc only if it actually holds processed chunks.
        checkpoint_pause(
            f"before model {transect_id} (batch {batch_num})",
            save_fn=(lambda: doc.save(psx_path)) if results else None,
        )

        # Skip if already processed
        status = get_transect_status(transect_id)
        if status.get("Step 1 complete", "False") == "True":
            logging.info(f"Model {transect_id} already processed, skipping...")
            continue

        logging.info(f"Processing model {transect_id} ({i+1}/{len(transects)})")

        # A chunk already labeled transect_id in an opened-existing document
        # is by definition incomplete or superseded (the complete case was
        # caught by the skip above); drop it before reprocessing so we
        # don't accumulate duplicate labels in the psx.
        stale_chunks = [c for c in doc.chunks if c.label == transect_id]
        if stale_chunks:
            doc.remove(stale_chunks)
            logging.info(f"Removed {len(stale_chunks)} stale chunk(s) labeled {transect_id} before reprocessing")

        # Create a new chunk for this transect
        chunk = doc.addChunk()
        
        # Process the transect
        success = process_transect(transect_id, chunk, doc, psx_path)
        
        if success:
            results[transect_id] = psx_path
            # Update tracking with the PSX path
            update_tracking(transect_id, {"PSX file": psx_path})

            # Create report for this transect
            try:
                # Use processing/reports_initial for step 1 reports
                reports_initial_dir = os.path.join(DIRECTORIES["processing_root"], "reportsraw")
                os.makedirs(reports_initial_dir, exist_ok=True)

                # Generate report
                report_file_path = os.path.join(reports_initial_dir, f"{transect_id}_step1.pdf")
                chunk.exportReport(report_file_path, title=f"Model {transect_id} - Step 1 Report")

                # Update tracking with report path
                update_tracking(transect_id, {"Report file": report_file_path})

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
            Metashape.app.update()
            doc.save(psx_path)

            if verify_psx_chunk(psx_path, transect_id):
                update_tracking(transect_id, {
                    "Status": "Step 1 complete",
                    "Step 1 complete": "True",
                })
                logging.info(f"Step 1 verified for {transect_id} in {psx_path}")
                manifest.append_event(PROJECT_NAME, transect_id, "psx", "updated", psx_path,
                                      details="step1 complete, verified")
            else:
                error_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_tracking(transect_id, {
                    "Status": "Step 1 save verification failed",
                    "Step 1 complete": "False",
                    "Step 1 error time": error_time,
                    "Notes": (
                        f"doc.save returned but reopen could not find chunk "
                        f"{transect_id} with model+texture in {psx_path}. "
                        f"Treat as needs-rerun."
                    ),
                })
                logging.error(
                    f"Step 1 save verification FAILED for {transect_id} — "
                    f"row left as Step 1 complete=False"
                )
        else:
            # process_transect already marked the row as failed; just save
            # the doc so any partial chunk artifacts are persisted for
            # forensic inspection.
            logging.info(f"Saving document to {psx_path} after FAILED processing of {transect_id}")
            Metashape.app.update()
            doc.save(psx_path)
    
    # Final save of the document
    logging.info(f"Final save of batch {batch_num} to {psx_path}")
    Metashape.app.update() # Keep update BEFORE final save in process_batch
    doc.save(psx_path)
    
    # Important: Clear the document reference to fully release it
    doc = None
    
    return {psx_path: list(results.keys())}

def main():
    """Process transects in completely isolated batches."""
    # Preflight: confirm temp volume has room for Metashape intermediates,
    # then take an exclusive project lock so a second step1/step2 cannot
    # race against this one (the 2026-05 FLC T6 incident root cause).
    # The lock_fp must stay open for the lifetime of main(); we bind it
    # to a local so it is released on return / exception.
    check_temp_free_space()
    lock_fp = acquire_project_lock("step1")  # noqa: F841 -- holds the flock

    # Get list of transect directories with frames
    transect_dirs = []
    frames_dir = DIRECTORIES["frames"]
    if os.path.exists(frames_dir):
        transect_dirs = [d for d in os.listdir(frames_dir)
                        if os.path.isdir(os.path.join(frames_dir, d))]

    if not transect_dirs:
        logging.error(f"No model directories found in {frames_dir}")
        return

    # Filter for unprocessed transects
    unprocessed_transects = []
    for transect_id in transect_dirs:
        status = get_transect_status(transect_id)
        if status.get("Step 1 complete", "False") != "True":
            unprocessed_transects.append(transect_id)

    if not unprocessed_transects:
        logging.info("All models have already been processed")
        return

    logging.info(f"Found {len(unprocessed_transects)} models to process")

    # Process in completely isolated batches
    timestamp = datetime.datetime.now().strftime("%Y%m%d")
    
    # Split transects into batches
    batches = []
    current_batch = []
    
    for transect_id in unprocessed_transects:
        if len(current_batch) >= MAX_CHUNKS_PER_PSX:
            batches.append(current_batch)
            current_batch = []
        current_batch.append(transect_id)
    
    if current_batch:
        batches.append(current_batch)
    
    # Process each batch in complete isolation
    batch_mapping = {}
    
    for i, batch in enumerate(batches):
        batch_num = i + 1  # Start with batch 1

        # Pause boundary: the previous batch's document is fully saved + closed
        # here, so this is the cleanest place to stop. No open doc to flush.
        checkpoint_pause(f"before batch {batch_num} of {len(batches)}")

        logging.info(f"Starting batch {batch_num} of {len(batches)}")

        # Process the batch (completely isolated from other batches)
        batch_results = process_batch(batch, batch_num, timestamp)
        
        # Merge results
        batch_mapping.update(batch_results)
        
        # Force garbage collection
        import gc
        gc.collect()

    # Post-batch sweep: walk the tracking CSV one more time and re-verify
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
            sweep_failures.append((transect_id, f"verify failed for {psx_path}"))
            update_tracking(transect_id, {
                "Status": "Step 1 sweep failed",
                "Step 1 complete": "False",
                "Notes": (
                    f"Post-batch sweep could not verify {transect_id} in "
                    f"{psx_path}; row reset to needs-rerun."
                ),
            })
    if sweep_failures:
        logging.error(
            f"Step 1 completeness sweep flagged {len(sweep_failures)} model(s); "
            f"their tracking rows were reset:"
        )
        for tid, reason in sweep_failures:
            logging.error(f"  {tid}: {reason}")
    else:
        logging.info("Step 1 completeness sweep: all complete rows verified on disk")

    logging.info("Step 1 isolated processing complete")

if __name__ == "__main__":
    main()