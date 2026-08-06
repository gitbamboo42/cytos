# cytos

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image. Dropping editability (unlike
napari's Shapes layer) unlocks precomputation, immutable GPU buffers, and
tiling — the same approach Xenium Explorer, deck.gl, and Mapbox use at this
scale. Built on pygfx/wgpu directly, not a higher-level mapping library — see
`work-notes/plan.md` for the full phased plan (data model, offline
preprocessing, renderer, picking).

## Project layout

Properly installed package (`pyproject.toml`, hatchling; `pip install -e .`
into the `cytos` conda env), not a folder of scripts — `pip install cytos`
pulls in real dependencies and registers two console-script commands
(`cytos-viewer`, `cytos-convert-ome-zarr`, both defined in `cli.py`).

`src/cytos/` splits the way napari splits `layers/`+`components/` (core model)
from `_vispy/` (rendering backend) from `_qt/` (UI): `core/` (pure data model,
no GPU/UI imports), `prep/` (offline preprocessing), `render/` (pygfx scene
construction), `ui/` (Qt widgets only) — one-directional, each layer
importable without the ones "above" it.

`tools/make_synthetic_big_pyramid.py` stays outside the package, deliberately:
dev-only stress-test generator, no end user needs it. Package vs. not is
about what ships, not who runs it — `cytos-viewer`/`cytos-convert-ome-zarr`
are the real product despite living in `src/`.

`data/` and `work-notes/` are both fully gitignored — see `.gitignore` and
each folder's own contents for what's there.

## Core data model — two primitives, not one per data source

- **An OME-Zarr image** — the morphology/background layer ("the figure").
- **A polygon set** — cell (and nucleus) segmentation boundaries: flat
  `coords (M,2) float32`, `offsets (N+1,) uint32`, `cell_id (N,) uint32`, plus
  an Arrow `features` table for per-cell attributes.

**Naming convention:** name loaders/converters after what they produce
(`load_polygons`, `load_ome_zarr_image`, `polygons_from_parquet`), never after
the source platform (`load_xenium`) — Xenium is the first data source, not the
only one.

## Implementation gotchas (verified against a real bundle)

Input-data format facts (parquet layout, Xenium zarr schema, OME-TIFF vs.
zarr) live in `data/README.md`, not here — these are code-level pitfalls hit
while building the tool, not facts about the input data itself.

- `mapbox_earcut`'s nanobind binding requires **C-contiguous** `(*, 2)
  float32` arrays — `pandas.DataFrame.to_numpy()` on multiple columns returns
  Fortran-order, which fails with an unhelpful generic `TypeError`. Fix:
  `np.ascontiguousarray(...)` right after `to_numpy()`.
- Fluorescence channel data is **sparse with a heavy-tailed intensity
  distribution** (DAPI test channel: median 0, 99th pct 30, max 1194). Raw
  min/max `clim` crushes the image to near-black. Use percentile-based
  autocontrast (1st/99.5th pct), matching napari/Fiji.
- **World Y increases upward** (standard Cartesian/camera convention), fixed
  at the pixel↔world boundary in `PyramidLevel.row_to_world_y`/
  `world_y_to_row` (`src/cytos/core/image.py`) rather than left in raw
  row-major (downward) convention — needed so the pygfx view and the Qt
  minimap agree on orientation. **Consequence:** this does not match raw
  Xenium polygon Y (`cell_boundaries.parquet`, `cells.parquet`), which stays
  row-major — overlaying polygons will need `world_y = -raw_y`.
- **`camera.width`/`camera.height` don't reflect what's actually visible on
  screen** when the viewport aspect differs from the camera's —
  `OrthographicCamera(maintain_aspect=True)` pads internally without updating
  those properties. Any world-space view rect derived from them directly is
  too narrow. Use `src/cytos/render/camera.py:effective_camera_view_size()`
  instead, everywhere a camera-driven view is needed.
- **Sequential colormaps (matplotlib's `Blues`/`Greens`/`Reds`, vendored via
  `plotlet`) anchor at near-white, not black.** Additive-blended composite
  display (see the earlier fluorescence/autocontrast note) wants pixel value
  0 to render as black background; a white-anchored colormap washes the
  whole composite to gray instead. `src/cytos/render/image.py` registers its
  own black->hue set (`blue`, `green`, `red`, `cyan`, `magenta`, `yellow`) via
  `plotlet.register_colormap` at import time for this reason — don't default
  composite channels to a matplotlib sequential colormap without checking its
  0-end color first.
- **Xenium `morphology_focus.ome.tif` doesn't always have a channel axis.**
  Protein-panel bundles (e.g. `human_kidney_tiny/`) ship multi-channel
  (`axes="CYX"`), but gene-only-panel bundles (e.g.
  `xenium_breast_cancer_rep1/`) collapse to a single DAPI plane with
  `axes="YX"` — no channel dimension at all. `series.asarray()[channel]`
  silently mis-indexes that 2D array (grabs one *row*, not a channel plane)
  instead of raising. `src/cytos/prep/pyramid.py` checks `series.axes` and
  only indexes by channel when a channel axis actually exists.

## Environment

Python setup (conda envs, interpreter paths) is in the gitignored
`CLAUDE.local.md`, not here — it's machine-specific, not general project
knowledge.
