#!/usr/bin/env python3
"""
Module: inspect_psx.py
Purpose: Read-only inspection of Metashape psx bundles for the manual edit
         CHECKS (spec 2026-09-03-voyager-program-design.md section 6.7).
         For every psx it reports one JSON document: per chunk the label,
         whether a transform is present, the scale bars and the re-measured
         scale error (metres, mm, ppm, PASS or MANUAL_NEEDED through
         scale_utils), faces, textures, tie points, cameras total and
         aligned, and the elevation resolution; per psx the psx and .files
         mtimes, the Metashape version and whether the seat is activated.
Inputs:  one or more psx paths; optionally --params <analysis_params.yaml>
         (else the file beside the psx, else the defaults), --threshold-m,
         --bar-length-m, --out <file>, and --fake <fixture.json> for tests.
Outputs: JSON Lines, one document per psx, on stdout or in --out.

Real mode runs only under the Metashape binary:
    metashape -r src/inspect_psx.py <psx> [<psx> ...] --out <file>
Metashape is imported inside real mode only; fake mode builds FakeDocument
objects from the fixture so any Python can run the tests.

Exit codes: 0 every psx inspected, 1 at least one document carries an
error, 2 a usage or fixture problem (message on stderr).
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scale_utils  # noqa: E402

AST = timezone(timedelta(hours=-4))
DEFAULT_THRESHOLD_M = 0.009
DEFAULT_BAR_LENGTH_M = 0.75
MM_PER_M = 1000.0
DEM_DECIMALS = 3
MODE_REAL = "real"
MODE_FAKE = "fake"
MODES = (MODE_REAL, MODE_FAKE)
PSX_SUFFIX = ".psx"
FILES_SUFFIX = ".files"
PARAMS_FILENAME = "analysis_params.yaml"
EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2
CHUNK_FIELDS = (
    "label", "has_transform", "scale_bars", "scale_error_m", "scale_error_mm", "scale_error_ppm",
    "scale_status", "faces", "textures", "tie_points", "cameras_total", "cameras_aligned",
    "dem_mm_per_pix", "notes",
)


# --- parameters ---------------------------------------------------------------


def mean_bar_length(model_cfg) -> float:
    """Mean declared scale-bar length in metres, the ppm denominator (same rule as step1).

    Parameters:
        model_cfg: the processing.model_processing mapping of analysis_params.yaml.

    Returns:
        The mean of every positive numeric scale_bars[].distance, or 0.0 when none.
    """
    lengths = []
    bars = model_cfg.get("scale_bars", []) if isinstance(model_cfg, Mapping) else []
    for bar in bars or []:
        if not isinstance(bar, Mapping):
            continue
        try:
            length = float(bar.get("distance", 0) or 0)
        except (TypeError, ValueError):
            continue
        if length > 0:
            lengths.append(length)
    return sum(lengths) / len(lengths) if lengths else 0.0


def read_params(path: str) -> tuple[float, float, str]:
    """The scale error threshold and mean bar length from an analysis_params.yaml.

    Parameters:
        path: the YAML file.

    Returns:
        (threshold_m, bar_length_m, note): the defaults stand in for anything
        unreadable and `note` says what was substituted ("" when nothing was).
    """
    try:
        import yaml
    except ImportError:
        return DEFAULT_THRESHOLD_M, DEFAULT_BAR_LENGTH_M, "pyyaml is not importable; the default threshold and bar length were used"
    if not os.path.exists(path):
        return DEFAULT_THRESHOLD_M, DEFAULT_BAR_LENGTH_M, f"{path} not found; the default threshold and bar length were used"
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        return DEFAULT_THRESHOLD_M, DEFAULT_BAR_LENGTH_M, f"{path} could not be read ({exc}); the defaults were used"
    processing = data.get("processing") if isinstance(data, Mapping) else None
    model_cfg = processing.get("model_processing") if isinstance(processing, Mapping) else None
    if not isinstance(model_cfg, Mapping):
        return DEFAULT_THRESHOLD_M, DEFAULT_BAR_LENGTH_M, f"{path} has no processing.model_processing block; the defaults were used"
    notes = []
    threshold = _positive_number(model_cfg.get("scale_error_threshold"))
    if threshold is None:
        threshold = DEFAULT_THRESHOLD_M
        notes.append(f"scale_error_threshold unreadable in {path}; {DEFAULT_THRESHOLD_M} m used")
    bar_length = mean_bar_length(model_cfg)
    if bar_length <= 0:
        bar_length = DEFAULT_BAR_LENGTH_M
        notes.append(f"no usable scale_bars distance in {path}; {DEFAULT_BAR_LENGTH_M} m used")
    return threshold, bar_length, "; ".join(notes)


def _positive_number(value):
    """A positive finite float from a number or numeric string; None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _validate_numbers(threshold_m, bar_length_m) -> tuple[float, float]:
    """threshold_m as a positive float and bar_length_m as a non-negative float."""
    for name, value in (("threshold_m", threshold_m), ("bar_length_m", bar_length_m)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number, got {type(value).__name__}")
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite, got {value!r}")
    if threshold_m <= 0:
        raise ValueError(f"threshold_m must be positive, got {threshold_m!r}")
    if bar_length_m < 0:
        raise ValueError(f"bar_length_m must not be negative, got {bar_length_m!r}")
    return float(threshold_m), float(bar_length_m)


# --- inspection ---------------------------------------------------------------


def _read(fetch, what: str, notes: list, default):
    """Call `fetch`; on any failure record "<what>: <error>" in `notes` and return `default`.

    The Metashape API raises assorted exception types for an absent asset, so
    every read is guarded and every failure is written down, never swallowed.
    """
    try:
        return fetch()
    except Exception as exc:  # noqa: BLE001 - recorded in notes, see docstring
        notes.append(f"{what}: {type(exc).__name__}: {exc}")
        return default


def _scale_facts(chunk, has_transform: bool, bars: int, threshold_m: float, bar_length_m: float, notes: list) -> dict:
    """Re-measure the scale of a chunk without touching it."""
    error = None
    if has_transform and bars > 0:
        measured = _read(lambda: float(scale_utils.mean_scale_bar_error(chunk)), "scale error", notes, None)
        if measured is not None and measured < scale_utils.SENTINEL_ERROR:
            error = measured
    status = scale_utils.decide(bars, error if error is not None else scale_utils.SENTINEL_ERROR, threshold_m)
    return {
        "scale_error_m": error,
        "scale_error_mm": round(error * MM_PER_M, 2) if error is not None else None,
        "scale_error_ppm": scale_utils.ppm(error, bar_length_m) if error is not None else None,
        "scale_status": status,
    }


def inspect_chunk(chunk, threshold_m: float, bar_length_m: float) -> dict:
    """The facts of one chunk.

    Parameters:
        chunk: a Metashape.Chunk (or a FakeChunk).
        threshold_m: the scale error threshold in metres.
        bar_length_m: the mean declared bar length in metres (0 gives ppm 0).

    Returns:
        A dict with CHUNK_FIELDS; a fact that could not be read is None and
        `notes` says why.

    Raises:
        TypeError, ValueError: for a non-numeric, non-positive threshold or a
            negative bar length.
    """
    threshold, bar_length = _validate_numbers(threshold_m, bar_length_m)
    notes: list = []
    label = _read(lambda: str(chunk.label), "label", notes, "")
    matrix = _read(lambda: chunk.transform.matrix, "transform", notes, None)
    has_transform = bool(matrix)
    bars = _read(lambda: len(chunk.scalebars), "scalebars", notes, 0)
    model = _read(lambda: chunk.model, "model", notes, None)
    faces = _read(lambda: int(model.statistics().faces), "faces", notes, None) if model else None
    textures = _read(lambda: len(model.textures), "textures", notes, None) if model else None
    tie_points = _read(lambda: chunk.tie_points, "tie points", notes, None)
    tie_count = _read(lambda: len(tie_points.points), "tie point count", notes, None) if tie_points is not None else None
    cameras = _read(lambda: list(chunk.cameras), "cameras", notes, [])
    aligned = sum(1 for camera in cameras if _read(lambda: camera.transform, "camera transform", notes, None))
    elevation = _read(lambda: chunk.elevation, "elevation", notes, None)
    dem = (_read(lambda: round(float(elevation.resolution) * MM_PER_M, DEM_DECIMALS), "elevation resolution", notes, None)
           if elevation is not None else None)
    facts = {
        "label": label, "has_transform": has_transform, "scale_bars": int(bars),
        "faces": faces, "textures": textures, "tie_points": tie_count,
        "cameras_total": len(cameras), "cameras_aligned": aligned, "dem_mm_per_pix": dem,
    }
    facts.update(_scale_facts(chunk, has_transform, int(bars), threshold, bar_length, notes))
    facts["notes"] = notes
    return {field: facts[field] for field in CHUNK_FIELDS}


def inspect_document(doc, threshold_m: float, bar_length_m: float) -> list[dict]:
    """inspect_chunk for every chunk of an open document, in document order.

    Raises:
        TypeError: when `doc` has no chunks attribute.
    """
    chunks = getattr(doc, "chunks", None)
    if chunks is None:
        raise TypeError(f"the document has no chunks attribute ({type(doc).__name__})")
    return [inspect_chunk(chunk, threshold_m, bar_length_m) for chunk in chunks]


def _iso(epoch):
    """An epoch as an ISO stamp in AST, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), AST).isoformat(timespec="seconds")


def file_times(psx_path: str) -> dict:
    """The psx and .files mtimes from disk (None when absent)."""
    psx_epoch = os.path.getmtime(psx_path) if os.path.isfile(psx_path) else None
    files_dir = os.path.splitext(psx_path)[0] + FILES_SUFFIX
    files_epoch = os.path.getmtime(files_dir) if os.path.isdir(files_dir) else None
    return {
        "psx_mtime": _iso(psx_epoch), "psx_mtime_epoch": psx_epoch,
        "files_mtime": _iso(files_epoch), "files_mtime_epoch": files_epoch,
    }


def inspect_psx(psx_path: str, open_document, app_info, threshold_m: float, bar_length_m: float, mode: str) -> dict:
    """One JSON-ready document for a psx.

    Parameters:
        psx_path: the bundle's .psx file.
        open_document: callable(path) returning an open document (real_open or a fake opener).
        app_info: callable() returning {"app_version", "activated"}.
        threshold_m, bar_length_m: the scale parameters.
        mode: "real" or "fake", recorded in the document.

    Returns:
        {psx_path, psx_basename, mode, inspected_at, threshold_m, bar_length_m,
         app_version, activated, psx_mtime, psx_mtime_epoch, files_mtime,
         files_mtime_epoch, chunks: [...], error: None|str, notes: [...]}.
        A failed open yields chunks [] and the error text; nothing raises.

    Raises:
        ValueError: when psx_path is blank or not a .psx, or mode is unknown.
    """
    if not isinstance(psx_path, str) or not psx_path.strip():
        raise ValueError("psx_path is blank")
    if not psx_path.lower().endswith(PSX_SUFFIX):
        raise ValueError(f"psx_path must end in {PSX_SUFFIX}, got {psx_path!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    threshold, bar_length = _validate_numbers(threshold_m, bar_length_m)
    notes: list = []
    info = _read(app_info, "application info", notes, {}) or {}
    document = {
        "psx_path": psx_path, "psx_basename": os.path.basename(psx_path), "mode": mode,
        "inspected_at": datetime.now(AST).isoformat(timespec="seconds"),
        "threshold_m": threshold, "bar_length_m": bar_length,
        "app_version": info.get("app_version"), "activated": info.get("activated"),
    }
    document.update(file_times(psx_path))
    doc = None
    try:
        doc = open_document(psx_path)
        document["chunks"] = inspect_document(doc, threshold, bar_length)
        document["error"] = None
        if document["psx_mtime_epoch"] is None:
            document["psx_mtime_epoch"] = getattr(doc, "psx_mtime_epoch", None)
            document["psx_mtime"] = _iso(document["psx_mtime_epoch"])
        if document["files_mtime_epoch"] is None:
            document["files_mtime_epoch"] = getattr(doc, "files_mtime_epoch", None)
            document["files_mtime"] = _iso(document["files_mtime_epoch"])
    except Exception as exc:  # noqa: BLE001 - the error is the document's payload
        document["chunks"] = []
        document["error"] = f"could not inspect {psx_path}: {type(exc).__name__}: {exc}"
    finally:
        doc = None
        gc.collect()
    document["notes"] = notes
    return document


# --- real mode ----------------------------------------------------------------


def _import_metashape():
    """Import Metashape, which exists only inside the Metashape binary's interpreter."""
    try:
        import Metashape  # noqa: WPS433 - only importable under `metashape -r`
    except ImportError as exc:
        raise RuntimeError(
            f"Metashape is only importable under the Metashape binary; run this script as "
            f"`metashape -r {os.path.abspath(__file__)} <psx>...` ({exc})") from exc
    return Metashape


def real_open(psx_path: str):
    """Open a psx read-only, ignoring its lock, the way step1's probes do."""
    Metashape = _import_metashape()
    doc = Metashape.Document()
    doc.open(psx_path, read_only=True, ignore_lock=True)
    return doc


def real_app_info() -> dict:
    """The Metashape version and activation state of the running binary."""
    Metashape = _import_metashape()
    return {"app_version": str(Metashape.app.version), "activated": bool(Metashape.app.activated)}


# --- fake mode ----------------------------------------------------------------


class FakeVector:
    """A 3-vector with the two operations mean_scale_bar_error uses."""

    def __init__(self, x, y, z):
        """Store the three components as floats."""
        self.v = (float(x), float(y), float(z))

    def __sub__(self, other):
        """The component-wise difference of two vectors."""
        return FakeVector(*[a - b for a, b in zip(self.v, other.v)])

    def norm(self):
        """The Euclidean length."""
        return sum(a * a for a in self.v) ** 0.5


class FakeMatrix:
    """An identity chunk transform: mulp returns the point unchanged."""

    def mulp(self, point):
        """Return the point unchanged: the identity transform."""
        return point


class FakeTransform:
    """chunk.transform with a matrix (present) or None (absent)."""

    def __init__(self, present: bool):
        """Give the transform a matrix when present, else None."""
        self.matrix = FakeMatrix() if present else None


class FakeReference:
    """A scalebar reference: the declared distance, its accuracy and the enabled flag."""
    def __init__(self, distance: float):
        """Record the declared distance with the step 1 accuracy."""
        self.distance = distance
        self.accuracy = scale_utils.SCALEBAR_ACCURACY
        self.enabled = True


class FakePoint:
    """A marker position holder, the shape scalebar.point0 and point1 expose."""
    def __init__(self, position: FakeVector):
        """Wrap a FakeVector position."""
        self.position = position


class FakeScalebar:
    """A scale bar between two points with a declared distance."""

    def __init__(self, p0, p1, distance):
        """Build the two end points and the reference from a fixture bar."""
        self.point0 = FakePoint(FakeVector(*p0))
        self.point1 = FakePoint(FakeVector(*p1))
        self.reference = FakeReference(float(distance))


class FakeStatistics:
    """The model.statistics() result, carrying the face count."""
    def __init__(self, faces: int):
        """Record the face count."""
        self.faces = faces


class FakeModel:
    """chunk.model with statistics().faces and a textures list."""

    def __init__(self, faces: int, textures: int):
        """Record the face count and build one placeholder per texture."""
        self._faces = faces
        self.textures = [object() for _ in range(textures)]

    def statistics(self):
        """The FakeStatistics of this model."""
        return FakeStatistics(self._faces)


class FakeTiePoints:
    """chunk.tie_points with a points sequence of the given length."""
    def __init__(self, count: int):
        """Build a points range of the given length."""
        self.points = range(count)


class FakeCamera:
    """A camera whose transform is set when aligned and None otherwise."""
    def __init__(self, aligned: bool):
        """Set the transform when aligned."""
        self.transform = FakeMatrix() if aligned else None


class FakeElevation:
    """chunk.elevation with a resolution in metres per pixel."""
    def __init__(self, resolution_m: float):
        """Record the resolution in metres per pixel."""
        self.resolution = resolution_m


def _spec_int(spec: Mapping, key: str, label: str, default=None, allow_none=True):
    """A non-negative int from a fixture chunk spec, or `default` when the key is absent."""
    value = spec.get(key, default)
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"fixture chunk {label!r}: {key} must be a non-negative integer, got {value!r}")
    return value


