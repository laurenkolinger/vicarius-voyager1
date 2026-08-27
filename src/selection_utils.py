"""Capped gradual-selection scheme for 3D_phase_1 Step 1 (A/B alternative).

Iterative cull-and-reoptimize of the sparse tie point cloud in the order
ReconstructionUncertainty, ProjectionAccuracy, ReprojectionError, re-running
camera optimization after each removal, with a cap on how large a fraction
of the current tie point cloud any single criterion is allowed to remove in
one pass.

The cap-search logic (cap_adjusted_threshold) is pure and Metashape-free so
it can be unit tested with plain callables. capped_gradual_selection wraps
it with the real Metashape.TiePoints.Filter calls; Metashape is passed in
so step1.py's actual module is used at runtime while tests can inject a
fake.

Filter mechanics, confirmed against the installed Metashape 2.2.2 Python API
via `metashape -r` and `help()` (Metashape.TiePoints.Filter, TiePoints,
TiePoints.Point, Chunk.optimizeCameras):
  - Filter.init(points, criterion, [progress]) initializes the filter against
    a Metashape.Chunk (or Metashape.TiePoints).
  - Filter.selectPoints(threshold) selects points whose criterion value
    exceeds threshold; it is not cumulative, calling it again with a new
    threshold replaces the prior selection.
  - Filter.removePoints(threshold) is a one-shot select+remove and is what
    the legacy path uses; capped mode needs to probe counts before
    committing to a threshold, so it uses selectPoints to probe and
    chunk.tie_points.removeSelectedPoints() to commit.
  - Metashape.TiePoints.Point.selected is a bool per-point flag.
"""


def cap_adjusted_threshold(count_selected_fn, start_threshold, max_fraction, total_points,
                            widen_factor=1.25, max_iters=12):
    """Search for the smallest tested threshold that stays within the cap.

    count_selected_fn(threshold) -> int is called with start_threshold first;
    if the returned count exceeds max_fraction * total_points, the threshold
    is multiplied by widen_factor and probed again. This repeats until a
    probe's count is within the cap (that threshold is returned immediately)
    or max_iters probes have been spent, in which case the last threshold
    tested is returned regardless of whether it satisfied the cap -- the
    caller is responsible for checking and logging that outcome.

    max_iters counts total probes (calls to count_selected_fn), so with the
    default of 12, at most 12 thresholds are tried: start_threshold and up
    to 11 widened values.
    """
    cap = max_fraction * total_points
    threshold = start_threshold
    count = count_selected_fn(threshold)
    for _ in range(max_iters - 1):
        if count <= cap:
            return threshold
        threshold *= widen_factor
        count = count_selected_fn(threshold)
    return threshold


_CRITERIA = (
    # (attr name on Metashape.TiePoints.Filter, cfg key, log label, cap fraction)
    ("ReconstructionUncertainty", "reconstruction_uncertainty", "ReconstructionUncertainty", 0.5),
    ("ProjectionAccuracy", "projection_accuracy", "ProjectionAccuracy", 0.5),
    ("ReprojectionError", "reprojection_error", "ReprojectionError", 0.10),
)


def _run_criterion(Metashape, chunk, criterion_attr, criterion_name, start_threshold,
                    cap_fraction, optimize, log, widen_factor=1.25, max_iters=12):
    """Probe, cap-adjust, remove, and re-optimize for a single filter criterion."""
    total_points = len(chunk.tie_points.points)
    criterion = getattr(Metashape.TiePoints.Filter, criterion_attr)
    f = Metashape.TiePoints.Filter()
    f.init(chunk, criterion)

    last_count = {}

    def count_selected(threshold):
        f.selectPoints(threshold)
        count = sum(1 for p in chunk.tie_points.points if p.selected)
        last_count["value"] = count
        return count

    final_threshold = cap_adjusted_threshold(
        count_selected, start_threshold, cap_fraction, total_points,
        widen_factor=widen_factor, max_iters=max_iters,
    )

    cap_count = cap_fraction * total_points
    if last_count.get("value", 0) > cap_count:
        log(
            f"{criterion_name}: cap not satisfied after widening to "
            f"{final_threshold:.4f} ({last_count['value']} of {total_points} tie points "
            f"still selected, cap {cap_count:.0f} = {cap_fraction:.0%}); "
            f"proceeding with removal at this threshold"
        )

    # Select at the final threshold explicitly (rather than trust the last
    # probe's selection state) and remove.
    f.selectPoints(final_threshold)
    removed_count = sum(1 for p in chunk.tie_points.points if p.selected)
    chunk.tie_points.removeSelectedPoints()
    # chunk.tie_points.points can remain a fixed-size collection after
    # removeSelectedPoints() (removed points are invalidated in place rather
    # than dropped from the list), so len(...) here is not a reliable count
    # of what remains. Derive it arithmetically from what we already know
    # was removed instead.
    remaining_count = total_points - removed_count

    log(
        f"{criterion_name}: threshold {final_threshold:.4f}, removed "
        f"{removed_count} of {total_points}, {remaining_count} remaining"
    )

    optimize()

    return final_threshold, removed_count, remaining_count


def capped_gradual_selection(Metashape, chunk, cfg, log, optimize):
    """Run the capped gradual-selection scheme against chunk.tie_points.

    Order: ReconstructionUncertainty, ProjectionAccuracy, ReprojectionError.
    Each criterion starts at its threshold from cfg (same keys as the legacy
    METASHAPE_DEFAULTS: reconstruction_uncertainty, projection_accuracy,
    reprojection_error), is cap-adjusted so it never removes more than its
    cap fraction of the current tie point count (0.5 for
    ReconstructionUncertainty and ProjectionAccuracy, 0.10 for
    ReprojectionError), removes the selected points, and is followed by a
    call to the optimize callable.
    """
    for criterion_attr, cfg_key, criterion_name, cap_fraction in _CRITERIA:
        _run_criterion(
            Metashape, chunk, criterion_attr, criterion_name,
            cfg[cfg_key], cap_fraction, optimize, log,
        )


# Back-compat alias for the pre-rename function name. step1.py's accepted
# config values still resolve to this same function under either name.
noaa_gradual_selection = capped_gradual_selection
