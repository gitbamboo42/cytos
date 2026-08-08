"""One colormap picker, shared by every panel that needs one (image channels
in `cytos.ui.channel_panel`, the segment layer in `cytos.ui.segment_panel`).

The gradient swatch next to each name is built from the same
`cytos.render.image.colormap_lut_array` the GPU samples, so the dropdown can't
drift away from what actually renders.
"""

from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from cytos.render.image import CURATED_COLORMAPS, colormap_lut_array

_SWATCH_W, _SWATCH_H = 56, 14
_ICON_CACHE: dict[str, QtGui.QIcon] = {}


def colormap_icon(name: str, width: int = _SWATCH_W, height: int = _SWATCH_H) -> QtGui.QIcon:
    """Small horizontal gradient swatch for a colormap combo-box entry, built
    once per name and cached — every row would otherwise rebuild the same
    swatch over and over."""
    if name in _ICON_CACHE:
        return _ICON_CACHE[name]
    rgb = (colormap_lut_array(name)[:, :3] * 255).astype(np.uint8)
    idx = np.linspace(0, 255, width).astype(np.uint8)
    row = np.ascontiguousarray(rgb[idx])
    img = np.ascontiguousarray(np.repeat(row[None, :, :], height, axis=0))
    qimage = QtGui.QImage(img.tobytes(), width, height, width * 3, QtGui.QImage.Format.Format_RGB888).copy()
    icon = QtGui.QIcon(QtGui.QPixmap.fromImage(qimage))
    _ICON_CACHE[name] = icon
    return icon


def make_colormap_combo(current: str) -> QtWidgets.QComboBox:
    """The curated list, with `current` appended first if it isn't in it —
    a name loaded from the command line can be any `plotlet.list_colormaps()`
    entry, and `QComboBox.setCurrentText` is a silent no-op on a non-editable
    combo when the text isn't already an item, which would leave the dropdown
    showing something other than what's rendering."""
    combo = QtWidgets.QComboBox()
    combo.setIconSize(QtCore.QSize(_SWATCH_W, _SWATCH_H))
    for name in CURATED_COLORMAPS:
        combo.addItem(colormap_icon(name), name)
    if current not in CURATED_COLORMAPS:
        combo.addItem(colormap_icon(current), current)
    combo.setCurrentText(current)
    return combo
