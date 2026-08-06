"""Generate a large synthetic OME-Zarr pyramid to stress-test Phase 0's tile
streaming at a scale our one real dataset (6915x2963) doesn't reach.

Pattern is deliberately structured, not random noise: each native chunk gets a
constant "chunk ID" band (checkerboard-ish, visible at whole-image zoom) plus a
fine ripple only resolvable at native zoom. That gives two independent visual
checks: does the coarse view actually show the checkerboard (proof a downsampled
level is being used, not a blocky degrade of level 0), and does zooming into one
chunk reveal the ripple (proof native-res tiles carry real per-pixel detail, not
just the coarse block value).

Usage:
    python tools/make_synthetic_big_pyramid.py --size 16384 --out data/synthetic_big.ome.zarr
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import dask.array as da
import numpy as np
from ome_zarr.io import parse_url
from ome_zarr.writer import write_image
from zarr import group as zarr_group

CHUNK = 1024


def make_block(block, block_info=None):
    loc = block_info[0]["array-location"]
    (y0, y1), (x0, x1) = loc
    cy, cx = y0 // CHUNK, x0 // CHUNK
    checker = 40000.0 if (cy + cx) % 2 == 0 else 5000.0

    yy, xx = np.mgrid[y0:y1, x0:x1]
    ripple = 3000.0 * (np.sin(yy * 0.3) * np.cos(xx * 0.3))
    return np.clip(checker + ripple, 0, 65535).astype(np.uint16)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=16384, help="side length in pixels")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pixel-size", type=float, default=0.2125, help="um/px, matches the real dataset")
    args = parser.parse_args()

    n = args.size
    # map_blocks needs something to map over for per-block location info;
    # an empty-graph zeros array is the cheapest source of that.
    template = da.zeros((n, n), chunks=(CHUNK, CHUNK), dtype=np.uint16)
    image = template.map_blocks(make_block, dtype=np.uint16)

    print(f"synthetic image: {n}x{n} uint16, chunk={CHUNK}, ~{n*n*2/1e9:.2f} GB uncompressed at level 0")

    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    store = parse_url(str(args.out), mode="w").store
    root = zarr_group(store=store)
    write_image(
        image=image,
        group=root,
        axes="yx",
        scale={"y": args.pixel_size, "x": args.pixel_size},
        axes_units={"y": "micrometer", "x": "micrometer"},
        storage_options=dict(chunks=(CHUNK, CHUNK)),
    )
    t1 = time.perf_counter()
    print(f"wrote {args.out} in {t1-t0:.1f}s")

    du = sum(f.stat().st_size for f in args.out.rglob("*") if f.is_file())
    print(f"on-disk size: {du/1e9:.2f} GB")


if __name__ == "__main__":
    main()
