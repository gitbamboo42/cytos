"""Transcript-point streaming: reads `cytos.prep.points`'s tiled zarr cache
(Hilbert-sorted) and keeps a GPU-resident LRU cache of just the visible tiles
as pygfx point clouds -- the same camera -> visible-tiles -> upload-new/
evict-old loop as `cytos.render.image.TileCache` and
`cytos.render.polygons.PolygonTileCache`.

Points are drawn with `size_space="screen"`, so a transcript stays a
constant-pixel dot at every zoom instead of shrinking into nothing when you
zoom out -- the same reasoning as the polygon outline's screen-space
thickness.

Colour is per *gene*, through the shared `cytos.render.lut.IdColorLut`: every
point carries its gene's dense id as a texcoord, so all of

* switching between one flat colour and a colour per gene,
* changing the palette, and
* showing or hiding individual genes

are the same operation -- rewrite a LUT with at most a few thousand rows and
upload it once. None of them touch point geometry, which is what makes gene
filtering usable on a whole-slide run of tens of millions of transcripts.
Hiding is alpha 0 in the LUT rather than a separate visibility flag, since
there is no per-gene object to flip: one tile's points are one draw call
covering many genes.
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
import plotlet
import pygfx as gfx

from cytos.core.points import PointTileGrid, visible_point_tile_keys
from cytos.render.image import colormap_lut_array
from cytos.render.lut import IdColorLut

# Above the polygon layer (fill 0.01, outline 0.02): a transcript sits *in* a
# cell, so it must not be hidden by that cell's fill.
_POINT_Z = 0.03

# Qualitative palettes, for colour-per-gene. tab10 is the default because the
# common case is a handful of selected genes taking the first few entries, and
# every one of tab10's ten is a different hue. tab20 and Paired hold twice as
# many colours but alternate light/dark pairs of the *same* hue, so picking two
# genes there would hand them two shades of blue -- fine when all 400 genes are
# shown and colours are only separating neighbours, bad for a selection.
# "colorblind" is here because a many-gene figure is exactly the kind that
# needs it.
CURATED_PALETTES = ["tab10", "colorblind", "Set1", "Dark2", "Set2", "tab20", "Paired", "Set3"]
DEFAULT_PALETTE = "tab10"

# Bright and not blue: the morphology channel underneath is usually DAPI shown
# in blue, so a blue default would sink into the nuclei it's meant to sit on.
DEFAULT_COLORMAP = "yellow"

COLOR_MODE_FLAT = "flat"
COLOR_MODE_GENE = "gene"


def _hex_to_rgba(value: str) -> np.ndarray:
    value = value.lstrip("#")
    rgba = np.ones(4, dtype=np.float32)
    rgba[:3] = [int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    return rgba


def _palette_array(palette: str) -> np.ndarray:
    colors = [_hex_to_rgba(c) for c in plotlet.palette(palette)]
    if not colors:
        colors = [np.ones(4, dtype=np.float32)]
    return np.asarray(colors, dtype=np.float32)


def gene_colors(
    n: int,
    palette: str = DEFAULT_PALETTE,
    visible: set[int] | None = None,
) -> np.ndarray:
    """(n, 4) float32 RGBA per dense gene id, with hidden genes at alpha 0.

    The palette is spent on the genes actually being *looked at*, not on the
    whole panel. Showing everything (`visible=None`) can only cycle the palette
    over gene ids -- 400 genes outrun any qualitative palette, and the goal
    there is just telling neighbours apart. But the useful view is two or three
    genes at once, and cycling by id would hand those whichever colours their
    alphabetical positions happened to land on, which is regularly two shades
    of the same hue (tab20 alternates light/dark pairs). So a selection gets
    palette entries by rank instead: first selected gene gets the first colour,
    second the second, and they stay clearly distinct.
    """
    pal = _palette_array(palette)
    if visible is None:
        return pal[np.arange(n) % len(pal)]

    colors = np.zeros((n, 4), dtype=np.float32)
    for rank, gene_id in enumerate(sorted(visible)):
        if 0 <= gene_id < n:
            colors[gene_id] = pal[rank % len(pal)]
    return colors


def flat_colors(n: int, colormap: str = DEFAULT_COLORMAP) -> np.ndarray:
    """(n, 4) float32 RGBA, every gene the same colour -- the top of
    `colormap`, the same convention the segment layer uses so "flat colour"
    needs no separate colour-picker widget."""
    return np.repeat(colormap_lut_array(colormap)[-1:], n, axis=0)


class PointTileCache:
    """LRU cache of GPU-resident transcript tiles, all sharing one per-gene
    colour LUT texture."""

    def __init__(
        self,
        grid: PointTileGrid,
        max_tiles: int = 64,
        size: float = 3.0,
        opacity: float = 0.9,
        color_mode: str = COLOR_MODE_GENE,
        colormap: str = DEFAULT_COLORMAP,
        palette: str = DEFAULT_PALETTE,
    ):
        self.grid = grid
        self.max_tiles = max_tiles
        self.size = size
        self.opacity = opacity
        self.color_mode = color_mode
        self.colormap = colormap
        self.palette = palette

        self.n_genes = max(len(grid.gene_names), 1)
        # None means "every gene", kept distinct from "all of them happen to be
        # checked" so a cache with no gene table still draws.
        self._visible_genes: set[int] | None = None

        self._group = gfx.Group()
        self._cache: OrderedDict[tuple[int, int], gfx.Points] = OrderedDict()
        self._live: set[tuple[int, int]] = set()

        self._lut = IdColorLut(self.n_genes)
        self._refresh_colors()

    @property
    def group(self) -> gfx.Group:
        return self._group

    # -- color -------------------------------------------------------------

    def _refresh_colors(self) -> None:
        # Alpha 0 rather than a removed vertex, in both modes: hiding a gene
        # must not cost a geometry rebuild of every visible tile.
        if self.color_mode == COLOR_MODE_GENE:
            colors = gene_colors(self.n_genes, self.palette, self._visible_genes)
        else:
            colors = flat_colors(self.n_genes, self.colormap).copy()
            if self._visible_genes is not None:
                hidden = np.ones(self.n_genes, dtype=bool)
                hidden[[g for g in self._visible_genes if 0 <= g < self.n_genes]] = False
                colors[hidden, 3] = 0.0
        self._lut.set_colors(colors)

    def set_color_mode(self, mode: str) -> None:
        self.color_mode = mode
        self._refresh_colors()

    def set_colormap(self, colormap: str) -> None:
        self.colormap = colormap
        self._refresh_colors()

    def set_palette(self, palette: str) -> None:
        self.palette = palette
        self._refresh_colors()

    def set_visible_genes(self, gene_ids: set[int] | None) -> None:
        """`None` shows every gene. An empty set hides them all -- which is a
        real state the UI can be in ("uncheck all"), not an error."""
        self._visible_genes = None if gene_ids is None else set(gene_ids)
        self._refresh_colors()

    # -- appearance --------------------------------------------------------

    def set_size(self, size: float) -> None:
        self.size = float(size)
        for points in self._cache.values():
            points.material.size = self.size

    def set_opacity(self, opacity: float) -> None:
        self.opacity = float(opacity)
        for points in self._cache.values():
            points.material.opacity = self.opacity

    # -- tiles -------------------------------------------------------------

    def _make_tile(self, row: int, col: int) -> gfx.Points:
        tile = self.grid.tile(row, col)
        coords = np.asarray(tile["coords"])
        gene_ids = np.asarray(tile["gene_id"])

        positions = np.zeros((len(coords), 3), dtype=np.float32)
        positions[:, :2] = coords
        positions[:, 2] = _POINT_Z

        geometry = gfx.Geometry(positions=positions, texcoords=self._lut.uv(gene_ids))
        material = gfx.PointsMaterial(
            size=self.size,
            size_space="screen",
            map=self._lut.map,
            color_mode="vertex_map",
        )
        material.opacity = self.opacity
        # Same reason as the polygon fill: the image tiles below blend
        # *additively* (see cytos.render.image), and "auto" would add points to
        # that sum instead of drawing them over it. Also what makes an alpha-0
        # LUT entry actually disappear rather than draw as a black dot.
        material.alpha_mode = "blend"
        return gfx.Points(geometry, material)

    def update(self, world_rect: tuple[float, float, float, float]) -> dict:
        needed = set(visible_point_tile_keys(self.grid, world_rect))
        self._live = needed

        fetched = 0
        for key in needed:
            if key not in self._cache:
                row, col = key
                self._cache[key] = self._make_tile(row, col)
                self._group.add(self._cache[key])
                fetched += 1
            else:
                self._cache.move_to_end(key)

        for key, points in self._cache.items():
            points.visible = key in needed

        evicted = 0
        while len(self._cache) > self.max_tiles:
            old_key, old_points = self._cache.popitem(last=False)
            if old_key in needed:
                # shouldn't happen if max_tiles comfortably covers one screen's
                # worth of tiles, but never evict something we need this frame
                self._cache[old_key] = old_points
                self._cache.move_to_end(old_key)
                break
            self._group.remove(old_points)
            evicted += 1

        return {
            "needed": len(needed),
            "fetched": fetched,
            "evicted": evicted,
            "cache_size": len(self._cache),
        }
