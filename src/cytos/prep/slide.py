"""`cytos-import`: turn a source dataset into a `.cytos` slide -- one
directory holding every layer plus the manifest that describes them (see
`cytos.core.slide` for the layout and why it is a plain directory rather than
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
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from cytos.core.slide import (
    CYTOS_FORMAT,
    DEFAULT_CHANNEL_COLORMAPS,
    IMAGE_FORMAT,
    IMAGE_ZIP_FORMAT,
    TILES_FORMAT,
    TILES_ZIP_FORMAT,
    write_manifest,
)
from cytos.core.image import load_pyramid_levels
from cytos.core.points import DEFAULT_MIN_QV, load_transcripts
from cytos.prep.archive import zip_store
from cytos.prep.labels import DEFAULT_SIMPLIFY
from cytos.prep.points import prep_points
from cytos.prep.pyramid import convert_ome_zarr, ome_channel_names
from cytos.prep.segments import (
    SegmentSource,
    check_fits_slide,
    default_layer_id,
    load_segments,
    unique_layer_id,
    write_segment_layer,
)
from cytos.prep.tiling import choose_tile_depth

DEFAULT_TILE_SIZE = 500.0


@dataclass
class _ImageSource:
    id: str
    path: Path  # an .ome.zarr to copy, or an .ome.tif to convert
    channel: int = 0


@dataclass
class _PointSource:
    id: str
    transcripts: Path


def _channel_layer_id(name: str, channel: int) -> str:
    """A layer id from an OME channel name. Stain names arrive as marker
    lists ("ATP1A1/CD45/E-Cadherin", "alphaSMA/Vimentin") — keep the words,
    normalise everything between them to single underscores. The id names
    files and session keys, so it has to be filesystem- and JSON-key-safe."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or f"ch{channel}"


