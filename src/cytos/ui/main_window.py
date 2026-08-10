"""cytos's live viewer. Opens `.cytos` slides and nothing else — a slide's
manifest says which layers exist, how they're colored, and what world space
they share (see `cytos.core.slide`; `cytos-import` builds one).

There is exactly one way in: File > Open Slide…. The app starts as an empty
window offering that menu, rather than taking a path on the command line, so a
slide can never arrive by a route that skips what that dialog does.
Reassembling a dataset from loose paths, the way this used to work, meant
telling the viewer every time what the data already knew — and left each layer
free to sit on its own world grid.

One process, too: a slide's session is owned by a single window at a time, and
that can only be enforced among windows this process can see. Launching again
brings the running app to the front instead of starting a second one.

Several OME-Zarr pyramids are composited together, each mapped through its own
colormap (cytos.render.image's TileCache(colormap=...)) and additively blended
— the standard multi-channel fluorescence display model (Fiji "composite" mode,
napari additive blending). All channels share one camera/level-selection loop,
since they're assumed spatially registered.

The dock panel groups by layer kind (Images, Segments, Points — see
cytos.ui.collapsible_section), each section independently expandable, each
holding one row per layer in the slide:

- Images (cytos.ui.channel_panel): colormap picker, visibility, contrast.
- Segments (cytos.ui.segment_panel): outline/fill toggles, fill opacity, and a
  colormap spread over a per-cell measurement — so cells are colored by their
  own data rather than by a fixed color.
- Points (cytos.ui.points_panel): transcript locations, one hue per gene, with
  a checkable gene list so a few genes show at once in clearly different
  colors — the usual way these are read.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-viewer
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

import numpy as np
import pygfx as gfx
from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

# Imported for its side effect: selects the Qt backend for rendercanvas, which
# `cytos.ui.canvas_input`'s render widget is built on. The loop object itself
# is unused -- see main() for why this app runs Qt's loop directly.
import rendercanvas.qt  # noqa: F401

from cytos.core.slide import SLIDE_SUFFIX, load_slide
from cytos.core.image import load_pyramid_levels, select_level
from cytos.core.session import load_session, save_session
from cytos.core.points import load_point_tile_grid
from cytos.core.polygons import load_polygon_tile_grid, numeric_feature_names
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import COMPOSITE_COLORMAPS, TileCache
from cytos.render.points import PointTileCache
from cytos.render.polygons import PolygonTileCache
from cytos.ui.channel_panel import Channel, ChannelRow
from cytos.ui.collapsible_section import CollapsibleSection
from cytos.ui.minimap import MinimapWidget, make_composite_thumbnail
from cytos.ui.scale_bar import ScaleBarWidget, unit_abbrev
from cytos.ui.points_panel import PointsRow
from cytos.ui.segment_panel import SegmentRow
from cytos.ui.session_picker import choose_session
from cytos.ui.canvas_input import CanvasRenderWidget
from cytos.ui.import_window import ImportPanel


# Which fields of each layer kind a session may override. The manifest holds
# the default for every one of them, which is what makes "reset" mean "drop the
# session and re-read the manifest" rather than a second list of magic values.
_STATE_FIELDS = {
    "image": ("visible", "colormap", "clim"),
    "segments": ("visible", "colormap", "color_by", "show_outline", "show_fill", "fill_opacity"),
    "points": ("visible", "color_mode", "palette", "colormap", "size", "opacity"),
}


def _iter_layers(slide):
    for kind, layers in (("image", slide.images), ("segments", slide.segments), ("points", slide.points)):
        for layer in layers:
            yield f"{kind}:{layer.id}", kind, layer


def _capture(layer, kind: str) -> dict:
    return {field: getattr(layer, field) for field in _STATE_FIELDS[kind]}


def _restore(layer, kind: str, state: dict) -> None:
    for field in _STATE_FIELDS[kind]:
        if field in state:
            setattr(layer, field, state[field])


# Every open window lives here. Qt does not own a top-level window on Python's
# behalf: one with no remaining Python reference is garbage collected and
# disappears mid-session, which is exactly what happens to a window opened from
# a menu handler whose locals then go out of scope.
_OPEN_WINDOWS: list["_MainWindow"] = []

# Width of the layer panel's contents — see where it is applied for why it is
# pinned rather than merely a minimum.
_DOCK_WIDTH = 340

# The welcome window needs holding for the same reason, and it was previously
# dropped on the floor: its only reference was the return value of the call
# that made it. A collected window takes its menu bar with it, which on macOS
# is the *global* menu bar.
_WELCOME_WINDOW = None

# macOS shows one menu bar for the whole app, sourced from the active window.
# Minimize every window and there is no active window, so that bar empties out
# and there's no way to open anything. A QMenuBar with no parent is Qt's
# answer: it becomes the default menu bar, used whenever no window supplies
# one. Held module-level because it, too, has no parent to own it.
_APP_MENU_BAR = None


class _MainWindow(QtWidgets.QMainWindow):
    """QMainWindow that writes its session on close, via `save_now`. Saving on
    close rather than on every widget change keeps the file quiet and keeps
    "reset to defaults" from racing an autosave. It is also called from outside
    -- see `build_window` -- whenever someone needs the file on disk to match
    what this window is showing."""

    def __init__(self):
        super().__init__()
        self.save_now = None
        self.slide_root = None
        # Every window is bound to exactly one session, and no two windows to
        # the same one -- see cytos.ui.session_picker.
        self.session_name = None
        self.max_tiles = 64

    def closeEvent(self, event):  # noqa: N802 - Qt's own naming
        if self.save_now is not None:
            self.save_now()
        if self in _OPEN_WINDOWS:
            _OPEN_WINDOWS.remove(self)

        # Closing the last slide returns to the welcome window rather than
        # quitting: there's no path on the command line any more, so quitting
        # would mean relaunching just to look at a different slide. Closing
        # the welcome window itself does quit, so "close windows until they're
        # gone" still ends the app.
        #
        # Built here, synchronously, so the visible-window count never reaches
        # zero -- Qt quits the application the moment it does.
        if not _OPEN_WINDOWS:
            build_welcome_window(self.max_tiles)

        super().closeEvent(event)


def _default_channel_name(path: str) -> str:
    """Store name with the OME-Zarr suffix stripped, e.g. 'dapi.ome.zarr' ->
    'dapi' — used when a channel is opened via the file dialog instead of
    coming from the slide manifest."""
    name = Path(path).name
    for suffix in (".ome.zarr.zip", ".zarr.zip", ".ome.zarr", ".zarr"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def prompt_open_slide(parent, max_tiles: int) -> bool:
    """Ask for a slide and open each pick in its own window. Returns whether
    anything opened.

    The one way into the app: startup calls this with no parent, File > Open
    Slide… calls it from a window. Same dialog, same code path, so a slide
    can't arrive by a route that skips whatever this does.
    """
    dialog = QtWidgets.QFileDialog(parent, "Open .cytos slide")
    dialog.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
    dialog.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
    dialog.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
    if not dialog.exec():
        return False

    opened = False
    for path in dialog.selectedFiles():
        try:
            # None means the session picker was cancelled -- a deliberate
            # "actually, no", not a failure to report.
            if build_window(Path(path), max_tiles, parent=parent) is not None:
                opened = True
        except (ValueError, KeyError, OSError) as err:
            QtWidgets.QMessageBox.warning(parent, "Could not open slide", str(err))
    return opened


def _place_window(win: QtWidgets.QWidget) -> None:
    """Centre a window on its screen, nudged down-right per window already open.

    Qt puts an unpositioned window wherever the platform feels like, which on
    macOS is the top-left corner every time -- so a second window lands exactly
    on the first. Centring is the expected place for a window nobody has moved
    yet, and the cascade keeps a stack of them individually reachable.
    """
    screen = win.screen() or QtWidgets.QApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    # Wraps rather than marching off the screen when many are open.
    step = 30 * (len(_OPEN_WINDOWS) % 6)
    x = area.x() + max(0, (area.width() - win.width()) // 2) + step
    y = area.y() + max(0, (area.height() - win.height()) // 3) + step
    win.move(x, y)


def _version() -> str:
    """The installed package version, or a placeholder when cytos is being run
    from a checkout that was never installed."""
    try:
        return metadata.version("cytos")
    except metadata.PackageNotFoundError:
        return "dev"


def prompt_new_slide(parent, max_tiles: int) -> bool:
    """Build a slide from a source dataset, in a window that becomes the viewer
    once it's built. Returns whether an import started.

    The window exists from the moment you ask for the slide, not from the moment
    the slide is ready: an import is minutes of work, and a viewer that shows
    nothing until it finishes leaves you unable to tell progress from a hang.
    So this is the same `_MainWindow` the slide will end up in -- it just spends
    its first minutes showing the importer's log (see `cytos.ui.import_window`).
    """
    # Non-native, matching prompt_open_slide: the two pickers sit in one menu,
    # and Qt's own dialog is the one that reliably selects a folder.
    picker = QtWidgets.QFileDialog(parent, "Choose a Xenium output folder")
    picker.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
    picker.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
    picker.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
    if not picker.exec() or not picker.selectedFiles():
        return False
    source_path = Path(picker.selectedFiles()[0])

    saver = QtWidgets.QFileDialog(parent, "Save the new slide as")
    saver.setAcceptMode(QtWidgets.QFileDialog.AcceptMode.AcceptSave)
    saver.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
    saver.setDirectory(str(source_path.parent))
    saver.selectFile(f"{source_path.name}{SLIDE_SUFFIX}")
    if not saver.exec() or not saver.selectedFiles():
        return False
    out_path = Path(saver.selectedFiles()[0])

    QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = _MainWindow()
    win.max_tiles = max_tiles
    # The title it will keep once it's a viewer, so the window doesn't rename
    # itself out from under you halfway through.
    win.setWindowTitle(f"cytos — {out_path.stem}")
    panel = ImportPanel(source_path, out_path, parent=win)
    win.setCentralWidget(panel)
    file_menu = win.menuBar().addMenu("File")
    file_menu.addAction("Open Slide…").triggered.connect(lambda: prompt_open_slide(win, max_tiles))

    def become_viewer() -> None:
        try:
            build_window(out_path, max_tiles, parent=win, win=win)
        except (ValueError, KeyError, OSError) as err:
            QtWidgets.QMessageBox.warning(win, "Could not open the new slide", str(err))

    def on_finished(ok: bool) -> None:
        if not ok:
            return  # panel keeps the log on screen; closing the window is the way out
        # Not inline: taking over the window deletes the panel, and doing that
        # while the panel's own signal is still being delivered means the rest
        # of the emit runs on a destroyed object. A zero timer lets the signal
        # finish first.
        QtCore.QTimer.singleShot(0, become_viewer)

    panel.finished.connect(on_finished)
    # After the menu bar exists, since that is part of what the window has to fit.
    panel.adjust_window()
    _place_window(win)
    win.show()
    # In _OPEN_WINDOWS from the start: it is a slide window that happens to
    # still be loading, so the "last window closed" bookkeeping needs it, and
    # nothing else holds a reference to keep it alive.
    _OPEN_WINDOWS.append(win)
    panel.start()
    return True


def build_welcome_window(max_tiles: int) -> QtWidgets.QMainWindow:
    """The window the app starts in: no slide, just the menu that opens one.

    Starting with a file dialog already up puts a modal in front of someone who
    hasn't asked for anything yet. An empty window with a visible File menu
    says the same thing without blocking, and it's where napari, Fiji and most
    viewers start too. It closes itself once a slide is actually open.
    """
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("cytos")
    win.resize(760, 480)
    _place_window(win)

    def on_open():
        if prompt_open_slide(win, max_tiles):
            win.close()

    def on_new():
        if prompt_new_slide(win, max_tiles):
            win.close()

    # What the program is, and nothing else. The File menu is where you act;
    # repeating it as buttons puts the same two commands on screen twice.
    page = QtWidgets.QWidget()
    page.setStyleSheet("QWidget { background: #141414; }")
    page_layout = QtWidgets.QVBoxLayout(page)
    page_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    page_layout.setSpacing(6)

    title = QtWidgets.QLabel("cytos")
    title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    title.setStyleSheet("color: #e8e8e8; font-size: 34px;")
    page_layout.addWidget(title)

    subtitle = QtWidgets.QLabel(f"A fast viewer for spatial biology slides — version {_version()}")
    subtitle.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    subtitle.setStyleSheet("color: #8a8a8a; font-size: 13px;")
    page_layout.addWidget(subtitle)

    page_layout.addSpacing(22)
    # Where to go next, named but not spelled out: which source formats exist is
    # the next dialog's business, not this screen's.
    hint = QtWidgets.QLabel("File ▸ Open Slide…\nFile ▸ New Slide from…")
    # Text left-aligned inside a block that is itself centred: centring each
    # line separately staggers them, so the two "File"s wouldn't start at the
    # same place.
    hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
    hint.setStyleSheet("color: #6f6f6f; font-size: 13px;")
    page_layout.addWidget(hint, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
    win.setCentralWidget(page)

    file_menu = win.menuBar().addMenu("File")
    open_action = file_menu.addAction("Open Slide…")
    open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
    open_action.triggered.connect(on_open)
    # The other half of "I have no slide": this window is exactly where someone
    # who has only a Xenium folder ends up, and offering only Open makes it a
    # dead end for them.
    file_menu.addMenu("New Slide from").addAction("Xenium Output…").triggered.connect(on_new)

    win.show()
    global _WELCOME_WINDOW
    _WELCOME_WINDOW = win
    return win


def build_app_menu_bar(max_tiles: int) -> QtWidgets.QMenuBar:
    """The parentless menu bar macOS falls back to when no window is active —
    with every window minimized, this is the only menu on screen."""
    global _APP_MENU_BAR
    if _APP_MENU_BAR is not None:
        return _APP_MENU_BAR

    bar = QtWidgets.QMenuBar()  # deliberately no parent
    file_menu = bar.addMenu("File")
    action = file_menu.addAction("Open Slide…")
    action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
    action.triggered.connect(lambda: prompt_open_slide(None, max_tiles))
    file_menu.addMenu("New Slide from").addAction("Xenium Output…").triggered.connect(
        lambda: prompt_new_slide(None, max_tiles)
    )
    _APP_MENU_BAR = bar
    return bar


def build_window(
    slide_path: Path, max_tiles: int = 64, parent=None, win: "_MainWindow | None" = None
) -> _MainWindow | None:
    """Open one slide in its own window and return it, already shown. Returns
    None if the session picker was cancelled.

    Every window is fully independent — own scene, camera, tile caches, dock,
    and its own session — so opening a second slide, or a second view of the
    same slide, never disturbs the first.
    """
    slide = load_slide(slide_path)
    print(
        f"slide '{slide.name}': {len(slide.images)} image(s), "
        f"{len(slide.segments)} segment layer(s), {len(slide.points)} point layer(s)"
    )

    # Anything already open on this slide is written out before the picker
    # appears. Otherwise a session's preview is the frame from when its window
    # was last closed, which for a window still open is the one view you can be
    # sure is out of date. Only this slide's windows: no others are listed.
    for open_window in _OPEN_WINDOWS:
        if open_window.slide_root == slide.root and open_window.save_now is not None:
            open_window.save_now()

    # Which sessions of this slide are spoken for. One window per session, so
    # two windows never write the same file -- the picker greys these out.
    in_use = {
        w.session_name for w in _OPEN_WINDOWS if w.slide_root == slide.root and w.session_name
    }
    session_name = choose_session(slide.root, slide.name, in_use, parent)
    if session_name is None:
        print(f"slide '{slide.name}': no session chosen, not opening")
        return None

    # The manifest's values are the defaults; the session overrides them.
    # Snapshot the defaults *before* overriding, so "reset" has something real
    # to go back to (see cytos.core.session).
    session = load_session(slide.root, session_name)
    saved_layers = session.get("layers", {})
    defaults = {}
    for key, kind, layer in _iter_layers(slide):
        defaults[key] = _capture(layer, kind)
        if key in saved_layers:
            _restore(layer, kind, saved_layers[key])
    print(f"session '{session_name}'" + ("" if session else " (new)"))

    scene = gfx.Scene()
    # An empty pygfx scene renders fully *transparent* black (alpha=0), not
    # opaque black -- with every layer hidden that let the Qt panel's own
    # background color show through the canvas instead of true black.
    scene.add(gfx.Background(None, gfx.BackgroundMaterial("#000000")))

    # The slide's own world bounds, not a union recomputed from whatever
    # happens to be loaded: every layer was placed against these at import.
    minx, miny, maxx, maxy = slide.world_bounds

    channels: list[Channel] = []

    def build_channel(path, colormap: str, name: str, clim=None) -> Channel:
        # visibility/minimap compositing are keyed by name (see below) — a
        # collision (e.g. opening the same folder twice via the multi-select
        # file dialog) would silently link two otherwise-independent
        # channels' minimap visibility, so dedupe before that key exists.
        existing = {c.name for c in channels}
        unique_name = name
        suffix = 2
        while unique_name in existing:
            unique_name = f"{name} ({suffix})"
            suffix += 1
        name = unique_name

        levels = load_pyramid_levels(Path(path))
        if clim is None:
            # Only for channels opened loose via File > Open — a slide's
            # clim was measured at import and lives in the manifest.
            coarsest = np.asarray(levels[-1].data)
            clim = tuple(float(v) for v in np.percentile(coarsest, [1, 99.5]))
        cache = TileCache(levels, clim=tuple(clim), max_tiles=max_tiles, colormap=colormap)
        scene.add(cache.group)
        print(f"channel '{name}': {path} colormap={colormap} clim={tuple(round(float(v), 1) for v in clim)}")
        return Channel(name=name, colormap=colormap, levels=levels, cache=cache)

    # (layer, channel) per image in the slide — File > Open adds channels to
    # `channels` that have no slide layer behind them, so they aren't here.
    image_layers = []
    for layer in slide.images:
        ch = build_channel(layer.path, layer.colormap, layer.id, layer.clim)
        channels.append(ch)
        image_layers.append((layer, ch))

    # (layer, grid, cache, numeric feature names) per segment layer in the slide.
    segment_layers = []
    for layer in slide.segments:
        grid = load_polygon_tile_grid(layer, slide.world_bounds)
        feature_names = numeric_feature_names(grid.features)
        # The manifest's choice, unless the feature table can't back it.
        color_by = layer.color_by if layer.color_by in feature_names else None
        cache = PolygonTileCache(
            grid,
            max_tiles=max_tiles,
            colormap=layer.colormap,
            color_by=color_by,
            show_outline=layer.show_outline,
            show_fill=layer.show_fill,
            fill_opacity=layer.fill_opacity,
        )
        cache.group.visible = layer.visible
        scene.add(cache.group)
        segment_layers.append((layer, grid, cache, feature_names))
        print(
            f"segments '{layer.id}': {grid.n_cells} cells, {len(grid.tiles)} tiles, "
            f"colormap={layer.colormap} color_by={color_by or 'flat'}"
        )

    # (layer, grid, cache) per point layer.
    point_layers = []
    for layer in slide.points:
        grid = load_point_tile_grid(layer, slide.world_bounds)
        cache = PointTileCache(
            grid,
            max_tiles=max_tiles,
            size=layer.size,
            color_mode=layer.color_mode,
            colormap=layer.colormap,
            palette=layer.palette,
        )
        cache.group.visible = layer.visible
        scene.add(cache.group)
        point_layers.append((layer, grid, cache))
        print(
            f"points '{layer.id}': {grid.n_points} transcripts, {len(grid.gene_names)} genes, "
            f"{len(grid.tiles)} tiles, color={layer.color_mode}"
        )

    QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # `win` is passed in when the window already exists and was showing an
    # import running into it -- the same window becomes the view, rather than
    # one closing and another appearing. It is already on screen and already in
    # _OPEN_WINDOWS, so neither is repeated below.
    reused = win is not None
    if win is None:
        win = _MainWindow()
        win.resize(1300, 950)
        _place_window(win)
    else:
        # Grow out of the loading size now that there is a slide to show, and
        # re-centre, since a window sized for a progress card is nowhere near
        # where a viewer belongs.
        win.resize(1300, 950)
        _place_window(win)
        # It already has the File menu it carried while importing, and the menus
        # below are added, not replaced -- without this the window ends up with
        # two File menus side by side. A fresh bar rather than clear(): clear()
        # only removes the actions, leaving the old QMenus alive as hidden
        # children of the bar, which then turn up in later findChildren scans
        # and hand back stale wrappers once Python collects them.
        win.setMenuBar(QtWidgets.QMenuBar(win))
    # The session is in the title because it's what tells two windows on the
    # same slide apart -- which region you're looking at, not a bare "(2)".
    win.setWindowTitle(f"cytos — {slide.name} · {session_name}")
    win.slide_root = slide.root
    win.session_name = session_name
    win.max_tiles = max_tiles

    render_widget = CanvasRenderWidget(parent=win)
    win.setCentralWidget(render_widget)
    # A child of the canvas, so it follows the canvas's own geometry rather than
    # needing the window layout to leave a hole for it.
    scale_bar = ScaleBarWidget(render_widget)
    units_label = unit_abbrev(slide.world_units)
    renderer = gfx.WgpuRenderer(render_widget)
    camera = gfx.OrthographicCamera()

    def fit_camera_to_slide() -> None:
        camera.show_rect(minx, maxx, miny, maxy)

    saved_camera = session.get("camera")
    if saved_camera:
        cx, cy = saved_camera["center"]
        cw, chh = saved_camera["size"]
        camera.show_rect(cx - cw / 2, cx + cw / 2, cy - chh / 2, cy + chh / 2)
    else:
        fit_camera_to_slide()
    gfx.PanZoomController(camera, register_events=renderer)

    dock_widget = QtWidgets.QWidget()
    dock_layout = QtWidgets.QVBoxLayout(dock_widget)
    # A collapsed CollapsibleSection's hidden content doesn't count toward
    # the layout's width (Qt layouts ignore invisible children's size), so
    # without a hard-pinned width the dock visibly grows the moment a wide
    # section (e.g. a ChannelRow's colormap combo, measured at 317px) first
    # expands. A *minimum* isn't enough -- Qt still grows the dock up to the
    # expanded content's sizeHint; only a fixed width can't change at all.
    # 340 clears the measured 317px with margin for longer colormap names.
    dock_widget.setFixedWidth(_DOCK_WIDTH)

    minimap = MinimapWidget(world_bounds=(minx, miny, maxx, maxy))

    def on_minimap_click(wx: float, wy: float) -> None:
        camera.local.position = (wx, wy, camera.local.position[2])

    minimap.position_clicked.connect(on_minimap_click)
    dock_layout.addWidget(minimap)

    # Grouped by layer kind (napari groups its layer list by type too) so
    # the panel reads as "what kinds of things can be on screen", not one
    # flat, ever-growing stack of unrelated rows.
    images_section = CollapsibleSection("Images")
    dock_layout.addWidget(images_section)
    segments_section = CollapsibleSection("Segments")
    dock_layout.addWidget(segments_section)
    # Collapsed by default even when loaded: its gene list is by far the
    # tallest thing in the panel, and the layer is legible on screen without
    # it -- unlike Images/Segments, whose controls are the only way to tell
    # what's being shown.
    points_section = CollapsibleSection("Points", expanded=False)
    dock_layout.addWidget(points_section)

    # Each section checkbox is a master switch layered *on top of* the
    # per-layer checkboxes under it, not a replacement: turning a section back
    # on restores each layer to whatever its own checkbox last said, rather
    # than force-showing layers the user had individually hidden.
    segment_visibility = {layer.id: layer.visible for layer, *_ in segment_layers}
    # (layer, feature names, row) — kept so "Reset to Slide Defaults" and the
    # session save on close can reach every row.
    segment_rows = []
    point_rows = []
    image_rows = []

    if segment_layers:
        def on_segments_section_visibility(section_visible: bool) -> None:
            for layer, _grid, cache, _features in segment_layers:
                cache.group.visible = section_visible and segment_visibility.get(layer.id, True)

        segments_section.visibility_changed.connect(on_segments_section_visibility)

        for layer, _grid, cache, feature_names in segment_layers:
            segment_row = SegmentRow(
                layer.id.capitalize(),
                feature_names,
                cache.colormap,
                cache.color_by,
                cache.show_outline,
                cache.show_fill,
                cache.fill_opacity,
                layer.visible,
            )
            segment_row.colormap_changed.connect(cache.set_colormap)
            segment_row.color_by_changed.connect(cache.set_color_by)
            segment_row.outline_changed.connect(cache.set_outline_visible)
            segment_row.fill_changed.connect(cache.set_fill_visible)
            segment_row.fill_opacity_changed.connect(cache.set_fill_opacity)

            def on_segment_visibility(visible, layer_id=layer.id, c=cache):
                segment_visibility[layer_id] = visible
                c.group.visible = visible and segments_section.is_checked()

            segment_row.visibility_changed.connect(on_segment_visibility)
            segments_section.add_widget(segment_row)
            segment_rows.append((layer, feature_names, segment_row))
    else:
        segments_section.setEnabled(False)

    if point_layers:
        def on_points_section_visibility(section_visible: bool) -> None:
            for layer, _grid, cache in point_layers:
                cache.group.visible = section_visible and layer.visible

        points_section.visibility_changed.connect(on_points_section_visibility)

        for layer, grid, cache in point_layers:
            saved_genes = saved_layers.get(f"points:{layer.id}", {}).get("genes")
            points_row = PointsRow(
                layer.id.capitalize(),
                grid.gene_names,
                grid.genes.column("count").to_pylist() if grid.genes is not None else [],
                cache.color_mode,
                cache.palette,
                cache.colormap,
                cache.size,
                cache.opacity,
                set(saved_genes) if saved_genes is not None else None,
            )
            points_row.color_mode_changed.connect(cache.set_color_mode)
            points_row.palette_changed.connect(cache.set_palette)
            points_row.colormap_changed.connect(cache.set_colormap)
            points_row.size_changed.connect(cache.set_size)
            points_row.opacity_changed.connect(cache.set_opacity)
            points_row.visible_genes_changed.connect(cache.set_visible_genes)
            points_section.add_widget(points_row)
            point_rows.append((layer, points_row))
            if saved_genes is not None:
                # The row was *built* with the saved selection, so it never
                # emitted -- push it through once so the LUT agrees with it.
                cache.set_visible_genes(set(saved_genes))
    else:
        points_section.setEnabled(False)

    visibility = {ch.name: layer.visible for layer, ch in image_layers}

    def refresh_minimap():
        minimap.set_image(make_composite_thumbnail(channels, visibility))

    def on_images_section_visibility(section_visible: bool) -> None:
        for ch in channels:
            ch.cache.group.visible = section_visible and visibility.get(ch.name, True)

    images_section.visibility_changed.connect(on_images_section_visibility)

    # New rows (initial or opened later) always insert right after the last
    # row in the Images section -- so dynamically-opened channels land in
    # the right visual spot instead of after whatever comes next.
    rows_insert_at = [images_section.content_layout.count()]

    def add_row(ch: Channel, visible: bool = True) -> ChannelRow:
        intensity_max = float(np.asarray(ch.levels[-1].data).max()) * 1.2
        row = ChannelRow(ch, intensity_max, visible)
        row.clim_changed.connect(ch.cache.set_clim)
        row.clim_changed.connect(lambda *_: refresh_minimap())
        row.visibility_changed.connect(lambda v, c=ch: setattr(c.cache.group, "visible", v))

        def on_visibility(v, name=ch.name):
            visibility[name] = v
            refresh_minimap()

        row.visibility_changed.connect(on_visibility)

        def on_colormap(name, c=ch):
            c.cache.set_colormap(name)
            refresh_minimap()

        row.colormap_changed.connect(on_colormap)
        images_section.insert_widget(rows_insert_at[0], row)
        rows_insert_at[0] += 1
        # A channel opened while the Images master switch is off should
        # start hidden too, not fight the section checkbox it's under.
        ch.cache.group.visible = images_section.is_checked() and visible
        return row

    for layer, ch in image_layers:
        image_rows.append((layer, ch, add_row(ch, layer.visible)))

    if not channels:
        images_section.setEnabled(False)

    # After the rows exist and the section handlers are connected, so a
    # restored section checkbox actually reaches the layers under it.
    saved_sections = session.get("sections", {})
    for name, section, default_expanded in (
        ("images", images_section, True),
        ("segments", segments_section, True),
        ("points", points_section, False),
    ):
        saved = saved_sections.get(name)
        if saved:
            section.apply(bool(saved.get("expanded", default_expanded)), bool(saved.get("checked", True)))

    refresh_minimap()

    stats_label = QtWidgets.QLabel("—")
    stats_label.setWordWrap(False)
    stats_label.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
    stats_label.setFixedWidth(240)
    # One row per layer, plus the resolution line, plus a row of slack.
    stats_label.setMinimumHeight(20 * (len(channels) + len(segment_layers) + len(point_layers) + 1) + 20)
    stats_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
    dock_layout.addWidget(stats_label)
    dock_layout.addStretch()

    def add_images():
        dialog = QtWidgets.QFileDialog(win, "Add OME-Zarr image")
        dialog.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
        dialog.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
        # Native directory pickers only allow single selection; the
        # DontUseNativeDialog + ExtendedSelection combo is the standard Qt
        # workaround for picking several OME-Zarr folders in one dialog.
        dialog.setOption(QtWidgets.QFileDialog.Option.DontUseNativeDialog, True)
        for view in dialog.findChildren(QtWidgets.QAbstractItemView):
            view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        if not dialog.exec():
            return

        # An OME-Zarr store is a *folder*, so a directory picker can't filter
        # out the wrong ones -- picking a slide root or a segments folder is
        # an ordinary mistake. Report it in the window instead of raising into
        # Qt's slot handler, which prints a traceback to a terminal the user
        # may not even be looking at and leaves the dialog looking ignored.
        failed = []
        for path in dialog.selectedFiles():
            try:
                ch = build_channel(path, COMPOSITE_COLORMAPS[len(channels) % len(COMPOSITE_COLORMAPS)],
                                   _default_channel_name(path))
            except (ValueError, KeyError, OSError) as err:
                failed.append(f"{Path(path).name}\n    {err}")
                continue
            channels.append(ch)
            visibility[ch.name] = True
            add_row(ch)

        if channels:
            images_section.setEnabled(True)
        refresh_minimap()
        if failed:
            QtWidgets.QMessageBox.warning(
                win,
                "Could not add image",
                "These folders aren't OME-Zarr images:\n\n" + "\n\n".join(failed),
            )

    dock = QtWidgets.QDockWidget("Layers", win)
    # restoreState matches docks by objectName; without one the saved layout
    # silently comes back at the default position.
    dock.setObjectName("layers_dock")
    # The panel scrolls rather than forcing the window to be tall enough for it.
    # A dock's minimum height is its content's, and the content's is the sum of
    # every expanded section -- 955px on a three-layer slide, which is more
    # vertical room than a laptop screen has once the menu bar and title bar are
    # taken out. The window then can't shrink below that and can't grow past the
    # screen, so its height stops moving at all while its width still works.
    dock_scroll = QtWidgets.QScrollArea()
    dock_scroll.setWidget(dock_widget)
    # Without this the scroll area shows the panel at its minimum size in a
    # corner instead of filling the width it was given.
    dock_scroll.setWidgetResizable(True)
    dock_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    dock_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # The panel inside is pinned to _DOCK_WIDTH, so the scroll area has to be
    # that much wider again to leave room for its own scrollbar -- otherwise the
    # bar sits on top of the panel's right edge and clips the widest controls.
    scrollbar_width = dock_scroll.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent)
    dock_scroll.setFixedWidth(_DOCK_WIDTH + scrollbar_width)
    dock.setWidget(dock_scroll)
    dock.setFeatures(
        QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
    )
    win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def open_slide():
        prompt_open_slide(win, max_tiles)

    file_menu = win.menuBar().addMenu("File")
    # Always a new window, never a replacement: comparing two samples, or two
    # regions of one sample, is the reason to open a second slide at all.
    open_action = file_menu.addAction("Open Slide…")
    open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
    open_action.triggered.connect(open_slide)
    file_menu.addMenu("New Slide from").addAction("Xenium Output…").triggered.connect(
        lambda: prompt_new_slide(win, max_tiles)
    )
    # "Add", not "Open": the slide is already open, and this puts another
    # image on top of it rather than replacing anything. The dialog takes
    # several folders at once, so no "(s)" is needed in the label.
    add_action = file_menu.addAction("Add Image…")
    add_action.triggered.connect(add_images)
    file_menu.addSeparator()
    # Closing saves anyway; this is for saving a good view *before* carrying on
    # poking at it, which is the moment you actually want it captured.
    save_action = file_menu.addAction(f"Save Session “{session_name}”")
    save_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
    save_action.triggered.connect(lambda: (save_now(), print(f"session '{session_name}': saved")))

    menu = win.menuBar().addMenu("View")
    menu.addAction(dock.toggleViewAction())

    def collect_session() -> dict:
        layers_state = {}
        for layer, _ch, row in image_rows:
            layers_state[f"image:{layer.id}"] = row.state()
        for layer, _features, row in segment_rows:
            layers_state[f"segments:{layer.id}"] = row.state()
        for layer, row in point_rows:
            layers_state[f"points:{layer.id}"] = row.state()
        return {
            "camera": {
                "center": [float(camera.local.position[0]), float(camera.local.position[1])],
                "size": [float(camera.width), float(camera.height)],
            },
            "window": {
                "geometry": bytes(win.saveGeometry().toBase64()).decode(),
                "state": bytes(win.saveState().toBase64()).decode(),
            },
            "sections": {
                name: {"expanded": section.is_expanded(), "checked": section.is_checked()}
                for name, section in (
                    ("images", images_section),
                    ("segments", segments_section),
                    ("points", points_section),
                )
            },
            "layers": layers_state,
        }

    def reset_to_defaults() -> None:
        """Back to exactly what `cytos-import` wrote, and forget the session.
        Every `apply` below re-emits its row's signals, so the tile caches
        follow the widgets rather than needing a second update path."""
        for key, kind, layer in _iter_layers(slide):
            _restore(layer, kind, defaults[key])
        for layer, ch, row in image_rows:
            row.apply(layer.colormap, layer.visible, layer.clim or ch.cache.clim)
        for layer, feature_names, row in segment_rows:
            color_by = layer.color_by if layer.color_by in feature_names else None
            row.apply(
                layer.colormap, color_by, layer.show_outline, layer.show_fill, layer.fill_opacity, layer.visible
            )
        for layer, row in point_rows:
            # Genes have no manifest default -- "all of them" is the default.
            row.apply(layer.color_mode, layer.palette, layer.colormap, layer.size, layer.opacity, None)
        images_section.apply(True, True)
        segments_section.apply(True, True)
        points_section.apply(False, True)
        fit_camera_to_slide()
        refresh_minimap()
        # The session file isn't deleted -- you named it, so it stays and is
        # simply back to holding nothing but the slide's own defaults, which
        # is what gets written on the next save.
        print(f"session '{session_name}': reset to slide defaults")

    menu.addSeparator()
    reset_action = menu.addAction("Reset to Slide Defaults")
    reset_action.triggered.connect(reset_to_defaults)

    def snapshot():
        """The rendered frame, for the picker's thumbnail. Best-effort: a
        failed readback must never cost you the session itself."""
        try:
            return renderer.snapshot()
        except Exception as err:  # noqa: BLE001 - any GPU readback failure
            print(f"session '{session_name}': no snapshot ({err})")
            return None

    def save_now():
        save_session(slide.root, session_name, collect_session(), snapshot())

    win.save_now = save_now

    saved_window = session.get("window")
    if saved_window:
        # After the dock exists: restoreState matches docks by objectName, and
        # a dock added later would come back at its default spot instead.
        win.restoreGeometry(QtCore.QByteArray.fromBase64(saved_window["geometry"].encode()))
        win.restoreState(QtCore.QByteArray.fromBase64(saved_window["state"].encode()))

    last_level = [None]
    latest_stats_text = ["—"]
    latest_world_rect = [(minx, miny, maxx, maxy)]
    latest_world_per_px = [None]

    def animate():
        logical_w, logical_h = render_widget.get_logical_size()
        cx, cy = float(camera.local.position[0]), float(camera.local.position[1])
        eff_w, eff_h = effective_camera_view_size(camera.width, camera.height, logical_w, logical_h)
        half_w, half_h = eff_w / 2, eff_h / 2
        world_rect = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        world_per_px = eff_w / logical_w if logical_w else eff_w
        latest_world_rect[0] = world_rect
        latest_world_per_px[0] = world_per_px

        # The resolution leads the readout: it is the one number that answers
        # "what am I actually looking at now", where the rest are cache health.
        lines = [f"{world_per_px:.3g} {units_label}/px"]
        for ch in channels:
            level_idx = select_level(ch.levels, world_per_px)
            stats = ch.cache.update(level_idx, world_rect)
            lines.append(f"{ch.name[:12]:12s} L{level_idx} n={stats['needed']} c={stats['cache_size']}")

        if channels:
            level0 = select_level(channels[0].levels, world_per_px)
            if level0 != last_level[0]:
                print(f"level={level0} world_per_px={world_per_px:.4f}")
                last_level[0] = level0

        for layer, _grid, cache, _features in segment_layers:
            stats = cache.update(world_rect)
            lines.append(f"{layer.id[:12]:12s} n={stats['needed']} c={stats['cache_size']}")
        for layer, _grid, cache in point_layers:
            stats = cache.update(world_rect)
            lines.append(f"{layer.id[:12]:12s} n={stats['needed']} c={stats['cache_size']}")

        latest_stats_text[0] = "\n".join(lines)

        renderer.render(scene, camera)
        render_widget.request_draw()

    # Throttled to a fixed low rate, not the (uncapped) render loop — updating
    # Qt widgets every render frame caused visible layout thrash before
    # (see CLAUDE.md); painting the minimap rect is cheap but still no reason
    # to redo it hundreds of times a second.
    def tick():
        stats_label.setText(latest_stats_text[0])
        minimap.set_view_rect(latest_world_rect[0])
        scale_bar.set_scale(latest_world_per_px[0], slide.world_units)

    # Parented to the window, so the window owns it. Without a parent nothing
    # holds this timer once build_window returns, Python collects it on the way
    # out, and it silently stops -- taking the minimap's view rectangle and the
    # live stats text with it, since tick() is the only thing that updates
    # either. It survived only while this code lived inside main(), whose frame
    # stays alive for as long as the app runs.
    stats_timer = QtCore.QTimer(win)
    stats_timer.timeout.connect(tick)
    stats_timer.start(100)

    render_widget.request_draw(animate)
    win.show()
    if not reused:
        _OPEN_WINDOWS.append(win)
    print("window open — left-drag to pan, scroll to zoom, per-layer controls in the dock panel")
    return win


# One app, not one per slide. Sessions are owned by a single window at a time
# (see cytos.core.session), and that rule can only be enforced against windows
# this process can see -- a second process would happily open a session the
# first one already has.
_IPC_NAME = "cytos-viewer"
_ipc_server = None  # module-level: a QLocalServer that goes out of scope stops listening


def _raise_all_windows() -> None:
    # Every visible top-level window, not just _OPEN_WINDOWS -- the welcome
    # window has no slide behind it and so isn't in that list, but it's
    # exactly what's on screen when a second launch happens before any slide
    # has been opened.
    for win in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(win, QtWidgets.QMainWindow) and win.isVisible():
            win.setWindowState(win.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
            win.raise_()
            win.activateWindow()


def _claim_single_instance() -> bool:
    """True if this process is the app. False means one is already running --
    it's been asked to come to the front, and this process should exit."""
    global _ipc_server

    probe = QtNetwork.QLocalSocket()
    probe.connectToServer(_IPC_NAME)
    if probe.waitForConnected(300):
        probe.write(b"raise")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return False

    # Nothing answered. Either no instance is running, or one crashed and left
    # its socket file behind -- removeServer clears a stale one so listen()
    # can bind. Safe precisely because the probe above just failed.
    QtNetwork.QLocalServer.removeServer(_IPC_NAME)
    _ipc_server = QtNetwork.QLocalServer()
    if not _ipc_server.listen(_IPC_NAME):
        # Losing the race to bind isn't fatal: it only means the "raise the
        # running app" handshake won't work, not that this window can't run.
        print(f"note: could not listen on {_IPC_NAME} ({_ipc_server.errorString()})")
    _ipc_server.newConnection.connect(lambda: _raise_all_windows())
    return True


