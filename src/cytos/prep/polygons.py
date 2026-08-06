"""Offline preprocessing for the polygon layer: triangulate every cell once,
Hilbert-sort so spatially-close cells become memory-close, tile into a flat
world-space grid, write to a zarr cache. See work-notes/plan.md Phase 2 --
this is the precomputation that buys the renderer its speed; napari
triangulates inside add_shapes() instead.

Tiling reuses the Hilbert curve itself rather than a separate quadtree
structure: a curve of `order` bits per axis visits every order-L quadrant as
one contiguous run, so grouping cells by their top (order-L) grid bits after
sorting by Hilbert index gives quadtree tiles for free, each already a
contiguous slice of the sorted arrays (verified in work-notes -- see
`Polygons`/`load_polygons` in cytos.core.polygons for the loader this
extends).

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-prep-polygons data/xenium_breast_cancer_rep1/cell_boundaries.parquet \
        --cells data/xenium_breast_cancer_rep1/cells.parquet \
        --tile-size 500 \
        --out data/xenium_breast_cancer_rep1/polygons_cache/
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import mapbox_earcut as earcut
import numpy as np
import pyarrow.parquet as pq
import zarr

from cytos.core.polygons import load_polygons

_HILBERT_ORDER = 16  # bits/axis -- 65536x65536 grid, far finer than any real cell spacing


def _hilbert_index(order: int, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Distance along a 2**order x 2**order Hilbert curve for each (x, y)
    grid point. Standard iterative xy2d (Wikipedia), vectorized. Each
    order-L quadtree quadrant maps to one contiguous range of the result --
    that's the property tiling below relies on."""
    n = 1 << order
    x = x.astype(np.int64).copy()
    y = y.astype(np.int64).copy()
    d = np.zeros(x.shape, dtype=np.int64)
    s = n >> 1
    while s > 0:
        rx = ((x & s) > 0).astype(np.int64)
        ry = ((y & s) > 0).astype(np.int64)
        d += s * s * ((3 * rx) ^ ry)
        swap = ry == 0
        flip = swap & (rx == 1)
        x_new = np.where(flip, n - 1 - x, x)
        y_new = np.where(flip, n - 1 - y, y)
        x, y = np.where(swap, y_new, x_new), np.where(swap, x_new, y_new)
        s >>= 1
    return d


