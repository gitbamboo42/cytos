"""OME-Zarr pyramid data model: level metadata, level selection, and chunk-grid
tiling math. Pure numpy and no GPU: drawing is the viewer's job, in
`viewer/src/render/image.ts`.

Reuses OME-Zarr's own chunk grid as the tile grid rather than inventing a
second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zarr

from cytos.core.store import open_zarr_group


@dataclass
class PyramidLevel:
    index: int
    data: zarr.Array  # sliced lazily; only visible chunks are ever read
    shape: tuple[int, int]  # (H, W) px
    scale: tuple[float, float]  # (y, x) um/px
    translation: tuple[float, float]  # (y, x) um
    chunk_shape: tuple[int, int]  # (H, W) px

    def world_bounds(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) in world (um) space.

        World Y increases *downward*, the same direction as the pixel rows it
        comes from -- and the same convention as napari, QuPath, Xenium's own
        coordinates, and XYZ map tiles. So row->world_y is a plain scale and
        offset with no flip anywhere on the data path, and world bounds come
        out positive.

        Whether a flip is needed to put row 0 at the top of the screen is
        the view's business, not the data's -- deck.gl's OrthographicView
        already points y down and needs none. A display convention belongs
        there, in one place, never on the data path.
        """
        h, w = self.shape
        sy, sx = self.scale
        ty, tx = self.translation
        minx, maxx = tx, tx + w * sx
        miny, maxy = self.row_to_world_y(0), self.row_to_world_y(h)
        return minx, miny, maxx, maxy

    def row_to_world_y(self, row: float) -> float:
        ty, _ = self.translation
        sy, _ = self.scale
        return ty + row * sy

    def world_y_to_row(self, world_y: float) -> float:
        ty, _ = self.translation
        sy, _ = self.scale
        return (world_y - ty) / sy


def load_pyramid_levels(path: Path) -> list[PyramidLevel]:
    """Read an OME-NGFF multiscale pyramid's levels from a store directory or
    a zipped store (`cytos.core.store`).

    Reads the `multiscales` metadata directly rather than going through
    ome-zarr-py: all that's wanted from it is the per-level scale, translation
    and array, every consumer here only ever does
    `np.asarray(level.data[rows, cols])`, and a plain zarr array does that as
    well as a dask one. Doing it here keeps `cytos.core` off both ome-zarr and
    dask, and is what lets a zipped store open at all -- ome-zarr's
    `parse_url` returns None for a zip.
    """
    path = Path(path)
    # Anyone can point a file dialog at the wrong folder. Name the path in the
    # message; the errors underneath don't.
    if not path.exists():
        raise ValueError(f"{path}: not an OME-Zarr image — nothing there")
    try:
        root = open_zarr_group(path)
    except ValueError as err:  # zarr's GroupNotFoundError is a ValueError
        raise ValueError(f"{path}: not an OME-Zarr image — no zarr store there") from err
    return pyramid_levels(root, str(path))


def pyramid_levels(root: zarr.Group, label: str) -> list[PyramidLevel]:
    """The levels of an already-open multiscale group. Split out from
    `load_pyramid_levels` because an OME-NGFF label mask keeps its multiscale
    one group down, under `labels/<name>` (see `cytos.prep.labels`), which no
    path on its own can address inside a zipped store. `label` names the source
    in error messages."""
    attrs = dict(root.attrs)
    # NGFF 0.5 (zarr v3) nests its metadata under "ome"; 0.4 (zarr v2) puts
    # "multiscales" straight at the top level. Both are still in the wild.
    multiscales = attrs.get("ome", attrs).get("multiscales")
    if not multiscales:
        raise ValueError(f"{label}: a zarr store, but holds no OME-Zarr image")

    levels = []
    for i, dataset in enumerate(multiscales[0]["datasets"]):
        data = root[dataset["path"]]
        transforms = dataset.get("coordinateTransformations", [])
        scale = next((t["scale"] for t in transforms if t["type"] == "scale"), None)
        if scale is None:
            raise ValueError(f"{label}: level {dataset['path']} has no scale transform")
        translation = next(
            (t["translation"] for t in transforms if t["type"] == "translation"),
            (0.0, 0.0),
        )
        levels.append(
            PyramidLevel(
                index=i,
                data=data,
                shape=tuple(data.shape),
                scale=tuple(scale),
                translation=tuple(translation),
                chunk_shape=tuple(data.chunks),
            )
        )
    return levels


def select_scale(scales: list[float], world_per_px: float) -> int:
    """THE level-selection rule -- every pyramid in cytos goes through this
    one function, so no layer can drift onto its own switching logic.
    `scales` is the ladder: each level's resolution in world units per
    screen pixel, finest first, coarsening as you climb. Picks the coarsest
    level still at least as fine as the screen, clamping to the finest level
    zoomed in past the ladder and the coarsest zoomed out past it. What
    differs between layers is only the ladder each one brings: an image
    pyramid brings its levels' actual pixel sizes, a point layer brings its
    aggregation ladder (see `cytos.core.points.select_point_level`)."""
    best = 0
    for i, scale in enumerate(scales):
        if scale <= world_per_px:
            best = i
        else:
            break
    return best


def select_level(levels: list[PyramidLevel], world_per_px: float) -> int:
    """Coarsest level whose pixel size is still <= world_per_px (i.e. still >=1
    screen pixel). Clamps to level 0 if zoomed in past native resolution, and to
    the coarsest level if zoomed out past it."""
    return select_scale([lvl.scale[0] for lvl in levels], world_per_px)


def visible_chunk_keys(level: PyramidLevel, world_rect: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """world_rect = (minx, miny, maxx, maxy), world Y increasing downward.
    Returns (chunk_row, chunk_col) keys."""
    minx, miny, maxx, maxy = world_rect
    h, w = level.shape
    _, sx = level.scale
    _, tx = level.translation
    ch, cw = level.chunk_shape

    px0 = max(0, int((minx - tx) / sx))
    px1 = min(w, int(np.ceil((maxx - tx) / sx)))
    # World Y runs the same way as pixel rows, so miny is the smaller row.
    row_lo = level.world_y_to_row(miny)
    row_hi = level.world_y_to_row(maxy)
    py0 = max(0, int(row_lo))
    py1 = min(h, int(np.ceil(row_hi)))
    if px0 >= px1 or py0 >= py1:
        return []

    cy0, cy1 = py0 // ch, (py1 - 1) // ch
    cx0, cx1 = px0 // cw, (px1 - 1) // cw
    return [(cy, cx) for cy in range(cy0, cy1 + 1) for cx in range(cx0, cx1 + 1)]
