"""Rasterize a boundary parquet into an OME-Zarr label mask.

Dev-only, like `make_synthetic_big_pyramid.py` — no end user needs it. It
exists so the label-mask import path (`cytos.prep.labels`) can be tested
against ground truth: rasterize a segmentation cytos already reads as
polygons, trace it back, and the two should land on top of each other. That
round trip is what checks the pixel->world conversion, the Y direction, and
the scale/translation handling all at once.

Writes a single-level pyramid on purpose: downsampling a label mask would
interpolate between object ids and invent objects that were never segmented,
and the tracer only ever reads level 0.

    python tools/make_label_mask.py \
        data/human_kidney_tiny/cell_boundaries.parquet \
        --out data/kidney_cell_labels.ome.zarr --px 0.2125
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import zarr
from skimage.draw import polygon as draw_polygon

from cytos.core.polygons import polygons_from_parquet

# Pixels of blank border around the segmentation, so no object is clipped by
# the edge of the raster and every ring has background to close against.
MARGIN_PX = 4


def make_label_mask(
    boundaries: Path, out: Path, px: float = 0.2125, labels_group: str | None = None
) -> Path:
    polygons = polygons_from_parquet(boundaries)
    n = len(polygons.offsets) - 1
    minx, miny = polygons.coords.min(axis=0)
    maxx, maxy = polygons.coords.max(axis=0)

    # World -> pixel, the inverse of what cytos.prep.labels does on the way
    # back: translation is the world coordinate of pixel (0, 0).
    tx, ty = float(minx) - MARGIN_PX * px, float(miny) - MARGIN_PX * px
    width = int(np.ceil((float(maxx) - tx) / px)) + MARGIN_PX
    height = int(np.ceil((float(maxy) - ty) / px)) + MARGIN_PX

    dtype = np.uint16 if n < np.iinfo(np.uint16).max else np.uint32
    mask = np.zeros((height, width), dtype=dtype)
    for i in range(n):
        v0, v1 = int(polygons.offsets[i]), int(polygons.offsets[i + 1])
        ring = polygons.coords[v0:v1]
        rows = (ring[:, 1] - ty) / px
        cols = (ring[:, 0] - tx) / px
        rr, cc = draw_polygon(rows, cols, shape=mask.shape)
        mask[rr, cc] = i + 1  # 0 is background, so ids start at 1

    print(f"{n} polygons -> {height}x{width} px label mask ({mask.dtype}), {int((mask > 0).sum())} px filled")

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(out), mode="w")
    target = root.create_group(f"labels/{labels_group}") if labels_group else root

    # Written by hand rather than through ome_zarr.writer.write_image, which
    # for this one job does the two things a label mask cannot survive: it
    # builds a downsampled pyramid whatever `scaler` says (interpolating
    # between object ids, inventing objects nobody segmented), and it replaces
    # the coordinateTransformations passed to it with unit scale and zero
    # translation. The metadata below is all `cytos.core.image.pyramid_levels`
    # reads anyway.
    target.create_array("0", shape=mask.shape, chunks=(1024, 1024), dtype=mask.dtype)[:] = mask
    target.attrs["ome"] = {
        "version": "0.5",
        "multiscales": [
            {
                "name": labels_group or "labels",
                "axes": [
                    {"name": "y", "type": "space", "unit": "micrometer"},
                    {"name": "x", "type": "space", "unit": "micrometer"},
                ],
                "datasets": [
                    {
                        "path": "0",
                        "coordinateTransformations": [
                            {"type": "scale", "scale": [px, px]},
                            {"type": "translation", "translation": [ty, tx]},
                        ],
                    }
                ],
            }
        ],
    }
    if labels_group:
        # The NGFF convention ome-zarr-py and spatialdata write: the root
        # advertises which label images live under labels/.
        root.attrs["ome"] = {"version": "0.5", "labels": [labels_group]}
        root["labels"].attrs["ome"] = {"version": "0.5", "labels": [labels_group]}

    print(f"wrote {out}  scale={px} translation=({ty:.3f}, {tx:.3f})")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boundaries", type=Path, help="a long-format boundary parquet")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--px", type=float, default=0.2125, help="micrometres per pixel; default Xenium's 0.2125")
    parser.add_argument(
        "--labels-group",
        default=None,
        metavar="NAME",
        help="write as labels/NAME (the nested NGFF layout) instead of a plain multiscale at the root",
    )
    args = parser.parse_args()
    make_label_mask(args.boundaries, args.out, px=args.px, labels_group=args.labels_group)


if __name__ == "__main__":
    main()
