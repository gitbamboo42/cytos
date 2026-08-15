"""Cell segmentation polygon data model: ragged-array boundary geometry plus
per-cell attributes. Pure numpy/pyarrow -- no pygfx/GPU dependency, see
cytos.render for that (work-notes/plan.md Phase 1).

Raw vertex coordinates are already in the same world space as
cytos.core.image's PyramidLevel (world Y increasing downward, matching pixel
rows), so loading is a straight column read with no reprojection -- Xenium's
row-major Y and cytos world Y agree.

`polygons_from_parquet` reads *any* long-format boundary table, not just
Xenium's: column names are detected from a list of common spellings and can be
given outright, rows need not arrive grouped by object, and a ring that repeats
its first vertex at the end is closed back up. Segmentation from another tool
is the ordinary case, not the exception -- see `cytos.prep.labels` for the
other input format (an OME-Zarr label mask), and `cytos.prep.segments` for the
dispatcher that picks between them.
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


# Tried in order, so a Xenium file's `cell_id` wins over the `label_id` column
# sitting right next to it. Everything here is a spelling seen in the wild for
# "which object does this vertex belong to" / "where is it".
_ID_ALIASES = ("cell_id", "object_id", "label_id", "label", "id")
_X_ALIASES = ("vertex_x", "x", "X")
_Y_ALIASES = ("vertex_y", "y", "Y")


def _pick_column(available: set[str], aliases: tuple[str, ...], what: str, path: Path) -> str:
    for name in aliases:
        if name in available:
            return name
    raise ValueError(
        f"{path}: no {what} column -- looked for {', '.join(aliases)}, found {', '.join(sorted(available))}. "
        f"Name the columns explicitly if they are spelled differently."
    )


def _run_starts(ids: np.ndarray) -> np.ndarray:
    run_start = np.empty(len(ids), dtype=bool)
    run_start[0] = True
    run_start[1:] = ids[1:] != ids[:-1]
    return np.flatnonzero(run_start)


def _ring_areas(coords: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Shoelace area per ring, in world units squared. Absolute, because world
    Y runs downward and would otherwise make every ring negative."""
    n = len(offsets) - 1
    starts, ends = offsets[:-1], offsets[1:]
    # "Next vertex within the same ring", wrapping each ring's last back to its
    # own first -- the same trick the outline renderer uses.
    next_idx = np.arange(len(coords)) + 1
    next_idx[ends - 1] = starts
    x, y = coords[:, 0].astype(np.float64), coords[:, 1].astype(np.float64)
    cross = x * y[next_idx] - x[next_idx] * y
    return np.abs(np.add.reduceat(cross, starts)) / 2.0 if n else np.empty(0, np.float64)


# Field metadata that marks a features column as categorical -- a palette,
# not a ramp (see `cytos.render.polygons`). Metadata rather than Arrow's
# dictionary type, because parquet treats dictionaries as an *encoding* and
# hands the column back as plain integers, while the schema's field
# metadata survives the round trip intact. Nothing anywhere matches on
# column names.
CATEGORICAL_META = {b"cytos:categorical": b"true"}


def is_categorical_feature(field: pa.Field) -> bool:
    return bool(field.metadata) and b"cytos:categorical" in field.metadata


def join_categories(features: pa.Table, categories: list[tuple[str, Path]]) -> pa.Table:
    """Left-join named (id, category) tables -- e.g. a clustering's
    `clusters.csv` -- onto a per-cell `features` table by its "id" column,
    one new column per table, aligned by position so row order is untouched.
    New columns carry `CATEGORICAL_META`; their values are the source's own
    category numbers, so a category keeps one colour whatever subset of
    cells a layer holds. Objects absent from a table get null --
    "unassigned", drawn dim. The file's first column is the id, the second
    the category; header names are the source's own business."""
    import pyarrow.csv as pacsv

    ids = features.column("id").combine_chunks()
    for name, path in categories:
        table = pacsv.read_csv(path)
        cat_ids = table.column(0).combine_chunks().cast(ids.type)
        pos = pc.index_in(ids, value_set=cat_ids)
        vals = pc.take(table.column(1).combine_chunks(), pos).cast(pa.int64())
        features = features.append_column(
            pa.field(name, pa.int64(), metadata=CATEGORICAL_META), vals
        )
    return features


