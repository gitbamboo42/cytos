"""The `.cytos` slide: one directory holding every layer of one dataset, plus
a plain-JSON manifest that says what's in it. The viewer (the app in `web/`)
opens a slide and nothing else; `cytos-import` (see `cytos.prep.slide`) is
what builds one.

Why a directory with a JSON manifest, rather than one big zarr hierarchy:
nothing at the top level is then tied to any storage format. The root is an
ordinary directory, the manifest is ordinary JSON, and zarr is confined to the
leaves -- so a layer can change its on-disk format later without the slide
itself having to change. That's what the per-layer `format` field is for.

The manifest also carries the **tile index** (`tiles`: the (row, col) of every
tile a vector layer actually wrote). That is not just bookkeeping: it's what
keeps "which tiles does the camera touch" a pure arithmetic question against an
in-memory set, instead of a few hundred existence probes against the tile store
on every camera move.

Layout::

    sample.cytos/
      cytos.json                    <- the manifest; readable with no zarr import
      images/dapi.zarr.zip          <- OME-NGFF, still openable on its own
      segments/cell/
        tiles.zarr.zip
        features.parquet
      points/transcripts/
        tiles.zarr.zip
        genes.parquet

Each zarr store is a single zipped file by default rather than a directory of
chunk files -- a slide is written once and only ever read, so nothing needs
in-place writes, and one file per layer survives being copied between machines
in a way ~4000 loose files does not. `cytos.core.store` has the full reasoning;
`cytos-import --no-zip` writes plain directories instead, and both forms open.

World space is decided once, at import, and written here: every layer in a
slide shares one `world_bounds` and one `tile_depth`, so all the vector layers
sit on the same tile grid. Prep used to derive those per layer from that
layer's own data, which quietly gave the polygon and point caches two different
grids.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# Bumped only for a change old viewers can't read. A viewer refuses a slide
# whose format is newer than it knows, rather than guessing at missing fields
# the way the old free-standing caches had to.
CYTOS_FORMAT = 1

MANIFEST_NAME = "cytos.json"
SLIDE_SUFFIX = ".cytos"

# The zarr-backed tile layout `cytos.prep` writes today. Named in every vector
# layer so a future packed/mmap layout can land one layer at a time.
#
# Same tiles either way -- the `-zip-` variants just say the store is a single
# zipped file rather than a directory of chunk files (see `cytos.core.store`
# for why that is the default). The tag is what lets both keep working.
TILES_FORMAT = "zarr-tiles-v1"
TILES_ZIP_FORMAT = "zarr-zip-tiles-v1"
IMAGE_FORMAT = "ome-ngff-0.5"
IMAGE_ZIP_FORMAT = "ome-ngff-0.5-zip"

# Black->hue ramps, in the order channels get assigned one when the importer
# isn't told otherwise. The hues themselves are the viewer's
# (`web/src/core/colormaps.ts`); what the manifest carries is these names.
DEFAULT_CHANNEL_COLORMAPS = ("blue", "green", "red", "cyan", "magenta", "yellow")


@dataclass
class ImageLayer:
    """One single-channel OME-Zarr pyramid. A slide lists one of these per
    channel; they are assumed spatially registered, and get composited
    additively the way a multi-channel fluorescence view always is."""

    id: str
    path: Path
    colormap: str
    format: str = IMAGE_FORMAT
    clim: tuple[float, float] | None = None
    # Largest value anywhere in the channel, measured at import. Bounds the
    # contrast controls in both viewers; None on slides written before it was
    # recorded, which makes the viewer measure it at open instead.
    intensity_max: float | None = None
    visible: bool = True


@dataclass
class SegmentLayer:
    """A triangulated, tiled polygon set (`cytos.prep.polygons`)."""

    id: str
    path: Path
    tile_depth: int
    tiles: set[tuple[int, int]]
    n_cells: int
    format: str = TILES_FORMAT
    colormap: str = "viridis"
    color_by: str | None = None
    show_outline: bool = True
    show_fill: bool = False
    fill_opacity: float = 0.35
    visible: bool = True
    # Presentation of categorical features (see the viewer's legend):
    # the qualitative palette, per-category colour overrides, and hidden
    # categories -- both keyed feature -> category, where a category key is
    # a JSON-safe string ("7", or "unassigned" for cells absent from the
    # feature). Session state -- the importer writes none of these, so
    # these defaults are the slide defaults.
    palette: str = "tab20"
    category_colors: dict = field(default_factory=dict)  # feature -> {key -> "#rrggbb"}
    hidden_categories: dict = field(default_factory=dict)  # feature -> [key, ...]


@dataclass
class PointLayer:
    """A tiled transcript point cloud (`cytos.prep.points`).

    `levels` counts detail levels: full-detail tiles at `tile_depth` plus
    aggregate levels up to depth 0. 1 means a slide from before levels
    existed -- full detail only, which still draws."""

    id: str
    path: Path
    tile_depth: int
    tiles: set[tuple[int, int]]
    n_points: int
    levels: int = 1
    format: str = TILES_FORMAT
    palette: str = "tab10"
    colormap: str = "yellow"
    color_mode: str = "gene"
    size: float = 3.0
    opacity: float = 0.9
    visible: bool = True


@dataclass
class Slide:
    root: Path
    name: str
    world_units: str
    world_bounds: tuple[float, float, float, float]
    tile_depth: int
    images: list[ImageLayer] = field(default_factory=list)
    segments: list[SegmentLayer] = field(default_factory=list)
    points: list[PointLayer] = field(default_factory=list)

    def has_data(self) -> bool:
        return bool(self.images or self.segments or self.points)


def _tiles_to_set(raw) -> set[tuple[int, int]]:
    return {(int(r), int(c)) for r, c in raw}


def load_slide(path: Path | str) -> Slide:
    """Read `<path>/cytos.json` and resolve every layer path against the
    slide root, so a slide can be moved or renamed without rewriting it."""
    root = Path(path)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        raise ValueError(f"{root} is not a cytos slide -- no {MANIFEST_NAME} in it")

    manifest = json.loads(manifest_path.read_text())
    version = int(manifest.get("cytos_format", 0))
    if version > CYTOS_FORMAT:
        raise ValueError(
            f"{manifest_path}: slide format {version} is newer than this cytos "
            f"understands ({CYTOS_FORMAT}) -- upgrade cytos to open it"
        )

    slide = Slide(
        root=root,
        name=manifest.get("name", root.stem),
        world_units=manifest.get("world_units", "micrometer"),
        world_bounds=tuple(float(v) for v in manifest["world_bounds"]),
        tile_depth=int(manifest["tile_depth"]),
    )

    for layer in manifest.get("layers", []):
        kind = layer["kind"]
        layer_path = root / layer["path"]
        if kind == "image":
            clim = layer.get("clim")
            slide.images.append(
                ImageLayer(
                    id=layer["id"],
                    path=layer_path,
                    colormap=layer.get("colormap", DEFAULT_CHANNEL_COLORMAPS[0]),
                    format=layer.get("format", IMAGE_FORMAT),
                    clim=tuple(float(v) for v in clim) if clim else None,
                    intensity_max=(
                        float(layer["intensity_max"])
                        if layer.get("intensity_max") is not None
                        else None
                    ),
                    visible=bool(layer.get("visible", True)),
                )
            )
        elif kind == "segments":
            slide.segments.append(
                SegmentLayer(
                    id=layer["id"],
                    path=layer_path,
                    tile_depth=int(layer.get("tile_depth", slide.tile_depth)),
                    tiles=_tiles_to_set(layer.get("tiles", [])),
                    n_cells=int(layer.get("n_cells", 0)),
                    format=layer.get("format", TILES_FORMAT),
                    colormap=layer.get("colormap", "viridis"),
                    color_by=layer.get("color_by"),
                    show_outline=bool(layer.get("show_outline", True)),
                    show_fill=bool(layer.get("show_fill", False)),
                    fill_opacity=float(layer.get("fill_opacity", 0.35)),
                    visible=bool(layer.get("visible", True)),
                    palette=layer.get("palette", "tab20"),
                    category_colors=dict(layer.get("category_colors", {})),
                    hidden_categories=dict(layer.get("hidden_categories", {})),
                )
            )
        elif kind == "points":
            slide.points.append(
                PointLayer(
                    id=layer["id"],
                    path=layer_path,
                    tile_depth=int(layer.get("tile_depth", slide.tile_depth)),
                    tiles=_tiles_to_set(layer.get("tiles", [])),
                    n_points=int(layer.get("n_points", 0)),
                    levels=int(layer.get("levels", 1)),
                    format=layer.get("format", TILES_FORMAT),
                    palette=layer.get("palette", "tab10"),
                    colormap=layer.get("colormap", "yellow"),
                    color_mode=layer.get("color_mode", "gene"),
                    size=float(layer.get("size", 3.0)),
                    opacity=float(layer.get("opacity", 0.9)),
                    visible=bool(layer.get("visible", True)),
                )
            )
        else:
            # Forward compatibility within one format version: a layer kind
            # this build doesn't know is skipped, not fatal.
            print(f"{manifest_path}: skipping unknown layer kind {kind!r} ({layer.get('id')})")

    _check_layer_paths(slide, manifest_path)
    return slide


def _check_layer_paths(slide: Slide, manifest_path: Path) -> None:
    """The one cost of a manifest separate from the data: it can name a layer
    that isn't on disk. Cheaper to say so at open than to fail later mid-render.
    """
    missing = [
        layer.id
        for layer in (*slide.images, *slide.segments, *slide.points)
        if not layer.path.exists()
    ]
    if missing:
        raise ValueError(f"{manifest_path}: layers listed but not on disk: {', '.join(missing)}")


def write_manifest(root: Path, manifest: dict) -> None:
    """Write `cytos.json`, replacing any existing one in a single step.

    Via a temp file and `os.replace` rather than a plain write, because the
    manifest is no longer only ever written once at import: `cytos.prep.
    segments` appends a layer to a slide that may already be open in a viewer
    (or two). A half-written manifest is a slide that no longer opens at all,
    and `os.replace` is atomic on every platform this runs on, so a reader
    sees either the old file or the new one.
    """
    path = root / MANIFEST_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, path)
