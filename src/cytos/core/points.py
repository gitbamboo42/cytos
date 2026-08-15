"""Transcript-point data model: one (x, y) per detected transcript plus the
gene it belongs to. Pure numpy/pyarrow -- no pygfx/GPU dependency, see
`cytos.render.points` for that.

The third primitive alongside the OME-Zarr image and the polygon set (see
skills/developers.md). Where a polygon set is ragged (each cell has its own vertex run),
points are flat: one row per transcript, so the only per-item attribute the
renderer needs is a small dense `gene_id` into a shared name table -- which is
what lets colour-by-gene be a single tiny LUT upload rather than a per-point
buffer rewrite.

Named for what it produces, not the platform it reads: `load_transcripts`, not
`load_xenium_transcripts` -- Xenium is the first source, not the only one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import zarr

from cytos.core.image import select_scale
from cytos.core.slide import PointLayer
from cytos.core.store import open_tile_store
from cytos.core.tiling import tile_world_size, visible_tile_keys

# Xenium's own recommended quality cutoff: Phred-scaled, so 20 means a 1-in-100
# chance the transcript was miscalled. 10x's analysis pipeline and Xenium
# Explorer both filter here by default, and the discarded tail is not small
# (kidney slide: 5,464 of 26,463 rows), so keeping it would show noise the
# platform's own tools never display.
DEFAULT_MIN_QV = 20.0


@dataclass
class Transcripts:
    coords: np.ndarray  # (M, 2) float32, world space, C-contiguous
    gene_id: np.ndarray  # (M,) uint32, dense index into gene_names
    gene_names: list[str]  # gene_names[gene_id[i]] is transcript i's gene


def load_transcripts(
    path: Path,
    min_qv: float = DEFAULT_MIN_QV,
    genes_only: bool = True,
) -> Transcripts:
    """Load a Xenium `transcripts.parquet` (one row per detected transcript).

    `min_qv` drops low-confidence calls; `genes_only` drops the control
    codewords (negative-control probes, unassigned codewords) that share the
    file with real gene calls. Both columns are optional -- older slide
    versions don't ship `is_gene`/`qv`, and a missing column just means that
    filter doesn't apply rather than an error.

    Coordinates are read straight through, matching
    `cytos.core.polygons.polygons_from_parquet`: raw Xenium coordinates are row-major
    (Y down) and so is cytos world space, so there is nothing to convert.
    """
    available = set(pq.ParquetFile(path).schema_arrow.names)
    wanted = ["feature_name", "x_location", "y_location"]
    missing = [c for c in wanted if c not in available]
    if missing:
        raise ValueError(f"{path}: not a transcripts table, missing {missing}")
    for optional in ("qv", "is_gene"):
        if optional in available:
            wanted.append(optional)

    # feature_name is decoded straight to dictionary form (one small int per
    # row plus the few thousand distinct names), never as one string object
    # per row: a numpy unicode copy of this column costs 4 bytes x the longest
    # name x the row count -- gigabytes at the 10^8 rows a 5K-panel run ships
    # (see docs/importing-xenium.md), where the dictionary is ~0.5 GB total.
    table = pq.read_table(path, columns=wanted, read_dictionary=["feature_name"])

    keep = np.ones(len(table), dtype=bool)
    if min_qv is not None and "qv" in wanted:
        keep &= table.column("qv").to_numpy(zero_copy_only=False) >= min_qv
    if genes_only and "is_gene" in wanted:
        keep &= table.column("is_gene").to_numpy(zero_copy_only=False).astype(bool)
    if not keep.all():
        table = table.filter(pa.array(keep))

    # combine_chunks() unifies the per-row-group dictionaries the reader
    # produced into one, so the indices all point into a single name table.
    encoded = table.column("feature_name").combine_chunks()
    strings = encoded.dictionary.to_pylist()
    idx = encoded.indices.to_numpy(zero_copy_only=False)
    # The dictionary still lists names whose every row was just filtered out
    # (the control codewords, usually) -- keep only names that still occur,
    # in sorted order, so the result matches what np.unique over the
    # filtered rows produced before. The dense id *is* the row index into
    # gene_names -- the renderer's colour LUT index and the genes.parquet
    # row -- so it must be deterministic and free of ghost entries.
    used = np.zeros(len(strings), dtype=bool)
    used[idx] = True
    present = np.flatnonzero(used)
    order = present[np.argsort([strings[i] for i in present])]
    rank = np.zeros(len(strings), dtype=np.uint32)
    rank[order] = np.arange(len(order), dtype=np.uint32)
    gene_id = rank[idx]
    gene_names = [str(strings[i]) for i in order]

    coords = np.empty((len(table), 2), dtype=np.float32)
    coords[:, 0] = table.column("x_location").to_numpy(zero_copy_only=False)
    coords[:, 1] = table.column("y_location").to_numpy(zero_copy_only=False)
    coords = np.ascontiguousarray(coords)

    return Transcripts(coords=coords, gene_id=gene_id, gene_names=gene_names)


@dataclass
class PointTile:
    """One tile's points, as plain numpy -- the renderer never sees the store
    they came from (same reasoning as `cytos.core.polygons.PolygonTile`).

    One shape at every level: a point has a position, a gene, and a weight.
    At full detail every point is a real transcript and `count` is None
    (weight 1). At an aggregate level (see `cytos.prep.points`) a point is
    one gene's representative transcript for a region -- still a real
    position -- and `count` says how many of that gene it stands for.
    Nothing downstream needs to know which level a tile came from."""

    coords: np.ndarray  # (P, 2) float32, world space
    gene_id: np.ndarray  # (P,) uint32, dense index into the gene table
    count: np.ndarray | None = None  # (P,) uint32 weights; None = every point is one transcript
    # Optional per-point size multiplier (1.0 = the layer's point size) at
    # full detail -- no current source writes one, but the format accepts it
    # for sources whose points carry their own size.
    size: np.ndarray | None = None  # (P,) float32


@dataclass
class PointTileGrid:
    """A `cytos.prep.points` cache: Hilbert-sorted transcript points tiled over
    the same flat world grid the polygon cache uses (`cytos.core.tiling`).
    Reading a tile is a cheap array read.

    `genes` is the cache's gene table -- row i describes dense gene id i, with
    at least a "name" and a "count" column, so the UI can list genes by
    abundance without touching a single point.

    `levels` counts the cache's detail levels (see `cytos.prep.points`):
    level k lives at grid depth `tile_depth - k`; level 0 is every real
    transcript, coarser levels hold one weighted dot per (region, gene),
    stored with the same tile schema. 1 -- a slide from before levels
    existed -- means only the full-detail depth is on disk, and everything
    here still works against it.
    """

    store: zarr.Group
    tile_depth: int
    world_bounds: tuple[float, float, float, float]  # (minx, miny, maxx, maxy), world space
    n_points: int
    tiles: set[tuple[int, int]]  # which tiles exist, from the manifest's index
    genes: pa.Table | None = None
    levels: int = 1
    # Coarse-level tile sets, derived once from the manifest's fine index --
    # a tile exists at a coarse depth iff any of its children exist, because
    # the level sample never empties an occupied tile.
    _tiles_at: dict[int, set[tuple[int, int]]] = field(default_factory=dict, repr=False)

    @property
    def gene_names(self) -> list[str]:
        if self.genes is None:
            return []
        return [str(n) for n in self.genes.column("name").to_pylist()]

    def tile_world_size(self) -> float:
        return tile_world_size(self.world_bounds, self.tile_depth)

    def tiles_at(self, depth: int) -> set[tuple[int, int]]:
        if depth == self.tile_depth:
            return self.tiles
        cached = self._tiles_at.get(depth)
        if cached is None:
            shift = self.tile_depth - depth
            cached = {(r >> shift, c >> shift) for r, c in self.tiles}
            self._tiles_at[depth] = cached
        return cached

    def tile(self, row: int, col: int, depth: int | None = None, genes=None) -> PointTile:
        """One tile's points -- the same read at every level, because every
        level has the same schema. `depth` picks the detail level (default:
        full detail); `genes` -- an iterable of dense gene ids -- reads only
        those genes' runs via the tile's gene index, a handful of contiguous
        slices instead of the whole tile. On a cache without the index (an
        old slide), `genes` degrades to the full read; the renderer filters
        what it draws, so the result only costs more, never shows more."""
        if depth is None:
            depth = self.tile_depth
        group = self.store[f"tile/{depth}/{row}/{col}"]
        count_arr = group["count"] if "count" in group else None
        size_arr = group["size"] if "size" in group else None
        if genes is not None and "gene_starts" in group:
            gene_ids = np.asarray(group["gene_ids"])
            gene_starts = np.asarray(group["gene_starts"])
            wanted = np.flatnonzero(np.isin(gene_ids, np.fromiter(genes, dtype=np.int64)))
            if len(wanted) == 0:
                return PointTile(
                    coords=np.empty((0, 2), dtype=np.float32),
                    gene_id=np.empty(0, dtype=np.uint32),
                )
            spans = [(gene_starts[i], gene_starts[i + 1]) for i in wanted]
            coords_arr, gid_arr = group["coords"], group["gene_id"]
            return PointTile(
                coords=np.concatenate([coords_arr[j0:j1] for j0, j1 in spans]),
                gene_id=np.concatenate([gid_arr[j0:j1] for j0, j1 in spans]),
                count=None
                if count_arr is None
                else np.concatenate([count_arr[j0:j1] for j0, j1 in spans]),
                size=None
                if size_arr is None
                else np.concatenate([size_arr[j0:j1] for j0, j1 in spans]),
            )
        return PointTile(
            coords=np.asarray(group["coords"]),
            gene_id=np.asarray(group["gene_id"]),
            count=None if count_arr is None else np.asarray(count_arr),
            size=None if size_arr is None else np.asarray(size_arr),
        )


def load_point_tile_grid(layer: PointLayer, world_bounds: tuple[float, float, float, float]) -> PointTileGrid:
    """Open the tile store a slide's point layer points at. `world_bounds` is
    the slide's, not the layer's -- every layer shares one grid."""
    store = open_tile_store(layer.path, layer.format, "point")

    genes = None
    genes_path = layer.path / "genes.parquet"
    if genes_path.exists():
        genes = pq.read_table(genes_path)

    return PointTileGrid(
        store=store,
        tile_depth=layer.tile_depth,
        world_bounds=world_bounds,
        n_points=layer.n_points,
        tiles=layer.tiles,
        genes=genes,
        levels=layer.levels,
    )


def visible_point_tile_keys(
    grid: PointTileGrid,
    world_rect: tuple[float, float, float, float],
    depth: int | None = None,
) -> list[tuple[int, int]]:
    """(tile_row, tile_col) for every cached tile the camera rect touches at
    `depth` (default: full detail) -- world_rect = (minx, miny, maxx, maxy),
    world Y increasing downward."""
    if depth is None:
        depth = grid.tile_depth
    return visible_tile_keys(grid.tiles_at(depth), depth, grid.world_bounds, world_rect)


# Level 0's resolution on the shared ladder: zoomed in past this, individual
# sub-um transcripts are separable on screen, so every real one is drawn.
# The analog of an image pyramid's native pixel size -- the physical scale
# of this layer's atoms, where its ladder starts.
FULL_DETAIL_WORLD_PER_PX = 0.5


def select_point_level(
    levels: int,
    world_per_px: float | None,
    full_detail_world_per_px: float = FULL_DETAIL_WORLD_PER_PX,
) -> int:
    """Which detail level a zoom deserves: 0 = full detail, k = the level at
    grid depth `tile_depth - k`.

    Not a second rule: this is `cytos.core.image.select_scale` -- the same
    function the image pyramid goes through -- fed this layer's ladder. The
    ladder is `full_detail_world_per_px * 2^k` because each aggregation
    level bins two-by-two, quartering the dots and doubling the resolvable
    scale, exactly as an image pyramid's levels double their pixel size.
    The two ladders differ only in where they start: the scanner's pixel
    size for the image, transcript separability for points."""
    if world_per_px is None:
        return 0
    scales = [full_detail_world_per_px * (1 << k) for k in range(max(levels, 1))]
    return select_scale(scales, world_per_px)