def _reorder_ragged(flat: np.ndarray, offsets: np.ndarray, perm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Reorder a ragged array's *groups* per perm (perm[j] = which original
    group is now at position j); each group's own internal element order is
    left untouched. Used for both `coords` (per-cell vertex blocks) and the
    per-cell triangle-index blocks below -- same ragged-gather either way."""
    counts = offsets[1:] - offsets[:-1]
    new_counts = counts[perm]
    new_offsets = np.empty(len(offsets), dtype=np.int64)
    new_offsets[0] = 0
    np.cumsum(new_counts, out=new_offsets[1:])
    total = int(new_offsets[-1])
    group_id = np.repeat(np.arange(len(perm)), new_counts)
    within = np.arange(total) - new_offsets[group_id]
    src = offsets[perm][group_id] + within
    return flat[src], new_offsets


def _triangulate_per_cell(coords: np.ndarray, offsets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """mapbox_earcut per cell (no hole support needed -- Xenium boundaries are
    simple rings). Returns local (0-based within each cell) triangle vertex
    indices as one flat ragged array, plus its own per-cell offsets -- in
    units of index *count* (3 per triangle), not triangle count."""
    tri_chunks = []
    tri_idx_counts = np.empty(len(offsets) - 1, dtype=np.int64)
    for i in range(len(offsets) - 1):
        verts = coords[offsets[i] : offsets[i + 1]]
        rings = np.array([len(verts)], dtype=np.uint32)
        tris = earcut.triangulate_float32(verts, rings)
        tri_chunks.append(tris)
        tri_idx_counts[i] = len(tris)
    tri_local_flat = np.concatenate(tri_chunks).astype(np.uint32) if tri_chunks else np.empty(0, np.uint32)
    tri_offsets = np.concatenate([[0], np.cumsum(tri_idx_counts)]).astype(np.int64)
    return tri_local_flat, tri_offsets


def prep_polygons(
    boundaries_path: Path,
    out_dir: Path,
    cells_path: Path | None = None,
    tile_size: float = 500.0,
    hilbert_order: int = _HILBERT_ORDER,
) -> None:
    polygons = load_polygons(boundaries_path, cells_path)
    n_cells = len(polygons.offsets) - 1
    offsets = polygons.offsets.astype(np.int64)

    tri_local_flat, tri_offsets = _triangulate_per_cell(polygons.coords, offsets)

    # Per-cell centroid (mean of its own vertices) drives Hilbert order --
    # quantized into the curve's grid, not left in world (float) units.
    vcounts = (offsets[1:] - offsets[:-1]).astype(np.float64)
    cell_of_vertex = np.repeat(np.arange(n_cells), offsets[1:] - offsets[:-1])
    vertex_sums = np.zeros((n_cells, 2), dtype=np.float64)
    np.add.at(vertex_sums, cell_of_vertex, polygons.coords.astype(np.float64))
    centroids = vertex_sums / vcounts[:, None]

    minx, miny = polygons.coords[:, 0].min(), polygons.coords[:, 1].min()
    maxx, maxy = polygons.coords[:, 0].max(), polygons.coords[:, 1].max()
    span = max(maxx - minx, maxy - miny)
    grid_n = 1 << hilbert_order
    gx = np.clip(((centroids[:, 0] - minx) / span * grid_n).astype(np.int64), 0, grid_n - 1)
    gy = np.clip(((centroids[:, 1] - miny) / span * grid_n).astype(np.int64), 0, grid_n - 1)
    d = _hilbert_index(hilbert_order, gx, gy)
    perm = np.argsort(d, kind="stable")

    new_coords, new_offsets = _reorder_ragged(polygons.coords, offsets, perm)
    new_tri_local, new_tri_offsets = _reorder_ragged(tri_local_flat, tri_offsets, perm)
    new_vcounts = new_offsets[1:] - new_offsets[:-1]
    new_tricounts = new_tri_offsets[1:] - new_tri_offsets[:-1]

    # Local (per-cell 0-based) -> global (into new_coords) triangle indices.
    tri_group_id = np.repeat(np.arange(n_cells), new_tricounts)
    global_tri_idx = new_tri_local.astype(np.int64) + new_offsets[:-1][tri_group_id]
    # Dense post-sort cell id, per vertex -- what the renderer's per-vertex
    # LUT lookup (Phase 3) indexes a global color texture by.
    vertex_cell_id = np.repeat(np.arange(n_cells), new_vcounts).astype(np.uint32)

    tile_depth = int(np.clip(round(np.log2(max(span / tile_size, 1.0))), 0, hilbert_order))
    tile_row = (gy[perm] >> (hilbert_order - tile_depth)).astype(np.int64) if tile_depth else np.zeros(n_cells, np.int64)
    tile_col = (gx[perm] >> (hilbert_order - tile_depth)).astype(np.int64) if tile_depth else np.zeros(n_cells, np.int64)
    tile_key = tile_row * (1 << tile_depth) + tile_col
    run_start = np.empty(n_cells, dtype=bool)
    run_start[0] = True
    run_start[1:] = tile_key[1:] != tile_key[:-1]
    cell_run_starts = np.flatnonzero(run_start)
    cell_run_ends = np.append(cell_run_starts[1:], n_cells)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    root = zarr.open_group(str(out_dir / "tiles.zarr"), mode="w")
    root.attrs["hilbert_order"] = hilbert_order
    root.attrs["tile_depth"] = tile_depth
    root.attrs["world_bounds"] = [float(minx), float(miny), float(maxx), float(maxy)]
    root.attrs["n_cells"] = n_cells
    root.attrs["n_vertices"] = int(len(new_coords))
    root.attrs["n_triangles"] = int(len(global_tri_idx) // 3)

    for cell_j0, cell_j1 in zip(cell_run_starts, cell_run_ends):
        row, col = int(tile_row[cell_j0]), int(tile_col[cell_j0])
        v0, v1 = int(new_offsets[cell_j0]), int(new_offsets[cell_j1])
        ti0, ti1 = int(new_tri_offsets[cell_j0]), int(new_tri_offsets[cell_j1])
        group = root.create_group(f"tile/{tile_depth}/{row}/{col}")
        group.create_array("coords", data=new_coords[v0:v1])
        group.create_array("triangle_indices", data=(global_tri_idx[ti0:ti1] - v0).astype(np.uint32))
        group.create_array("vertex_cell_id", data=vertex_cell_id[v0:v1])

    features = polygons.features.take(perm)
    pq.write_table(features, out_dir / "features.parquet")

    print(
        f"wrote {out_dir}: {n_cells} cells, {len(new_coords)} vertices, "
        f"{len(global_tri_idx) // 3} triangles, tile_depth={tile_depth} "
        f"({len(cell_run_starts)} tiles)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boundaries", type=Path, help="e.g. cell_boundaries.parquet")
    parser.add_argument("--cells", type=Path, default=None, help="e.g. cells.parquet, for per-cell features")
    parser.add_argument("--tile-size", type=float, default=500.0, help="target tile size, world units (um)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prep_polygons(args.boundaries, args.out, cells_path=args.cells, tile_size=args.tile_size)


if __name__ == "__main__":
    main()