def _discover_xenium(source: Path) -> tuple[list[_ImageSource], list[SegmentSource], list[_PointSource]]:
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
        # XOA 2.0's multimodal layout: morphology_focus_0000.ome.tif ..
        # _0003.ome.tif are one multi-file OME-TIFF, one file per stain
        # channel. Open the first and tifffile assembles the whole series, so
        # each channel becomes an _ImageSource with a channel *index* into
        # that one series — unlike the pre-2.0 one-file-per-channel layout
        # below, where the file itself is the channel.
        multi = sorted(focus_dir.glob("morphology_focus_*.ome.tif")) if focus_dir.is_dir() else []
        if multi:
            names = ome_channel_names(multi[0])
            # DAPI first, same rule as the zarr branch above: channels take
            # their default colormap by position, and the nuclear stain
            # should land on blue.
            order = sorted(range(len(names)), key=lambda i: ("dapi" not in names[i].lower(), i))
            for i in order:
                images.append(_ImageSource(id=_channel_layer_id(names[i], i), path=multi[0], channel=i))
        elif tif.exists():
            images.append(_ImageSource(id="morphology", path=tif))
        elif focus_dir.is_dir():
            for channel_tif in sorted(focus_dir.glob("ch*.ome.tif")):
                images.append(_ImageSource(id=channel_tif.name.split(".")[0], path=channel_tif))

    cells = source / "cells.parquet"
    cells = cells if cells.exists() else None
    # The pipeline's clusterings (analysis/clustering/*/clusters.csv,
    # Barcode -> 1-based cluster) become categorical features on every
    # segment layer: "cluster" for graphclust -- the one 10x's own tools
    # show by default -- then the fixed-k alternatives by their k.
    categories: list[tuple[str, Path]] = []
    for csv in sorted((source / "analysis" / "clustering").glob("*/clusters.csv")):
        name = csv.parent.name.removeprefix("gene_expression_")
        name = "cluster" if name == "graphclust" else name.removesuffix("_clusters")
        categories.append((name, csv))
    categories.sort(key=lambda nc: (nc[0] != "cluster", len(nc[0]), nc[0]))
    segments: list[SegmentSource] = []
    if (source / "cell_boundaries.parquet").exists():
        segments.append(
            SegmentSource(
                id="cell", path=source / "cell_boundaries.parquet", cells=cells, categories=categories
            )
        )
    if (source / "nucleus_boundaries.parquet").exists():
        # Off by default: nuclei sit inside the cell outlines already on
        # screen, so showing both at once reads as doubled lines rather than
        # as two layers. Present and one checkbox away.
        segments.append(
            SegmentSource(
                id="nucleus",
                path=source / "nucleus_boundaries.parquet",
                cells=cells,
                visible=False,
                categories=categories,
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


def import_slide(
    source: Path,
    out: Path,
    name: str | None = None,
    tile_size: float = DEFAULT_TILE_SIZE,
    min_qv: float = DEFAULT_MIN_QV,
    genes_only: bool = True,
    images_override: list[tuple[str, str]] | None = None,
    extra_segments: list[SegmentSource] | None = None,
    zip_stores: bool = True,
) -> Path:
    images, segments, points = _discover_xenium(source)
    # Added to what the source offers, not swapped for it -- unlike
    # `--image`, which replaces a channel set wholesale. A segmentation from
    # another tool is a second opinion about the same cells, and the usual
    # thing to want is both on screen, one checkbox apart.
    for extra in extra_segments or []:
        extra.id = unique_layer_id(extra.id, {seg.id for seg in segments})
        segments.append(extra)
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
        print(f"  segments {seg.id:12s} {seg.path.name}")
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

    extra_ids = {seg.id for seg in (extra_segments or [])}
    loaded_polygons = {}
    for seg in segments:
        # Loaded (and, for a label mask, traced) once here rather than again in
        # pass 2: the geometry is what the world bounds are decided from, and
        # tracing a whole-slide mask twice would double the longest step in
        # the whole import.
        polygons = load_segments(seg)
        loaded_polygons[seg.id] = polygons
        if seg.id not in extra_ids:
            world_bounds = _union(world_bounds, _coords_bounds(polygons.coords))
        print(f"  loaded {seg.id}: {len(polygons.offsets) - 1} objects")

    loaded_points = {}
    for pts in points:
        transcripts = load_transcripts(pts.transcripts, min_qv=min_qv, genes_only=genes_only)
        loaded_points[pts.id] = transcripts
        world_bounds = _union(world_bounds, _coords_bounds(transcripts.coords))
        print(f"  loaded {pts.id}: {len(transcripts.coords)} transcripts, {len(transcripts.gene_names)} genes")

    # A `--segments` layer is checked against the extent the *source dataset*
    # occupies, and only then allowed to widen it. Unioned blindly, a
    # segmentation in some other coordinate space would simply make the world
    # bigger and sit in a far corner of it -- a slide that imports cleanly and
    # is wrong. Nothing to check against if the source brought no extent of its
    # own, which is the "--segments is the whole slide" case.
    for seg in segments:
        if seg.id in extra_ids:
            if world_bounds is not None:
                check_fits_slide(loaded_polygons[seg.id], world_bounds, str(seg.path))
            world_bounds = _union(world_bounds, _coords_bounds(loaded_polygons[seg.id].coords))

    if world_bounds is None:
        raise ValueError(f"{source}: no layer with a spatial extent -- cannot place a world grid")

    tile_depth = choose_tile_depth(world_bounds, tile_size)
    print(
        f"world_bounds {tuple(round(v, 2) for v in world_bounds)}  "
        f"tile_depth {tile_depth} ({(max(world_bounds[2] - world_bounds[0], world_bounds[3] - world_bounds[1]) / (1 << tile_depth)):.1f} um/tile)"
    )

    # -- pass 2: write the slide
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    layers = []

    for i, img in enumerate(images):
        image_dir = out / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        store_name = f"{img.id}.zarr.zip" if zip_stores else f"{img.id}.zarr"
        dest = image_dir / store_name
        staged = None
        if img.id in image_levels:
            # Real copy, not a link: a slide has to survive being moved to
            # another machine on its own. Zipping reads the source pyramid
            # straight into the archive, so it is still written exactly once --
            # no copy-then-pack of a whole-slide image.
            if zip_stores:
                zip_store(img.path, dest)
            else:
                shutil.copytree(img.path, dest)
        else:
            # Converted as a directory first, because that is what the OME-Zarr
            # writer produces. Packing it waits until after the clim below.
            staged = image_dir / f"{img.id}.zarr"
            convert_ome_zarr(img.path, staged, channel=img.channel, quiet=True)
            image_levels[img.id] = load_pyramid_levels(staged)

        # Percentile autocontrast, decided here rather than at every open:
        # fluorescence channels are sparse and heavy-tailed, so raw min/max
        # crushes them to near-black (see skills/developers.md).
        coarsest = np.asarray(image_levels[img.id][-1].data)
        clim = [float(v) for v in np.percentile(coarsest, [1, 99.5])]

        # Only now, because these levels read from `staged`, and a zarr array
        # whose chunk files have been deleted reads back as fill value -- all
        # zeros, no error. Packing before the clim silently produced clim
        # [0, 0] and a black image.
        if staged is not None and zip_stores:
            zip_store(staged, dest, remove_source=True)
        colormap = colormaps.get(img.id, DEFAULT_CHANNEL_COLORMAPS[i % len(DEFAULT_CHANNEL_COLORMAPS)])
        layers.append(
            {
                "kind": "image",
                "id": img.id,
                "path": f"images/{store_name}",
                "format": IMAGE_ZIP_FORMAT if zip_stores else IMAGE_FORMAT,
                "colormap": colormap,
                "clim": clim,
                "visible": True,
            }
        )
        print(f"  wrote images/{store_name}  colormap={colormap} clim={[round(v, 1) for v in clim]}")

    for seg in segments:
        layers.append(
            write_segment_layer(
                loaded_polygons[seg.id],
                out,
                seg.id,
                world_bounds,
                tile_depth,
                zip_stores=zip_stores,
                visible=seg.visible,
            )
        )

    for pts in points:
        transcripts = loaded_points[pts.id]
        layer_dir = out / "points" / pts.id
        stats = prep_points(transcripts, layer_dir, world_bounds, tile_depth)
        if zip_stores:
            zip_store(layer_dir / "tiles.zarr", remove_source=True)
        layers.append(
            {
                "kind": "points",
                "id": pts.id,
                "path": f"points/{pts.id}",
                "format": TILES_ZIP_FORMAT if zip_stores else TILES_FORMAT,
                "tile_depth": stats["tile_depth"],
                "tiles": stats["tiles"],
                "n_points": stats["n_points"],
                "levels": stats["levels"],
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
            # Provenance: enough to know what a slide came from and what
            # filters were applied, without going back to the source.
            "source": {
                "platform": "xenium",
                "path": str(source.resolve()),
                "imported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tile_size": tile_size,
                "min_qv": min_qv,
                "genes_only": genes_only,
                # Segmentation from elsewhere is no longer a thing the source
                # path implies, so record where each layer actually came from.
                "extra_segments": [
                    {"id": seg.id, "path": str(seg.path.resolve())} for seg in (extra_segments or [])
                ],
            },
            "layers": layers,
        },
    )
    print(f"wrote {out} ({len(layers)} layers)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a Xenium output directory")
    parser.add_argument("--out", type=Path, required=True, help="slide directory to create, e.g. sample.cytos")
    parser.add_argument("--name", default=None, help="display name; defaults to the slide's own filename")
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
        "--no-zip",
        action="store_true",
        help=(
            "write each zarr store as a directory of chunk files instead of one "
            "zipped file — slower to copy between machines, but readable by "
            "napari and other OME-NGFF tools"
        ),
    )
    parser.add_argument(
        "--image",
        nargs=2,
        action="append",
        metavar=("PATH", "COLORMAP"),
        default=None,
        help="repeatable; use these images instead of whatever the source offers",
    )
    parser.add_argument(
        "--segments",
        nargs="+",
        action="append",
        metavar="PATH [NAME]",
        default=None,
        help=(
            "repeatable; add a segmentation from a boundary parquet or an OME-Zarr "
            "label mask, *alongside* whatever the source offers (unlike --image, "
            "which replaces). NAME defaults to the file's own name"
        ),
    )
    parser.add_argument(
        "--segments-columns",
        default=None,
        metavar="ID,X,Y",
        help="boundary-table column names for --segments, if they aren't detected (e.g. label,x,y)",
    )
    parser.add_argument(
        "--segments-simplify",
        type=float,
        default=DEFAULT_SIMPLIFY,
        help=(
            f"label masks: Douglas-Peucker tolerance in pixels, spent on cutting the "
            f"vertex count marching squares produces; default {DEFAULT_SIMPLIFY}"
        ),
    )
    args = parser.parse_args()

    columns = tuple(args.segments_columns.split(",")) if args.segments_columns else None
    if columns is not None and len(columns) != 3:
        print("error: --segments-columns takes exactly three names, ID,X,Y", file=sys.stderr)
        raise SystemExit(2)
    extra_segments = []
    for values in args.segments or []:
        if len(values) > 2:
            print(f"error: --segments takes a path and an optional name, got {len(values)} values", file=sys.stderr)
            raise SystemExit(2)
        path = Path(values[0])
        extra_segments.append(
            SegmentSource(
                id=values[1] if len(values) == 2 else default_layer_id(path),
                path=path,
                columns=columns,
                simplify=args.segments_simplify,
            )
        )

    try:
        import_slide(
            args.source,
            args.out,
            name=args.name,
            tile_size=args.tile_size,
            min_qv=args.min_qv,
            genes_only=not args.keep_controls,
            images_override=args.image,
            extra_segments=extra_segments,
            zip_stores=not args.no_zip,
        )
    except (ValueError, KeyError, OSError) as err:
        # Pointing this at the wrong folder is an ordinary mistake, not a bug,
        # and a traceback answers a question nobody asked. The message is the
        # whole of what's useful, and it is what the import window shows.
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
