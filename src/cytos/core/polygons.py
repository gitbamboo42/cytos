"""Cell segmentation polygon data model: ragged-array boundary geometry plus
per-cell attributes. Pure numpy/pyarrow -- no pygfx/GPU dependency, see
cytos.render for that (work-notes/plan.md Phase 1).

Raw vertex coordinates are already in the same world space as
cytos.core.image's PyramidLevel (world Y increasing downward, matching pixel
rows), so loading is a straight column read with no reprojection -- Xenium's
row-major Y and cytos world Y agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import zarr

from cytos.core.slide import SegmentLayer
from cytos.core.store import open_tile_store
from cytos.core.tiling import tile_world_size, visible_tile_keys


@dataclass
class Polygons:
    coords: np.ndarray  # (M, 2) float32, world space, C-contiguous
    offsets: np.ndarray  # (N+1,) uint32 -- polygon i = coords[offsets[i]:offsets[i+1]]
    cell_id: np.ndarray  # (N,) uint32, dense 0..N-1 -- GPU vertex/LUT index (Phase 3)
    features: pa.Table  # per-cell attributes, row i matches cell_id[i]; always has "id"


def load_polygons(boundaries_path: Path, cells_path: Path | None = None) -> Polygons:
    """Load a Xenium `*_boundaries.parquet` (long format: one row per vertex,
    consecutive rows already grouped by cell -- verified against both the
    kidney and breast-cancer datasets) into the ragged-array model.

    If `cells_path` is given (e.g. `cells.parquet`), its per-cell attributes
    are left-joined onto `features` by the dataset's original cell id
    (string or int, whichever the slide uses), restoring row order to match
    `cell_id`/`offsets` afterward since Arrow joins don't preserve it.
    """
    table = pq.read_table(boundaries_path, columns=["cell_id", "vertex_x", "vertex_y"])
    ids = table.column("cell_id").to_numpy(zero_copy_only=False)

    run_start = np.empty(len(ids), dtype=bool)
    run_start[0] = True
    run_start[1:] = ids[1:] != ids[:-1]
    starts = np.flatnonzero(run_start)
    original_ids = pa.array(ids[starts])
    offsets = np.append(starts, len(ids)).astype(np.uint32)

    coords = np.empty((len(ids), 2), dtype=np.float32)
    coords[:, 0] = table.column("vertex_x").to_numpy()
    coords[:, 1] = table.column("vertex_y").to_numpy()
    coords = np.ascontiguousarray(coords)

    cell_id = np.arange(len(original_ids), dtype=np.uint32)
    features = pa.table({"id": original_ids})

    if cells_path is not None:
        cells = pq.read_table(cells_path)
        cells = cells.rename_columns(["id" if c == "cell_id" else c for c in cells.column_names])
        # cells.parquet's id column can differ in Arrow subtype from the
        # boundaries file's (e.g. large_string vs. string) even when both
        # hold the same values -- join() requires an exact type match.
        cells = cells.set_column(cells.column_names.index("id"), "id", cells.column("id").cast(original_ids.type))
        joined = features.join(cells, keys="id", join_type="left outer")
        order = pc.index_in(joined.column("id"), original_ids)
        features = joined.take(pc.sort_indices(order))

    return Polygons(coords=coords, offsets=offsets, cell_id=cell_id, features=features)


@dataclass
class PolygonTile:
    """One tile's geometry, as plain numpy. The renderer only ever sees this,
    never the store it came out of -- which is what lets the tile format change
    (`SegmentLayer.format`) without touching `cytos.render`."""

    coords: np.ndarray  # (V, 2) float32, world space
    triangle_indices: np.ndarray  # (T*3,) uint32, local to this tile's coords
    vertex_cell_id: np.ndarray  # (V,) uint32, dense cell id -- indexes the color LUT


@dataclass
class PolygonTileGrid:
    """A `cytos.prep.polygons` cache: triangulated, Hilbert-sorted polygon
    tiles at a single flat-grid depth (see that module for why one depth is
    enough -- coarse zoom gets a raster stand-in instead, Phase 2/3). Reading
    a tile is a cheap array read, not a re-triangulation.

    `features` is the cache's per-cell attribute table, already permuted into
    dense-cell-id order at prep time (`prep_polygons` writes
    `features.take(perm)`), so row i *is* cell i -- what the renderer's color
    LUT is indexed by.
    """

    store: zarr.Group
    tile_depth: int
    world_bounds: tuple[float, float, float, float]  # (minx, miny, maxx, maxy), world space
    n_cells: int
    tiles: set[tuple[int, int]]  # which tiles exist, from the manifest's index
    features: pa.Table | None = None

    def tile_world_size(self) -> float:
        return tile_world_size(self.world_bounds, self.tile_depth)

    def tile(self, row: int, col: int) -> PolygonTile:
        group = self.store[f"tile/{self.tile_depth}/{row}/{col}"]
        return PolygonTile(
            coords=np.asarray(group["coords"]),
            triangle_indices=np.asarray(group["triangle_indices"]),
            vertex_cell_id=np.asarray(group["vertex_cell_id"]),
        )


def load_polygon_tile_grid(layer: SegmentLayer, world_bounds: tuple[float, float, float, float]) -> PolygonTileGrid:
    """Open the tile store a slide's segment layer points at. `world_bounds`
    is the slide's, not the layer's -- every layer shares one grid."""
    store = open_tile_store(layer.path, layer.format, "segment")

    features = None
    features_path = layer.path / "features.parquet"
    if features_path.exists():
        features = pq.read_table(features_path)

    return PolygonTileGrid(
        store=store,
        tile_depth=layer.tile_depth,
        world_bounds=world_bounds,
        n_cells=layer.n_cells,
        tiles=layer.tiles,
        features=features,
    )


# Numeric, but not a *measurement* of the cell: "id" is an arbitrary label and
# the centroids just re-encode position, so colouring by them says nothing
# about the cells themselves. Kept out of the "Color by" picker rather than
# dropped from the table, which other code still reads.
_NON_MEASUREMENT_FEATURES = {"id", "cell_id", "x_centroid", "y_centroid"}


def numeric_feature_names(features: pa.Table | None) -> list[str]:
    """Per-cell measurement columns that can drive a colormap, in table order
    (e.g. cell_area, nucleus_area, transcript_counts, total_counts)."""
    if features is None:
        return []
    names = []
    for field in features.schema:
        if field.name in _NON_MEASUREMENT_FEATURES:
            continue
        if pa.types.is_integer(field.type) or pa.types.is_floating(field.type):
            names.append(field.name)
    return names


def visible_polygon_tile_keys(
    grid: PolygonTileGrid, world_rect: tuple[float, float, float, float]
) -> list[tuple[int, int]]:
    """world_rect = (minx, miny, maxx, maxy), world Y increasing downward --
    same convention as `cytos.core.image.visible_chunk_keys`, but polygon
    coords are already in world space (see `load_polygons`), so unlike that
    function this needs no pixel-row conversion at all."""
    return visible_tile_keys(grid.tiles, grid.tile_depth, grid.world_bounds, world_rect)