class FakeChunk:
    """A chunk built from a fixture spec: label, transform, scalebars, faces, textures,
    tie_points, cameras, cameras_aligned, elevation_resolution_m, model."""

    def __init__(self, label, transform, scalebars, model, tie_points, cameras, elevation):
        """Store the chunk parts under the attribute names Metashape.Chunk uses."""
        self.label = label
        self.transform = transform
        self.scalebars = scalebars
        self.model = model
        self.tie_points = tie_points
        self.cameras = cameras
        self.elevation = elevation

    @classmethod
    def from_spec(cls, spec) -> "FakeChunk":
        """Build a chunk from one fixture entry.

        Raises:
            ValueError: when the entry is malformed (the message names the chunk and key).
        """
        if not isinstance(spec, Mapping):
            raise ValueError(f"fixture chunk must be an object, got {type(spec).__name__}")
        label = spec.get("label")
        if not isinstance(label, str):
            raise ValueError(f"fixture chunk needs a string label, got {label!r}")
        bars_spec = spec.get("scalebars", [])
        if not isinstance(bars_spec, list):
            raise ValueError(f"fixture chunk {label!r}: scalebars must be a list")
        scalebars = []
        for bar in bars_spec:
            if not isinstance(bar, Mapping) or "p0" not in bar or "p1" not in bar or "distance" not in bar:
                raise ValueError(f"fixture chunk {label!r}: each scalebar needs p0, p1 and distance")
            scalebars.append(FakeScalebar(bar["p0"], bar["p1"], bar["distance"]))
        model = None
        if spec.get("model", True):
            model = FakeModel(_spec_int(spec, "faces", label, 0, allow_none=False),
                              _spec_int(spec, "textures", label, 0, allow_none=False))
        tie_count = _spec_int(spec, "tie_points", label, 0)
        total = _spec_int(spec, "cameras", label, 0, allow_none=False)
        aligned = _spec_int(spec, "cameras_aligned", label, total, allow_none=False)
        if aligned > total:
            raise ValueError(f"fixture chunk {label!r}: cameras_aligned {aligned} exceeds cameras {total}")
        resolution = spec.get("elevation_resolution_m")
        elevation = FakeElevation(float(resolution)) if resolution is not None else None
        return cls(
            label=label, transform=FakeTransform(bool(spec.get("transform", True))), scalebars=scalebars,
            model=model, tie_points=FakeTiePoints(tie_count) if tie_count is not None else None,
            cameras=[FakeCamera(i < aligned) for i in range(total)], elevation=elevation,
        )


