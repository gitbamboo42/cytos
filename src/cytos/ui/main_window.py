"""cytos's live viewer: several OME-Zarr pyramids composited together, each
mapped through its own colormap (cytos.render.image's TileCache(colormap=...),
names from cytos.render.image.CURATED_COLORMAPS) and additively blended — the
standard multi-channel fluorescence display model (Fiji "composite" mode,
napari additive blending).

Each channel gets its own settings group in the dock panel: a colormap picker,
a visibility checkbox, and contrast controls. All channels share one
camera/level-selection loop, since they're assumed to be spatially registered
(same pyramid structure).

Starts empty by default — pip doesn't ship any data with the package. Load
channels either via --channel flags or File > Open Channel(s)… once the
window is up.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-viewer
    cytos-viewer \
        --channel data/human_kidney_tiny/dapi.ome.zarr blue DAPI \
        --channel data/human_kidney_tiny/ch1_boundary.ome.zarr green Boundary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pygfx as gfx
from PySide6 import QtCore, QtGui, QtWidgets
from rendercanvas.qt import loop

from cytos.core.image import load_pyramid_levels, select_level
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import COMPOSITE_COLORMAPS, TileCache
from cytos.ui.channel_panel import Channel, ChannelRow
from cytos.ui.minimap import MinimapWidget, make_composite_thumbnail
from cytos.ui.canvas_input import CanvasRenderWidget


def _default_channel_name(path: str) -> str:
    """Folder name with the OME-Zarr suffix stripped, e.g. 'dapi.ome.zarr' ->
    'dapi' — used when a channel is opened via the file dialog instead of
    named explicitly on the command line."""
    name = Path(path).name
    for suffix in (".ome.zarr", ".zarr"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        nargs=3,
        action="append",
        metavar=("PATH", "COLORMAP", "NAME"),
        default=None,
        help="repeatable; COLORMAP is a name from "
        "cytos.render.image.CURATED_COLORMAPS (e.g. blue, viridis) or any "
        "plotlet.list_colormaps() name; omit to start with an empty viewer "
        "(use File > Open Channel(s)… once it's up)",
    )
    parser.add_argument("--max-tiles", type=int, default=64, help="per channel")
    args = parser.parse_args()

    specs = args.channel or []
    channels: list[Channel] = []
    scene = gfx.Scene()
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    def build_channel(path: str, colormap: str, name: str) -> Channel:
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
        coarsest = np.asarray(levels[-1].data)
        clim = tuple(float(v) for v in np.percentile(coarsest, [1, 99.5]))
        cache = TileCache(levels, clim=clim, max_tiles=args.max_tiles, colormap=colormap)
        scene.add(cache.group)
        print(f"channel '{name}': {path} colormap={colormap} clim={clim}")
        return Channel(name=name, colormap=colormap, levels=levels, cache=cache)

    for path, colormap, name in specs:
        ch = build_channel(path, colormap, name)
        cx0, cy0, cx1, cy1 = ch.levels[0].world_bounds()
        minx, miny = min(minx, cx0), min(miny, cy0)
        maxx, maxy = max(maxx, cx1), max(maxy, cy1)
        channels.append(ch)

    # Started with no channels (the common case now: pip doesn't ship data,
    # so the first real bounds come from whatever's opened via File > Open
    # once the window is up) — camera/minimap need *some* finite rect in the
    # meantime, and get properly re-fit the first time real data arrives
    # (see camera_fitted below).
    camera_fitted = [bool(channels)]
    if not channels:
        minx, miny, maxx, maxy = -1.0, -1.0, 1.0, 1.0

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("cytos - Phase 0 multi-channel composite")
    win.resize(1300, 950)

    render_widget = CanvasRenderWidget(parent=win)
    win.setCentralWidget(render_widget)
    renderer = gfx.WgpuRenderer(render_widget)
    camera = gfx.OrthographicCamera()
    camera.show_rect(minx, maxx, miny, maxy)
    gfx.PanZoomController(camera, register_events=renderer)

    dock_widget = QtWidgets.QWidget()
    dock_layout = QtWidgets.QVBoxLayout(dock_widget)

    minimap = MinimapWidget(world_bounds=(minx, miny, maxx, maxy))
    def on_minimap_click(wx: float, wy: float) -> None:
        camera.local.position = (wx, wy, camera.local.position[2])

    minimap.position_clicked.connect(on_minimap_click)
    dock_layout.addWidget(minimap)

    visibility = {ch.name: True for ch in channels}

    def refresh_minimap():
        minimap.set_image(make_composite_thumbnail(channels, visibility))

    # New rows (initial or opened later) always insert right after the last
    # row, ahead of the stats label + stretch added below — so
    # dynamically-opened channels land in the right visual spot instead of
    # after the stretch.
    rows_insert_at = [dock_layout.count()]

    def add_row(ch: Channel):
        intensity_max = float(np.asarray(ch.levels[-1].data).max()) * 1.2
        row = ChannelRow(ch, intensity_max)
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
        dock_layout.insertWidget(rows_insert_at[0], row)
        rows_insert_at[0] += 1

    for ch in channels:
        add_row(ch)

    refresh_minimap()

    stats_label = QtWidgets.QLabel("—")
    stats_label.setWordWrap(False)
    stats_label.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
    stats_label.setFixedWidth(240)
    stats_label.setMinimumHeight(20 * len(channels) + 20)
    stats_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
    dock_layout.addWidget(stats_label)
    dock_layout.addStretch()

    def open_channels():
        dialog = QtWidgets.QFileDialog(win, "Open OME-Zarr channel(s)")
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
        for path in dialog.selectedFiles():
            name = _default_channel_name(path)
            colormap = COMPOSITE_COLORMAPS[len(channels) % len(COMPOSITE_COLORMAPS)]
            ch = build_channel(path, colormap, name)
            channels.append(ch)
            visibility[ch.name] = True
            add_row(ch)

        if not camera_fitted[0]:
            # First real data since starting empty — the camera/minimap were
            # sitting on a meaningless (-1, -1, 1, 1) placeholder, so fit to
            # what just got opened instead of leaving it invisible there.
            # Later opens after this one intentionally don't re-fit, so they
            # don't yank the user's view around mid-session.
            bx0 = by0 = float("inf")
            bx1 = by1 = float("-inf")
            for ch in channels:
                cx0, cy0, cx1, cy1 = ch.levels[0].world_bounds()
                bx0, by0 = min(bx0, cx0), min(by0, cy0)
                bx1, by1 = max(bx1, cx1), max(by1, cy1)
            camera.show_rect(bx0, bx1, by0, by1)
            minimap.set_world_bounds((bx0, by0, bx1, by1))
            camera_fitted[0] = True

        refresh_minimap()

    dock = QtWidgets.QDockWidget("Channels", win)
    dock.setWidget(dock_widget)
    dock.setFeatures(
        QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
    )
    win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    file_menu = win.menuBar().addMenu("File")
    open_action = file_menu.addAction("Open Channel(s)…")
    open_action.triggered.connect(open_channels)

    menu = win.menuBar().addMenu("View")
    menu.addAction(dock.toggleViewAction())

    last_level = [None]
    latest_stats_text = ["No channels open — File > Open Channel(s)…" if not channels else "—"]
    latest_world_rect = [(minx, miny, maxx, maxy)]

    def animate():
        logical_w, logical_h = render_widget.get_logical_size()
        cx, cy = float(camera.local.position[0]), float(camera.local.position[1])
        eff_w, eff_h = effective_camera_view_size(camera.width, camera.height, logical_w, logical_h)
        half_w, half_h = eff_w / 2, eff_h / 2
        world_rect = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        world_per_px = eff_w / logical_w if logical_w else eff_w
        latest_world_rect[0] = world_rect

        if channels:
            lines = []
            for ch in channels:
                level_idx = select_level(ch.levels, world_per_px)
                stats = ch.cache.update(level_idx, world_rect)
                lines.append(f"{ch.name[:12]:12s} L{level_idx} n={stats['needed']} c={stats['cache_size']}")
            latest_stats_text[0] = "\n".join(lines)

            level0 = select_level(channels[0].levels, world_per_px)
            if level0 != last_level[0]:
                print(f"level={level0} world_per_px={world_per_px:.4f}")
                last_level[0] = level0

        renderer.render(scene, camera)
        render_widget.request_draw()

    # Throttled to a fixed low rate, not the (uncapped) render loop — updating
    # Qt widgets every render frame caused visible layout thrash before
    # (see CLAUDE.md); painting the minimap rect is cheap but still no reason
    # to redo it hundreds of times a second.
    def tick():
        stats_label.setText(latest_stats_text[0])
        minimap.set_view_rect(latest_world_rect[0])

    stats_timer = QtCore.QTimer()
    stats_timer.timeout.connect(tick)
    stats_timer.start(100)

    render_widget.request_draw(animate)
    win.show()
    print("window open — left-drag to pan, scroll to zoom, per-channel controls in the dock panel")
    loop.run()


if __name__ == "__main__":
    main()
