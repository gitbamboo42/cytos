# cytos developer guide

AI-oriented onboarding for working on the cytos codebase. Vendor-neutral —
symlink to it as `CLAUDE.md`, `.cursorrules`, `AGENTS.md`, or whatever your
tool expects; human contributors can read it directly. It ships inside the
package: `cytos-ctl skill developer` prints it. The guide for *operating*
the viewer (driving it with `cytos-ctl`/MCP) is `users.md` next to this
file — `cytos-ctl skill`.

## What cytos is

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image. Dropping editability (unlike
napari's Shapes layer) unlocks precomputation, immutable GPU buffers, and
tiling — the same approach Xenium Explorer, deck.gl, and Mapbox use at this
scale. Built on pygfx/wgpu directly, not a higher-level mapping library — see
`work-notes/plan.md` for the design rationale and roadmap.

## Project layout

Properly installed package (`pyproject.toml`, hatchling; `pip install -e .`
into the `cytos` conda env), not a folder of scripts — `pip install cytos`
pulls in real dependencies and registers console-script commands (see
`cli.py`). Two of them are the product: `cytos-import` builds a `.cytos`
slide from a source dataset, `cytos-viewer` opens one. (`cytos-convert-
ome-zarr` is a standalone OME-TIFF → OME-Zarr utility the importer also
calls.)

`src/cytos/` splits the way napari splits `layers/`+`components/` (core model)
from `_vispy/` (rendering backend) from `_qt/` (UI): `core/` (pure data model,
no GPU/UI imports), `prep/` (offline preprocessing), `render/` (pygfx scene
construction), `remote/` (control socket, `cytos-ctl`, MCP — no UI imports),
`ui/` (Qt widgets only) — one-directional, each layer importable without the
ones "above" it. Three more places carry the outward face: `cli.py` at the
root (the one place listing every console script), `skills/` (the
shipped AI guides plus the `guide_text()` that serves them to `cytos-ctl
skill` and the MCP `usage_guide` tool), and `docs/` (what cytos does to
read each third-party data source, one `importing-<source>.md` per source —
see below).

`tools/make_synthetic_big_pyramid.py` and `tools/make_label_mask.py` stay
outside the package, deliberately: dev-only generators, no end user needs
them. Package vs. not is about what ships, not who runs it —
`cytos-viewer`/`cytos-convert-ome-zarr` are the real product despite living in
`src/`.

`data/` and `work-notes/` are both fully gitignored — see `.gitignore` and
each folder's own contents for what's there.

## Core data model — two primitives, not one per data source

- **An OME-Zarr image** — the morphology/background layer ("the figure").
- **A polygon set** — cell (and nucleus) segmentation boundaries: flat
  `coords (M,2) float32`, `offsets (N+1,) uint32`, `cell_id (N,) uint32`, plus
  an Arrow `features` table for per-cell attributes.

**Naming convention:** name loaders/converters after what they produce
(`polygons_from_parquet`, `polygons_from_labels`, `load_ome_zarr_image`), never
after the source platform (`load_xenium`) — today's data source is the first,
not the only one. The same rule keeps this guide vendor-free: what cytos
does to read a specific source — and the format facts that depends on —
lives in `src/cytos/docs/importing-<source>.md`, committed and shipped in
the package, so those facts never need re-finding; the source's own product
documentation is linked from there, not rewritten. The gitignored
`data/README.md` only records which datasets are on this machine.

A polygon set has **two input formats, and nothing downstream knows which one a
layer came from**: a long-format boundary table (`polygons_from_parquet`, one
row per vertex — most platforms' exports), or an OME-Zarr **label mask** whose
pixel values are object ids (`cytos.prep.labels.polygons_from_labels` — what
Cellpose, StarDist and friends produce). Both land as the same `Polygons`, and
`cytos.prep.segments` is the dispatcher that picks between them by looking at
the file. Nothing about a polygon set assumes the objects are cells, or that
they nest, or that one layer's objects relate to another's.

## The `.cytos` slide — the viewer's only entry point

`cytos-viewer` takes one slide directory and nothing else; `cytos-import`
builds one. A slide is a plain directory with a plain-JSON manifest
(`cytos.json`), not one big zarr hierarchy, so nothing at the top level is tied
to a storage format — zarr stays in the leaves, each named by a per-layer
`format` field. Three consequences to keep: world space (`world_bounds`,
`tile_depth`) is decided once at import and shared by every layer, so no layer
picks its own grid; the manifest carries each layer's tile index, so
visible-tile lookup is arithmetic on an in-memory set (0.004 ms) rather than
probing the store (~8 ms per camera move); and `tile()` returns numpy
dataclasses, so `cytos.render` never sees a storage object. That last one, not
the directory layout, is what keeps the tile format swappable. See
`src/cytos/core/slide.py`.

Each store in the leaves is **one zipped file** (`tiles.zarr.zip`) — a slide is
written once and only ever read, and 6 files copy between machines in a way
3,970 don't. Reads are as fast or faster. Directories still open; the `format`
tag says which, and `cytos-import --no-zip` writes them. See
`src/cytos/core/store.py`.

`cytos.json` holds each layer's *defaults*; `session.json` (same folder, written
on window close) holds only your overrides plus camera and window state. Two
files, so View > Reset to Slide Defaults can just drop the session and re-read
the manifest. See `src/cytos/core/session.py`.

The importer writes the manifest, but it is no longer the only thing that does:
`cytos.prep.segments.add_segments_to_slide` appends a segmentation layer to a
slide that already exists (File ▸ Add Segments… in the viewer, or
`python -m cytos.prep.segments <slide> <source>`). Two consequences that are
load-bearing. `write_manifest` writes via a temp file and `os.replace`, because
a slide can be open in a viewer — or two — while a layer is being added to it,
and a half-written `cytos.json` is a slide that no longer opens at all. And an
added layer must reuse the slide's **existing** `world_bounds` and `tile_depth`
rather than deriving its own; `cytos.prep.tiling.sort_and_tile` silently
*clips* out-of-bounds anchors into the edge of the grid, so unregistered
segmentation would otherwise import without complaint and draw as a smear along
one side. `check_bounds_fit_slide` is what refuses that, and it runs before the
expensive tracing step, not after it.

## Remote control — the viewer is scriptable, including by AI

The single-instance socket (how a second launch raises the first) doubles as
a JSON command channel: `cytos-ctl` sends one JSON line, the app answers with
one (`src/cytos/remote/ipc.py` is the wire, `_dispatch` in
`src/cytos/ui/main_window.py` is the complete command list). Three rules keep
it honest. Commands go **through the dock-panel widgets** — each
`WindowController` method (`src/cytos/ui/controller.py`) calls the existing
rows' `apply()`, whose signals reach the tile caches, so a remote command and
a mouse click are one code path and the panel never lies. The vocabulary is
the **saved-session vocabulary** — `state` returns what `collect_session`
writes, `set` takes a partial dict of the same shape, so the session file
format is the API and there is no second schema. And `describe` lists every
legal value (layer keys, colormaps, features, genes), so a caller — human or
AI — can always form a valid command instead of guessing; invalid values are
rejected with the legal list, never silently ignored (Qt combo boxes ignore
unknown `setCurrentText`, which is exactly the trap). `snapshot` renders
**offscreen, never via the window**: `render_offscreen` in `build_window` runs
the frame prep (tile loads are synchronous) and `renderer.render(...,
flush=False)`, skipping only the blit-to-canvas. Relying on a widget repaint
was tried first and returned stale frames — Qt won't paint a hidden or
occluded window, and back-to-back socket commands leave no time for the
render loop to catch up. Remote `open` takes a session name to skip the
picker dialog (`build_window(session_name=)`); it enforces the same
one-window-per-session rule the picker does.

`cytos-mcp` (`src/cytos/remote/mcp_server.py`, optional extra `cytos[mcp]`) serves
the same socket over MCP for AI clients with no shell; it is a pure adapter —
every tool is one socket command, and its `snapshot` returns the PNG as MCP
image content. It never starts the viewer.

The onboarding guide for AI assistants ships *inside the package*
(`src/cytos/skills/users.md`, printed by `cytos-ctl skill`, served by the
MCP `usage_guide` tool — CLI-only, because cytos is an app, not a library).
The README deliberately does not explain the
command surface; the guide does. When a command, field, or convention
changes, update the guide in the same change — it is the interface contract
an AI reads, and a stale one teaches wrong commands.

## Developing the panel

`cytos-ctl snapshot --panel` (MCP: `snapshot` with `panel=true`) captures
the dock via `QWidget.grab` — check UI changes with your own eyes; never
make the human referee pixels. Offscreen widget tests verify logic, not
looks. Keep the Fusion style (`_ensure_app`): native styles draw hidden
padding their reported metrics don't match, differently per OS. Compose
pixel-exact rows from plain widgets (the legend in `segment_panel.py`) —
composite widgets like QTreeWidget bury their checkbox geometry.

## Implementation gotchas (verified against a real slide)

Nothing here is about any one vendor's format — those facts live in
`src/cytos/docs/`, per vendor. These are code-level pitfalls hit while
building the tool, stated so the guards in the code don't look removable.

- **`ome_zarr.writer.write_image` cannot write a label mask.** It ignores the
  `coordinate_transformations` passed to it (writing unit scale and zero
  translation instead), and it builds a downsampled pyramid whatever `scaler`
  says — which for a label image *interpolates between object ids*, inventing
  objects nobody segmented. Both failures are silent; the first shows up as
  traced polygons landing in pixel coordinates, ~22x too big and in the wrong
  place. `tools/make_label_mask.py` writes the NGFF metadata by hand instead,
  which is all of ~20 lines because `cytos.core.image.pyramid_levels` only
  reads `multiscales[0].datasets[].coordinateTransformations`.
- **Tracing a label mask is per-object Python, so it has to be read in
  blocks.** The obvious loop — for each object, slice its bounding box out of
  the zarr — re-decompresses the same chunks hundreds of thousands of times.
  `cytos.prep.labels` instead reads 2048 px blocks once each and traces every
  object that falls inside one, leaving only the few that straddle a block
  edge to a read of their own. Measured on 167,780 objects over a 10964x15060
  mask: **85 s, 515 MB peak** — well under the raster itself (660 MB), which
  is the point of never holding the whole mask.
- **Marching-squares rings need simplifying, and land half a pixel out.**
  `find_contours` emits about one vertex per pixel step, so a 30 px cell
  arrives with ~120 vertices where a platform's own boundary table ships
  ~25 — vertex count is what the
  tile store, the GPU buffers and the earcut loop all scale with. Douglas-Peucker
  at 0.75 px is a default, not a polish step. The ring also sits on the outer
  pixel boundary, so a round trip (rasterize a segmentation, trace it back)
  returns areas ~2% larger; area centroids agree to 0.05 um, a quarter of a
  pixel, with no directional bias — that round trip is the check to re-run if
  the pixel→world conversion is ever touched.
- **Parquet silently drops Arrow's dictionary type**: write a
  `dictionary<int>` column, read back plain `int64` — parquet sees the
  dictionary as compression, not meaning. So a column's *type* cannot mark
  it as categorical across a save/load. Categorical columns (clusterings)
  carry the field metadata `cytos:categorical` instead, which parquet does
  keep (`cytos.core.polygons.is_categorical_feature`); everything
  downstream asks that marker, never a column-name pattern.
- `mapbox_earcut`'s nanobind binding requires **C-contiguous** `(*, 2)
  float32` arrays — `pandas.DataFrame.to_numpy()` on multiple columns returns
  Fortran-order, which fails with an unhelpful generic `TypeError`. Fix:
  `np.ascontiguousarray(...)` right after `to_numpy()`.
- Fluorescence channel data is **sparse with a heavy-tailed intensity
  distribution** (DAPI test channel: median 0, 99th pct 30, max 1194). Raw
  min/max `clim` crushes the image to near-black. Use percentile-based
  autocontrast (1st/99.5th pct), matching napari/Fiji.
- **World Y increases downward**, the same direction as pixel rows, as the
  source platforms' raw coordinates, and as every neighbouring tool (napari, QuPath, Fiji,
  XYZ map tiles). So nothing on the data path converts Y: image levels are a
  plain scale-and-offset (`PyramidLevel.row_to_world_y`), and polygon/point
  loaders read `vertex_y`/`y_location` straight through. World bounds are
  positive, and **tile row 0 is the top row** (`src/cytos/core/tiling.py`).
  pygfx renders +y upward, so exactly one flip is still needed and it lives on
  the camera — `camera.local.scale_y = -1` in `src/cytos/ui/main_window.py`.
  Put display conventions in the view, not in the data. Two things make that
  flip safe, and both were checked: `camera.width`/`height` stay positive (so
  the note below still applies unchanged), and `PanZoomController` derives its
  pan basis by unprojecting through the camera, so drag directions follow the
  mirror with no change to the controller.
- **`camera.width`/`camera.height` don't reflect what's actually visible on
  screen** when the viewport aspect differs from the camera's —
  `OrthographicCamera(maintain_aspect=True)` pads internally without updating
  those properties. Any world-space view rect derived from them directly is
  too narrow. Use `src/cytos/render/camera.py:effective_camera_view_size()`
  instead, everywhere a camera-driven view is needed.
- **A switched-off polygon fill still renders, at opacity 0.** The fill mesh
  doubles as the pick surface (`PolygonTileCache.pick_cell`); `visible=False`
  would make cell interiors un-hoverable whenever only outlines show. pygfx
  picks fully transparent meshes (verified). Related trap: an *opaque*
  object drawn above a transparent one depth-culls it out of the pick
  buffer — every layer above the polygon fill must keep
  `alpha_mode="blend"` (no depth write) or picking under it dies.
- **Sequential colormaps (matplotlib's `Blues`/`Greens`/`Reds`, vendored via
  `plotlet`) anchor at near-white, not black.** Additive-blended composite
  display (see the earlier fluorescence/autocontrast note) wants pixel value
  0 to render as black background; a white-anchored colormap washes the
  whole composite to gray instead. `src/cytos/render/image.py` registers its
  own black->hue set (`blue`, `green`, `red`, `cyan`, `magenta`, `yellow`) via
  `plotlet.register_colormap` at import time for this reason — don't default
  composite channels to a matplotlib sequential colormap without checking its
  0-end color first.
- **A single-channel OME-TIFF may have no channel axis at all.** Some
  pipelines write `SizeC=1` as a plain 2D plane (`axes="YX"`) where their
  multi-channel output is `axes="CYX"` (which versions do which:
  `src/cytos/docs/`). `series.asarray()[channel]` silently mis-indexes the
  2D case (grabs one *row*, not a channel plane) instead of raising.
  `src/cytos/prep/pyramid.py` checks `series.axes` and only indexes by
  channel when a channel axis actually exists.
- **A multi-file OME-TIFF's sibling pages arrive with closed file handles.**
  Newer morphology images are one OME-TIFF split across several files, one
  per stain (which layouts exist per version: `src/cytos/docs/`); tifffile
  assembles the whole `CYX` series from the first file, but parses the
  sibling files once and closes them — reading a page that lives in one
  later auto-reopens it with a `UserWarning`. `_read_plane` in
  `src/cytos/prep/pyramid.py` reopens the handle on purpose instead. Same
  path, two more lessons: read a channel via its *page* —
  `series.asarray()[channel]` decodes every whole-slide plane to keep one —
  and the decode needs `imagecodecs` (JPEG-2000), which is why that is a
  real dependency. Discovery must know old and new layouts both: a glob for
  only the old per-channel names finds nothing in a new bundle and imports
  an image-less slide without complaint.
- **A PySide6 QAction wrapper must stay referenced while you use what it
  returned.** `bar.actions()[0].menu()` dies with `Internal C++ object
  (QMenu) already deleted`: the QAction wrapper is a temporary, Python
  collects it as soon as `.menu()` returns, and shiboken invalidates the
  menu it handed out along with it. Hold the action in a variable for as
  long as the menu is in use (`act = bar.actions()[0]; menu = act.menu()`).
  Nothing in the app walks menus this way — this bites in *tests and
  probes* that inspect a built menu, where it looks exactly like a
  wrapper-lifetime bug in the menu-building code. It isn't; the same
  failure reproduces on any hand-built QMenuBar.

## Errors and warnings

Take every error and warning seriously, even harmless-looking ones — fix the
cause, or say why it stays. Console noise trains you to stop reading it. wgpu/Qt
teardown messages look like somebody else's problem and usually aren't (see
`_shutdown_gpu` in `src/cytos/ui/main_window.py`).

## Environment

Python setup (conda envs, interpreter paths) is in the gitignored
`CLAUDE.local.md`, not here — it's machine-specific, not general project
knowledge.
