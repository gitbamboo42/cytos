# cytos users guide

For AI assistants operating cytos — a desktop viewer for spatial biology
slides (a big microscopy image with cell boundaries and transcript dots on
top). You drive a running viewer over a local socket; the human sees the
same window move. This tool is not in your training data; this guide is
where the answers are.

This guide ships inside the package: `cytos-ctl skill` prints it. If the
user asks you to save it as a persistent/global skill, store this text in
your tool's skill format with the description: "Operating the cytos slide
viewer for spatial biology — driving the app with cytos-ctl/MCP, camera,
layers, snapshots."

## Mental model

- A **slide** (`.cytos` directory) holds the data: image channels, segment
  (cell boundary) layers, transcript point layers. `cytos-import` builds one
  from a Xenium output folder; the viewer only ever reads them.
- A **session** is a named saved view of a slide (camera + layer settings).
  Opening a slide means opening it *under a session*. One window per session,
  enforced. Sessions are saved on window close; they are cheap and personal.
- The **viewer** (`cytos-viewer`) is a GUI app the user starts. You talk to
  the running app; you cannot start it (if nothing answers, ask the user to
  launch `cytos-viewer`).
- Two equivalent ways in, never mix-and-match knowledge between projects:
  `cytos-ctl` (a CLI for anything with a shell — every reply is JSON on
  stdout) and `cytos-mcp` (the same commands as MCP tools, for clients
  without a shell; its `snapshot` returns the image directly).

## The loop

Almost every task is this sequence:

```
cytos-ctl status                          # running? how many windows?
cytos-ctl open <slide.cytos> --session <name>
cytos-ctl describe                        # THE discovery step — see below
cytos-ctl camera --center X Y --width W   # or --fit, --rect, --height
cytos-ctl set '<partial state JSON>'      # layer styling, visibility
cytos-ctl snapshot out.png                # then actually look at it
```

`describe` returns every layer's key (`image:dapi`, `segments:cell`,
`points:transcripts`, …), its current state, and **every legal value for
every settable field** — colormap names, `color_by` feature names, gene
names, palettes. Never guess a value: read it from `describe`. Wrong values
are rejected with the legal list in the error message.

## Setting state

`set` takes a *partial* dict shaped exactly like a saved session:

```json
{
  "camera":   {"center": [3800, 2600], "width": 500},
  "sections": {"points": {"checked": true}},
  "layers": {
    "image:dapi":         {"colormap": "green", "clim": [0, 40], "visible": false},
    "segments:cell":      {"show_fill": true, "fill_opacity": 0.5,
                           "colormap": "magma", "color_by": "transcript_counts"},
    "points:transcripts": {"genes": ["ACTA2", "ACKR1"], "size": 6}
  }
}
```

Only the fields you give change. Notes that will save you a retry:

- `color_by: null` means flat color; the feature names come from `describe`.
  Categorical features (clusterings like `cluster`, `kmeans_5`) draw with a
  qualitative palette, not the `colormap`: pick it with `palette`, recolor
  single categories with `category_colors` (e.g.
  `{"cluster": {"7": "#e41a1c"}}`), and hide some entirely with
  `hidden_categories` (e.g. `{"cluster": ["3", "unassigned"]}`) —
  `describe` lists each layer's categorical features and their legal
  category keys under `categories`.
- `genes` accepts names or ids; `null` means all genes. Point layers have no
  per-layer `visible` — turn the whole `points` section on/off instead
  (`"sections": {"points": {"checked": true}}`).
- A snapshot showing less than you expected is usually state, not a bug:
  read `state` (sections checked? genes selected? layer visible?) before
  concluding anything about the data.
- Zoomed out, a point layer draws *aggregated* dots — one dot per gene per
  region, sitting on one of that gene's real transcripts there, in that
  gene's color, sized by how many it stands for. A gene selection narrows
  which genes' dots load, at any zoom; individual transcripts appear when
  you zoom in past about 0.5 µm per screen pixel.
- Camera: `width` alone keeps the aspect ratio ("show 500 µm across").
  `{"fit": true}` shows the whole slide.
- `state` reads the current values back in the same shape — you can edit its
  output and pass it straight back to `set`; `reset` returns to the slide's
  own defaults.
- `describe`'s camera also carries a `view_rect`: the world rectangle
  actually on screen, which is wider than `size` when the window's shape
  differs from the camera's. It is read-only — computed, not settable.

## Coordinates

World units are the slide's own (see `world_units` in `describe`, usually
micrometers). **Y increases downward** — `world_bounds` is
`[minx, miny, maxx, maxy]` and `miny` is the *top* edge on screen. This
matches the data files, napari, QuPath, and Fiji.

## Snapshots

`snapshot` renders the current state fresh (it never returns a stale frame,
even if the window is hidden) and writes a PNG; over MCP it returns the
image inline. Take one after any change you care about and *look at it* —
the render is the ground truth, not the state JSON. `snapshot --panel`
captures the dock panel (the controls) instead of the slide — for checking
UI work with your own eyes, not for looking at data.

## Picking a cell

`cytos-ctl pick X Y` (MCP: `pick`) answers "what is at this world point".
A cell answers with its segment layer key, index, and whole per-cell
feature row; a transcript dot answers with its gene (and, zoomed out, the
`count` of transcripts the aggregate dot stands for). The point must be
inside the current view — picking reads the rendered frame, so move the
camera first. What you see is what picks: a shown segment layer answers
anywhere inside a cell, fill or no fill; hidden layers, categories, and
unselected genes return `hit: false`; where things overlap, the topmost
drawn one answers (a dot over a cell, a nucleus over a cell). (In the app,
the same answer follows the mouse in the status bar.)

## Sessions and politeness

Opening under `--session default` will overwrite the user's default view
when the window closes. Unless the user says otherwise, work in your own
session (e.g. `--session ai-scratch`); `cytos-ctl sessions <slide>` lists
what exists (works without the viewer running). Deleting
`<slide>/sessions/<name>.json` and `.png` removes one cleanly.

## When several windows are open

`cytos-ctl windows` lists them with ids; pass `-w <id>` (or `"window": <id>`
over MCP) to target one. With exactly one window open, targeting is
automatic.

## Building a slide

If the user only has a raw Xenium output folder, there is no slide yet:

```
cytos-import <xenium output dir> --out <name>.cytos
```

Segmentation can also be added to an existing slide from a boundary parquet
or an OME-Zarr label mask: `python -m cytos.prep.segments <slide> <source>`
(the viewer's File ▸ Add Segments… does the same).