class FakeDocument:
    """A document with chunks and the fixture's optional mtimes."""

    def __init__(self, chunks, psx_mtime_epoch=None, files_mtime_epoch=None):
        """Hold the chunks and the fixture's optional mtimes."""
        self.chunks = chunks
        self.psx_mtime_epoch = psx_mtime_epoch
        self.files_mtime_epoch = files_mtime_epoch

    @classmethod
    def from_spec(cls, spec) -> "FakeDocument":
        """Build a document from a fixture psx entry ({chunks: [...], psx_mtime_epoch?, files_mtime_epoch?})."""
        if not isinstance(spec, Mapping):
            raise ValueError(f"fixture psx entry must be an object, got {type(spec).__name__}")
        chunks_spec = spec.get("chunks", [])
        if not isinstance(chunks_spec, list):
            raise ValueError("fixture psx entry: chunks must be a list")
        return cls([FakeChunk.from_spec(chunk) for chunk in chunks_spec],
                   spec.get("psx_mtime_epoch"), spec.get("files_mtime_epoch"))


def load_fixture(path: str) -> dict:
    """Read a --fake fixture: {"app_version", "activated", "psx": {path or basename: entry}}.

    Raises:
        FileNotFoundError: when the file is missing.
        ValueError: when it is not JSON or not shaped as a fixture.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"fixture not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture {path} is not readable JSON: {exc}") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("psx"), Mapping):
        raise ValueError(f"fixture {path} must be an object with a psx object")
    return dict(data)


def fake_opener(fixture: Mapping):
    """An open_document callable over a fixture; a psx absent from it raises FileNotFoundError."""
    entries = fixture["psx"]

    def open_document(psx_path: str):
        """Open the fixture entry for psx_path (full path first, then basename)."""
        entry = entries.get(psx_path)
        if entry is None:
            entry = entries.get(os.path.basename(psx_path))
        if entry is None:
            raise FileNotFoundError(f"{psx_path} not found in the fixture")
        if isinstance(entry, Mapping) and entry.get("open_error"):
            raise RuntimeError(str(entry["open_error"]))
        return FakeDocument.from_spec(entry)

    return open_document


def fake_app_info(fixture: Mapping):
    """An app_info callable answering the fixture's app_version and activated."""
    return lambda: {"app_version": fixture.get("app_version"), "activated": fixture.get("activated")}