def _shutdown_gpu() -> None:
    """Drain and stop wgpu's poll thread while Qt is still standing.

    wgpu delivers finished GPU work on a background thread it starts per device,
    and that thread hands the result to the Qt main thread by emitting a signal.
    On the way out PySide invalidates the object that signal is emitted from, so
    a readback landing late -- the closing snapshot's, usually -- raises inside a
    C callback, where the exception has nowhere to go and gets printed as
    "Exception ignored". Harmless in itself, but a traceback on every exit is
    exactly the kind of noise that gets you used to skipping the console.

    Blocking on the device first lets anything genuinely in flight report back
    while the receiver is still alive; stopping the poller then guarantees
    nothing else is dispatched afterwards.
    """
    from pygfx.renderers.wgpu.engine.shared import Shared

    shared = Shared.get_instance()  # None if no window ever opened, so no device
    if shared is None:
        return
    device = shared.device
    device._poll_wait()
    # Private, and only a tidy-up -- worth checking for rather than depending on.
    poller = getattr(device, "_poller", None)
    if poller is not None:
        poller.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-tiles", type=int, default=64, help="GPU tile cache size, per layer")
    args = parser.parse_args()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    if not _claim_single_instance():
        print("cytos is already running — bringing it to the front")
        return

    # Before any window, so the fallback bar exists the first time every
    # window is minimized rather than only after one has been built.
    build_app_menu_bar(args.max_tiles)
    build_welcome_window(args.max_tiles)
    # Qt's own loop, not rendercanvas's: rendercanvas.run() returns immediately
    # when no canvas exists yet, and the app now starts on a welcome window
    # that has none. Equivalent either way -- rendercanvas's Qt backend just
    # calls app.exec() itself, and its canvases are Qt widgets driven by this
    # loop regardless of who started it.
    app.exec()
    _shutdown_gpu()


if __name__ == "__main__":
    main()
