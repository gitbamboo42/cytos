"""cytos's live viewer: several OME-Zarr pyramids composited together, each
tinted its own color and additively blended (cytos.render.image's
TileCache(color=...)) — the standard multi-channel fluorescence display model
(Fiji "composite" mode, napari additive blending).

Each channel gets its own settings group in the dock panel: a visibility
checkbox and contrast controls. All channels share one camera/level-selection
loop, since they're assumed to be spatially registered (same pyramid structure).

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-viewer
    cytos-viewer \
        --channel data/human_kidney_tiny/dapi.ome.zarr 0,0,255 DAPI \
        --channel data/human_kidney_tiny/ch1_boundary.ome.zarr 0,255,0 Boundary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pygfx as gfx
from PySide6 import QtCore, QtGui, QtWidgets
from rendercanvas.qt import RenderWidget, loop

from cytos.core.image import load_pyramid_levels, select_level
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import TileCache
from cytos.ui.channel_panel import Channel, ChannelRow
from cytos.ui.minimap import MinimapWidget, make_composite_thumbnail

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "human_kidney_tiny"
DEFAULT_CHANNELS = [
    (str(_DATA_DIR / "dapi.ome.zarr"), (0, 0, 255), "DAPI (nuclear)"),
    (str(_DATA_DIR / "ch1_boundary.ome.zarr"), (0, 255, 0), "Boundary"),
    (str(_DATA_DIR / "ch2_18s.ome.zarr"), (255, 0, 0), "18S"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel",
        nargs=3,
        action="append",
        metavar=("PATH", "R,G,B", "NAME"),
        default=None,
        help="repeatable; defaults to the 3 kidney test channels if omitted",
    )
    parser.add_argument("--max-tiles", type=int, default=64, help="per channel")
    args = parser.parse_args()

    specs = args.channel or DEFAULT_CHANNELS
    channels: list[Channel] = []
    scene = gfx.Scene()
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    for path, color, name in specs:
        if isinstance(color, str):
            color = tuple(float(c) for c in color.split(","))
        levels = load_pyramid_levels(Path(path))
        cx0, cy0, cx1, cy1 = levels[0].world_bounds()
        minx, miny = min(minx, cx0), min(miny, cy0)
        maxx, maxy = max(maxx, cx1), max(maxy, cy1)

        coarsest = np.asarray(levels[-1].data)
        clim = tuple(float(v) for v in np.percentile(coarsest, [1, 99.5]))

        cache = TileCache(levels, clim=clim, max_tiles=args.max_tiles, color=tuple(c / 255 for c in color))
        scene.add(cache.group)
        channels.append(Channel(name=name, color=color, levels=levels, cache=cache))
        print(f"channel '{name}': {path} color={color} clim={clim}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("cytos - Phase 0 multi-channel composite")
    win.resize(1300, 950)

    render_widget = RenderWidget(parent=win)
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

    for ch in channels:
        intensity_max = float(np.asarray(ch.levels[-1].data).max()) * 1.2
        row = ChannelRow(ch, intensity_max)
        row.clim_changed.connect(ch.cache.set_clim)
        row.clim_changed.connect(lambda *_: refresh_minimap())
        row.visibility_changed.connect(lambda v, c=ch: setattr(c.cache.group, "visible", v))

        def on_visibility(v, name=ch.name):
            visibility[name] = v
            refresh_minimap()

        row.visibility_changed.connect(on_visibility)
        dock_layout.addWidget(row)

    refresh_minimap()

    stats_label = QtWidgets.QLabel("—")
    stats_label.setWordWrap(False)
    stats_label.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
    stats_label.setFixedWidth(240)
    stats_label.setMinimumHeight(20 * len(channels) + 20)
    stats_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
    dock_layout.addWidget(stats_label)
    dock_layout.addStretch()

    dock = QtWidgets.QDockWidget("Channels", win)
    dock.setWidget(dock_widget)
    dock.setFeatures(
        QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
    )
    win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    menu = win.menuBar().addMenu("View")
    menu.addAction(dock.toggleViewAction())

    last_level = [None]
    latest_stats_text = ["—"]
    latest_world_rect = [(minx, miny, maxx, maxy)]

    def animate():
        logical_w, logical_h = render_widget.get_logical_size()
        cx, cy = float(camera.local.position[0]), float(camera.local.position[1])
        eff_w, eff_h = effective_camera_view_size(camera.width, camera.height, logical_w, logical_h)
        half_w, half_h = eff_w / 2, eff_h / 2
        world_rect = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        world_per_px = eff_w / logical_w if logical_w else eff_w
        latest_world_rect[0] = world_rect

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
