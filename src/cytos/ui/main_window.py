"""cytos's live viewer: several OME-Zarr pyramids composited together, each
mapped through its own colormap (cytos.render.image's TileCache(colormap=...),
names from cytos.render.image.CURATED_COLORMAPS) and additively blended — the
standard multi-channel fluorescence display model (Fiji "composite" mode,
napari additive blending).

The dock panel groups by layer kind (Images, Segments, Points — see
cytos.ui.collapsible_section), each section independently expandable. Every
channel gets its own settings group under Images: a colormap picker, a
visibility checkbox, and contrast controls. All channels share one
camera/level-selection loop, since they're assumed to be spatially registered
(same pyramid structure).

Segments (cytos.ui.segment_panel) gets outline/fill toggles, a fill-opacity
control, and a colormap spread over a per-cell measurement — cell_area by
default, so cells are colored by their own data rather than by a fixed color.

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
from cytos.core.polygons import load_polygon_tile_grid, numeric_feature_names
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import COMPOSITE_COLORMAPS, TileCache
from cytos.render.polygons import DEFAULT_COLORMAP, PolygonTileCache
from cytos.ui.channel_panel import Channel, ChannelRow
from cytos.ui.collapsible_section import CollapsibleSection
from cytos.ui.minimap import MinimapWidget, make_composite_thumbnail
from cytos.ui.segment_panel import SegmentRow
from cytos.ui.canvas_input import CanvasRenderWidget

# Preferred default for the segment layer's "Color by": the most broadly
# meaningful per-cell measurement present. Falls back to the first numeric
# feature the cache happens to carry, then to a flat color if it has none.
_PREFERRED_COLOR_BY = ("cell_area", "nucleus_area", "transcript_counts", "total_counts")


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
    parser.add_argument(
        "--polygons",
        default=None,
        help="path to a cytos-prep-polygons cache directory (e.g. "
        "data/xenium_breast_cancer_rep1/polygons_cache) — draws the cell "
        "boundary layer over the image channels",
    )
    parser.add_argument(
        "--segment-colormap",
        default=DEFAULT_COLORMAP,
        help="colormap for the segment layer (same names as --channel); "
        f"default {DEFAULT_COLORMAP}",
    )
    parser.add_argument(
        "--segment-fill",
        action="store_true",
        help="also fill the cells, not just their outlines",
    )
    parser.add_argument(
        "--segment-fill-only",
        action="store_true",
        help="fill the cells and hide the outlines",
    )
    parser.add_argument(
        "--segment-fill-opacity",
        type=float,
        default=0.35,
        help="0-1, default 0.35 — kept low so the image underneath stays readable",
    )
    args = parser.parse_args()

    specs = args.channel or []
    channels: list[Channel] = []
    scene = gfx.Scene()
    # An empty pygfx scene renders fully *transparent* black (alpha=0), not
    # opaque black -- with every layer hidden that let the Qt panel's own
    # background color show through the canvas instead of true black.
    scene.add(gfx.Background(None, gfx.BackgroundMaterial("#000000")))
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

    polygon_cache = None
    polygon_features: list[str] = []
    if args.polygons is not None:
        polygon_grid = load_polygon_tile_grid(Path(args.polygons))
        polygon_features = numeric_feature_names(polygon_grid.features)
        color_by = next(
            (name for name in _PREFERRED_COLOR_BY if name in polygon_features),
            polygon_features[0] if polygon_features else None,
        )
        polygon_cache = PolygonTileCache(
            polygon_grid,
            max_tiles=args.max_tiles,
            colormap=args.segment_colormap,
            color_by=color_by,
            show_outline=not args.segment_fill_only,
            show_fill=args.segment_fill or args.segment_fill_only,
            fill_opacity=args.segment_fill_opacity,
        )
        scene.add(polygon_cache.group)
        print(
            f"segments: {polygon_grid.n_cells} cells, colormap={args.segment_colormap} "
            f"color_by={color_by or 'flat'}"
        )
        px0, py0, px1, py1 = polygon_grid.world_bounds
        minx, miny = min(minx, px0), min(miny, py0)
        maxx, maxy = max(maxx, px1), max(maxy, py1)

    # Started with no channels (the common case now: pip doesn't ship data,
    # so the first real bounds come from whatever's opened via File > Open
    # once the window is up) — camera/minimap need *some* finite rect in the
    # meantime, and get properly re-fit the first time real data arrives
    # (see camera_fitted below).
    camera_fitted = [bool(channels) or polygon_cache is not None]
    if not channels and polygon_cache is None:
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
    # A collapsed CollapsibleSection's hidden content doesn't count toward
    # the layout's width (Qt layouts ignore invisible children's size), so
    # without a hard-pinned width the dock visibly grows the moment a wide
    # section (e.g. a ChannelRow's colormap combo, measured at 317px) first
    # expands. A *minimum* isn't enough -- Qt still grows the dock up to the
    # expanded content's sizeHint; only a fixed width can't change at all.
    # 340 clears the measured 317px with margin for longer colormap names.
    dock_widget.setFixedWidth(340)

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
    # No points support yet (see work-notes/plan.md's non-goals) -- an empty,
    # collapsed placeholder so the panel's shape doesn't change the day that
    # changes, rather than conjuring the section into existence then.
    points_section = CollapsibleSection("Points", expanded=False)
    points_section.setEnabled(False)
    dock_layout.addWidget(points_section)

    # Segments currently holds exactly one layer (cell boundaries), so the
    # section checkbox is that layer's master on/off; the row under it says
    # *how* it's drawn (outline/fill/opacity) and *what colors it* (colormap
    # over a per-cell feature).
    if polygon_cache is not None:
        segments_section.visibility_changed.connect(lambda v: setattr(polygon_cache.group, "visible", v))
        segment_row = SegmentRow(
            "Cells",
            polygon_features,
            polygon_cache.colormap,
            polygon_cache.color_by,
            polygon_cache.show_outline,
            polygon_cache.show_fill,
            polygon_cache.fill_opacity,
        )
        segment_row.colormap_changed.connect(polygon_cache.set_colormap)
        segment_row.color_by_changed.connect(polygon_cache.set_color_by)
        segment_row.outline_changed.connect(polygon_cache.set_outline_visible)
        segment_row.fill_changed.connect(polygon_cache.set_fill_visible)
        segment_row.fill_opacity_changed.connect(polygon_cache.set_fill_opacity)
        segments_section.add_widget(segment_row)
    else:
        segments_section.setEnabled(False)

    visibility = {ch.name: True for ch in channels}

    def refresh_minimap():
        minimap.set_image(make_composite_thumbnail(channels, visibility))

    def on_images_section_visibility(section_visible: bool) -> None:
        # A master switch layered on top of each channel's own visibility
        # checkbox, not a replacement for it: turning the section back on
        # restores each channel to whatever its own checkbox last said,
        # rather than force-showing channels the user had individually hidden.
        for ch in channels:
            ch.cache.group.visible = section_visible and visibility.get(ch.name, True)

    images_section.visibility_changed.connect(on_images_section_visibility)

    # New rows (initial or opened later) always insert right after the last
    # row in the Images section -- so dynamically-opened channels land in
    # the right visual spot instead of after whatever comes next.
    rows_insert_at = [images_section.content_layout.count()]

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
        images_section.insert_widget(rows_insert_at[0], row)
        rows_insert_at[0] += 1
        # A channel opened while the Images master switch is off should
        # start hidden too, not fight the section checkbox it's under.
        ch.cache.group.visible = images_section.is_checked()

    for ch in channels:
        add_row(ch)

    refresh_minimap()

    stats_label = QtWidgets.QLabel("—")
    stats_label.setWordWrap(False)
    stats_label.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
    stats_label.setFixedWidth(240)
    stats_label.setMinimumHeight(20 * (len(channels) + (1 if polygon_cache is not None else 0)) + 20)
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

    dock = QtWidgets.QDockWidget("Layers", win)
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

        if polygon_cache is not None:
            poly_stats = polygon_cache.update(world_rect)
            poly_line = f"{'polygons':12s} n={poly_stats['needed']} c={poly_stats['cache_size']}"
            latest_stats_text[0] = (
                f"{latest_stats_text[0]}\n{poly_line}" if channels else poly_line
            )

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
