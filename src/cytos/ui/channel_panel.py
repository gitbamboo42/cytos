"""Per-channel UI: the bundled state a channel needs on screen (name,
colormap, pyramid levels, GPU tile cache) and the dock-panel row that
controls it (visibility checkbox, contrast sliders, colormap picker)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore, QtWidgets

from cytos.core.image import PyramidLevel
from cytos.render.image import TileCache
from cytos.ui.colormap_combo import make_colormap_combo


@dataclass
class Channel:
    name: str
    colormap: str
    levels: list[PyramidLevel]
    cache: TileCache


class ChannelRow(QtWidgets.QGroupBox):
    clim_changed = QtCore.Signal(float, float)
    visibility_changed = QtCore.Signal(bool)
    colormap_changed = QtCore.Signal(str)

    def __init__(self, channel: Channel, intensity_max: float, visible: bool = True):
        super().__init__(channel.name)
        layout = QtWidgets.QFormLayout(self)

        self.cmap_combo = make_colormap_combo(channel.colormap)
        self.cmap_combo.currentTextChanged.connect(self.colormap_changed)
        layout.addRow("Colormap", self.cmap_combo)

        self.visible_check = QtWidgets.QCheckBox("Visible")
        self.visible_check.setChecked(visible)
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

    def state(self) -> dict:
        """What this row currently shows — the half of a saved session that
        belongs to this layer. Mirrors `apply`."""
        return {
            "colormap": self.cmap_combo.currentText(),
            "visible": self.visible_check.isChecked(),
            "clim": [self.low_spin.value(), self.high_spin.value()],
        }

    def apply(self, colormap: str, visible: bool, clim: tuple[float, float]) -> None:
        """Push a saved (or default) state back into the widgets. Each setter
        re-emits this row's own signal, which is how the change reaches the
        tile cache -- Qt only emits when a value actually differs, so restoring
        an unchanged row costs nothing."""
        self.cmap_combo.setCurrentText(colormap)
        self.visible_check.setChecked(visible)
        self.low_spin.setValue(float(clim[0]))
        self.high_spin.setValue(float(clim[1]))
