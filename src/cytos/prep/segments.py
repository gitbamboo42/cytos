"""Bringing a segmentation into a slide, whatever shape it arrived in.

Two input formats, one output. A segmentation is either

* a **boundary table** -- long-format parquet, one row per vertex
  (`cytos.core.polygons.polygons_from_parquet`), which is what Xenium and most
  GIS-flavoured exports ship; or
* an **OME-Zarr label mask** -- an image whose pixel values are object ids
  (`cytos.prep.labels`), which is what Cellpose, StarDist and friends produce.

Both become the same `Polygons`, and from there the existing pipeline takes
over unchanged: triangulate, Hilbert-sort, tile, write. Nothing downstream --
not `prep_polygons`, not the tile grid, not the renderer -- knows which format a
layer came from, which is the point.

This module is also the only place that *adds a layer to a slide that already
exists*. `cytos-import` builds a slide in one shot from sources it was handed;
`add_segments_to_slide` appends one afterwards, which is what File > Add
Segments… in the viewer runs (in a subprocess -- tracing a whole-slide mask is
minutes of CPU, and the viewer has a render loop to keep feeding).

The one invariant that matters here: a layer added later must land on the
slide's **existing** world grid. `world_bounds` and `tile_depth` are read from
the manifest and passed through untouched. Recomputing them from the new
segmentation would put it on a grid no other layer shares, and nothing would
report an error -- the layer would simply draw in the wrong place.

    python -m cytos.prep.segments sample.cytos nuclei_masks.ome.zarr --name nuclei
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cytos.core.polygons import Polygons, polygons_from_parquet, feature_names
from cytos.core.slide import (
    MANIFEST_NAME,
    TILES_FORMAT,
    TILES_ZIP_FORMAT,
    write_manifest,
)
from cytos.prep.archive import zip_store
from cytos.prep.labels import DEFAULT_SIMPLIFY, label_mask_world_bounds, polygons_from_labels
from cytos.prep.polygons import prep_polygons

# Preferred default for a segment layer's "Color by": the most broadly
# meaningful per-object measurement present. Baked into the manifest at import
# so the viewer doesn't have to re-guess every time it opens. `area` is last
# because it is the one this module synthesizes when the source carries no
# attributes of its own -- a real measured column beats a derived one.
PREFERRED_COLOR_BY = ("cell_area", "nucleus_area", "transcript_counts", "total_counts", "area")

FORMAT_PARQUET = "parquet"
FORMAT_LABELS = "labels"


@dataclass
class SegmentSource:
    """One segmentation to import, and how to read it."""

    id: str
    path: Path  # a boundary parquet, or an OME-Zarr label mask
    cells: Path | None = None  # optional per-object attribute table (parquet only)
    columns: tuple[str, str, str] | None = None  # (id, x, y), parquet only
    simplify: float = DEFAULT_SIMPLIFY  # label masks only
    visible: bool = True
    # Named (id, category) tables to join as categorical features -- e.g.
    # clusterings (see cytos.core.polygons.join_categories). Parquet only.
    categories: list = field(default_factory=list)


def segment_format(path: Path) -> str:
    """Which loader reads `path`. Decided by what the file *is*, not by a flag
    the caller has to remember to set."""
    path = Path(path)
    if path.suffix == ".parquet":
        return FORMAT_PARQUET
    if path.is_dir() or path.suffix in (".zarr", ".zip"):
        return FORMAT_LABELS
    raise ValueError(
        f"{path}: not a segmentation cytos can read -- expected a boundary table "
        f"(*.parquet) or an OME-Zarr label mask (*.ome.zarr or *.ome.zarr.zip)"
    )


def load_segments(source: SegmentSource, progress=print) -> Polygons:
    """Read one segmentation into the ragged-array polygon model."""
    if segment_format(source.path) == FORMAT_PARQUET:
        return polygons_from_parquet(source.path, source.cells, source.columns, source.categories)
    return polygons_from_labels(source.path, simplify=source.simplify, progress=progress)


def default_layer_id(path: Path) -> str:
    """A layer name from the file name: `nuclei_masks.ome.zarr` -> `nuclei_masks`."""
    name = Path(path).name
    for suffix in (".ome.zarr.zip", ".ome.zarr", ".zarr.zip", ".zarr", ".parquet", ".zip"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def unique_layer_id(wanted: str, taken: set[str]) -> str:
    """`wanted`, or `wanted-2`, `wanted-3`… -- layer ids key the session's
    per-layer state, so two layers sharing one would share their settings."""
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


def write_segment_layer(
    polygons: Polygons,
    slide_root: Path,
    layer_id: str,
    world_bounds: tuple[float, float, float, float],
    tile_depth: int,
    zip_stores: bool = True,
    visible: bool = True,
    progress=print,
) -> dict:
    """Prep one segmentation into `<slide>/segments/<layer_id>/` and return the
    manifest entry describing it. `world_bounds` and `tile_depth` are the
    slide's -- see this module's docstring."""
    layer_dir = slide_root / "segments" / layer_id
    # Cleared first, so a run that was cancelled or failed part-way through
    # can't leave files behind that the next one mixes with its own -- a
    # `tiles.zarr.zip` next to a fresh `tiles.zarr`, say.
    shutil.rmtree(layer_dir, ignore_errors=True)
    stats = prep_polygons(polygons, layer_dir, world_bounds, tile_depth)
    if zip_stores:
        zip_store(layer_dir / "tiles.zarr", remove_source=True)

    names = feature_names(polygons.features)
    color_by = next(
        (n for n in PREFERRED_COLOR_BY if n in names),
        names[0] if names else None,
    )
    progress(
        f"  wrote segments/{layer_id}  {stats['n_cells']} objects, {stats['n_vertices']} vertices, "
        f"{stats['n_triangles']} triangles, {len(stats['tiles'])} tiles, color_by={color_by or 'flat'}"
    )
    return {
        "kind": "segments",
        "id": layer_id,
        "path": f"segments/{layer_id}",
        "format": TILES_ZIP_FORMAT if zip_stores else TILES_FORMAT,
        "tile_depth": stats["tile_depth"],
        "tiles": stats["tiles"],
        "n_cells": stats["n_cells"],
        "colormap": "viridis",
        "color_by": color_by,
        "show_outline": True,
        "show_fill": False,
        "fill_opacity": 0.35,
        "visible": visible,
    }


def _overlap_fraction(
    coords_bounds: tuple[float, float, float, float], world_bounds: tuple[float, float, float, float]
) -> float:
    """How much of the segmentation's own extent falls inside the slide's, by
    area. Zero means it belongs to some other slide -- or to this one but in
    un-registered coordinates, which is the same problem."""
    ax0, ay0, ax1, ay1 = coords_bounds
    bx0, by0, bx1, by1 = world_bounds
    ox = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    oy = max(0.0, min(ay1, by1) - max(ay0, by0))
    own = max(ax1 - ax0, 1e-9) * max(ay1 - ay0, 1e-9)
    return (ox * oy) / own


def check_bounds_fit_slide(
    bounds: tuple[float, float, float, float],
    world_bounds: tuple[float, float, float, float],
    label: str,
) -> None:
    """Refuse an extent that doesn't belong to this slide.

    Worth an explicit check because the failure is otherwise silent:
    `sort_and_tile` *clips* out-of-bounds anchors into the edge of the grid
    (see `cytos.prep.tiling`), so an unregistered segmentation would import
    without complaint and draw as a smear along one side of the slide.

    The fraction measured is of the *segmentation's* own extent, not the
    slide's -- a segmentation covering one small region of a big slide is an
    ordinary thing to have and is 100% inside it, while anything that reaches
    far outside the slide is in the wrong coordinate space. So the bar is
    high: mostly-outside is refused, and any clipping at all is reported.
    """
    fraction = _overlap_fraction(bounds, world_bounds)
    if fraction < 0.5:
        raise ValueError(
            f"{label}: {'none' if fraction <= 0 else f'only {fraction:.0%}'} of it lies inside "
            f"this slide. Its extent is {tuple(round(v, 1) for v in bounds)} and the slide's is "
            f"{tuple(round(v, 1) for v in world_bounds)} -- they are in different coordinate "
            f"spaces, so it needs registering into the slide's before it can be added."
        )
    if fraction < 1.0:
        print(
            f"warning: {label}: {1 - fraction:.0%} of it lies outside the slide and will be "
            f"clipped to the edge of the tile grid"
        )


def check_fits_slide(
    polygons: Polygons, world_bounds: tuple[float, float, float, float], label: str
) -> None:
    """`check_bounds_fit_slide` for a loaded segmentation's own extent."""
    coords = polygons.coords
    check_bounds_fit_slide(
        (
            float(coords[:, 0].min()),
            float(coords[:, 1].min()),
            float(coords[:, 0].max()),
            float(coords[:, 1].max()),
        ),
        world_bounds,
        label,
    )


