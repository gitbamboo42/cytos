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
import pygfx as gfx

from cytos.core.image import PyramidLevel, visible_chunk_keys


def _make_color_lut(color: tuple[float, float, float]) -> gfx.Texture:
    """Black -> `color` linear ramp, the standard trick for tinting a grayscale
    channel for multi-channel composite display (what Fiji/napari do)."""
    ramp = np.linspace(0, 1, 256, dtype=np.float32)
    lut = np.zeros((256, 4), dtype=np.float32)
    lut[:, 0] = ramp * color[0]
    lut[:, 1] = ramp * color[1]
    lut[:, 2] = ramp * color[2]
    lut[:, 3] = 1.0
    return gfx.Texture(lut, dim=1)


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
    scene as the camera moves. Reads chunks lazily via dask — only what's
    actually visible gets pulled off disk.

    `color`, if given, tints this channel (black -> color ramp) and blends it
    additively with whatever else is in the scene, for multi-channel composite
    display. Leave as None for a plain grayscale single-channel layer.
    """

    def __init__(
        self,
        levels: list[PyramidLevel],
        clim: tuple[float, float],
        max_tiles: int = 64,
        color: tuple[float, float, float] | None = None,
    ):
        self.levels = levels
        self.clim = clim
        self.max_tiles = max_tiles
        self.color = color
        self._lut = _make_color_lut(color) if color is not None else None
        self._group = gfx.Group()
        self._cache: OrderedDict[tuple[int, int, int], gfx.Image] = OrderedDict()

    @property
    def group(self) -> gfx.Group:
        return self._group

    def set_clim(self, clim: tuple[float, float]) -> None:
        self.clim = clim
        for image in self._cache.values():
            image.material.clim = clim

    def _make_tile(self, level: PyramidLevel, cy: int, cx: int) -> gfx.Image:
        h, w = level.shape
        ch, cw = level.chunk_shape
        y0, y1 = cy * ch, min((cy + 1) * ch, h)
        x0, x1 = cx * cw, min((cx + 1) * cw, w)
        chunk = np.asarray(level.data[y0:y1, x0:x1]).astype(np.float32)

        texture = gfx.Texture(chunk, dim=2)
        material = gfx.ImageBasicMaterial(clim=self.clim, map=self._lut)
        if self.color is not None:
            material.alpha_config = _ADDITIVE_ALPHA_CONFIG
        image = gfx.Image(gfx.Geometry(grid=texture), material)
        _, sx = level.scale
        _, tx = level.translation
        # Negative Y scale: local row 0 (top of chunk) anchors at its world_y,
        # and increasing local row must *decrease* world_y (world Y increases
        # upward; pixel rows increase downward) — see PyramidLevel.world_bounds.
        image.local.scale = (sx, -level.scale[0], 1)
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
