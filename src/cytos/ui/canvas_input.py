"""Input-event translation for the main render canvas: platform/device
quirks that Qt or rendercanvas don't already normalize into pygfx's generic
event vocabulary ("wheel", "pointer_down", ...) get adapted here, in one
place. napari's own canvas (`_vispy/canvas.py`) keeps all its mouse/wheel/
gesture dispatch centralized the same way rather than splitting per input
type — new devices/platforms get a new branch or handler method below, not
a new file.

Currently handles: macOS trackpad pinch-to-zoom. See
`CanvasRenderWidget._handle_pinch_zoom` for why it's needed and untested-
elsewhere caveats.
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui
from rendercanvas.qt import RenderWidget as _BaseRenderWidget


class CanvasRenderWidget(_BaseRenderWidget):
    """The render canvas's central widget, extended with input adapters for
    devices/platforms rendercanvas's Qt backend doesn't handle on its own."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.grabGesture(QtCore.Qt.GestureType.PinchGesture)

    def event(self, event):  # noqa: N802
        if event.type() == QtCore.QEvent.Type.Gesture and self._handle_gesture(event):
            return True
        return super().event(event)

    def _handle_gesture(self, event) -> bool:
        pinch = event.gesture(QtCore.Qt.GestureType.PinchGesture)
        if pinch is not None and pinch.scaleFactor() > 0:
            return self._handle_pinch_zoom(pinch)
        return False

    def _handle_pinch_zoom(self, pinch) -> bool:
        """macOS trackpad pinch-to-zoom.

        rendercanvas's Qt backend (rendercanvas/qt.py:RenderWidget.wheelEvent)
        only ever translates QWheelEvent into pygfx's "wheel" control — it
        has no handling anywhere for QNativeGestureEvent/QPinchGesture, so
        mouse scroll-wheel zoom works (gfx.PanZoomController maps plain
        "wheel" to zoom_to_point with no modifier needed) but a real
        trackpad pinch never reaches pygfx at all. Feeding it through the
        same "wheel" event path is the fix, since the library itself can't
        be edited.

        The dy is scaled to reproduce PanZoomController's own math exactly
        (zoom_to_point's fy = 2**delta, wheel's multiplier = -0.001 — see
        pygfx.controllers._panzoom.PanZoomController._default_controls)
        rather than an arbitrary constant, so a pinch produces the same
        zoom factor a wheel scroll would for an equivalent delta.
        macOS-specific: not tested against other platforms' QPinchGesture
        delivery.
        """
        dy = -1000.0 * math.log2(pinch.scaleFactor())
        pos = self.mapFromGlobal(QtGui.QCursor.pos())
        self.submit_event(
            {
                "event_type": "wheel",
                "dx": 0,
                "dy": dy,
                "x": float(pos.x()),
                "y": float(pos.y()),
                "buttons": (),
                "modifiers": (),
            }
        )
        return True
