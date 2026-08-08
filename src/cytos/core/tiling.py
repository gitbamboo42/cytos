"""The flat world-space tile grid that both vector layers stream through:
cell polygons (`cytos.core.polygons`) and transcript points
(`cytos.core.points`).

Every layer in a bundle shares one `world_bounds` and one `tile_depth` (both
fixed at import time and recorded in the manifest, see `cytos.core.bundle`), so
"which tiles does this camera rect touch" is one question with one answer --
kept here rather than copied into each layer.

Which tiles *exist* is answered from the manifest's tile index, an in-memory
set, not by probing the tile store. That matters more than it sounds: tissue
never fills its own bounding square, so most of the grid is empty and the check
has to happen on every camera move. Probing zarr for it measured ~8 ms per move
on the 167K-cell bundle -- about half a 60fps frame budget -- against 0.004 ms
from the index. It also keeps this module free of any storage-format import.

Tiles are indexed with world Y increasing upward, the same convention as
`cytos.core.image`'s pyramid levels, so no row flip is needed anywhere on this
path (see CLAUDE.md).
"""

from __future__ import annotations

import numpy as np


def tile_world_size(world_bounds: tuple[float, float, float, float], tile_depth: int) -> float:
    """Side length of one tile, world units. The grid is square and covers the
    layer's *longest* axis, so a non-square dataset leaves part of the grid
    empty rather than stretching the tiles."""
    minx, miny, maxx, maxy = world_bounds
    return max(maxx - minx, maxy - miny) / (1 << tile_depth)


def visible_tile_keys(
    tiles: set[tuple[int, int]],
    tile_depth: int,
    world_bounds: tuple[float, float, float, float],
    world_rect: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    """(row, col) of every tile the camera rect touches *that the layer
    actually wrote* -- `tiles` is the layer's tile index from the manifest."""
    minx, miny, maxx, maxy = world_rect
    bminx, bminy, bmaxx, bmaxy = world_bounds
    n = 1 << tile_depth
    size = tile_world_size(world_bounds, tile_depth)
    if size <= 0 or not tiles:
        return []

    col0 = max(0, int((minx - bminx) / size))
    col1 = min(n, int(np.ceil((maxx - bminx) / size)))
    row0 = max(0, int((miny - bminy) / size))
    row1 = min(n, int(np.ceil((maxy - bminy) / size)))
    if col0 >= col1 or row0 >= row1:
        return []

    return [(row, col) for row in range(row0, row1) for col in range(col0, col1) if (row, col) in tiles]
