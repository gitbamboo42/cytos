"""Offline preprocessing for the transcript-point layer: filter, Hilbert-sort
so spatially-close transcripts become memory-close, tile into the same flat
world-space grid the polygon cache uses, write to a zarr cache.

Much lighter than `cytos.prep.polygons` -- there's nothing to triangulate, one
row per transcript -- but the same reason to do it offline: a real whole-slide
Xenium run has tens of millions of transcripts, so the viewer must be able to
read just the visible tiles instead of parsing the whole parquet at startup.

The per-tile `gene_id` array is a dense index into `genes.parquet`, not a
repeated gene *name* string: names cost far more to store and can't index a
colour LUT on the GPU.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-prep-points data/human_kidney_tiny/transcripts.parquet \
        --tile-size 500 \
        --out data/human_kidney_tiny/points_cache/
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import zarr

from cytos.core.points import DEFAULT_MIN_QV, load_transcripts
from cytos.prep.tiling import HILBERT_ORDER, run_bounds, sort_and_tile


def prep_points(
    transcripts_path: Path,
    out_dir: Path,
    tile_size: float = 500.0,
    min_qv: float = DEFAULT_MIN_QV,
    genes_only: bool = True,
    hilbert_order: int = HILBERT_ORDER,
) -> None:
    transcripts = load_transcripts(transcripts_path, min_qv=min_qv, genes_only=genes_only)
    coords = transcripts.coords
    n_points = len(coords)
    if n_points == 0:
        raise ValueError(f"{transcripts_path}: no transcripts left after filtering")

    minx, miny = float(coords[:, 0].min()), float(coords[:, 1].min())
    maxx, maxy = float(coords[:, 0].max()), float(coords[:, 1].max())
    world_bounds = (minx, miny, maxx, maxy)

    # Points are their own anchor -- unlike polygons, where a whole cell has to
    # be reduced to one centroid before it can be sorted.
    perm, tile_row, tile_col, tile_depth = sort_and_tile(coords, world_bounds, tile_size, hilbert_order)
    sorted_coords = coords[perm]
    sorted_gene_id = transcripts.gene_id[perm]

    tile_key = tile_row * (1 << tile_depth) + tile_col
    starts, ends = run_bounds(tile_key)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    root = zarr.open_group(str(out_dir / "tiles.zarr"), mode="w")
    root.attrs["hilbert_order"] = hilbert_order
    root.attrs["tile_depth"] = tile_depth
    root.attrs["world_bounds"] = [minx, miny, maxx, maxy]
    root.attrs["n_points"] = n_points
    root.attrs["n_genes"] = len(transcripts.gene_names)
    root.attrs["min_qv"] = float(min_qv) if min_qv is not None else None
    root.attrs["genes_only"] = bool(genes_only)

    for j0, j1 in zip(starts, ends):
        row, col = int(tile_row[j0]), int(tile_col[j0])
        group = root.create_group(f"tile/{tile_depth}/{row}/{col}")
        group.create_array("coords", data=sorted_coords[j0:j1])
        group.create_array("gene_id", data=sorted_gene_id[j0:j1].astype(np.uint32))

    # Counts here, not in the viewer: the UI lists genes most-abundant-first,
    # and counting tens of millions of rows at startup is exactly the work this
    # cache exists to avoid.
    counts = np.bincount(transcripts.gene_id, minlength=len(transcripts.gene_names))
    genes = pa.table({"name": pa.array(transcripts.gene_names), "count": pa.array(counts.astype(np.int64))})
    pq.write_table(genes, out_dir / "genes.parquet")

    print(
        f"wrote {out_dir}: {n_points} transcripts, {len(transcripts.gene_names)} genes, "
        f"tile_depth={tile_depth} ({len(starts)} tiles)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcripts", type=Path, help="e.g. transcripts.parquet")
    parser.add_argument("--tile-size", type=float, default=500.0, help="target tile size, world units (um)")
    parser.add_argument(
        "--min-qv",
        type=float,
        default=DEFAULT_MIN_QV,
        help=f"drop transcripts below this Phred quality; default {DEFAULT_MIN_QV} (Xenium's own cutoff)",
    )
    parser.add_argument(
        "--keep-controls",
        action="store_true",
        help="keep negative-control probes and unassigned codewords, which are dropped by default",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prep_points(
        args.transcripts,
        args.out,
        tile_size=args.tile_size,
        min_qv=args.min_qv,
        genes_only=not args.keep_controls,
    )


if __name__ == "__main__":
    main()
