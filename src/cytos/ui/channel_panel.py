"""Per-channel UI: the bundled state a channel needs on screen (name,
colormap, pyramid levels, GPU tile cache) and the dock-panel row that
controls it (visibility checkbox, contrast sliders, colormap picker)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from cytos.core.image import PyramidLevel
from cytos.render.image import CURATED_COLORMAPS, TileCache, colormap_lut_array


@dataclass
class Channel:
    name: str
    colormap: str
    levels: list[PyramidLevel]
    cache: TileCache


_ICON_CACHE: dict[str, QtGui.QIcon] = {}


def _colormap_icon(name: str, width: int = 56, height: int = 14) -> QtGui.QIcon:
    """Small horizontal gradient swatch for a colormap combo-box entry, built
    once per name and cached — each channel row would otherwise rebuild the
    same swatch over and over."""
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


class ChannelRow(QtWidgets.QGroupBox):
    clim_changed = QtCore.Signal(float, float)
    visibility_changed = QtCore.Signal(bool)
    colormap_changed = QtCore.Signal(str)

    def __init__(self, channel: Channel, intensity_max: float):
        super().__init__(channel.name)
        layout = QtWidgets.QFormLayout(self)

        self.cmap_combo = QtWidgets.QComboBox()
        self.cmap_combo.setIconSize(QtCore.QSize(56, 14))
        for cmap_name in CURATED_COLORMAPS:
            self.cmap_combo.addItem(_colormap_icon(cmap_name), cmap_name)
        if channel.colormap not in CURATED_COLORMAPS:
            # Loaded via --channel with a name outside the curated set (any
            # plotlet.list_colormaps() name is accepted there) — add it so
            # the dropdown reflects what's actually rendering instead of
            # silently falling back to the first entry (QComboBox.
            # setCurrentText is a no-op for non-editable combos when the
            # text isn't already an item).
            self.cmap_combo.addItem(_colormap_icon(channel.colormap), channel.colormap)
        self.cmap_combo.setCurrentText(channel.colormap)
        self.cmap_combo.currentTextChanged.connect(self.colormap_changed)
        layout.addRow("Colormap", self.cmap_combo)

        self.visible_check = QtWidgets.QCheckBox("Visible")
        self.visible_check.setChecked(True)
        self.visible_check.toggled.connect(self.visibility_changed)
        layout.addRow(self.visible_check)

        clim = channel.cache.clim
        self.low_spin = QtWidgets.QDoubleSpinBox()
        self.low_spin.setRange(0, intensity_max)
        self.low_spin.setValue(clim[0])
        self.high_spin = QtWidgets.QDoubleSpinBox()
        self.high_spin.setRange(0, intensity_max)
        self.high_spin.setValue(clim[1])
        self.low_spin.valueChanged.connect(self._emit_clim)
        self.high_spin.valueChanged.connect(self._emit_clim)
        layout.addRow("Contrast low", self.low_spin)
        layout.addRow("Contrast high", self.high_spin)

    def _emit_clim(self):
        self.clim_changed.emit(self.low_spin.value(), self.high_spin.value())