def add_segments_to_slide(
    slide_root: Path,
    source: Path,
    name: str | None = None,
    cells: Path | None = None,
    columns: tuple[str, str, str] | None = None,
    simplify: float = DEFAULT_SIMPLIFY,
    progress=print,
) -> str:
    """Add one segmentation to a slide that already exists, and return the id
    of the layer written.

    The manifest is re-read here and written back atomically: two viewer
    windows can be open on one slide, and a half-written `cytos.json` is a
    slide that no longer opens at all.
    """
    slide_root, source = Path(slide_root), Path(source)
    manifest_path = slide_root / MANIFEST_NAME
    if not manifest_path.exists():
        raise ValueError(f"{slide_root}: not a cytos slide -- no {MANIFEST_NAME} in it")
    manifest = json.loads(manifest_path.read_text())

    world_bounds = tuple(float(v) for v in manifest["world_bounds"])
    tile_depth = int(manifest["tile_depth"])
    layers = manifest.get("layers", [])
    layer_id = unique_layer_id(name or default_layer_id(source), {layer["id"] for layer in layers})

    # Follow whatever the slide already does, rather than imposing this
    # build's default on a slide written by another one.
    zip_stores = not any(layer.get("format") == TILES_FORMAT for layer in layers)

    # A label mask carries its own extent in its metadata, so the "this is
    # from a different slide" mistake can be caught before paying for the
    # trace rather than minutes after it.
    if segment_format(source) == FORMAT_LABELS:
        check_bounds_fit_slide(label_mask_world_bounds(source), world_bounds, str(source))

    progress(f"reading {source}")
    polygons = load_segments(
        SegmentSource(id=layer_id, path=source, cells=cells, columns=columns, simplify=simplify),
        progress=progress,
    )
    check_fits_slide(polygons, world_bounds, str(source))

    layers.append(
        write_segment_layer(
            polygons,
            slide_root,
            layer_id,
            world_bounds,
            tile_depth,
            zip_stores=zip_stores,
            progress=progress,
        )
    )
    manifest["layers"] = layers
    manifest.setdefault("added", []).append(
        {"kind": "segments", "id": layer_id, "path": str(source.resolve())}
    )
    write_manifest(slide_root, manifest)
    progress(f"added segments '{layer_id}' to {slide_root}")
    return layer_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slide", type=Path, help="an existing .cytos slide")
    parser.add_argument("source", type=Path, help="a boundary parquet or an OME-Zarr label mask")
    parser.add_argument("--name", default=None, help="layer id; defaults to the source's own filename")
    parser.add_argument("--cells", type=Path, default=None, help="optional per-object attribute parquet")
    parser.add_argument(
        "--columns",
        default=None,
        metavar="ID,X,Y",
        help="boundary table column names, if they aren't detected (e.g. label,x,y)",
    )
    parser.add_argument(
        "--simplify",
        type=float,
        default=DEFAULT_SIMPLIFY,
        help=f"label masks: Douglas-Peucker tolerance in pixels; default {DEFAULT_SIMPLIFY}",
    )
    args = parser.parse_args()
    columns = tuple(args.columns.split(",")) if args.columns else None
    if columns is not None and len(columns) != 3:
        print("error: --columns takes exactly three names, ID,X,Y", file=sys.stderr)
        raise SystemExit(2)
    try:
        add_segments_to_slide(
            args.slide,
            args.source,
            name=args.name,
            cells=args.cells,
            columns=columns,
            simplify=args.simplify,
        )
    except (ValueError, KeyError, OSError) as err:
        # Same contract as cytos-import: pointing this at the wrong file is an
        # ordinary mistake, and the message is the whole of what's useful --
        # it is what the viewer's Add Segments panel shows.
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
