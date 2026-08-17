# cytos Qt viewer — guide

The Qt/pygfx desktop viewer (`cytos-viewer`): how it is driven and the
pitfalls specific to it. Read alongside [`developers.md`](developers.md),
which holds the data model, the `.cytos` format and the web UI.

**This viewer is being replaced by the web UI** (`web/`). It is complete, at
real scale, and still the
reference for *behaviour* — when the two viewers disagree about what a
setting means, this one is right. But `render/` and `ui/` retire with it:
keep them working, don't grow them. Everything in this file goes away with
them, which is why it is a separate file.

Nothing here is about the data model, the `.cytos` format, or the import
pipeline — those are shared, and live in the developer guide.

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

**The three rules above are the part worth keeping.** The transport (unix
socket, offscreen pygfx render) is not; a web equivalent will look nothing
like it. Carry the rules over, not the mechanism.

## Running it

One process, no arguments — it opens a welcome window, and slides are opened
from the File menu or over the socket:

```
cytos-viewer
cytos-ctl open data/<slide>.cytos --session default
cytos-ctl camera --center 3600 1450 --width 400
cytos-ctl snapshot /tmp/view.png            # the scene, rendered offscreen
cytos-ctl snapshot --panel /tmp/panel.png   # the dock widget
```

## Developing the panel

`cytos-ctl snapshot --panel` (MCP: `snapshot` with `panel=true`) captures
the dock via `QWidget.grab` — check UI changes with your own eyes; never
make the human referee pixels. Offscreen widget tests verify logic, not
looks. Keep the Fusion style (`_ensure_app`): native styles draw hidden
padding their reported metrics don't match, differently per OS. Compose
pixel-exact rows from plain widgets (the legend in `segment_panel.py`) —
composite widgets like QTreeWidget bury their checkbox geometry.

## Gotchas (verified against a real slide)

- **The camera carries the y-flip.** World Y increases downward everywhere in
  cytos (see the developer guide), and pygfx renders +y upward — so exactly
  one flip is needed and it lives on the camera:
  `camera.local.scale_y = -1` in `src/cytos/ui/main_window.py`. Put display
  conventions in the view, not in the data. Two things make that flip safe,
  and both were checked: `camera.width`/`height` stay positive (so the note
  below applies unchanged), and `PanZoomController` derives its pan basis by
  unprojecting through the camera, so drag directions follow the mirror with
  no change to the controller.
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
  display (see the fluorescence/autocontrast note in the developer guide)
  wants pixel value 0 to render as black background; a white-anchored
  colormap washes the whole composite to gray instead.
  `src/cytos/render/image.py` registers its own black->hue set (`blue`,
  `green`, `red`, `cyan`, `magenta`, `yellow`) via `plotlet.register_colormap`
  at import time for this reason — don't default composite channels to a
  matplotlib sequential colormap without checking its 0-end color first.
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

wgpu/Qt teardown messages look like somebody else's problem and usually
aren't — see `_shutdown_gpu` in `src/cytos/ui/main_window.py`.
