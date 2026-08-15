"""Transcript-point streaming: reads `cytos.prep.points`'s tiled cache
(Hilbert-sorted) and keeps a GPU-resident LRU cache of just the visible tiles
as pygfx point clouds -- the same camera -> visible-tiles -> upload-new/
evict-old loop as `cytos.render.image.TileCache` and
`cytos.render.polygons.PolygonTileCache`, plus two decisions per frame the
other caches don't make (see `cytos.prep.points` for the cache's side):

* **Which detail level.** Zoom decides, nothing else -- the same
  `select_scale` rule the image pyramid uses, fed the point ladder. Zoomed
  out, tiles come from an aggregate level: one weighted dot per (region,
  gene), sitting on one of that gene's real transcripts there and sized by
  sqrt(count). Genes never merge -- two genes in one region are two dots at
  their own real positions, so a multi-gene view stays interleaved at
  every zoom, the way it looks at full detail.
* **What the visible gene set means.** One state covers every case: the
  set of genes being shown (`None` stands for all of them). It never picks
  the level; it only changes which genes' runs a tile read returns --
  through the same per-tile gene index at every level. A one-gene
  whole-slide view is that gene's weighted dots, nothing else loaded.
  Changing the set reloads the visible tiles -- small reads by
  construction.

Points are drawn with `size_space="screen"`, so a transcript stays a
constant-pixel dot at every zoom instead of shrinking into nothing when you
zoom out -- the same reasoning as the polygon outline's screen-space
thickness.

Colour is per *gene*, through the shared `cytos.render.lut.IdColorLut`:
every point at every level carries its gene's dense id as a texcoord, so
switching between flat colour and colour-per-gene, or changing the palette,
is a rewrite of a LUT with at most a few thousand rows -- no tile reload at
any level. The LUT only ever *colours* -- what is shown is decided by what
is loaded, never by hiding entries.
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
import plotlet
import pygfx as gfx

from cytos.core.points import PointTile, PointTileGrid, select_point_level, visible_point_tile_keys
from cytos.render.image import colormap_lut_array
from cytos.render.lut import IdColorLut

# Above the polygon layer (fill 0.01, outline 0.02): a transcript sits *in* a
# cell, so it must not be hidden by that cell's fill.
_POINT_Z = 0.03

# Read optimisation only, never a behaviour boundary: a gene set up to this
# size loads full-detail tiles via the per-tile gene index (a few contiguous
# slices per tile); past it, slice reads stop being cheaper than reading the
# whole tile and filtering in memory. Either way the same points end up on
# screen.
GENE_SLICE_MAX = 256

# Aggregate dot sizes, in screen px before the layer's size scaling. The
# bin grid puts neighbouring dots ~3-6 px apart at the zooms a level serves,
# so the largest dot must stay near that spacing -- sized past it, dense
# tissue floods into one solid mass and the density reading is gone. Sizes
# spread sqrt(count / tile max) across this range: area tracks count (the
# same encoding Explorer's scaled view uses), normalised within the tile so
# hotspots stand out of dense tissue instead of everything saturating.
_AGG_SIZE_MIN = 1.2
_AGG_SIZE_MAX = 6.0

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
    """(n, 4) float32 RGBA per dense gene id. Genes outside `visible` are
    zeroed -- not to hide them (unselected genes are never loaded, let alone
    drawn) but simply because no colour is spent on them.

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
        # The LUT only colours -- genes outside the visible set are never
        # loaded, so there is nothing to hide here. In gene mode a selection
        # still ranks its palette colours (see `gene_colors`).
        if self.color_mode == COLOR_MODE_GENE:
            colors = gene_colors(self.n_genes, self.palette, self._visible_genes)
        else:
            colors = flat_colors(self.n_genes, self.colormap)
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
        """The visible gene set. `None` stands for every gene; an empty set
        is an ordinary value that shows none of them. A tile's geometry
        holds exactly the set's points (or its re-weighted aggregate dots),
        so a different set means different geometry: drop the cached tiles
        and let update() reload -- small reads by construction."""
        new = None if gene_ids is None else set(gene_ids)
        if new != self._visible_genes:
            self._visible_genes = new
            self._clear_tiles()
        self._refresh_colors()

    def _clear_tiles(self) -> None:
        for points in self._cache.values():
            if points is not None:
                self._group.remove(points)
        self._cache.clear()
        self._live = set()

    # -- appearance --------------------------------------------------------

    def set_size(self, size: float) -> None:
        self.size = float(size)
        for points in self._cache.values():
            if points is None:
                continue
            if points.material.size_mode == "vertex":
                # Aggregate sizes are baked per vertex from this base size;
                # cheapest correct move is a reload of the few coarse tiles.
                self._clear_tiles()
                return
            points.material.size = self.size

    def set_opacity(self, opacity: float) -> None:
        self.opacity = float(opacity)
        for points in self._cache.values():
            if points is not None:
                points.material.opacity = self.opacity

    # -- tiles -------------------------------------------------------------

    def _make_tile(self, depth: int, row: int, col: int) -> gfx.Points | None:
        sel = self._visible_genes
        if sel is not None and not sel:
            # The empty set: nothing to read, so don't.
            return None
        if sel is not None and len(sel) > GENE_SLICE_MAX:
            # Too many genes for slice reads to win: read the whole tile and
            # filter in memory. Same points on screen as the sliced path.
            whole = self.grid.tile(row, col, depth=depth)
            keep = np.isin(whole.gene_id, np.fromiter(sel, dtype=np.int64))
            tile = PointTile(
                coords=whole.coords[keep],
                gene_id=whole.gene_id[keep],
                count=None if whole.count is None else whole.count[keep],
                size=None if whole.size is None else whole.size[keep],
            )
        else:
            tile = self.grid.tile(row, col, depth=depth, genes=sel)
        if len(tile.coords) == 0:
            # A filtered read can come back empty (none of the set's genes in
            # this tile) -- cache the emptiness, pygfx can't hold a
            # zero-length buffer.
            return None

        positions = np.zeros((len(tile.coords), 3), dtype=np.float32)
        positions[:, :2] = tile.coords
        positions[:, 2] = _POINT_Z

        if tile.count is not None:
            # Weighted points (an aggregate level): the same LUT colouring
            # as everywhere else -- a dot is one gene's dot -- plus a
            # per-vertex size from its weight, normalised within the tile
            # so hotspots stand out of dense tissue.
            rel = np.sqrt(tile.count.astype(np.float32) / float(tile.count.max()))
            sizes = (
                (_AGG_SIZE_MIN + (_AGG_SIZE_MAX - _AGG_SIZE_MIN) * rel) * (self.size / 3.0)
            ).astype(np.float32)
            geometry = gfx.Geometry(
                positions=positions,
                texcoords=self._lut.uv(tile.gene_id),
                sizes=sizes,
            )
            material = gfx.PointsMaterial(
                size_space="screen",
                size_mode="vertex",
                map=self._lut.map,
                color_mode="vertex_map",
            )
        elif tile.size is not None:
            # Full detail from a source whose points carry their own size:
            # per-point multiplier on the layer's point size, still colored
            # per gene through the LUT.
            geometry = gfx.Geometry(
                positions=positions,
                texcoords=self._lut.uv(tile.gene_id),
                sizes=(tile.size * self.size).astype(np.float32),
            )
            material = gfx.PointsMaterial(
                size_space="screen",
                size_mode="vertex",
                map=self._lut.map,
                color_mode="vertex_map",
            )
        else:
            geometry = gfx.Geometry(positions=positions, texcoords=self._lut.uv(tile.gene_id))
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

    def update(
        self,
        world_rect: tuple[float, float, float, float],
        world_per_px: float | None = None,
    ) -> dict:
        level = select_point_level(self.grid.levels, world_per_px)
        depth = self.grid.tile_depth - level
        needed = {(depth, r, c) for r, c in visible_point_tile_keys(self.grid, world_rect, depth)}
        self._live = needed

        fetched = 0
        for key in needed:
            if key not in self._cache:
                points = self._make_tile(*key)
                self._cache[key] = points
                if points is not None:
                    self._group.add(points)
                fetched += 1
            else:
                self._cache.move_to_end(key)

        for key, points in self._cache.items():
            if points is not None:
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
            if old_points is not None:
                self._group.remove(old_points)
            evicted += 1

        return {
            "level": level,
            "needed": len(needed),
            "fetched": fetched,
            "evicted": evicted,
            "cache_size": len(self._cache),
        }
