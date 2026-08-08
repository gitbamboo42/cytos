"""OME-Zarr pyramid data model: level metadata, level selection, and chunk-grid
tiling math. Pure numpy — no pygfx/GPU dependency, see `cytos.render.image` for
that.

Reuses OME-Zarr's own chunk grid as the tile grid (see work-notes/plan.md's open
question on this) rather than inventing a second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ome_zarr.io import parse_url
from ome_zarr.reader import Reader


@dataclass
class PyramidLevel:
    index: int
    data: "dask.array.Array"  # noqa: F821
    shape: tuple[int, int]  # (H, W) px
    scale: tuple[float, float]  # (y, x) um/px
    translation: tuple[float, float]  # (y, x) um
    chunk_shape: tuple[int, int]  # (H, W) px

    def world_bounds(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) in world (um) space.

        World Y increases upward (standard Cartesian / pygfx-camera
        convention), matching how the data actually gets displayed (row 0 at
        the top of the screen). Pixel rows increase downward, so row->world_y
        is a flip: see `row_to_world_y` / `world_y_to_row`.
        """
        h, w = self.shape
        sy, sx = self.scale
        ty, tx = self.translation
        minx, maxx = tx, tx + w * sx
        miny, maxy = self.row_to_world_y(h), self.row_to_world_y(0)
        return minx, miny, maxx, maxy

    def row_to_world_y(self, row: float) -> float:
        ty, _ = self.translation
        sy, _ = self.scale
        return -(ty + row * sy)

    def world_y_to_row(self, world_y: float) -> float:
        ty, _ = self.translation
        sy, _ = self.scale
        return (-world_y - ty) / sy


def load_pyramid_levels(path: Path) -> list[PyramidLevel]:
    # parse_url returns None for anything that isn't a zarr store, and
    # ome_zarr's Reader then asserts on it -- an AttributeError deep in a
    # dependency, with no mention of the path that caused it. Anyone can point
    # a file dialog at the wrong folder, so say what's actually wrong.
    url = parse_url(str(path))
    if url is None:
        raise ValueError(f"{path}: not an OME-Zarr image — no zarr store there")
    nodes = list(Reader(url)())
    if not nodes:
        raise ValueError(f"{path}: a zarr store, but holds no OME-Zarr image")
    node = nodes[0]
    transforms = node.metadata["coordinateTransformations"]
    levels = []
    for i, data in enumerate(node.data):
        scale = next(t["scale"] for t in transforms[i] if t["type"] == "scale")
        translation = next(
            (t["translation"] for t in transforms[i] if t["type"] == "translation"),
            (0.0, 0.0),
        )
        levels.append(
            PyramidLevel(
                index=i,
                data=data,
                shape=tuple(data.shape),
                scale=tuple(scale),
                translation=tuple(translation),
                chunk_shape=tuple(data.chunksize),
            )
        )
    return levels


def select_level(levels: list[PyramidLevel], world_per_px: float) -> int:
    """Coarsest level whose pixel size is still <= world_per_px (i.e. still >=1
    screen pixel). Clamps to level 0 if zoomed in past native resolution, and to
    the coarsest level if zoomed out past it."""
    best = 0
    for lvl in levels:
        if lvl.scale[0] <= world_per_px:
            best = lvl.index
        else:
            break
    return best


def visible_chunk_keys(level: PyramidLevel, world_rect: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """world_rect = (minx, miny, maxx, maxy), world Y increasing upward.
    Returns (chunk_row, chunk_col) keys."""
    minx, miny, maxx, maxy = world_rect
    h, w = level.shape
    _, sx = level.scale
    _, tx = level.translation
    ch, cw = level.chunk_shape

    px0 = max(0, int((minx - tx) / sx))
    px1 = min(w, int(np.ceil((maxx - tx) / sx)))
    # World Y is flipped relative to pixel rows: larger world_y -> smaller row.
    row_lo = level.world_y_to_row(maxy)
    row_hi = level.world_y_to_row(miny)
    py0 = max(0, int(row_lo))
    py1 = min(h, int(np.ceil(row_hi)))
    if px0 >= px1 or py0 >= py1:
        return []

    cy0, cy1 = py0 // ch, (py1 - 1) // ch
    cx0, cx1 = px0 // cw, (px1 - 1) // cw
    return [(cy, cx) for cy in range(cy0, cy1 + 1) for cx in range(cx0, cx1 + 1)]
