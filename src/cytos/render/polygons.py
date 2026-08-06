"""Polygon-tile streaming: reads `cytos.prep.polygons`'s tiled zarr cache
(already triangulated, Hilbert-sorted) and keeps a GPU-resident LRU cache of
just the visible tiles as pygfx line objects -- the same camera ->
visible-tiles -> upload-new/evict-old loop as `cytos.render.image.TileCache`
(built once in Phase 0, reused here per work-notes/plan.md Phase 3).

Draws boundaries only, not filled cells -- the plan's own Phase 2 open
question ("triangulated outline strips, or fill + screen-space edge
shader?") settled in favor of the screen-space option, but even simpler:
`gfx.LineSegmentMaterial(thickness_space="screen")` already renders
constant-pixel-width edges without a custom shader, so this reads the
boundary ring straight back out of the cache's `coords` (still in each
cell's original ring order -- only *cell* order was permuted at prep time,
not the order of vertices within one cell) instead of `triangle_indices`,
which is only needed for a filled mesh nothing here draws.

Recoloring (Phase 3's "one trick that earns the whole project"): color never
lives in the vertex buffer. Each vertex carries its cell's dense id as a
texcoord into a color LUT texture, nearest-filtered so adjacent cells never
blend into each other at a shared boundary vertex -- the same
TextureMap(filter="nearest") pattern pygfx's own colormap textures use (see
`pygfx.utils.cm.registered_colormap`). Recoloring the whole dataset is one
texture upload, not touching any vertex data.
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
import pygfx as gfx

from cytos.core.polygons import PolygonTileGrid, visible_polygon_tile_keys

# 2D grid layout for the LUT (rather than a literal 1D texture) so it stays
# well under GPU texture-dimension limits even at whole-slide cell counts
# (millions) -- a 1D texture that wide wouldn't be allocatable.
_LUT_WIDTH = 2048


def _lut_shape(n_cells: int) -> tuple[int, int]:
    height = max(1, -(-n_cells // _LUT_WIDTH))  # ceil div
    return height, _LUT_WIDTH


class PolygonTileCache:
    """LRU cache of GPU-resident polygon tile boundary lines, sharing one
    color LUT texture across all of them."""

    def __init__(self, grid: PolygonTileGrid, max_tiles: int = 64, thickness: float = 1.5):
        self.grid = grid
        self.max_tiles = max_tiles
        self.thickness = thickness
        self._group = gfx.Group()
        self._cache: OrderedDict[tuple[int, int], gfx.Line] = OrderedDict()

        height, width = _lut_shape(grid.n_cells)
        self._lut_array = np.ones((height, width, 4), dtype=np.float32)
        self._lut_texture = gfx.Texture(self._lut_array, dim=2)
        self._lut_map = gfx.TextureMap(self._lut_texture, filter="nearest", wrap="clamp")

    @property
    def group(self) -> gfx.Group:
        return self._group

    def set_colors(self, colors: np.ndarray) -> None:
        """colors: (n_cells, 4) float32 RGBA in [0, 1], indexed by the same
        dense cell id as `cytos.core.polygons.load_polygons`'s `features`
        rows / `cytos.prep.polygons`'s `vertex_cell_id` -- the single
        recolor-by-cluster operation, independent of how many tiles/vertices
        that touches on screen."""
        height, width = self._lut_array.shape[:2]
        flat = self._lut_array.reshape(height * width, 4)
        flat[: len(colors)] = colors
        self._lut_texture.update_full()

    def _cell_id_to_uv(self, cell_ids: np.ndarray) -> np.ndarray:
        height, width = self._lut_array.shape[:2]
        col = (cell_ids % width).astype(np.float32)
        row = (cell_ids // width).astype(np.float32)
        uv = np.empty((len(cell_ids), 2), dtype=np.float32)
        uv[:, 0] = (col + 0.5) / width
        uv[:, 1] = (row + 0.5) / height
        return uv

    def _make_tile(self, row: int, col: int) -> gfx.Line:
        tile = self.grid.tile(row, col)
        coords = np.asarray(tile["coords"])
        cell_ids = np.asarray(tile["vertex_cell_id"])
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
        texcoords = np.repeat(self._cell_id_to_uv(cell_ids), 2, axis=0)

        geometry = gfx.Geometry(positions=positions, texcoords=texcoords)
        material = gfx.LineSegmentMaterial(
            thickness=self.thickness,
            thickness_space="screen",
            map=self._lut_map,
            color_mode="vertex_map",
        )
        return gfx.Line(geometry, material)

    def update(self, world_rect: tuple[float, float, float, float]) -> dict:
        needed = set(visible_polygon_tile_keys(self.grid, world_rect))

        fetched = 0
        for key in needed:
            if key not in self._cache:
                row, col = key
                self._cache[key] = self._make_tile(row, col)
                self._group.add(self._cache[key])
                fetched += 1
            else:
                self._cache.move_to_end(key)

        for key, mesh in self._cache.items():
            mesh.visible = key in needed

        evicted = 0
        while len(self._cache) > self.max_tiles:
            old_key, old_mesh = self._cache.popitem(last=False)
            if old_key in needed:
                # shouldn't happen if max_tiles comfortably covers one
                # screen's worth of tiles, but never evict something we need
                # this frame -- see cytos.render.image.TileCache.update
                self._cache[old_key] = old_mesh
                self._cache.move_to_end(old_key)
                break
            self._group.remove(old_mesh)
            evicted += 1

        return {
            "needed": len(needed),
            "fetched": fetched,
            "evicted": evicted,
            "cache_size": len(self._cache),
        }
