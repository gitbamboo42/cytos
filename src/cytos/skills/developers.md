# cytos developer guide

AI-oriented onboarding for working on the cytos codebase. Vendor-neutral —
symlink it as `CLAUDE.md`, `.cursorrules`, `AGENTS.md`, or whatever your tool
expects; humans can read it directly. It ships inside the
package: `cytos-ctl skill developer` prints it. Two siblings: `users.md` for
*operating* the viewer (`cytos-ctl skill`), and [`developers_qt.md`](developers_qt.md)
for the Qt desktop viewer the web UI is replacing.

## What cytos is

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image. Dropping editability (unlike
napari's Shapes layer) unlocks precomputation, immutable GPU buffers, and
tiling — the same approach Xenium Explorer, deck.gl, and Mapbox use at this
scale. See `work-notes/plan.md` for the design rationale and roadmap.

The UI is a web app (`web/`: React + deck.gl + viv) reading slides over
plain HTTP. A Qt desktop viewer still exists and is still the reference for
*behaviour* — when the two disagree about what a setting means, Qt is right —
but it is being replaced, and `render/` + `ui/` retire with it, so new UI work
goes in `web/` (see `developers_qt.md` before touching either). Python keeps
the pipeline: `prep/` needs tifffile, imagecodecs, scikit-image, earcut and
pyarrow, none of which belongs in a browser, and `core/` stays with it because
it defines the format `prep/` writes. **The seam between the two languages is
the `.cytos` format, not an API.**

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

`web/` is a separate vite + React + TypeScript app with its own
`package.json` and `node_modules` (gitignored); nothing in `src/` imports it,
and `pip install cytos` does not carry it. It carries the same one-directional
rule, as directories: `core/` (model) ← `io/` (readers) ← `render/` (deck.gl
layers) ← `ui/` (React). Check direction before adding an import.

`tools/` stays outside the package, deliberately: `make_synthetic_big_pyramid.py`
and `make_label_mask.py` are dev-only generators, `serve_slides.py` is the dev
file server for the web viewer, and no end user needs any of them. Package
vs. not is about what ships, not who runs it —
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

## The `.cytos` slide — the one thing both viewers read

A viewer takes one slide directory and nothing else; `cytos-import`
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
`src/cytos/core/store.py`. Both readers take either form — the web one reads
a zip over HTTP ranges (`web/src/io/zip.ts`), so no slide needs a second
`--no-zip` copy. That works because the zip **compresses nothing**: every
entry is STORED, since zarr already compressed each chunk, so an entry's
bytes *are* the chunk and there is no inflate step. Opening costs two small
reads (end-of-central-directory, then the directory); after that a chunk is
one ranged GET, the same as a directory would cost. `ReadRange` in
`web/src/io/read.ts` is the one place bytes enter the web app — an Electron
local-file reader lands there and nowhere else.

`cytos.json` holds each layer's *defaults*; `session.json` (same folder, written
on window close) holds only your overrides plus camera and window state. Two
files, so View > Reset to Slide Defaults can just drop the session and re-read
the manifest. See `src/cytos/core/session.py`. The web viewer reads manifest
defaults and does not write sessions yet.

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

## Running the web viewer

**Never make the human referee pixels** — the viewer screenshots itself, so
look before reporting that something works. Two processes plus a browser:

```
python tools/serve_slides.py                   # serves data/ on :8787
cd web && npm run dev                          # serves the page on :5173
node shot.mjs 'http://localhost:5173/?slide=http://127.0.0.1:8787/<slide>.cytos' \
     /tmp/web.png 7000
```

`serve_slides.py` adds CORS, single-`Range` support and HTTP/1.1 keep-alive
with Nagle off — without the last two, a polygon tile's ~4 small requests cost
~65 ms each instead of ~3 ms. `shot.mjs` screenshots the page and prints
console errors, failed responses and `window.__tileStats`; pass `--headed`
when judging *speed* (headless uses software GL, ~50x slower to tessellate).
`smoke.mjs` checks the data path with no browser; `npm run build` runs
`tsc --noEmit` first, so it is also the typecheck.
`shot.mjs` catches the panel along with the scene — check UI changes with your
own eyes. The native `<select>` popup is drawn by the OS at system font size
and ignores page CSS, so `ui/controls.tsx` ships its own `Dropdown`: if a
control's geometry isn't yours, you can't line it up.

