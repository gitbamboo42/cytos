# cytos

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image.

## Install

```
pip install -e .
```

## Usage

```
cytos-viewer
```

Convert a Xenium morphology OME-TIFF channel to OME-Zarr first, if needed:

```
cytos-convert-ome-zarr path/to/morphology.ome.tif --channel 0 --out out.ome.zarr
```

## Web viewer and desktop app

A second viewer lives in `web/` — the one the project is moving to. It is not
part of the pip install and needs Node. From a checkout:

```
cd web
npm install
npm run app -- ../data/some_slide.cytos   # desktop window, reads the disk
npm run make                              # builds release/mac-arm64/cytos.app
```

`cytos.app` is double-clickable and takes a slide path as an argument. It is
unsigned, which only matters for a build someone downloads — one you built
yourself opens without a warning.

To run it as a plain web page instead, `npm run dev` serves the page and
`python tools/serve_slides.py` serves the slides. Full notes, including how to
screenshot either one, are in `src/cytos/skills/developers.md`.

## Scripting the viewer, and AI assistants

A running viewer can be driven from outside — open a slide, move the
camera, change layer settings, screenshot the view. Tell your AI assistant
to run `cytos-ctl skill` and follow it: the full operating guide ships
inside the installed package, so it is always in reach and always current.

For AI clients that can't run shell commands (Claude Desktop, claude.ai),
the same control is available as an MCP server, where snapshots come back
as images the model sees directly:

```
pip install 'cytos[mcp]'
claude mcp add cytos -- cytos-mcp        # or your client's MCP config
```

Either way the viewer itself must already be running.
