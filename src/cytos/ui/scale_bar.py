"""Scale bar drawn over the render canvas, and the unit names it shares with
the dock's resolution readout.

A viewer with free zoom has no fixed sense of size — after a few scroll wheel
turns "how big is that?" has no answer on screen. Every microscopy viewer solves
this the same way, with a bar of a round length labelled in real units, and this
is that.

A plain Qt child widget rather than anything in the pygfx scene: the canvas
presents to a bitmap it paints in its own `paintEvent` (`WgpuContextToBitmap`),
so Qt composites children over it normally. That keeps the bar in `cytos.ui`
where the rest of the widgets live, instead of needing a second camera and an
overlay pass in `cytos.render`.
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

# Bar lengths are a 1/2/5 step times a power of ten, so the label is always a
# round number the eye can multiply without effort -- 200 um, never 187 um.
_NICE_STEPS = (5, 2, 1)
_TARGET_PX = 120  # the length we aim for, before rounding down to a nice number
_MARGIN = 12  # gap from the canvas's bottom-left corner
_PAD = 8  # breathing room inside the bar's backing panel
_BAR_HEIGHT = 4
_TICK_HEIGHT = 9

_UNIT_ABBREV = {
    "micrometer": "µm",
    "micron": "µm",
    "millimeter": "mm",
    "nanometer": "nm",
}


def unit_abbrev(units: str) -> str:
    """Short form of a manifest `world_units` value, or the value itself when
    it isn't one this knows -- an unfamiliar unit is better shown in full than
    silently relabelled."""
    return _UNIT_ABBREV.get(units, units)


def nice_length(world_per_px: float) -> float:
    """The longest 1/2/5 x 10^n distance that still fits in `_TARGET_PX`."""
    target = _TARGET_PX * world_per_px
    power = 10.0 ** math.floor(math.log10(target))
    for step in _NICE_STEPS:
        if step * power <= target:
            return step * power
    return power


def format_length(length: float, units: str) -> str:
    """Label for a bar of `length`, promoted to a bigger unit once the number
    would otherwise run long -- "1.5 mm" reads faster than "1500 µm"."""
    abbrev = unit_abbrev(units)
    if abbrev == "µm" and length >= 1000:
        return f"{length / 1000:g} mm"
    return f"{length:g} {abbrev}"


class ScaleBarWidget(QtWidgets.QWidget):
    """Bar plus label, sitting in the canvas's bottom-left corner.

    Transparent to the mouse, so dragging over it still pans the view rather
    than the bar quietly eating the gesture.
    """

    def __init__(self, parent: QtWidgets.QWidget):
        super().__init__(parent)
        self._length_px = 0
        self._label = ""
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Repositioning has to follow the canvas, and the canvas is not ours to
        # subclass for it; watching its resize events keeps that knowledge here
        # rather than spread into the window that builds us.
        parent.installEventFilter(self)
        self.hide()

    def set_scale(self, world_per_px: float | None, units: str) -> None:
        """Re-fit the bar to the current zoom. Called on the same slow timer as
        the rest of the readouts, not per rendered frame."""
        if not world_per_px or world_per_px <= 0 or not math.isfinite(world_per_px):
            self.hide()
            return
        length = nice_length(world_per_px)
        self._length_px = max(1, round(length / world_per_px))
        self._label = format_length(length, units)

        metrics = self.fontMetrics()
        width = max(self._length_px, metrics.horizontalAdvance(self._label)) + 2 * _PAD
        height = _TICK_HEIGHT + metrics.height() + 2 * _PAD
        self.resize(width, height)
        self._reposition()
        self.show()
        self.update()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's own naming
        if watched is self.parentWidget() and event.type() == QtCore.QEvent.Type.Resize:
            self._reposition()
        return False

    def _reposition(self) -> None:
        canvas = self.parentWidget()
        if canvas is not None:
            self.move(_MARGIN, canvas.height() - self.height() - _MARGIN)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt's own naming
        if not self._label:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # A dark backing panel, because the bar sits on image data that can be
        # any brightness -- white-on-white is the failure mode otherwise.
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 140))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()), 4, 4)

        bar_left = (self.width() - self._length_px) / 2
        bar_top = self.height() - _PAD - _BAR_HEIGHT
        white = QtGui.QColor(255, 255, 255)
        painter.setBrush(white)
        painter.drawRect(QtCore.QRectF(bar_left, bar_top, self._length_px, _BAR_HEIGHT))
        # End ticks, so the bar's extent is unambiguous against a busy image.
        for x in (bar_left, bar_left + self._length_px - 1):
            painter.drawRect(QtCore.QRectF(x, bar_top - (_TICK_HEIGHT - _BAR_HEIGHT), 1, _TICK_HEIGHT))

        painter.setPen(white)
        text_rect = QtCore.QRect(0, _PAD // 2, self.width(), self.fontMetrics().height())
        painter.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignHCenter, self._label)
        painter.end()
