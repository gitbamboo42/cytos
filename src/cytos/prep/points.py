"""Offline preprocessing for the transcript-point layer: Hilbert-sort so
spatially-close transcripts become memory-close, tile into the bundle's shared
world-space grid -- the same one the polygon layer uses -- and write a zarr
cache.

Much lighter than `cytos.prep.polygons` -- there's nothing to triangulate, one
row per transcript -- but the same reason to do it offline: a real whole-slide
Xenium run has tens of millions of transcripts, so the viewer must be able to
read just the visible tiles instead of parsing the whole parquet at startup.

The per-tile `gene_id` array is a dense index into `genes.parquet`, not a
repeated gene *name* string: names cost far more to store and can't index a
colour LUT on the GPU.

Not a command of its own: `cytos-import` (`cytos.prep.bundle`) drives this, so
that the world bounds and tile depth are the bundle's rather than this layer's.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import zarr

from cytos.core.points import Transcripts
from cytos.prep.tiling import HILBERT_ORDER, run_bounds, sort_and_tile


def prep_points(
    transcripts: Transcripts,
    out_dir: Path,
    world_bounds: tuple[float, float, float, float],
    tile_depth: int,
    hilbert_order: int = HILBERT_ORDER,
) -> dict:
    """Write `out_dir/{tiles.zarr, genes.parquet}`. Takes an already-loaded
    `Transcripts` for the same reason `prep_polygons` takes `Polygons`: the
    importer needs this layer's extent to compute the bundle's shared world
    bounds, and parsing tens of millions of rows twice is not free.

    Returns this layer's manifest fields, including the `tiles` index.
    """
    coords = transcripts.coords
    n_points = len(coords)
    if n_points == 0:
        raise ValueError("no transcripts left after filtering")

    # Points are their own anchor -- unlike polygons, where a whole cell has to
    # be reduced to one centroid before it can be sorted.
    perm, tile_row, tile_col = sort_and_tile(coords, world_bounds, tile_depth, hilbert_order)
    sorted_coords = coords[perm]
    sorted_gene_id = transcripts.gene_id[perm]

    tile_key = tile_row * (1 << tile_depth) + tile_col
    starts, ends = run_bounds(tile_key)

    out_dir.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(out_dir / "tiles.zarr"), mode="w")
    root.attrs["hilbert_order"] = hilbert_order
    root.attrs["tile_depth"] = tile_depth
    root.attrs["world_bounds"] = [float(v) for v in world_bounds]
    root.attrs["n_points"] = n_points

    tiles = []
    for j0, j1 in zip(starts, ends):
        row, col = int(tile_row[j0]), int(tile_col[j0])
        group = root.create_group(f"tile/{tile_depth}/{row}/{col}")
        group.create_array("coords", data=sorted_coords[j0:j1])
        group.create_array("gene_id", data=sorted_gene_id[j0:j1].astype(np.uint32))
        tiles.append([row, col])

    # Counts here, not in the viewer: the UI lists genes most-abundant-first,
    # and counting tens of millions of rows at startup is exactly the work this
    # cache exists to avoid.
    counts = np.bincount(transcripts.gene_id, minlength=len(transcripts.gene_names))
    genes = pa.table({"name": pa.array(transcripts.gene_names), "count": pa.array(counts.astype(np.int64))})
    pq.write_table(genes, out_dir / "genes.parquet")

    return {
        "n_points": n_points,
        "n_genes": len(transcripts.gene_names),
        "tile_depth": tile_depth,
        "tiles": tiles,
    }
