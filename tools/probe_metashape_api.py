"""API probe against the installed Metashape. Run:
/home/bizon/applications/metashape-pro_2_2_2_amd64/metashape-pro/metashape -r tools/probe_metashape_api.py
Writes tools/probe_results.txt next to this script.
"""
import io
import os

import Metashape

out = io.StringIO()
out.write(f"Metashape version: {Metashape.app.version}\n\n")

checks = {
    "TargetType.CircularTarget20bit": hasattr(getattr(Metashape, "TargetType", object), "CircularTarget20bit"),
    "Chunk.detectMarkers": hasattr(Metashape.Chunk, "detectMarkers"),
    "Chunk.addScalebar": hasattr(Metashape.Chunk, "addScalebar"),
    "Chunk.updateTransform": hasattr(Metashape.Chunk, "updateTransform"),
    "Chunk.decimateModel": hasattr(Metashape.Chunk, "decimateModel"),
    "Chunk.smoothModel": hasattr(Metashape.Chunk, "smoothModel"),
    "Chunk.buildDem": hasattr(Metashape.Chunk, "buildDem"),
    "Chunk.exportRaster": hasattr(Metashape.Chunk, "exportRaster"),
    "Chunk.elevation attr": "elevation" in dir(Metashape.Chunk),
    "Chunk.models attr": "models" in dir(Metashape.Chunk),
    "Chunk.depth_maps attr": "depth_maps" in dir(Metashape.Chunk),
    "Chunk.depth_maps_sets attr": "depth_maps_sets" in dir(Metashape.Chunk),
    "Model.key attr": "key" in dir(getattr(Metashape, "Model", object)),
    "Model.statistics": hasattr(getattr(Metashape, "Model", object), "statistics"),
    "Model.area": hasattr(getattr(Metashape, "Model", object), "area"),
    "ElevationData": hasattr(Metashape, "ElevationData"),
    "DepthMapsData": hasattr(Metashape, "DepthMapsData"),
    "ImageFormatTIFF": hasattr(Metashape, "ImageFormatTIFF"),
}
for name, ok in sorted(checks.items()):
    out.write(("OK   " if ok else "MISS ") + name + "\n")
out.write("\n")

for fn_name in ("detectMarkers", "addScalebar", "updateTransform", "decimateModel",
                "smoothModel", "buildDem", "buildDepthMaps", "buildModel",
                "buildUV", "buildTexture", "exportRaster", "exportReport"):
    fn = getattr(Metashape.Chunk, fn_name, None)
    doc = (fn.__doc__ or "").strip() if fn else "ABSENT"
    out.write(f"--- Chunk.{fn_name} ---\n{doc}\n\n")

result = out.getvalue()
print(result)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_results.txt"), "w") as fh:
    fh.write(result)
