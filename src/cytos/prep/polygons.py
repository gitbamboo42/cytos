"""Offline preprocessing for the polygon layer: triangulate every cell once,
Hilbert-sort so spatially-close cells become memory-close, tile into the
slide's shared world-space grid, write to a zarr cache. See
work-notes/plan.md Phase 2 -- this is the precomputation that buys the
renderer its speed; napari triangulates inside add_shapes() instead.

Tiling reuses the Hilbert curve itself rather than a separate quadtree
structure: a curve of `order` bits per axis visits every order-L quadrant as
one contiguous run, so grouping cells by their top (order-L) grid bits after
sorting by Hilbert index gives quadtree tiles for free, each already a
contiguous slice of the sorted arrays (verified in work-notes -- see
`Polygons`/`load_polygons` in cytos.core.polygons for the loader this
extends).

Not a command of its own: `cytos-import` (`cytos.prep.slide`) drives this,
because the world bounds and tile depth are the *slide's*, decided once
across every layer. Running it standalone would put this layer on a grid that
no other layer in the slide shares.
"""

from __future__ import annotations

from pathlib import Path

import mapbox_earcut as earcut
import numpy as np
import pyarrow.parquet as pq
import zarr

from cytos.core.polygons import Polygons
from cytos.prep.tiling import HILBERT_ORDER as _HILBERT_ORDER, run_bounds, sort_and_tile


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
    polygons: Polygons,
    out_dir: Path,
    world_bounds: tuple[float, float, float, float],
    tile_depth: int,
    hilbert_order: int = _HILBERT_ORDER,
) -> dict:
    """Write `out_dir/{tiles.zarr, features.parquet}`. Takes an already-loaded
    `Polygons` rather than a path so the importer can compute the slide's
    shared world bounds from it without parsing the parquet twice.

    Returns the manifest fields for this layer -- notably `tiles`, the index of
    every (row, col) actually written, which is what the viewer uses to answer
    "which tiles are visible" without touching the store.
    """
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

    perm, tile_row, tile_col = sort_and_tile(centroids, world_bounds, tile_depth, hilbert_order)

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

    tile_key = tile_row * (1 << tile_depth) + tile_col
    cell_run_starts, cell_run_ends = run_bounds(tile_key)

    out_dir.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(out_dir / "tiles.zarr"), mode="w")
    # Self-describing, so a stray tile store can still be identified -- but the
    # slide manifest is what the viewer actually reads.
    root.attrs["hilbert_order"] = hilbert_order
    root.attrs["tile_depth"] = tile_depth
    root.attrs["world_bounds"] = [float(v) for v in world_bounds]
    root.attrs["n_cells"] = n_cells

    tiles = []
    for cell_j0, cell_j1 in zip(cell_run_starts, cell_run_ends):
        row, col = int(tile_row[cell_j0]), int(tile_col[cell_j0])
        v0, v1 = int(new_offsets[cell_j0]), int(new_offsets[cell_j1])
        ti0, ti1 = int(new_tri_offsets[cell_j0]), int(new_tri_offsets[cell_j1])
        group = root.create_group(f"tile/{tile_depth}/{row}/{col}")
        group.create_array("coords", data=new_coords[v0:v1])
        group.create_array("triangle_indices", data=(global_tri_idx[ti0:ti1] - v0).astype(np.uint32))
        group.create_array("vertex_cell_id", data=vertex_cell_id[v0:v1])
        tiles.append([row, col])

    features = polygons.features.take(perm)
    pq.write_table(features, out_dir / "features.parquet")

    return {
        "n_cells": n_cells,
        "n_vertices": int(len(new_coords)),
        "n_triangles": int(len(global_tri_idx) // 3),
        "tile_depth": tile_depth,
        "tiles": tiles,
    }
