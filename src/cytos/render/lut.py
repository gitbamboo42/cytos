"""A colour lookup texture indexed by a dense integer id, shared by the vector
layers: cell id for polygons (`cytos.render.polygons`), gene id for transcript
points (`cytos.render.points`).

This is the trick that makes recolouring cheap. Colour never lives in a vertex
buffer -- each vertex carries its id as a texcoord into this texture, so
recolouring an entire dataset is one small texture upload rather than a rewrite
of every vertex on the GPU. It also means several objects (a polygon layer's
outline *and* its fill, or every visible point tile) can share one LUT and
recolour together for the price of one upload.

`filter="nearest"` is essential, not a preference: with linear filtering,
adjacent ids would blend into each other, so two neighbouring cells would smear
their colours together at a shared boundary vertex. Same reason pygfx's own
colormap textures use it (see `pygfx.utils.cm.registered_colormap`).
"""

from __future__ import annotations

import numpy as np
import pygfx as gfx

# 2D grid layout (rather than a literal 1D texture) so the LUT stays well under
# GPU texture-dimension limits even at whole-slide counts -- a 1D texture of a
# few million cells wouldn't be allocatable.
_LUT_WIDTH = 2048


class IdColorLut:
    """RGBA per dense id, as a nearest-filtered 2D texture plus the texcoords
    that address it."""

    def __init__(self, n_ids: int, fill: float = 1.0):
        height = max(1, -(-max(n_ids, 1) // _LUT_WIDTH))  # ceil div
        self.n_ids = n_ids
        self._array = np.full((height, _LUT_WIDTH, 4), fill, dtype=np.float32)
        self._texture = gfx.Texture(self._array, dim=2)
        self.map = gfx.TextureMap(self._texture, filter="nearest", wrap="clamp")

    def uv(self, ids: np.ndarray) -> np.ndarray:
        """(n, 2) float32 texcoords for `ids` -- the centre of each id's texel,
        so nearest sampling can't land on a neighbour through rounding."""
        height, width = self._array.shape[:2]
        uv = np.empty((len(ids), 2), dtype=np.float32)
        uv[:, 0] = ((ids % width).astype(np.float32) + 0.5) / width
        uv[:, 1] = ((ids // width).astype(np.float32) + 0.5) / height
        return uv

    def set_colors(self, colors: np.ndarray) -> None:
        """colors: (n, 4) float32 RGBA in [0, 1], row i being dense id i. One
        upload, however many objects sample this LUT."""
        height, width = self._array.shape[:2]
        flat = self._array.reshape(height * width, 4)
        flat[: len(colors)] = colors
        self._texture.update_full()

    @property
    def array(self) -> np.ndarray:
        """The backing (height, width, 4) array -- for tests and debugging;
        writing to it directly won't reach the GPU without `set_colors`."""
        return self._array