# --- command line -------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """The command line: psx paths plus --out, --params, --threshold-m, --bar-length-m and --fake."""
    parser = argparse.ArgumentParser(
        description="Inspect Metashape psx bundles read-only and print one JSON document per psx.")
    parser.add_argument("psx", nargs="+", help="psx file(s) to inspect")
    parser.add_argument("--out", help="write the JSON lines here instead of stdout")
    parser.add_argument("--params", help="analysis_params.yaml for the threshold and bar length "
                                         "(default: the file beside each psx, else built-in defaults)")
    parser.add_argument("--threshold-m", type=float, help="scale error threshold in metres (overrides --params)")
    parser.add_argument("--bar-length-m", type=float, help="mean declared bar length in metres (overrides --params)")
    parser.add_argument("--fake", help="JSON fixture standing in for Metashape (tests)")
    return parser


def resolve_params(psx_path: str, params_path, threshold_flag, bar_flag) -> tuple[float, float, str]:
    """The threshold and bar length for one psx: flags, then --params, then the
    analysis_params.yaml beside the psx, then the defaults."""
    if params_path is not None:
        threshold, bar_length, note = read_params(params_path)
    else:
        beside = os.path.join(os.path.dirname(os.path.abspath(psx_path)), PARAMS_FILENAME)
        if os.path.exists(beside):
            threshold, bar_length, note = read_params(beside)
        else:
            threshold, bar_length, note = DEFAULT_THRESHOLD_M, DEFAULT_BAR_LENGTH_M, ""
    if threshold_flag is not None:
        threshold = threshold_flag
    if bar_flag is not None:
        bar_length = bar_flag
    return threshold, bar_length, note


