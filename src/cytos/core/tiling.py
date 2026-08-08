"""The flat world-space tile grid that both vector layers stream through:
cell polygons (`cytos.core.polygons`) and transcript points
(`cytos.core.points`).

Both caches are written by `cytos.prep` as `tile/<depth>/<row>/<col>` zarr
groups over the same square grid, so "which tiles does this camera rect touch,
and which of those actually exist on disk" is one question with one answer --
kept here rather than copied into each layer.

Tiles are indexed with world Y increasing upward, the same convention as
`cytos.core.image`'s pyramid levels, so no row flip is needed anywhere on this
path (see CLAUDE.md).
"""

from __future__ import annotations

import numpy as np
import zarr


def tile_world_size(world_bounds: tuple[float, float, float, float], tile_depth: int) -> float:
    """Side length of one tile, world units. The grid is square and covers the
    layer's *longest* axis, so a non-square dataset leaves part of the grid
    empty rather than stretching the tiles."""
    minx, miny, maxx, maxy = world_bounds
    return max(maxx - minx, maxy - miny) / (1 << tile_depth)


def visible_tile_keys(
    root: zarr.Group,
    tile_depth: int,
    world_bounds: tuple[float, float, float, float],
    world_rect: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    """(row, col) of every tile the camera rect touches *that exists in the
    cache*. Tissue rarely fills its own bounding square, so most of the grid
    is empty and checking is much cheaper than a failed read per tile."""
    minx, miny, maxx, maxy = world_rect
    bminx, bminy, bmaxx, bmaxy = world_bounds
    n = 1 << tile_depth
    size = tile_world_size(world_bounds, tile_depth)
    if size <= 0:
        return []

    col0 = max(0, int((minx - bminx) / size))
    col1 = min(n, int(np.ceil((maxx - bminx) / size)))
    row0 = max(0, int((miny - bminy) / size))
    row1 = min(n, int(np.ceil((maxy - bminy) / size)))
    if col0 >= col1 or row0 >= row1:
        return []

    existing = root["tile"][str(tile_depth)]
    keys = []
    for row in range(row0, row1):
        row_key = str(row)
        if row_key not in existing:
            continue
        row_group = existing[row_key]
        for col in range(col0, col1):
            if str(col) in row_group:
                keys.append((row, col))
    return keys
