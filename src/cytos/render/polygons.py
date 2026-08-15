"""Polygon-tile streaming: reads `cytos.prep.polygons`'s tiled cache
(already triangulated, Hilbert-sorted) and keeps a GPU-resident LRU cache of
just the visible tiles as pygfx objects -- the same camera ->
visible-tiles -> upload-new/evict-old loop as `cytos.render.image.TileCache`,
built once for image tiles and reused here.

Each tile draws as two objects that can be shown independently:

* **outline** -- `gfx.LineSegmentMaterial(thickness_space="screen")` gives
  constant-pixel-width edges with no custom shader, so the boundary ring is
  read straight back out of the cache's `coords` (still in each cell's
  original ring order -- only *cell* order was permuted at prep time, not the
  order of vertices within one cell).
* **fill** -- a `gfx.Mesh` over the cache's `triangle_indices`, the earcut
  triangulation `cytos.prep.polygons` already computed offline. Semi-
  transparent (`fill_opacity`) so the morphology image underneath stays
  readable. Outline and fill both come off the same cached tile, so neither
  needs its own preprocessed geometry.

Recoloring (the one trick that earns the whole project): color never
lives in the vertex buffer. Each vertex carries its cell's dense id as a
texcoord into a color LUT texture, nearest-filtered so adjacent cells never
blend into each other at a shared boundary vertex -- the same
TextureMap(filter="nearest") pattern pygfx's own colormap textures use (see
`pygfx.utils.cm.registered_colormap`). Outline and fill sample the *same* LUT,
so recoloring the whole dataset by a per-cell feature is one texture upload,
not touching any vertex data on either object.

Picking rides pygfx's own pick buffer rather than a second render
pass: both materials set `pick_write`, and `pick_cell` turns a pick-info dict
(mesh face index, or line vertex index) back into a dense cell id via small
per-tile lookup arrays. A *shown* layer answers anywhere inside a cell even
with the fill switched off -- an off fill draws at opacity 0 rather than
being hidden, so interiors keep writing the pick buffer (pygfx picks fully
transparent meshes; verified). Truly hidden things stay silent: a hidden
layer renders nothing, and a cell whose LUT alpha is 0 (a hidden category)
is refused in `pick_cell` itself.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pygfx as gfx

from cytos.core.polygons import PolygonTileGrid, is_categorical_feature, visible_polygon_tile_keys
from cytos.render.image import colormap_lut_array
from cytos.render.lut import IdColorLut
from cytos.render.points import hex_to_rgba, palette_array

# Both layers sit just in front of the image tiles (z=0) so neither depends on
# scene insertion order to land on top, and the outline sits in front of its
# own fill. World units, and tiny: an orthographic camera's depth range spans
# the whole scene, so this costs nothing visually.
_FILL_Z = 0.01
_OUTLINE_Z = 0.02

# Percentiles, not min/max, for the feature -> color range. Cell measurements
# are heavy-tailed the same way the fluorescence channels are (cell_area on the
# breast-cancer slide: median 140, 98th pct 780, max 1533), and a raw max
# would push a typical cell down into the bottom tenth of the colormap.
_FEATURE_PERCENTILES = (2.0, 98.0)

# Not white -- an all-white LUT (what this used to ship) is exactly the color
# the DAPI/morphology image saturates to, so boundaries disappeared into the
# bright parts of the image.
DEFAULT_COLORMAP = "viridis"


def feature_colors(values: np.ndarray, colormap: str) -> np.ndarray:
    """(n, 4) float32 RGBA: each cell's feature value mapped through `colormap`
    over its own robust range. Cells with no value (nulls from the left-join in
    `cytos.core.polygons.polygons_from_parquet`) land at the bottom of the ramp rather
    than dropping out of the LUT entirely."""
    lut = colormap_lut_array(colormap)  # (256, 4)
    v = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(v)
    if not finite.any():
        return np.repeat(lut[:1], len(v), axis=0)

    lo, hi = np.percentile(v[finite], _FEATURE_PERCENTILES)
    if hi <= lo:
        lo, hi = float(v[finite].min()), float(v[finite].max())
    if hi <= lo:
        return np.repeat(lut[-1:], len(v), axis=0)

    norm = np.zeros(len(v), dtype=np.float64)
    norm[finite] = np.clip((v[finite] - lo) / (hi - lo), 0.0, 1.0)
    return lut[(norm * 255).astype(np.uint8)]


# Categorical features (a clustering, not a measurement) get a qualitative
# palette, not a ramp: a ramp's order would imply cluster 7 is "more" than
# cluster 2. tab20 as the default because clusterings regularly exceed ten
# categories and here the palette only separates neighbours, the same
# reason the point layer allows it for many-gene views. Colour comes
# straight from the category number, so a category keeps its colour across
# layers, slides and sessions -- until a session overrides it, which is
# the user's "make it pretty" knob and lives in the session, never in the
# slide's data.
DEFAULT_CATEGORY_PALETTE = "tab20"
# Unassigned (null) objects: dim gray, visible but clearly not a category.
_UNASSIGNED_RGBA = (0.45, 0.45, 0.45, 1.0)


def _category_mask(values: np.ndarray, key: str) -> np.ndarray:
    """Which cells belong to category `key` -- a number as a string, or
    "unassigned" for cells absent from the feature (NaN)."""
    if key == "unassigned":
        return ~np.isfinite(values)
    return values == float(key)


def category_colors(
    values: np.ndarray,
    palette: str = DEFAULT_CATEGORY_PALETTE,
    overrides: dict[str, str] | None = None,
    hidden: set[str] | None = None,
) -> np.ndarray:
    """(n, 4) float32 RGBA for categorical features: `values` is the
    per-cell category number as float, NaN where a cell is unassigned.
    `overrides` maps a category key to a "#rrggbb" of the user's own
    choosing on top of the palette; `hidden` names categories drawn not at
    all (alpha 0 -- for a monolithic polygon tile, the LUT is the only
    per-cell hiding mechanism there is)."""
    pal = palette_array(palette)
    colors = np.empty((len(values), 4), dtype=np.float32)
    colors[:] = _UNASSIGNED_RGBA
    assigned = np.isfinite(values)
    colors[assigned] = pal[values[assigned].astype(np.int64) % len(pal)]
    for key, hex_color in (overrides or {}).items():
        colors[_category_mask(values, key)] = hex_to_rgba(hex_color)
    for key in hidden or ():
        colors[_category_mask(values, key), 3] = 0.0
    return colors


def flat_colors(n: int, colormap: str) -> np.ndarray:
    """(n, 4) float32 RGBA, every cell the same color -- the top of `colormap`.
    Reusing the colormap's own high end means "flat color" needs no second
    color-picker widget: black->cyan gives cyan, viridis gives its yellow."""
    return np.repeat(colormap_lut_array(colormap)[-1:], n, axis=0)


@dataclass
class _Tile:
    outline: gfx.Line
    fill: gfx.Mesh | None
    # Pick lookups: dense cell id per outline *position* (2 per vertex, see
    # `_make_outline`'s interleaving) and per fill *triangle* -- what turns a
    # pick-info index back into a cell.
    outline_cells: np.ndarray
    fill_cells: np.ndarray | None


class PolygonTileCache:
    """LRU cache of GPU-resident polygon tiles, each drawn as a screen-space
    outline and/or a filled mesh, all sharing one per-cell color LUT texture.

    `colormap` picks the ramp; `color_by` picks which column of the cache's
    per-cell `features` table drives it (None = one flat color for every cell).
    """

    def __init__(
        self,
        grid: PolygonTileGrid,
        max_tiles: int = 64,
        thickness: float = 1.5,
        colormap: str = DEFAULT_COLORMAP,
        color_by: str | None = None,
        show_outline: bool = True,
        show_fill: bool = False,
        fill_opacity: float = 0.35,
        palette: str = DEFAULT_CATEGORY_PALETTE,
        category_overrides: dict[str, dict[str, str]] | None = None,
        category_hidden: dict[str, list[str]] | None = None,
    ):
        self.grid = grid
        self.max_tiles = max_tiles
        self.thickness = thickness
        self.colormap = colormap
        self.color_by = color_by
        self.show_outline = show_outline
        self.show_fill = show_fill
        self.fill_opacity = fill_opacity
        self.palette = palette
        # feature -> {category key -> "#rrggbb"} and feature -> hidden keys:
        # kept per feature, so tweaks to "cluster" survive switching
        # color_by to "kmeans_5" and back. Category keys are strings ("7",
        # "unassigned"), matching what the session stores.
        self.category_overrides = dict(category_overrides or {})
        self.category_hidden = dict(category_hidden or {})

        self._group = gfx.Group()
        self._cache: OrderedDict[tuple[int, int], _Tile] = OrderedDict()
        # Tiles the last `update` decided are on screen. A visibility toggle
        # must not un-hide a cached-but-off-screen tile, and can arrive before
        # the first update (the panel is built before the first frame draws).
        self._live: set[tuple[int, int]] = set()
        # pick object -> its tile's cell lookup array (see `pick_cell`).
        self._cell_of: dict[gfx.WorldObject, np.ndarray] = {}
        # Which cells may pick at all -- False where the LUT drew alpha 0
        # (a hidden category): what's invisible shouldn't answer a hover.
        self._pickable: np.ndarray | None = None

        self._lut = IdColorLut(grid.n_cells)
        self._refresh_colors()

    @property
    def group(self) -> gfx.Group:
        return self._group

    # -- color -------------------------------------------------------------

    def set_colors(self, colors: np.ndarray) -> None:
        """colors: (n_cells, 4) float32 RGBA in [0, 1], indexed by the same
        dense cell id as `cytos.core.polygons.polygons_from_parquet`'s `features`
        rows / `cytos.prep.polygons`'s `vertex_cell_id` -- the single
        recolor-by-feature operation, independent of how many tiles/vertices
        that touches on screen."""
        self._lut.set_colors(colors)
        self._pickable = colors[:, 3] > 0.0

    def _refresh_colors(self) -> None:
        n = self.grid.n_cells
        features = self.grid.features
        if self.color_by is not None and features is not None and self.color_by in features.column_names:
            values = features.column(self.color_by).to_numpy(zero_copy_only=False).astype(np.float64)
            if is_categorical_feature(features.schema.field(self.color_by)):
                # Marked categorical at import (see cytos.core.polygons.
                # join_categories): palette, not ramp -- no name matching.
                overrides = self.category_overrides.get(self.color_by, {})
                hidden = set(self.category_hidden.get(self.color_by, ()))
                self.set_colors(category_colors(values, self.palette, overrides, hidden))
            else:
                self.set_colors(feature_colors(values, self.colormap))
        else:
            self.set_colors(flat_colors(n, self.colormap))

    def set_colormap(self, colormap: str) -> None:
        self.colormap = colormap
        self._refresh_colors()

    def set_color_by(self, color_by: str | None) -> None:
        self.color_by = color_by
        self._refresh_colors()

    def set_palette(self, palette: str) -> None:
        self.palette = palette
        self._refresh_colors()

    def set_category_overrides(self, overrides: dict[str, dict[str, str]]) -> None:
        """Replace the whole feature -> {category key -> hex} mapping (the
        panel owns the editing; this cache just displays it)."""
        self.category_overrides = dict(overrides or {})
        self._refresh_colors()

    def set_category_hidden(self, hidden: dict[str, list[str]]) -> None:
        """Replace the whole feature -> hidden category keys mapping."""
        self.category_hidden = dict(hidden or {})
        self._refresh_colors()

    # -- picking -----------------------------------------------------------

    def pick_cell(self, pick_info: dict) -> int | None:
        """The dense cell id behind a renderer pick-info dict, or None when
        the picked object isn't one of this cache's tiles (or the cell is
        hidden). The dict comes from `renderer.get_pick_info` or a pointer
        event's `pick_info` -- pygfx already picks on every pointer event to
        route it, so hover costs no extra readback."""
        cells = self._cell_of.get(pick_info.get("world_object"))
        if cells is None:
            return None
        index = pick_info.get("face_index")
        if index is None:
            index = pick_info.get("vertex_index")
        if index is None or not 0 <= index < len(cells):
            return None
        cell = int(cells[index])
        if self._pickable is not None and not self._pickable[cell]:
            return None
        return cell

    # -- what's drawn ------------------------------------------------------

    def set_outline_visible(self, visible: bool) -> None:
        self.show_outline = visible
        for key, tile in self._cache.items():
            tile.outline.visible = visible and self._is_live(key)

    def set_fill_visible(self, visible: bool) -> None:
        # An off fill is opacity 0, not visible=False: the fill mesh doubles
        # as the pick surface, and hover inside a cell must keep answering
        # when only outlines are showing.
        self.show_fill = visible
        for tile in self._cache.values():
            if tile.fill is not None:
                tile.fill.material.opacity = self._fill_opacity()

    def set_fill_opacity(self, opacity: float) -> None:
        self.fill_opacity = float(opacity)
        for tile in self._cache.values():
            if tile.fill is not None:
                tile.fill.material.opacity = self._fill_opacity()

    def _fill_opacity(self) -> float:
        return self.fill_opacity if self.show_fill else 0.0

    # -- tiles -------------------------------------------------------------

    def _make_outline(self, coords: np.ndarray, cell_ids: np.ndarray) -> gfx.Line:
        v = len(coords)

        # "Next vertex within the same cell's ring" for every vertex, wrapping
        # each cell's own last vertex back to its own first -- one edge per
        # vertex, no separate strip-per-cell bookkeeping needed at draw time.
        run_start = np.empty(v, dtype=bool)
        run_start[0] = True
        run_start[1:] = cell_ids[1:] != cell_ids[:-1]
        run_starts = np.flatnonzero(run_start)
        run_ends = np.append(run_starts[1:], v)
        run_start_of = np.repeat(run_starts, run_ends - run_starts)
        is_last = np.zeros(v, dtype=bool)
        is_last[run_ends - 1] = True
        next_idx = np.arange(v) + 1
        next_idx[is_last] = run_start_of[is_last]

        # LineSegmentMaterial draws each consecutive *pair* of positions as
        # one independent segment -- interleave (vertex, next-vertex) so ring
        # edges never connect across cells.
        positions = np.zeros((2 * v, 3), dtype=np.float32)
        positions[0::2, :2] = coords
        positions[1::2, :2] = coords[next_idx]
        positions[:, 2] = _OUTLINE_Z
        texcoords = np.repeat(self._lut.uv(cell_ids), 2, axis=0)

        geometry = gfx.Geometry(positions=positions, texcoords=texcoords)
        material = gfx.LineSegmentMaterial(
            thickness=self.thickness,
            thickness_space="screen",
            map=self._lut.map,
            color_mode="vertex_map",
        )
        material.pick_write = True
        return gfx.Line(geometry, material)

    def _make_fill(self, coords: np.ndarray, cell_ids: np.ndarray, indices: np.ndarray) -> gfx.Mesh | None:
        if len(indices) < 3:
            return None
        positions = np.zeros((len(coords), 3), dtype=np.float32)
        positions[:, :2] = coords
        positions[:, 2] = _FILL_Z

        geometry = gfx.Geometry(
            positions=positions,
            indices=indices.reshape(-1, 3).astype(np.uint32),
            texcoords=self._lut.uv(cell_ids),
        )
        material = gfx.MeshBasicMaterial(map=self._lut.map, color_mode="vertex_map")
        material.opacity = self._fill_opacity()
        material.pick_write = True
        # Explicit over-operator blending rather than the "auto" default: the
        # image tiles below use a custom *additive* alpha config (see
        # cytos.render.image), and "blend" composites the fill over whatever
        # they produced instead of adding to it -- which would wash a bright
        # region out rather than tint it. depth_write off (the "blend" default)
        # keeps the fill from occluding its own outline.
        material.alpha_mode = "blend"
        return gfx.Mesh(geometry, material)

    def _make_tile(self, row: int, col: int) -> _Tile:
        tile = self.grid.tile(row, col)

        # Both objects are built up front, even when one is switched off:
        # toggling fill/outline then costs nothing, where building lazily
        # would stall on a tile read plus an upload for every visible tile at
        # the moment the checkbox is clicked.
        made = _Tile(
            outline=self._make_outline(tile.coords, tile.vertex_cell_id),
            fill=self._make_fill(tile.coords, tile.vertex_cell_id, tile.triangle_indices),
            # A line pick reports a *position* index (2 per vertex, from the
            # interleaving), a mesh pick a *face* index; each gets an array
            # in its own index space.
            outline_cells=np.repeat(tile.vertex_cell_id, 2),
            fill_cells=tile.vertex_cell_id[tile.triangle_indices.reshape(-1, 3)[:, 0]]
            if len(tile.triangle_indices) >= 3
            else None,
        )
        self._cell_of[made.outline] = made.outline_cells
        if made.fill is not None:
            self._cell_of[made.fill] = made.fill_cells
        return made

    def _is_live(self, key: tuple[int, int]) -> bool:
        return key in self._live

    def update(self, world_rect: tuple[float, float, float, float]) -> dict:
        needed = set(visible_polygon_tile_keys(self.grid, world_rect))
        self._live = needed

        fetched = 0
        for key in needed:
            if key not in self._cache:
                row, col = key
                tile = self._make_tile(row, col)
                self._cache[key] = tile
                self._group.add(tile.outline)
                if tile.fill is not None:
                    self._group.add(tile.fill)
                fetched += 1
            else:
                self._cache.move_to_end(key)

        for key, tile in self._cache.items():
            live = key in needed
            tile.outline.visible = live and self.show_outline
            if tile.fill is not None:
                # Live fills always render (at opacity 0 when switched off)
                # so cell interiors keep answering picks -- see set_fill_visible.
                tile.fill.visible = live

        evicted = 0
        while len(self._cache) > self.max_tiles:
            old_key, old_tile = self._cache.popitem(last=False)
            if old_key in needed:
                # shouldn't happen if max_tiles comfortably covers one
                # screen's worth of tiles, but never evict something we need
                # this frame -- see cytos.render.image.TileCache.update
                self._cache[old_key] = old_tile
                self._cache.move_to_end(old_key)
                break
            self._group.remove(old_tile.outline)
            self._cell_of.pop(old_tile.outline, None)
            if old_tile.fill is not None:
                self._group.remove(old_tile.fill)
                self._cell_of.pop(old_tile.fill, None)
            evicted += 1

        return {
            "needed": len(needed),
            "fetched": fetched,
            "evicted": evicted,
            "cache_size": len(self._cache),
        }