def _usage_error(message: str) -> int:
    """Print a usage problem on stderr and return EXIT_USAGE."""
    print(f"inspect_psx: {message}", file=sys.stderr)
    return EXIT_USAGE


def main(argv=None) -> int:
    """Parse arguments, inspect every psx, write the JSON lines, return the exit code."""
    args = _parser().parse_args(argv)
    for path in args.psx:
        if not path.lower().endswith(PSX_SUFFIX):
            return _usage_error(f"{path} is not a {PSX_SUFFIX} file")
    if args.threshold_m is not None and not (math.isfinite(args.threshold_m) and args.threshold_m > 0):
        return _usage_error(f"--threshold-m must be positive, got {args.threshold_m}")
    if args.bar_length_m is not None and not (math.isfinite(args.bar_length_m) and args.bar_length_m >= 0):
        return _usage_error(f"--bar-length-m must not be negative, got {args.bar_length_m}")
    if args.fake:
        try:
            fixture = load_fixture(args.fake)
        except (FileNotFoundError, ValueError) as exc:
            return _usage_error(str(exc))
        open_document, app_info, mode = fake_opener(fixture), fake_app_info(fixture), MODE_FAKE
    else:
        open_document, app_info, mode = real_open, real_app_info, MODE_REAL
    documents = []
    for path in args.psx:
        threshold, bar_length, note = resolve_params(path, args.params, args.threshold_m, args.bar_length_m)
        document = inspect_psx(path, open_document, app_info, threshold, bar_length, mode)
        document["params_note"] = note
        documents.append(document)
    lines = "".join(json.dumps(document, ensure_ascii=True) + "\n" for document in documents)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(lines)
        except OSError as exc:
            return _usage_error(f"could not write {args.out}: {exc}")
    else:
        sys.stdout.write(lines)
        sys.stdout.flush()
    return EXIT_OK if all(document["error"] is None for document in documents) else EXIT_ERRORS


if __name__ == "__main__":
    sys.exit(main())