Not built yet: points/transcripts, session save/load, the minimap, and adding
a channel or segmentation to an open slide.

## The cross-language contract

These live in both languages, each carrying a "same as X in Python" comment.
Those comments *are* the contract and no CI enforces them — **change one
side, change the other in the same commit.**

| shared fact | Python | TypeScript |
|---|---|---|
| slide format version | `CYTOS_FORMAT`, `core/slide.py` | `core/manifest.ts` |
| tile world size | `tile_world_size`, `core/tiling.py` | `core/manifest.ts` |
| session vocabulary | `core/session.py` | `core/session.ts` |
| channel hues, color presets | `render/image.py` | `core/colormaps.ts` |
| non-measurement columns | `feature_names`, `core/polygons.py` | `io/features.ts` |
| categorical marker | `core/polygons.py` | `io/features.ts` |
| feature ramp domain (2nd/98th pct) | `render/polygons.py` | `io/features.ts` |
| autocontrast (1st/99.5th pct) | `prep/slide.py` | `render/image.ts` |

The session vocabulary is the load-bearing row: `core/session.ts` is written in
the field names `collect_session` produces, so a future save/load — or a remote
command — is a plain merge, not a translation.

## Scriptable, including by AI

The Qt viewer is driven over a JSON socket (`developers_qt.md`). The
transport won't survive the move to web; three rules should, and are worth
re-reading before designing a replacement: remote command and mouse click go
through **one code path**, so the panel can never lie; the **session file
format is the API**; `describe` **lists every legal value**.

The onboarding guide for AI assistants ships *inside the package*
(`src/cytos/skills/users.md`, printed by `cytos-ctl skill`; the README
deliberately does not explain the command surface). When a command, field or
convention changes, update it in the same change — it is the interface
contract an AI reads, and a stale one teaches wrong commands.

## Implementation gotchas (verified against a real slide)

Nothing here is about any one vendor's format — those live in
`src/cytos/docs/`, per vendor. These are code-level pitfalls hit while
building the tool, stated so the guards in the code don't look removable; the
pygfx/Qt ones are in `developers_qt.md`.

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
  Put display conventions in the view, not in the data — deck's
  `OrthographicView` already points y down, so the web viewer needs no flip
  at all; the Qt one carries a single flip on its camera (`developers_qt.md`).
- **A polygon fill switched off still renders, at opacity 0** in both
  renderers — it doubles as the pick surface, so hiding it would make cell
  interiors un-hoverable whenever only outlines show.
- **deck.gl regenerates tiles on data identity, not closure identity.** Build
  each tile's binary arrays in `getTileData`, never in `renderSubLayers` — the
  latter runs on every update for every visible tile, so fresh objects there
  make deck re-tessellate every loaded tile whenever a new one arrives
  (quadratic, and it showed). Conversely a new closure alone changes nothing,
  so any setting that alters a tile's appearance must be in `updateTriggers`.
  Our tile grid *is* deck's: `minZoom`/`maxZoom` at 0 with `tileSize` = one
  tile's world size makes deck's `(x, y)` exactly our `(col, row)`.
- **viv takes one flat RGB per channel** (`ColorPaletteExtension`), so a ramp
  colormap on a web image channel silently renders as its top color; offering
  ramps there needs a LUT in the shader.
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

## Errors and warnings

Take every error and warning seriously, even harmless-looking ones — fix the
cause, or say why it stays. Console noise trains you to stop reading it.
`shot.mjs` prints console errors, page errors and every response over 400 for
exactly this reason — a 404 on a tile chunk is a finding, not noise.

## Environment

Python setup (conda envs, interpreter paths) is in the gitignored
`CLAUDE.local.md` — machine-specific, not project knowledge. `web/` needs
Node and a plain `npm install`.
