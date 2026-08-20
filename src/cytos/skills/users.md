# cytos users guide

For AI assistants working with cytos — a viewer for spatial biology slides (a
big microscopy image with cell boundaries and transcript dots on top). This
tool is not in your training data; this guide is where the answers are.

Two halves. The **pip package** builds slides and defines the format:
`cytos-import` is its one real command. The **viewer** is a desktop app (and
the same page in a browser) built from `web/` — it only ever reads slides.
There is no command surface for driving it: it is a GUI, and you drive a GUI
the way a person does, by clicking it (see *Driving the app* below).

## Mental model

- A **slide** (`.cytos` directory) holds the data: image channels, segment
  (cell boundary) layers, transcript point layers. `cytos-import` builds one;
  nothing else writes into it except added layers.
- A **session** is a named saved view of a slide — camera plus every layer
  setting. Opening a slide means opening it *under a session*, chosen in the
  picker on the way in. One window holds one session, so two windows never
  write the same file. Sessions are written as you work, not on close.
- Sessions live in `<slide>/sessions/<slug>.json`, beside a `<slug>.png`
  thumbnail of the frame they were last saved at. A browser tab has no such
  folder and keeps them in its own IndexedDB instead.
- The **session file is the vocabulary**: everything the panel can change is
  a field in it, and there is no second schema. Reading one tells you exactly
  what state a view is in; editing one before the app opens it is a
  legitimate way to set up a view.

## Building a slide

If the user only has a raw Xenium output folder, there is no slide yet:

```
cytos-import <xenium output dir> --out <name>.cytos
```

Segmentation can be added to a slide that already exists, from a boundary
parquet or an OME-Zarr label mask:

```
python -m cytos.prep.segments <slide> <source>
```

Both take minutes on real data — a whole-slide import is not a quick check.

## Running the viewer

From `web/`:

```
npm run app:dev -- <slide dir>   # desktop shell, needs `npm run dev` alongside
npm run dev                      # plain page on :5173
```

A page needs slides served over HTTP (`python tools/serve_slides.py`, port
8787) and takes them as a URL parameter:

```
http://localhost:5173/?slide=http://127.0.0.1:8787/sample.cytos
```

Three parameters, and they are the only "commands" the viewer has:
`?slide=<url>`, `?session=<name>` (skips the picker, opens that session), and
`?view=x,y,zoom` in full-resolution image pixels.

## Driving the app

Use Playwright. `web/shot.mjs` opens a page, waits, screenshots it and prints
console errors, failed responses and tile statistics:

```
node shot.mjs '<url>' /tmp/web.png 7000
```

`app-shot.mjs` is the same for the desktop window, `smoke.mjs` checks the
data path with no browser at all. For anything interactive, write a short
Playwright script beside them: click the real controls, then screenshot and
**look at the image**. The render is the ground truth, never your assumption
about what a setting did. Headless Chromium falls back to software GL, which
is fine for "does it draw" and useless for judging speed — pass `--headed`
when timing anything.

## What the panel holds

Layers are keyed `image:<id>`, `segments:<id>`, `points:<id>` — the same keys
the session file uses. Per kind:

- **image** — `visible`, `colormap`, `clim`. A channel's colormap is a colour:
  a `"#rrggbb"` or a named hue ("blue", "green", …), rendered as a black→
  colour ramp. Ramps like viridis are not offered for channels.
- **segments** — `visible`, `show_outline`, `show_fill`, `fill_opacity`,
  `color_by`, `colormap`, and for a categorical `color_by` (a clustering):
  `palette`, `category_colors` (`{"cluster": {"7": "#e41a1c"}}`) and
  `hidden_categories` (`{"cluster": ["3", "unassigned"]}`). `color_by: null`
  is flat colour. A measurement takes a ramp; a clustering takes a
  qualitative palette, and its categories are listed under the row with a
  swatch, a checkbox and a cell count.
- **points** — `visible`, `genes` (dense ids, `null` for all), `gene_colors`,
  `size`, `opacity`, `color_mode` ("gene" or "flat"), `colormap`, `palette`.

Above them, three sections (`images`, `segments`, `points`) each have
`checked` — a master switch for that whole kind — and `expanded`. A slide
opens with segments and points switched off; if something you expected is
missing, check the section switch before suspecting the data.

## Reading the view

- The **navigator** at the top of the panel shows the whole slide with a
  rectangle for the camera; click or drag it to move.
- **Hovering** says what is under the cursor: a cell answers with its dataset
  id and the value currently colouring it (`cell odjkjhph-1 · cluster: 19`),
  a transcript with its gene name and, on an aggregate dot, how many
  transcripts it stands for. A hidden category does not answer.
- The **scale bar** and the line under the panel say how much ground a screen
  pixel covers, how many tiles have arrived, and how many failed.

## Coordinates

World units are the slide's own (`world_units` in `cytos.json`, usually
micrometers). **Y increases downward** — `world_bounds` is
`[minx, miny, maxx, maxy]` and `miny` is the *top* edge on screen. This
matches the data files, napari, QuPath and Fiji. The `?view=` parameter is in
full-resolution image pixels, not world units.

## Points at a distance

Zoomed out, a point layer draws *aggregated* dots — one dot standing for
several transcripts of a gene in a region, in that gene's colour, sized by
how many. Individual transcripts appear past about 0.5 µm per screen pixel.
Selecting genes narrows what loads at any zoom, and nothing is drawn until
some gene is selected.

## Politeness

Work in your own session — make one in the picker (New Session) rather than
opening `default`, which is the user's own view and gets overwritten as you
change things. Deleting `<slide>/sessions/<name>.json` and its `.png` removes
one cleanly. A slide's data is read-only; sessions are the only thing a
viewer writes.
