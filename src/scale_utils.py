"""Automatic scaling and texture sizing for 3D_init Step 1.

Scaling ported from 3D_phase2 src/step2.py: detect circular coded targets,
prune markers not named in the declared scale bars, build the bars, apply
the transform, report mean absolute scale-bar error in metres. Metashape is
passed in so tests can inject a fake.

prune_marker_projections implements the NOAA coral photogrammetry SOP
(TM NMFS-PIFSC-159, 2023) marker QC step: drop each marker's worst-error
projection until every marker's worst reprojection error is under a pixel
threshold, subject to a minimum-projections floor. It is an A/B alternative,
gated off by default (model_processing.marker_projection_prune).
"""
import math

SENTINEL_ERROR = 999.0
SCALEBAR_ACCURACY = 0.001  # metres, same value Phase 2 hardcodes


def find_marker_by_label(chunk, label):
    for marker in chunk.markers:
        if marker.label == label:
            return marker
    return None


def remove_unlisted_markers(chunk, scale_bars, log):
    keep = set()
    for bar in scale_bars:
        keep.add(bar["start_marker"])
        keep.add(bar["end_marker"])
    doomed = [m for m in chunk.markers if m.label not in keep]
    for marker in doomed:
        chunk.remove([marker])
    if doomed:
        log(f"Removed {len(doomed)} unlisted markers: {', '.join(m.label for m in doomed)}")


def add_scale_bars(chunk, scale_bars, log):
    added = 0
    for bar in scale_bars:
        start = find_marker_by_label(chunk, bar["start_marker"])
        end = find_marker_by_label(chunk, bar["end_marker"])
        if start is None or end is None:
            missing = [label for label, marker in
                       ((bar["start_marker"], start), (bar["end_marker"], end)) if marker is None]
            log(f"WARNING: could not find markers: {', '.join(missing)}")
            continue
        scalebar = chunk.addScalebar(start, end)
        scalebar.reference.distance = bar["distance"]
        scalebar.reference.accuracy = SCALEBAR_ACCURACY
        scalebar.reference.enabled = True
        added += 1
        log(f"Scale bar {bar['start_marker']} to {bar['end_marker']}: {bar['distance']} m")
    return added


def prune_marker_projections(Metashape, chunk, log, max_error_px=0.8, min_projections=5):
    """Drop each marker's worst-error projection until it clears max_error_px.

    Per NOAA TM NMFS-PIFSC-159 Section 4.3.2: per-projection reprojection
    error is the pixel distance between where the marker's 3D position
    reprojects onto a camera (camera.project) and the actual detected
    projection coordinate for that camera. While a marker's worst remaining
    projection exceeds max_error_px AND it has more than min_projections
    left, the worst projection is removed (marker.projections[camera] =
    None) and the marker's error is recomputed against what remains.
    Cameras without a transform (unaligned) are skipped entirely -- there is
    nothing to project them with, and they do not count toward
    min_projections.

    In Metashape 2.x, marker.projections[camera].coord is a 3-component
    Vector while camera.project(marker.position) returns a 2-component
    Vector, so they cannot be subtracted directly ("different vector
    dimensions"). The pixel error is instead computed from the .x/.y
    components of each with plain Python arithmetic, no Vector subtraction
    involved.

    A single marker's projections are never allowed to abort the whole
    scaling pass: any exception raised while processing one marker is
    caught, logged as a warning, and that marker is skipped so pruning
    continues for the rest.
    """
    for marker in chunk.markers:
        cameras = [camera for camera in marker.projections.keys() if camera.transform is not None]
        if not cameras:
            continue

        try:
            errors = {}
            for camera in cameras:
                projected = camera.project(marker.position)
                coord = marker.projections[camera].coord
                dx = projected.x - coord.x
                dy = projected.y - coord.y
                errors[camera] = (dx * dx + dy * dy) ** 0.5

            before_count = len(errors)
            before_worst = max(errors.values())

            removed = 0
            while errors and max(errors.values()) > max_error_px and len(errors) > min_projections:
                worst_camera = max(errors, key=errors.get)
                marker.projections[worst_camera] = None
                del errors[worst_camera]
                removed += 1

            after_count = len(errors)
            after_worst = max(errors.values()) if errors else 0.0
            if removed:
                log(
                    f"{marker.label}: pruned {removed} projection(s), worst error "
                    f"{before_worst:.3f}px -> {after_worst:.3f}px, projections "
                    f"{before_count} -> {after_count}"
                )
            else:
                log(
                    f"{marker.label}: no pruning needed, worst error {before_worst:.3f}px "
                    f"over {before_count} projection(s)"
                )
        except Exception as exc:
            log(f"WARNING: {marker.label}: projection pruning failed ({exc}); skipping marker")
            continue


def mean_scale_bar_error(chunk):
    errors = []
    for scalebar in chunk.scalebars:
        p0 = chunk.transform.matrix.mulp(scalebar.point0.position)
        p1 = chunk.transform.matrix.mulp(scalebar.point1.position)
        errors.append(abs((p1 - p0).norm() - scalebar.reference.distance))
    if not errors:
        return SENTINEL_ERROR
    return sum(errors) / len(errors)


def apply_scale(Metashape, chunk, model_config, log):
    """Detect targets, build the declared scale bars, apply the transform.

    Returns (status, mean_abs_error_m) with status "PASS" or "FAIL".
    """
    scale_bars = model_config.get("scale_bars", []) or []
    threshold = model_config.get("scale_error_threshold", 0.009)
    if not model_config.get("has_coded_scales", True) or not scale_bars:
        log("Scaling skipped: no coded scales declared in analysis_params")
        return "FAIL", SENTINEL_ERROR

    chunk.detectMarkers(
        target_type=Metashape.TargetType.CircularTarget20bit,
        tolerance=50,
        filter_mask=False,
    )
    log(f"Detected {len(chunk.markers)} markers")

    if model_config.get("remove_unlisted_markers", True):
        remove_unlisted_markers(chunk, scale_bars, log)

    if model_config.get("marker_projection_prune", False):
        prune_marker_projections(Metashape, chunk, log)

    added = add_scale_bars(chunk, scale_bars, log)
    if added == 0:
        log("ERROR: no scale bars could be added")
        return "FAIL", SENTINEL_ERROR

    chunk.updateTransform()
    error = mean_scale_bar_error(chunk)
    status = "PASS" if error < threshold else "FAIL"
    log(f"Scale {status}: mean abs error {error:.6f} m over {added} bar(s), threshold {threshold} m")
    return status, error


def compute_texture_pages(area_m2, texel_m, page_size, scaled, unscaled_pages):
    """Pages of page_size x page_size needed to texture area_m2 at texel_m."""
    if not scaled:
        return int(unscaled_pages)
    if area_m2 <= 0 or texel_m <= 0:
        return 1
    return max(1, math.ceil(area_m2 / (texel_m * texel_m) / float(page_size * page_size)))
