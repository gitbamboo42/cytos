"""Hilbert sorting and tile assignment, shared by both prep steps
(`cytos.prep.polygons`, `cytos.prep.points`).

The trick both rely on: a Hilbert curve of `order` bits per axis visits every
order-L quadrant as one *contiguous* run, so sorting by Hilbert index and then
grouping by the top L grid bits yields quadtree tiles for free -- each already
a contiguous slice of the sorted arrays, no separate tree structure to build or
store. Sorting also makes spatially-close items memory-close, which is what
makes a tile read one sequential block.
"""

from __future__ import annotations

import numpy as np

HILBERT_ORDER = 16  # bits/axis -- 65536x65536 grid, far finer than any real feature spacing


def hilbert_index(order: int, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Distance along a 2**order x 2**order Hilbert curve for each (x, y) grid
    point. Standard iterative xy2d (Wikipedia), vectorized."""
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


def choose_tile_depth(
    world_bounds: tuple[float, float, float, float],
    tile_size: float,
    hilbert_order: int = HILBERT_ORDER,
) -> int:
    """Grid depth whose tiles come closest to `tile_size` world units across.

    Split out from `sort_and_tile` so the *bundle* can pick one depth once and
    hand the same one to every layer -- when each layer chose its own from its
    own extent, the polygon and point caches ended up on grids that didn't line
    up (see `cytos.core.bundle`).
    """
    minx, miny, maxx, maxy = world_bounds
    span = max(maxx - minx, maxy - miny)
    return int(np.clip(round(np.log2(max(span / tile_size, 1.0))), 0, hilbert_order))


def sort_and_tile(
    anchors: np.ndarray,
    world_bounds: tuple[float, float, float, float],
    tile_depth: int,
    hilbert_order: int = HILBERT_ORDER,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`anchors` is one representative world (x, y) per item -- a cell's
    centroid for polygons, the point itself for transcripts.

    `world_bounds` and `tile_depth` come from the bundle, not from `anchors`:
    a layer that doesn't span the whole slide still has to land on the shared
    grid.

    Returns `(perm, tile_row, tile_col)`: `perm` puts the items in Hilbert
    order, and `tile_row`/`tile_col` are already in that sorted order (i.e.
    they describe `anchors[perm]`, not `anchors`), so the caller can slice
    tiles straight out of its reordered arrays.
    """
    minx, miny, maxx, maxy = world_bounds
    span = max(maxx - minx, maxy - miny)
    grid_n = 1 << hilbert_order
    gx = np.clip(((anchors[:, 0] - minx) / span * grid_n).astype(np.int64), 0, grid_n - 1)
    gy = np.clip(((anchors[:, 1] - miny) / span * grid_n).astype(np.int64), 0, grid_n - 1)

    perm = np.argsort(hilbert_index(hilbert_order, gx, gy), kind="stable")

    if tile_depth:
        shift = hilbert_order - tile_depth
        tile_row = (gy[perm] >> shift).astype(np.int64)
        tile_col = (gx[perm] >> shift).astype(np.int64)
    else:
        tile_row = np.zeros(len(perm), np.int64)
        tile_col = np.zeros(len(perm), np.int64)
    return perm, tile_row, tile_col


def run_bounds(keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start/end index of each run of equal consecutive values -- the tile
    boundaries in an already-Hilbert-sorted array."""
    n = len(keys)
    if n == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    run_start = np.empty(n, dtype=bool)
    run_start[0] = True
    run_start[1:] = keys[1:] != keys[:-1]
    starts = np.flatnonzero(run_start)
    return starts, np.append(starts[1:], n)
