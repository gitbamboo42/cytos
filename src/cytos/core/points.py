"""Transcript-point data model: one (x, y) per detected transcript plus the
gene it belongs to. Pure numpy/pyarrow -- no pygfx/GPU dependency, see
`cytos.render.points` for that.

The third primitive alongside the OME-Zarr image and the polygon set (see
CLAUDE.md). Where a polygon set is ragged (each cell has its own vertex run),
points are flat: one row per transcript, so the only per-item attribute the
renderer needs is a small dense `gene_id` into a shared name table -- which is
what lets colour-by-gene be a single tiny LUT upload rather than a per-point
buffer rewrite.

Named for what it produces, not the platform it reads: `load_transcripts`, not
`load_xenium_transcripts` -- Xenium is the first source, not the only one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import zarr

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

    table = pq.read_table(path, columns=wanted)

    keep = np.ones(len(table), dtype=bool)
    if min_qv is not None and "qv" in wanted:
        keep &= table.column("qv").to_numpy(zero_copy_only=False) >= min_qv
    if genes_only and "is_gene" in wanted:
        keep &= table.column("is_gene").to_numpy(zero_copy_only=False).astype(bool)
    if not keep.all():
        table = table.filter(pa.array(keep))

    names = table.column("feature_name").to_numpy(zero_copy_only=False).astype(str)
    # Sorted unique + inverse in one pass: the dense id *is* the row index into
    # gene_names, which is what the renderer's colour LUT is indexed by.
    gene_names, gene_id = np.unique(names, return_inverse=True)

    coords = np.empty((len(table), 2), dtype=np.float32)
    coords[:, 0] = table.column("x_location").to_numpy(zero_copy_only=False)
    coords[:, 1] = table.column("y_location").to_numpy(zero_copy_only=False)
    coords = np.ascontiguousarray(coords)

    return Transcripts(
        coords=coords,
        gene_id=gene_id.astype(np.uint32),
        gene_names=[str(n) for n in gene_names],
    )


@dataclass
class PointTile:
    """One tile's points, as plain numpy -- the renderer never sees the store
    they came from (same reasoning as `cytos.core.polygons.PolygonTile`)."""

    coords: np.ndarray  # (P, 2) float32, world space
    gene_id: np.ndarray  # (P,) uint32, dense index into the layer's gene table


@dataclass
class PointTileGrid:
    """A `cytos.prep.points` cache: Hilbert-sorted transcript points tiled over
    the same flat world grid the polygon cache uses (`cytos.core.tiling`).
    Reading a tile is a cheap array read.

    `genes` is the cache's gene table -- row i describes dense gene id i, with
    at least a "name" and a "count" column, so the UI can list genes by
    abundance without touching a single point.
    """

    store: zarr.Group
    tile_depth: int
    world_bounds: tuple[float, float, float, float]  # (minx, miny, maxx, maxy), world space
    n_points: int
    tiles: set[tuple[int, int]]  # which tiles exist, from the manifest's index
    genes: pa.Table | None = None

    @property
    def gene_names(self) -> list[str]:
        if self.genes is None:
            return []
        return [str(n) for n in self.genes.column("name").to_pylist()]

    def tile_world_size(self) -> float:
        return tile_world_size(self.world_bounds, self.tile_depth)

    def tile(self, row: int, col: int) -> PointTile:
        group = self.store[f"tile/{self.tile_depth}/{row}/{col}"]
        return PointTile(
            coords=np.asarray(group["coords"]),
            gene_id=np.asarray(group["gene_id"]),
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
    )


def visible_point_tile_keys(
    grid: PointTileGrid, world_rect: tuple[float, float, float, float]
) -> list[tuple[int, int]]:
    """(tile_row, tile_col) for every cached tile the camera rect touches --
    world_rect = (minx, miny, maxx, maxy), world Y increasing downward."""
    return visible_tile_keys(grid.tiles, grid.tile_depth, grid.world_bounds, world_rect)
