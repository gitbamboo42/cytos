"""Zoom-driven OME-Zarr pyramid streaming: given a camera's current view
(center + world-units-per-screen-pixel), keeps a GPU-resident LRU cache of just
the visible chunks of the right resolution level as pygfx tiles — so opening a
huge whole-slide image doesn't mean uploading it all at once, and zooming out
doesn't mean rendering it at full resolution.

Builds on the pure data model in `cytos.core.image`.
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np
import plotlet
import pygfx as gfx

from cytos.core.image import PyramidLevel, visible_chunk_keys

# Fluorescence composite display wants pixel value 0 -> black, not white:
# plotlet's (matplotlib-derived) "Blues"/"Greens"/"Reds" run near-white ->
# dark-color, which washes an additive-blended composite's background to
# gray instead of black. Register the standard microscopy-viewer black->hue
# set instead (same names/purpose as napari's SIMPLE_COLORMAPS). Plotlet
# colormap registration is per-process, so this runs once at import time.
_COMPOSITE_HUES = {
    "blue": "#0000ff",
    "green": "#00ff00",
    "red": "#ff0000",
    "cyan": "#00ffff",
    "magenta": "#ff00ff",
    "yellow": "#ffff00",
}
for _name, _hex in _COMPOSITE_HUES.items():
    if _name not in plotlet.list_colormaps():
        plotlet.register_colormap(_name, ["#000000", _hex])

# Public: the registered names, in a stable order — for callers (e.g. the
# "Open Channel(s)..." file dialog) that need to auto-assign a colormap per
# newly loaded channel by cycling through them.
COMPOSITE_COLORMAPS = list(_COMPOSITE_HUES.keys())

# The channel color picker's presets, (name, "#rrggbb") pairs: the classic
# fluorescence six first, then white, then the rest of the 30-degree hue
# wheel plus gray. A channel's `colormap` value may also be any "#rrggbb"
# of the user's own.
CHANNEL_COLOR_PRESETS = [
    ("Blue", "#0000ff"),
    ("Green", "#00ff00"),
    ("Red", "#ff0000"),
    ("Cyan", "#00ffff"),
    ("Magenta", "#ff00ff"),
    ("Yellow", "#ffff00"),
    ("White", "#ffffff"),
    ("Orange", "#ff8000"),
    ("Chartreuse", "#80ff00"),
    ("Spring green", "#00ff80"),
    ("Azure", "#0080ff"),
    ("Purple", "#8000ff"),
    ("Rose", "#ff0080"),
    ("Gray", "#808080"),
]


def normalize_channel_color(value: str) -> str:
    """A channel's `colormap` value, canonicalised: "#rrggbb" lowercased, a
    black->hue name replaced by its hex (they are the same ramp), any other
    colormap name kept as-is."""
    if value.startswith("#"):
        return value.lower()
    return _COMPOSITE_HUES.get(value, value)


def channel_color_hex(value: str) -> str:
    """The single color a channel's `colormap` value stands for — a "#rrggbb"
    as itself, a black->hue name as its hue, any other colormap as its top
    color. What seeds the custom color dialog."""
    if value.startswith("#"):
        return value.lower()
    if value in _COMPOSITE_HUES:
        return _COMPOSITE_HUES[value]
    top = colormap_lut_array(value)[-1, :3]
    return "#" + "".join(f"{int(round(float(c) * 255)):02x}" for c in top)

# Curated defaults for the channel colormap picker: the black->hue composite
# set above (plus plotlet's built-in "gray", already black->white) first,
# since composite fluorescence display is cytos's primary use case, then a
# handful of perceptual colormaps for single-channel/scientific viewing.
# napari's own default dropdown is a similarly curated ~30 out of its full
# catalog, not everything at once — see skills/developers.md.
CURATED_COLORMAPS = [
    *_COMPOSITE_HUES.keys(),
    "gray",
    "viridis",
    "magma",
    "plasma",
    "inferno",
    "cividis",
]

# The picker's ramp section: real colormaps a channel may use instead of a
# color — the curated perceptual set, i.e. everything curated that isn't
# just a black->hue ramp (those are the preset *colors*).
CHANNEL_RAMP_CHOICES = [n for n in CURATED_COLORMAPS if n not in _COMPOSITE_HUES]


def colormap_lut_array(name: str) -> np.ndarray:
    """(256, 4) float32 RGBA in [0, 1] for a named plotlet colormap, or the
    black->color ramp of a "#rrggbb" — the same array backs both the GPU
    texture here and the CPU minimap thumbnail (`cytos.ui.minimap`), so both
    read one LUT, not two independently-tinted ramps that could drift
    apart."""
    if name.startswith("#"):
        color = np.array([int(name[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float32) / 255.0
        lut = np.ones((256, 4), dtype=np.float32)
        lut[:, :3] = np.linspace(0.0, 1.0, 256, dtype=np.float32)[:, None] * color
        return lut
    raw = plotlet.draw.colormap_lut(name)  # 768 bytes: 256 RGB uint8 triples
    rgb = np.frombuffer(raw, dtype=np.uint8).reshape(256, 3).astype(np.float32) / 255.0
    lut = np.ones((256, 4), dtype=np.float32)
    lut[:, :3] = rgb
    return lut


# Additive: dst = src*1 + dst*1, so overlapping channels sum (blue + green =
# cyan) instead of the later one just overwriting the earlier one.
_ADDITIVE_ALPHA_CONFIG = {
    "mode": "custom",
    "method": "blended",
    "color_op": "add",
    "color_src": "one",
    "color_dst": "one",
    "alpha_op": "add",
    "alpha_src": "one",
    "alpha_dst": "one",
}


class TileCache:
    """LRU cache of GPU-resident image tiles, added to / removed from a pygfx
    scene as the camera moves. Reads chunks lazily straight from the zarr
    pyramid — only what's actually visible gets pulled off disk.

    `colormap`, if given (a name from `plotlet.list_colormaps()`), maps this
    channel's intensity through that colormap and blends it additively with
    whatever else is in the scene, for multi-channel composite display. Leave
    as None for a plain grayscale single-channel layer.
    """

    def __init__(
        self,
        levels: list[PyramidLevel],
        clim: tuple[float, float],
        max_tiles: int = 64,
        colormap: str | None = None,
    ):
        self.levels = levels
        self.clim = clim
        self.max_tiles = max_tiles
        self.colormap = colormap
        self.lut_array = colormap_lut_array(colormap) if colormap is not None else None
        self._lut = gfx.Texture(self.lut_array, dim=1) if self.lut_array is not None else None
        self._group = gfx.Group()
        self._cache: OrderedDict[tuple[int, int, int], gfx.Image] = OrderedDict()

    @property
    def group(self) -> gfx.Group:
        return self._group

    def set_clim(self, clim: tuple[float, float]) -> None:
        self.clim = clim
        for image in self._cache.values():
            image.material.clim = clim

    def set_colormap(self, colormap: str) -> None:
        self.colormap = colormap
        self.lut_array = colormap_lut_array(colormap)
        self._lut = gfx.Texture(self.lut_array, dim=1)
        for image in self._cache.values():
            image.material.map = self._lut
            image.material.alpha_config = _ADDITIVE_ALPHA_CONFIG

    def _make_tile(self, level: PyramidLevel, cy: int, cx: int) -> gfx.Image:
        h, w = level.shape
        ch, cw = level.chunk_shape
        y0, y1 = cy * ch, min((cy + 1) * ch, h)
        x0, x1 = cx * cw, min((cx + 1) * cw, w)
        chunk = np.asarray(level.data[y0:y1, x0:x1]).astype(np.float32)

        texture = gfx.Texture(chunk, dim=2)
        material = gfx.ImageBasicMaterial(clim=self.clim, map=self._lut)
        if self.colormap is not None:
            material.alpha_config = _ADDITIVE_ALPHA_CONFIG
        image = gfx.Image(gfx.Geometry(grid=texture), material)
        _, sx = level.scale
        _, tx = level.translation
        # Plain positive scale: world Y and pixel rows run the same way, so
        # local row 0 anchors at its world_y and increasing local row increases
        # world_y — see PyramidLevel.world_bounds. The one flip that puts row 0
        # at the top of the screen is on the camera, not here.
        image.local.scale = (sx, level.scale[0], 1)
        image.local.position = (tx + x0 * sx, level.row_to_world_y(y0), 0)
        return image

    def update(self, level_idx: int, world_rect: tuple[float, float, float, float]) -> dict:
        level = self.levels[level_idx]
        needed = {(level_idx, cy, cx) for cy, cx in visible_chunk_keys(level, world_rect)}

        fetched = 0
        for key in needed:
            if key not in self._cache:
                _, cy, cx = key
                self._cache[key] = self._make_tile(level, cy, cx)
                self._group.add(self._cache[key])
                fetched += 1
            else:
                self._cache.move_to_end(key)

        for key, image in self._cache.items():
            image.visible = key in needed

        evicted = 0
        while len(self._cache) > self.max_tiles:
            old_key, old_image = self._cache.popitem(last=False)
            if old_key in needed:
                # shouldn't happen if max_tiles comfortably covers one screen's
                # worth of tiles, but never evict something we need this frame
                self._cache[old_key] = old_image
                self._cache.move_to_end(old_key)
                break
            self._group.remove(old_image)
            evicted += 1

        return {
            "level": level_idx,
            "needed": len(needed),
            "fetched": fetched,
            "evicted": evicted,
            "cache_size": len(self._cache),
        }
