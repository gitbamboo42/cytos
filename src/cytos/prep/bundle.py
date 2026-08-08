"""`cytos-import`: turn a source dataset into a `.cytos` bundle -- one
directory holding every layer plus the manifest that describes them (see
`cytos.core.bundle` for the layout and why it is a plain directory rather than
one big zarr).

This is the only thing that runs the per-layer prep steps, and that is the
point. World space is decided **once, here**: the importer loads every layer's
geometry first, unions their extents with the image pyramid's, picks a single
`tile_depth` from that, and hands the same bounds and depth to every layer.
Each prep step used to derive those from its own data, which quietly put the
polygon and point caches on two grids that didn't line up.

Named for what it reads, not the platform: Xenium is the first source, not the
only one -- `_discover_xenium` is one detector, and adding another means adding
a detector, not a new command.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-import data/xenium_breast_cancer_rep1 --out data/breast_rep1.cytos
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from cytos.core.bundle import (
    CYTOS_FORMAT,
    DEFAULT_CHANNEL_COLORMAPS,
    IMAGE_FORMAT,
    TILES_FORMAT,
    write_manifest,
)
from cytos.core.image import load_pyramid_levels
from cytos.core.points import DEFAULT_MIN_QV, load_transcripts
from cytos.core.polygons import load_polygons, numeric_feature_names
from cytos.prep.points import prep_points
from cytos.prep.polygons import prep_polygons
from cytos.prep.pyramid import convert_ome_zarr
from cytos.prep.tiling import choose_tile_depth

DEFAULT_TILE_SIZE = 500.0

# Preferred default for a segment layer's "Color by": the most broadly
# meaningful per-cell measurement present. Baked into the manifest at import so
# the viewer doesn't have to re-guess every time it opens.
_PREFERRED_COLOR_BY = ("cell_area", "nucleus_area", "transcript_counts", "total_counts")


@dataclass
class _ImageSource:
    id: str
    path: Path  # an .ome.zarr to copy, or an .ome.tif to convert
    channel: int = 0


@dataclass
class _SegmentSource:
    id: str
    boundaries: Path
    cells: Path | None
    visible: bool = True


@dataclass
class _PointSource:
    id: str
    transcripts: Path


def _discover_xenium(source: Path) -> tuple[list[_ImageSource], list[_SegmentSource], list[_PointSource]]:
    """What a Xenium output directory offers. Prefers an OME-Zarr already
    sitting next to the raw data over re-deriving one from the OME-TIFF -- the
    pyramid is identical and converting a whole-slide morphology image again
    costs minutes for nothing."""
    images: list[_ImageSource] = []
    # DAPI first, so it takes the first default colormap. Channels get their
    # colour by position, and a nuclear stain shown in anything but blue reads
    # wrong to anyone used to these images.
    for store in sorted(source.glob("*.ome.zarr"), key=lambda p: ("dapi" not in p.name.lower(), p.name)):
        images.append(_ImageSource(id=store.name[: -len(".ome.zarr")], path=store))
    if not images:
        tif = source / "morphology_focus.ome.tif"
        focus_dir = source / "morphology_focus"
        if tif.exists():
            images.append(_ImageSource(id="morphology", path=tif))
        elif focus_dir.is_dir():
            for channel_tif in sorted(focus_dir.glob("ch*.ome.tif")):
                images.append(_ImageSource(id=channel_tif.name.split(".")[0], path=channel_tif))

    cells = source / "cells.parquet"
    cells = cells if cells.exists() else None
    segments: list[_SegmentSource] = []
    if (source / "cell_boundaries.parquet").exists():
        segments.append(_SegmentSource(id="cell", boundaries=source / "cell_boundaries.parquet", cells=cells))
    if (source / "nucleus_boundaries.parquet").exists():
        # Off by default: nuclei sit inside the cell outlines already on
        # screen, so showing both at once reads as doubled lines rather than
        # as two layers. Present and one checkbox away.
        segments.append(
            _SegmentSource(
                id="nucleus", boundaries=source / "nucleus_boundaries.parquet", cells=cells, visible=False
            )
        )

    points: list[_PointSource] = []
    if (source / "transcripts.parquet").exists():
        points.append(_PointSource(id="transcripts", transcripts=source / "transcripts.parquet"))

    return images, segments, points


def _union(a: tuple | None, b: tuple) -> tuple:
    if a is None:
        return b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _coords_bounds(coords: np.ndarray) -> tuple[float, float, float, float]:
    return (
        float(coords[:, 0].min()),
        float(coords[:, 1].min()),
        float(coords[:, 0].max()),
        float(coords[:, 1].max()),
    )


def import_bundle(
    source: Path,
    out: Path,
    name: str | None = None,
    tile_size: float = DEFAULT_TILE_SIZE,
    min_qv: float = DEFAULT_MIN_QV,
    genes_only: bool = True,
    images_override: list[tuple[str, str]] | None = None,
) -> Path:
    images, segments, points = _discover_xenium(source)
    if images_override:
        images = [_ImageSource(id=Path(p).name.split(".")[0], path=Path(p)) for p, _ in images_override]
        colormaps = {Path(p).name.split(".")[0]: c for p, c in images_override}
    else:
        colormaps = {}
    if not (images or segments or points):
        raise ValueError(f"{source}: nothing recognisable to import")

    print(f"source {source}")
    for img in images:
        print(f"  image    {img.id:12s} {img.path.name}")
    for seg in segments:
        print(f"  segments {seg.id:12s} {seg.boundaries.name}")
    for pts in points:
        print(f"  points   {pts.id:12s} {pts.transcripts.name}")

    # -- pass 1: load everything, so world space can be decided from all of it
    world_bounds = None

    image_levels = {}
    for img in images:
        if img.path.suffix == ".zarr" or img.path.name.endswith(".ome.zarr"):
            levels = load_pyramid_levels(img.path)
            image_levels[img.id] = levels
            world_bounds = _union(world_bounds, levels[0].world_bounds())

    loaded_polygons = {}
    for seg in segments:
        polygons = load_polygons(seg.boundaries, seg.cells)
        loaded_polygons[seg.id] = polygons
        world_bounds = _union(world_bounds, _coords_bounds(polygons.coords))
        print(f"  loaded {seg.id}: {len(polygons.offsets) - 1} cells")

    loaded_points = {}
    for pts in points:
        transcripts = load_transcripts(pts.transcripts, min_qv=min_qv, genes_only=genes_only)
        loaded_points[pts.id] = transcripts
        world_bounds = _union(world_bounds, _coords_bounds(transcripts.coords))
        print(f"  loaded {pts.id}: {len(transcripts.coords)} transcripts, {len(transcripts.gene_names)} genes")

    if world_bounds is None:
        raise ValueError(f"{source}: no layer with a spatial extent -- cannot place a world grid")

    tile_depth = choose_tile_depth(world_bounds, tile_size)
    print(
        f"world_bounds {tuple(round(v, 2) for v in world_bounds)}  "
        f"tile_depth {tile_depth} ({(max(world_bounds[2] - world_bounds[0], world_bounds[3] - world_bounds[1]) / (1 << tile_depth)):.1f} um/tile)"
    )

    # -- pass 2: write the bundle
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    layers = []

    for i, img in enumerate(images):
        dest = out / "images" / f"{img.id}.zarr"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if img.id in image_levels:
            # Real copy, not a link: a bundle has to survive being moved to
            # another machine on its own.
            shutil.copytree(img.path, dest)
        else:
            convert_ome_zarr(img.path, dest, channel=img.channel, quiet=True)
            image_levels[img.id] = load_pyramid_levels(dest)

        # Percentile autocontrast, decided here rather than at every open:
        # fluorescence channels are sparse and heavy-tailed, so raw min/max
        # crushes them to near-black (see CLAUDE.md).
        coarsest = np.asarray(image_levels[img.id][-1].data)
        clim = [float(v) for v in np.percentile(coarsest, [1, 99.5])]
        colormap = colormaps.get(img.id, DEFAULT_CHANNEL_COLORMAPS[i % len(DEFAULT_CHANNEL_COLORMAPS)])
        layers.append(
            {
                "kind": "image",
                "id": img.id,
                "path": f"images/{img.id}.zarr",
                "format": IMAGE_FORMAT,
                "colormap": colormap,
                "clim": clim,
                "visible": True,
            }
        )
        print(f"  wrote images/{img.id}.zarr  colormap={colormap} clim={[round(v, 1) for v in clim]}")

    for seg in segments:
        polygons = loaded_polygons[seg.id]
        stats = prep_polygons(polygons, out / "segments" / seg.id, world_bounds, tile_depth)
        feature_names = numeric_feature_names(polygons.features)
        color_by = next(
            (n for n in _PREFERRED_COLOR_BY if n in feature_names),
            feature_names[0] if feature_names else None,
        )
        layers.append(
            {
                "kind": "segments",
                "id": seg.id,
                "path": f"segments/{seg.id}",
                "format": TILES_FORMAT,
                "tile_depth": stats["tile_depth"],
                "tiles": stats["tiles"],
                "n_cells": stats["n_cells"],
                "colormap": "viridis",
                "color_by": color_by,
                "show_outline": True,
                "show_fill": False,
                "fill_opacity": 0.35,
                "visible": seg.visible,
            }
        )
        print(
            f"  wrote segments/{seg.id}  {stats['n_cells']} cells, {stats['n_vertices']} vertices, "
            f"{stats['n_triangles']} triangles, {len(stats['tiles'])} tiles, color_by={color_by or 'flat'}"
        )

    for pts in points:
        transcripts = loaded_points[pts.id]
        stats = prep_points(transcripts, out / "points" / pts.id, world_bounds, tile_depth)
        layers.append(
            {
                "kind": "points",
                "id": pts.id,
                "path": f"points/{pts.id}",
                "format": TILES_FORMAT,
                "tile_depth": stats["tile_depth"],
                "tiles": stats["tiles"],
                "n_points": stats["n_points"],
                "palette": "tab10",
                "colormap": "yellow",
                "color_mode": "gene",
                "size": 3.0,
                "opacity": 0.9,
                "visible": True,
            }
        )
        print(
            f"  wrote points/{pts.id}  {stats['n_points']} transcripts, {stats['n_genes']} genes, "
            f"{len(stats['tiles'])} tiles"
        )

    write_manifest(
        out,
        {
            "cytos_format": CYTOS_FORMAT,
            "name": name or out.stem,
            "world_units": "micrometer",
            "world_bounds": [float(v) for v in world_bounds],
            "tile_depth": tile_depth,
            # Provenance: enough to know what a bundle came from and what
            # filters were applied, without going back to the source.
            "source": {
                "platform": "xenium",
                "path": str(source.resolve()),
                "imported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tile_size": tile_size,
                "min_qv": min_qv,
                "genes_only": genes_only,
            },
            "layers": layers,
        },
    )
    print(f"wrote {out} ({len(layers)} layers)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a Xenium output directory")
    parser.add_argument("--out", type=Path, required=True, help="bundle directory to create, e.g. sample.cytos")
    parser.add_argument("--name", default=None, help="display name; defaults to the bundle's own filename")
    parser.add_argument(
        "--tile-size",
        type=float,
        default=DEFAULT_TILE_SIZE,
        help=f"target tile size in world units (um); default {DEFAULT_TILE_SIZE:g}",
    )
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
    parser.add_argument(
        "--image",
        nargs=2,
        action="append",
        metavar=("PATH", "COLORMAP"),
        default=None,
        help="repeatable; use these images instead of whatever the source offers",
    )
    args = parser.parse_args()
    import_bundle(
        args.source,
        args.out,
        name=args.name,
        tile_size=args.tile_size,
        min_qv=args.min_qv,
        genes_only=not args.keep_controls,
        images_override=args.image,
    )


if __name__ == "__main__":
    main()
