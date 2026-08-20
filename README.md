# cytos

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image.

Two halves, one format between them. The **viewer** is a desktop app (and the
same page in a browser) built from `web/`. The **pip package** is the
pipeline: it turns a source dataset into a `.cytos` slide and defines what
that slide is. Nothing in the package draws anything.

## Building slides

```
pip install -e .
cytos-import path/to/xenium_output --out sample.cytos
```

Convert a morphology OME-TIFF channel to OME-Zarr first, if needed:

```
cytos-convert-ome-zarr path/to/morphology.ome.tif --channel 0 --out out.ome.zarr
```

## The viewer

The app lives in `web/`. It is not part of the pip install and needs Node.
From a checkout:

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

## AI assistants

The guides ship inside the package as plain markdown, in
`src/cytos/skills/`: `users.md` for operating the viewer, `developers.md`
for working on the code. Point your assistant at them.

The web viewer is driven with Playwright — `web/shot.mjs` opens a page,
clicks the real controls and screenshots the result, which is how a change
gets checked without anyone refereeing pixels by hand.
