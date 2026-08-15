"""Per-channel UI: the bundled state a channel needs on screen (name,
colormap, pyramid levels, GPU tile cache) and the dock-panel row that
controls it.

A channel's color is a color, not a colormap: fluorescence composite display
maps intensity through a black->color ramp, so picking the color *is*
picking the colormap (`cytos.render.image.colormap_lut_array` accepts a
"#rrggbb" directly). One swatch shows the current color; clicking it opens
the preset list plus a custom picker. The session field stays `colormap`
and holds the hex.

The row reads like a legend row: "[x] [swatch] name" is the whole header —
switch, color, title — and the contrast limits under it are one two-handle
range slider flanked by number boxes for exact values."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from cytos.core.image import PyramidLevel
from cytos.render.image import TileCache, normalize_channel_color
from cytos.ui.color_picker import CustomColors, open_color_popup, show_value_on
from cytos.ui.segment_panel import _IndicatorCheckBox

_SWATCH_SIZE = 16


class _RangeSlider(QtWidgets.QWidget):
    """A two-handle slider for a (low, high) range on one groove — what
    contrast limits actually are. Qt ships no such widget; this is the
    plain-widget version: a groove, the selected span, two round handles,
    drag whichever is nearest."""

    range_changed = QtCore.Signal(float, float)

    _HANDLE_R = 5

    def __init__(self, maximum: float, low: float, high: float):
        super().__init__()
        self._max = max(maximum, 1e-9)
        self._low = float(low)
        self._high = float(high)
        self._drag: str | None = None
        self.setMinimumHeight(2 * self._HANDLE_R + 6)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )

    def set_values(self, low: float, high: float) -> None:
        """Programmatic update (from the number boxes or a session) — no
        signal, the caller already knows."""
        self._low = min(max(0.0, float(low)), self._max)
        self._high = min(max(self._low, float(high)), self._max)
        self.update()

    def values(self) -> tuple[float, float]:
        return self._low, self._high

    # -- geometry ----------------------------------------------------------

    def _x_of(self, value: float) -> float:
        pad = self._HANDLE_R + 1
        return pad + (value / self._max) * (self.width() - 2 * pad)

    def _value_at(self, x: float) -> float:
        pad = self._HANDLE_R + 1
        span = max(self.width() - 2 * pad, 1)
        return min(max((x - pad) / span, 0.0), 1.0) * self._max

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        cy = self.height() / 2
        x0, x1 = self._x_of(0.0), self._x_of(self._max)
        xl, xh = self._x_of(self._low), self._x_of(self._high)

        groove = QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.Mid), 3)
        groove.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(groove)
        painter.drawLine(QtCore.QPointF(x0, cy), QtCore.QPointF(x1, cy))

        span = QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.Highlight), 3)
        span.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(span)
        painter.drawLine(QtCore.QPointF(xl, cy), QtCore.QPointF(xh, cy))

        painter.setPen(QtGui.QPen(palette.color(QtGui.QPalette.ColorRole.Mid), 1))
        painter.setBrush(palette.color(QtGui.QPalette.ColorRole.Button))
        for x in (xl, xh):
            painter.drawEllipse(QtCore.QPointF(x, cy), self._HANDLE_R, self._HANDLE_R)

    # -- mouse -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        self._drag = "low" if abs(x - self._x_of(self._low)) <= abs(x - self._x_of(self._high)) else "high"
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            return
        value = self._value_at(event.position().x())
        if self._drag == "low":
            self._low = min(value, self._high)
        else:
            self._high = max(value, self._low)
        self.update()
        self.range_changed.emit(self._low, self._high)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None


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

    def __init__(
        self,
        channel: Channel,
        intensity_max: float,
        visible: bool = True,
        custom_colors: CustomColors | None = None,
    ):
        super().__init__()
        outer = QtWidgets.QVBoxLayout(self)

        # "[x] [swatch] dapi" -- the whole most-used setting is the one
        # header line, like a legend row: the checkbox switches the channel,
        # the swatch picks its color (presets or custom), the name is the
        # name. Only the contrast slider lives under the fold.
        self._name = channel.name
        self._color = normalize_channel_color(channel.colormap)
        # The window's shared pool of user-created colors (see
        # cytos.ui.color_picker); a private pool only in tests.
        self._custom_colors = custom_colors if custom_colors is not None else CustomColors()
        header = QtWidgets.QWidget()
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        self._fold_button = QtWidgets.QToolButton()
        self._fold_button.setCheckable(True)
        self._fold_button.setChecked(True)
        self._fold_button.setArrowType(QtCore.Qt.ArrowType.DownArrow)
        self._fold_button.setStyleSheet("QToolButton { border: none; }")
        self._fold_button.toggled.connect(self._on_fold)
        self.visible_check = _IndicatorCheckBox()
        self.visible_check.setChecked(visible)
        self.visible_check.toggled.connect(self.visibility_changed)
        self._swatch = QtWidgets.QToolButton()
        self._swatch.setFixedSize(_SWATCH_SIZE, _SWATCH_SIZE)
        self._swatch.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        show_value_on(self._swatch, self._color)
        self._swatch.clicked.connect(self._open_color_menu)
        header_layout.addWidget(self._fold_button)
        header_layout.addWidget(self.visible_check)
        header_layout.addWidget(self._swatch)
        header_layout.addWidget(QtWidgets.QLabel(channel.name))
        header_layout.addStretch()
        outer.addWidget(header)

        self._content = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(self._content)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._content)

        # Contrast: [low] ==o=====o== [high], numbers for exactness.
        clim = channel.cache.clim
        self._slider = _RangeSlider(intensity_max, clim[0], clim[1])
        self.low_spin = QtWidgets.QDoubleSpinBox()
        self.high_spin = QtWidgets.QDoubleSpinBox()
        for spin, value in ((self.low_spin, clim[0]), (self.high_spin, clim[1])):
            spin.setRange(0, intensity_max)
            spin.setDecimals(1)
            spin.setValue(value)
            spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
            spin.setFixedWidth(52)
        contrast = QtWidgets.QHBoxLayout()
        contrast.setContentsMargins(0, 0, 0, 0)
        contrast.setSpacing(6)
        contrast.addWidget(self.low_spin)
        contrast.addWidget(self._slider, 1)
        contrast.addWidget(self.high_spin)
        contrast_widget = QtWidgets.QWidget()
        contrast_widget.setLayout(contrast)
        layout.addRow(contrast_widget)

        self._slider.range_changed.connect(self._on_slider)
        self.low_spin.valueChanged.connect(self._on_spins)
        self.high_spin.valueChanged.connect(self._on_spins)

        # Folded by default: the header line is the everyday surface, the
        # contrast range is an occasional adjustment.
        self._fold_button.setChecked(False)

    # -- fold --------------------------------------------------------------

    def _on_fold(self, expanded: bool) -> None:
        self._fold_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)

    # -- color -------------------------------------------------------------

    def _open_color_menu(self) -> None:
        open_color_popup(
            self._swatch, self._color, self._custom_colors,
            include_ramps=True, on_pick=self.set_color,
        )

    def set_color(self, value: str) -> None:
        """Set the channel color: "#rrggbb", a ramp colormap name, or a
        legacy hue name (which normalises to its hex). Emits only on an
        actual change, so restoring an unchanged row costs nothing."""
        value = normalize_channel_color(str(value))
        if value == self._color:
            return
        self._color = value
        show_value_on(self._swatch, value)
        self.colormap_changed.emit(value)

    # -- contrast ----------------------------------------------------------

    def _on_slider(self, low: float, high: float) -> None:
        for spin, value in ((self.low_spin, low), (self.high_spin, high)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.clim_changed.emit(low, high)

    def _on_spins(self) -> None:
        low, high = self.low_spin.value(), self.high_spin.value()
        if high < low:
            high = low
        self._slider.set_values(low, high)
        self.clim_changed.emit(*self._slider.values())

    # -- session -----------------------------------------------------------

    def state(self) -> dict:
        """What this row currently shows — the half of a saved session that
        belongs to this layer. Mirrors `apply`."""
        low, high = self._slider.values()
        return {
            "colormap": self._color,
            "visible": self.visible_check.isChecked(),
            "clim": [low, high],
        }

    def apply(self, colormap: str, visible: bool, clim: tuple[float, float]) -> None:
        """Push a saved (or default) state back into the widgets. Each setter
        re-emits this row's own signal, which is how the change reaches the
        tile cache -- everything here only emits when a value actually
        differs, so restoring an unchanged row costs nothing."""
        self.set_color(colormap)
        self.visible_check.setChecked(visible)
        low, high = float(clim[0]), float(clim[1])
        if (low, high) != self._slider.values():
            self._slider.set_values(low, high)
            self._on_slider(*self._slider.values())
