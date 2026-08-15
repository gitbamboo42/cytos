"""Offline preprocessing for the transcript-point layer: Hilbert-sort so
spatially-close transcripts become memory-close, tile into the slide's shared
world-space grid -- the same one the polygon layer uses -- and write a zarr
cache.

Much lighter than `cytos.prep.polygons` -- there's nothing to triangulate, one
row per transcript -- but the same reason to do it offline: a real whole-slide
run has 10^8 transcripts, so the viewer must be able to read just the visible
tiles instead of parsing the whole parquet at startup. Tiling alone is not
enough at that scale, which is why the cache holds two more structures, both
decided here at prep time:

* **Aggregate detail levels (LOD).** Tiles answer "which region is on
  screen"; levels answer "how much detail does this zoom deserve". Level 0
  (at the slide's `tile_depth`) holds every real transcript; each coarser
  level bins the one below on a fixed per-tile grid (`BIN_GRID` bins per
  tile side) and stores **one weighted dot per (bin, gene)**: one of that
  gene's own transcripts in the bin as the position (a uniform,
  reproducible pick -- see the position note at the aggregation loop),
  carrying that gene's exact `count` there. Genes are never merged: two
  genes sharing a bin keep two dots, each where its own transcripts
  actually are, which is what keeps a multi-gene view interleaved instead
  of blended. The renderer sizes a dot by sqrt(count) -- dense regions
  read as larger dots, the way Xenium Explorer's "scaled view" draws them,
  except the count here is exact (theirs is re-clustered per frame from an
  already-subsampled load). A level's tiles have the **same schema as
  level 0** -- gene-major with the gene index below, plus the `count`
  column -- so every level is read, sliced and drawn by the one same code
  path, and per-level cost shrinks with the occupied (bin, gene) pairs,
  about four-fold per level.

* **A per-tile gene index at full detail.** Within every level-0 tile,
  points are sorted by gene (Hilbert order kept within each gene), with
  `gene_ids` / `gene_starts` arrays saying where each gene's run lies. The
  common real view is a few genes at once -- ~24k points per gene on a
  120M-point run -- and the index turns that view into a handful of
  contiguous slice reads instead of loading every point ever detected
  (`PointTileGrid.tile`'s `genes=`). XOA 3.0's own `transcripts.zarr` added
  the same structure (`gene_offset`), which is a decent sign it is the
  right one.

The per-tile `gene_id` array is a dense index into `genes.parquet`, not a
repeated gene *name* string: names cost far more to store and can't index a
colour LUT on the GPU.

A full-detail tile may also carry an optional `size` array -- a float32
per-point multiplier on the layer's point size (1.0 = normal). No current
source has one, so nothing here writes it, but the reader and renderer
honour it (`PointTile.size`), so a future source whose points carry their
own size needs no format change -- gene-sliced reads slice it alongside the
other arrays.

Not a command of its own: `cytos-import` (`cytos.prep.slide`) drives this, so
that the world bounds and tile depth are the slide's rather than this layer's.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import zarr

from cytos.core.points import Transcripts
from cytos.prep.tiling import HILBERT_ORDER, run_bounds, sort_and_tile

# Aggregate-level bin grid, per tile side: a tile holds at most BIN_GRID^2
# dots however many transcripts fall in it, which is what bounds the cost of
# a zoomed-out view. With one dot per (bin, gene), a bin often draws several
# dots, so the grid must be coarser than a one-dot-per-bin design would
# want: 32 puts neighbouring bins roughly 10-20 screen pixels apart at the
# zoom each level is selected for, leaving that room for the genes'
# interleaved dots (64 and 128 both read as a woven carpet).
BIN_GRID = 32


def _mix_hash(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """A well-mixed uint64 per point from its float32 coordinate bits (the
    splitmix64 finaliser). "Smallest hash in the bin" is then a uniform,
    reproducible pick among a bin's members. The mixing step is what makes
    it good enough: it severs any relationship between hash order and
    position, which a naive deterministic pick (lowest x, first in Hilbert
    order) would keep -- and a position-correlated pick is exactly what
    would draw a lattice again."""
    h = (x.view(np.uint32).astype(np.uint64) << np.uint64(32)) | y.view(np.uint32).astype(np.uint64)
    h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return h ^ (h >> np.uint64(31))


def prep_points(
    transcripts: Transcripts,
    out_dir: Path,
    world_bounds: tuple[float, float, float, float],
    tile_depth: int,
    hilbert_order: int = HILBERT_ORDER,
) -> dict:
    """Write `out_dir/{tiles.zarr, genes.parquet}`. Takes an already-loaded
    `Transcripts` for the same reason `prep_polygons` takes `Polygons`: the
    importer needs this layer's extent to compute the slide's shared world
    bounds, and parsing tens of millions of rows twice is not free.

    Returns this layer's manifest fields, including the `tiles` index.
    """
    coords = transcripts.coords
    n_points = len(coords)
    if n_points == 0:
        raise ValueError("no transcripts left after filtering")

    # Points are their own anchor -- unlike polygons, where a whole cell has to
    # be reduced to one centroid before it can be sorted.
    perm, tile_row, tile_col = sort_and_tile(coords, world_bounds, tile_depth, hilbert_order)

    out_dir.mkdir(parents=True, exist_ok=True)
    root = zarr.open_group(str(out_dir / "tiles.zarr"), mode="w")
    root.attrs["hilbert_order"] = hilbert_order
    root.attrs["tile_depth"] = tile_depth
    root.attrs["world_bounds"] = [float(v) for v in world_bounds]
    root.attrs["n_points"] = n_points
    levels = tile_depth + 1
    root.attrs["levels"] = levels

    sorted_coords = coords[perm]
    sorted_gene = transcripts.gene_id[perm].astype(np.uint32)

    # -- level 0: every real transcript, gene-major per tile with an index
    key = tile_row * (1 << tile_depth) + tile_col
    starts, ends = run_bounds(key)
    tiles = []
    for j0, j1 in zip(starts, ends):
        row, col = int(tile_row[j0]), int(tile_col[j0])
        # Gene-major within the tile, Hilbert order within each gene: stable
        # sort, so a gene's run stays one spatially-coherent slice.
        order = np.argsort(sorted_gene[j0:j1], kind="stable")
        tile_gene = sorted_gene[j0:j1][order]
        gs, _ge = run_bounds(tile_gene)
        n = int(j1 - j0)
        group = root.create_group(f"tile/{tile_depth}/{row}/{col}")
        # Chunked, not one chunk per array: a gene-sliced read should
        # decompress the chunks its slices land in, not the whole tile.
        group.create_array("coords", data=sorted_coords[j0:j1][order], chunks=(min(n, 65536), 2))
        group.create_array("gene_id", data=tile_gene, chunks=(min(n, 65536),))
        group.create_array("gene_ids", data=tile_gene[gs])
        group.create_array("gene_starts", data=np.append(gs, n).astype(np.int64))
        tiles.append([row, col])

    # -- coarser levels: one weighted dot per (bin, gene), on each depth's
    # grid. Iterative -- each level merges the one below, which is exact:
    # the first coarse level bins the transcripts themselves, and every bin
    # at a coarser depth is exactly two-by-two bins of the finer one, so
    # later levels just halve the bin coordinates and merge per (bin, gene)
    # key. Counts therefore always equal what an actual re-bin of level 0
    # would give, per gene.
    #
    # A dot's *position* is one of its gene's real transcripts in the bin:
    # the member with the smallest coordinate hash -- a uniform,
    # reproducible pick (see `_mix_hash`). Centroids were tried first and
    # looked synthetic: in evenly dense tissue every bin is full, a full
    # bin's centroid sits at the bin's centre, and the dots line up into a
    # lattice. A hashed pick has no centre bias and no correlation between
    # neighbouring bins, so the dots inherit the tissue's own irregularity
    # instead of the grid's -- and because a merged bin's minimum is the
    # minimum over all its members, a dot at any depth is the min-hash
    # transcript of everything it stands for, consistent across levels.
    minx, miny, maxx, maxy = world_bounds
    span = max(maxx - minx, maxy - miny)
    n_genes = len(transcripts.gene_names)
    rep_x = np.ascontiguousarray(sorted_coords[:, 0])
    rep_y = np.ascontiguousarray(sorted_coords[:, 1])
    rep_h = _mix_hash(rep_x, rep_y)
    gene = sorted_gene.astype(np.int64)
    count = np.ones(n_points, dtype=np.int64)
    bx = by = None  # per-dot bin coords at the previous (finer) depth
    for depth in range(tile_depth - 1, -1, -1):
        bins = (1 << depth) * BIN_GRID
        if bx is None:
            bx = np.clip(((rep_x.astype(np.float64) - minx) / span * bins).astype(np.int64), 0, bins - 1)
            by = np.clip(((rep_y.astype(np.float64) - miny) / span * bins).astype(np.int64), 0, bins - 1)
        else:
            bx, by = bx >> 1, by >> 1
        # Merge per (bin, gene), the min-hash member sorted to each run's
        # head: the head is the dot's position, the run sum its count.
        key = (by * bins + bx) * n_genes + gene
        gorder = np.lexsort((rep_h, key))
        skey = key[gorder]
        starts, _ends = run_bounds(skey)
        first = gorder[starts]
        count = np.add.reduceat(count[gorder], starts)
        rep_x, rep_y, rep_h = rep_x[first], rep_y[first], rep_h[first]
        binkey, gene = np.divmod(skey[starts], n_genes)
        by, bx = np.divmod(binkey, bins)
        # Write this level's tiles, gene-major with the same index level 0
        # gets -- one schema for every level. The carried state stays in
        # key order for the next merge; only the written views are
        # tile-ordered.
        t_row, t_col = by // BIN_GRID, bx // BIN_GRID
        worder = np.lexsort((gene, t_row * (1 << depth) + t_col))
        w_x, w_y = rep_x[worder], rep_y[worder]
        w_gene = gene[worder].astype(np.uint32)
        w_count = count[worder].astype(np.uint32)
        w_tkey = (t_row * (1 << depth) + t_col)[worder]
        t_starts, t_ends = run_bounds(w_tkey)
        for j0, j1 in zip(t_starts, t_ends):
            row, col = int(w_tkey[j0]) // (1 << depth), int(w_tkey[j0]) % (1 << depth)
            n = int(j1 - j0)
            tile_gene = w_gene[j0:j1]
            gs, _ge = run_bounds(tile_gene)
            dots = np.empty((n, 2), dtype=np.float32)
            dots[:, 0] = w_x[j0:j1]
            dots[:, 1] = w_y[j0:j1]
            group = root.create_group(f"tile/{depth}/{row}/{col}")
            group.create_array("coords", data=dots, chunks=(min(n, 65536), 2))
            group.create_array("gene_id", data=tile_gene, chunks=(min(n, 65536),))
            group.create_array("count", data=w_count[j0:j1], chunks=(min(n, 65536),))
            group.create_array("gene_ids", data=tile_gene[gs])
            group.create_array("gene_starts", data=np.append(gs, n).astype(np.int64))

    # Counts here, not in the viewer: the UI lists genes most-abundant-first,
    # and counting tens of millions of rows at startup is exactly the work this
    # cache exists to avoid.
    counts = np.bincount(transcripts.gene_id, minlength=len(transcripts.gene_names))
    genes = pa.table({"name": pa.array(transcripts.gene_names), "count": pa.array(counts.astype(np.int64))})
    pq.write_table(genes, out_dir / "genes.parquet")

    return {
        "n_points": n_points,
        "n_genes": len(transcripts.gene_names),
        "tile_depth": tile_depth,
        "tiles": tiles,
        "levels": levels,
    }
