"""cytos's live viewer. Opens one `.cytos` bundle and nothing else — the
bundle's manifest says which layers exist, how they're colored, and what world
space they share (see `cytos.core.bundle`; `cytos-import` builds one).

That single entry point is deliberate. Reassembling a dataset from loose
paths on the command line meant the viewer had to be told, every time, what
the data already knew — and left each layer free to sit on its own world grid.

Several OME-Zarr pyramids are composited together, each mapped through its own
colormap (cytos.render.image's TileCache(colormap=...)) and additively blended
— the standard multi-channel fluorescence display model (Fiji "composite" mode,
napari additive blending). All channels share one camera/level-selection loop,
since they're assumed spatially registered.

The dock panel groups by layer kind (Images, Segments, Points — see
cytos.ui.collapsible_section), each section independently expandable, each
holding one row per layer in the bundle:

- Images (cytos.ui.channel_panel): colormap picker, visibility, contrast.
- Segments (cytos.ui.segment_panel): outline/fill toggles, fill opacity, and a
  colormap spread over a per-cell measurement — so cells are colored by their
  own data rather than by a fixed color.
- Points (cytos.ui.points_panel): transcript locations, one hue per gene, with
  a checkable gene list so a few genes show at once in clearly different
  colors — the usual way these are read.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-viewer data/breast_rep1.cytos
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pygfx as gfx
from PySide6 import QtCore, QtGui, QtWidgets
from rendercanvas.qt import loop

from cytos.core.bundle import load_bundle
from cytos.core.image import load_pyramid_levels, select_level
from cytos.core.session import clear_session, load_session, save_session
from cytos.core.points import load_point_tile_grid
from cytos.core.polygons import load_polygon_tile_grid, numeric_feature_names
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import COMPOSITE_COLORMAPS, TileCache
from cytos.render.points import PointTileCache
from cytos.render.polygons import PolygonTileCache
from cytos.ui.channel_panel import Channel, ChannelRow
from cytos.ui.collapsible_section import CollapsibleSection
from cytos.ui.minimap import MinimapWidget, make_composite_thumbnail
from cytos.ui.points_panel import PointsRow
from cytos.ui.segment_panel import SegmentRow
from cytos.ui.canvas_input import CanvasRenderWidget


# Which fields of each layer kind a session may override. The manifest holds
# the default for every one of them, which is what makes "reset" mean "drop the
# session and re-read the manifest" rather than a second list of magic values.
_STATE_FIELDS = {
    "image": ("visible", "colormap", "clim"),
    "segments": ("visible", "colormap", "color_by", "show_outline", "show_fill", "fill_opacity"),
    "points": ("visible", "color_mode", "palette", "colormap", "size", "opacity"),
}


def _iter_layers(bundle):
    for kind, layers in (("image", bundle.images), ("segments", bundle.segments), ("points", bundle.points)):
        for layer in layers:
            yield f"{kind}:{layer.id}", kind, layer


def _capture(layer, kind: str) -> dict:
    return {field: getattr(layer, field) for field in _STATE_FIELDS[kind]}


def _restore(layer, kind: str, state: dict) -> None:
    for field in _STATE_FIELDS[kind]:
        if field in state:
            setattr(layer, field, state[field])


class _MainWindow(QtWidgets.QMainWindow):
    """QMainWindow that runs a callback on close — where the session is
    written. Saving on close rather than on every widget change keeps the file
    quiet and keeps "reset to defaults" from racing an autosave."""

    def __init__(self):
        super().__init__()
        self.on_close = None

    def closeEvent(self, event):  # noqa: N802 - Qt's own naming
        if self.on_close is not None:
            self.on_close()
        super().closeEvent(event)


def _default_channel_name(path: str) -> str:
    """Folder name with the OME-Zarr suffix stripped, e.g. 'dapi.ome.zarr' ->
    'dapi' — used when a channel is opened via the file dialog instead of
    coming from the bundle manifest."""
    name = Path(path).name
    for suffix in (".ome.zarr", ".zarr"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="a .cytos bundle directory — see cytos-import")
    parser.add_argument("--max-tiles", type=int, default=64, help="GPU tile cache size, per layer")
    args = parser.parse_args()

    bundle = load_bundle(args.bundle)
    print(
        f"bundle '{bundle.name}': {len(bundle.images)} image(s), "
        f"{len(bundle.segments)} segment layer(s), {len(bundle.points)} point layer(s)"
    )

    # The manifest's values are the defaults; a saved session overrides them.
    # Snapshot the defaults *before* overriding, so "reset" has something real
    # to go back to (see cytos.core.session).
    session = load_session(bundle.root)
    saved_layers = session.get("layers", {})
    defaults = {}
    for key, kind, layer in _iter_layers(bundle):
        defaults[key] = _capture(layer, kind)
        if key in saved_layers:
            _restore(layer, kind, saved_layers[key])
    if session:
        print(f"restored session from {bundle.root}/session.json")

    scene = gfx.Scene()
    # An empty pygfx scene renders fully *transparent* black (alpha=0), not
    # opaque black -- with every layer hidden that let the Qt panel's own
    # background color show through the canvas instead of true black.
    scene.add(gfx.Background(None, gfx.BackgroundMaterial("#000000")))

    # The bundle's own world bounds, not a union recomputed from whatever
    # happens to be loaded: every layer was placed against these at import.
    minx, miny, maxx, maxy = bundle.world_bounds

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
            # Only for channels opened loose via File > Open — a bundle's
            # clim was measured at import and lives in the manifest.
            coarsest = np.asarray(levels[-1].data)
            clim = tuple(float(v) for v in np.percentile(coarsest, [1, 99.5]))
        cache = TileCache(levels, clim=tuple(clim), max_tiles=args.max_tiles, colormap=colormap)
        scene.add(cache.group)
        print(f"channel '{name}': {path} colormap={colormap} clim={tuple(round(float(v), 1) for v in clim)}")
        return Channel(name=name, colormap=colormap, levels=levels, cache=cache)

    # (layer, channel) per image in the bundle — File > Open adds channels to
    # `channels` that have no bundle layer behind them, so they aren't here.
    image_layers = []
    for layer in bundle.images:
        ch = build_channel(layer.path, layer.colormap, layer.id, layer.clim)
        channels.append(ch)
        image_layers.append((layer, ch))

    # (layer, grid, cache, numeric feature names) per segment layer in the bundle.
    segment_layers = []
    for layer in bundle.segments:
        grid = load_polygon_tile_grid(layer, bundle.world_bounds)
        feature_names = numeric_feature_names(grid.features)
        # The manifest's choice, unless the feature table can't back it.
        color_by = layer.color_by if layer.color_by in feature_names else None
        cache = PolygonTileCache(
            grid,
            max_tiles=args.max_tiles,
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
    for layer in bundle.points:
        grid = load_point_tile_grid(layer, bundle.world_bounds)
        cache = PointTileCache(
            grid,
            max_tiles=args.max_tiles,
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

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = _MainWindow()
    win.setWindowTitle(f"cytos — {bundle.name}")
    win.resize(1300, 950)

    render_widget = CanvasRenderWidget(parent=win)
    win.setCentralWidget(render_widget)
    renderer = gfx.WgpuRenderer(render_widget)
    camera = gfx.OrthographicCamera()

    def fit_camera_to_bundle() -> None:
        camera.show_rect(minx, maxx, miny, maxy)

    saved_camera = session.get("camera")
    if saved_camera:
        cx, cy = saved_camera["center"]
        cw, chh = saved_camera["size"]
        camera.show_rect(cx - cw / 2, cx + cw / 2, cy - chh / 2, cy + chh / 2)
    else:
        fit_camera_to_bundle()
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
    # (layer, feature names, row) — kept so "Reset to Bundle Defaults" and the
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
    stats_label.setMinimumHeight(20 * (len(channels) + len(segment_layers) + len(point_layers)) + 20)
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
        # out the wrong ones -- picking a bundle root or a segments folder is
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
    dock.setWidget(dock_widget)
    dock.setFeatures(
        QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetClosable
    )
    win.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)

    file_menu = win.menuBar().addMenu("File")
    # "Add", not "Open": the bundle is already open, and this puts another
    # image on top of it rather than replacing anything. The dialog takes
    # several folders at once, so no "(s)" is needed in the label.
    add_action = file_menu.addAction("Add Image…")
    add_action.triggered.connect(add_images)

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
        for key, kind, layer in _iter_layers(bundle):
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
        fit_camera_to_bundle()
        refresh_minimap()
        clear_session(bundle.root)
        print("reset to bundle defaults — session.json removed")

    menu.addSeparator()
    reset_action = menu.addAction("Reset to Bundle Defaults")
    reset_action.triggered.connect(reset_to_defaults)

    win.on_close = lambda: save_session(bundle.root, collect_session())

    saved_window = session.get("window")
    if saved_window:
        # After the dock exists: restoreState matches docks by objectName, and
        # a dock added later would come back at its default spot instead.
        win.restoreGeometry(QtCore.QByteArray.fromBase64(saved_window["geometry"].encode()))
        win.restoreState(QtCore.QByteArray.fromBase64(saved_window["state"].encode()))

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

        latest_stats_text[0] = "\n".join(lines) if lines else "—"

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
    print("window open — left-drag to pan, scroll to zoom, per-layer controls in the dock panel")
    loop.run()


if __name__ == "__main__":
    main()
