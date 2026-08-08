"""Dock-panel controls for the cell-segmentation layer: what to draw (outline,
fill, or both), how opaque the fill is, and what colors the cells.

Colour is two choices, not one — a **colormap** (the ramp) and a **color by**
(which per-cell measurement the ramp is spread over, e.g. cell_area). Picking
"Flat color" uses the top of the ramp for every cell instead, which is why
there's no separate colour-picker widget here.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from cytos.ui.colormap_combo import make_colormap_combo

FLAT_COLOR_LABEL = "Flat color"


class SegmentRow(QtWidgets.QGroupBox):
    colormap_changed = QtCore.Signal(str)
    color_by_changed = QtCore.Signal(object)  # str feature name, or None for flat
    outline_changed = QtCore.Signal(bool)
    fill_changed = QtCore.Signal(bool)
    fill_opacity_changed = QtCore.Signal(float)

    def __init__(
        self,
        title: str,
        feature_names: list[str],
        colormap: str,
        color_by: str | None,
        show_outline: bool,
        show_fill: bool,
        fill_opacity: float,
    ):
        super().__init__(title)
        layout = QtWidgets.QFormLayout(self)

        self.cmap_combo = make_colormap_combo(colormap)
        self.cmap_combo.currentTextChanged.connect(self.colormap_changed)
        layout.addRow("Colormap", self.cmap_combo)

        self.color_by_combo = QtWidgets.QComboBox()
        self.color_by_combo.addItem(FLAT_COLOR_LABEL)
        for name in feature_names:
            self.color_by_combo.addItem(name)
        self.color_by_combo.setCurrentText(color_by if color_by is not None else FLAT_COLOR_LABEL)
        self.color_by_combo.currentTextChanged.connect(self._emit_color_by)
        # Nothing to spread a ramp over when the cache has no feature table —
        # leave the control visible but dead, so the panel's shape doesn't
        # depend on the dataset.
        self.color_by_combo.setEnabled(bool(feature_names))
        layout.addRow("Color by", self.color_by_combo)

        self.outline_check = QtWidgets.QCheckBox("Outline")
        self.outline_check.setChecked(show_outline)
        self.outline_check.toggled.connect(self.outline_changed)
        layout.addRow(self.outline_check)

        self.fill_check = QtWidgets.QCheckBox("Fill")
        self.fill_check.setChecked(show_fill)
        self.fill_check.toggled.connect(self.fill_changed)
        self.fill_check.toggled.connect(self._sync_opacity_enabled)
        layout.addRow(self.fill_check)

        self.opacity_spin = QtWidgets.QDoubleSpinBox()
        self.opacity_spin.setRange(0.0, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        self.opacity_spin.setValue(fill_opacity)
        self.opacity_spin.valueChanged.connect(self.fill_opacity_changed)
        layout.addRow("Fill opacity", self.opacity_spin)
        self._sync_opacity_enabled(show_fill)

    def _sync_opacity_enabled(self, fill_on: bool) -> None:
        self.opacity_spin.setEnabled(fill_on)

    def _emit_color_by(self, text: str) -> None:
        self.color_by_changed.emit(None if text == FLAT_COLOR_LABEL else text)