def polygons_from_parquet(
    path: Path,
    cells_path: Path | None = None,
    columns: tuple[str, str, str] | None = None,
    categories: list[tuple[str, Path]] | None = None,
) -> Polygons:
    """Load a long-format boundary table (one row per vertex) into the
    ragged-array model. Xenium's `*_boundaries.parquet` is the shape this was
    written for, but nothing here is specific to it.

    `columns` names the (id, x, y) columns outright; without it they are
    detected from the spellings above. Rows do not have to arrive grouped by
    object -- an interleaved file is stable-sorted by id first, since grouping
    by *runs* of equal ids would otherwise cut one object into several polygons
    with no error to show for it. A ring that repeats its first vertex as its
    last has that duplicate dropped: earcut and the outline's ring-wrap both
    assume an open ring, and a repeat gives a zero-length edge and a degenerate
    triangle.

    If `cells_path` is given (e.g. `cells.parquet`), its per-cell attributes
    are left-joined onto `features` by the dataset's original cell id
    (string or int, whichever the slide uses), restoring row order to match
    `cell_id`/`offsets` afterward since Arrow joins don't preserve it. Without
    it, `features` carries the id plus a shoelace `area`, so a foreign
    segmentation still has something for "Color by" to spread a ramp over.
    """
    available = set(pq.ParquetFile(path).schema_arrow.names)
    if columns is not None:
        id_col, x_col, y_col = columns
        missing = [c for c in columns if c not in available]
        if missing:
            raise ValueError(
                f"{path}: no column(s) {', '.join(missing)} -- found {', '.join(sorted(available))}"
            )
    else:
        id_col = _pick_column(available, _ID_ALIASES, "object id", path)
        x_col = _pick_column(available, _X_ALIASES, "vertex x", path)
        y_col = _pick_column(available, _Y_ALIASES, "vertex y", path)

    table = pq.read_table(path, columns=[id_col, x_col, y_col])
    if table.num_rows == 0:
        raise ValueError(f"{path}: no vertices in it")
    ids = table.column(id_col).to_numpy(zero_copy_only=False)

    starts = _run_starts(ids)
    # Checked over the run-start ids only (one per run, not one per vertex), so
    # the common already-grouped file pays for a unique over N values, not M.
    if len(np.unique(ids[starts])) < len(starts):
        # Arrow's sort is stable, so vertices keep their within-object order.
        order = pc.sort_indices(table, sort_keys=[(id_col, "ascending")])
        table = table.take(order)
        ids = table.column(id_col).to_numpy(zero_copy_only=False)
        starts = _run_starts(ids)

    original_ids = pa.array(ids[starts])
    offsets = np.append(starts, len(ids)).astype(np.int64)

    coords = np.empty((len(ids), 2), dtype=np.float32)
    coords[:, 0] = table.column(x_col).to_numpy(zero_copy_only=False)
    coords[:, 1] = table.column(y_col).to_numpy(zero_copy_only=False)

    # Closed rings -> open rings. Guarded on >= 4 vertices so a degenerate
    # 3-row "ring" that happens to start and end at the same point isn't
    # reduced to something that can't be triangulated at all.
    first, last = offsets[:-1], offsets[1:] - 1
    closed = (
        (coords[first, 0] == coords[last, 0])
        & (coords[first, 1] == coords[last, 1])
        & ((offsets[1:] - offsets[:-1]) >= 4)
    )
    if closed.any():
        keep = np.ones(len(ids), dtype=bool)
        keep[last[closed]] = False
        coords = coords[keep]
        counts = (offsets[1:] - offsets[:-1]) - closed.astype(np.int64)
        offsets = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)

    coords = np.ascontiguousarray(coords)
    cell_id = np.arange(len(original_ids), dtype=np.uint32)
    features = pa.table({"id": original_ids, "area": _ring_areas(coords, offsets)})
    offsets = offsets.astype(np.uint32)

    if cells_path is not None:
        # The dataset's own per-cell table says more than a shoelace does.
        features = features.drop_columns(["area"])
        cells = pq.read_table(cells_path)
        cells = cells.rename_columns(["id" if c == "cell_id" else c for c in cells.column_names])
        # cells.parquet's id column can differ in Arrow subtype from the
        # boundaries file's (e.g. large_string vs. string) even when both
        # hold the same values -- join() requires an exact type match.
        cells = cells.set_column(cells.column_names.index("id"), "id", cells.column("id").cast(original_ids.type))
        joined = features.join(cells, keys="id", join_type="left outer")
        order = pc.index_in(joined.column("id"), original_ids)
        features = joined.take(pc.sort_indices(order))

    if categories:
        features = join_categories(features, categories)

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


def feature_names(features: pa.Table | None) -> list[str]:
    """Per-cell columns that can drive the colour LUT, in table order:
    numeric measurements (continuous ramp -- e.g. cell_area,
    transcript_counts) and categorical columns (a palette -- e.g. a
    clustering, see `join_categories` / `is_categorical_feature`)."""
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
    coords are already in world space (see `polygons_from_parquet`), so unlike that
    function this needs no pixel-row conversion at all."""
    return visible_tile_keys(grid.tiles, grid.tile_depth, grid.world_bounds, world_rect)
