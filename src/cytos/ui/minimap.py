"""Minimap navigator: a static composite thumbnail plus a live rectangle
showing what the main camera currently sees, like Photoshop's Navigator or
napari's overview. Click or drag on it to recenter the main camera there."""

from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from cytos.ui.channel_panel import Channel


def make_composite_thumbnail(channels: list[Channel], visibility: dict[str, bool]) -> np.ndarray:
    """Composite each channel's coarsest pyramid level (already small, already
    loaded) the same way the main view does — map through the channel's LUT,
    clip by clim, additive sum — for a static preview image. Returns (H, W, 3)
    uint8. Indexes `cache.lut_array` directly (the same array backing the GPU
    texture), so this can't visually drift from the main view's colormap."""
    layers = []
    for ch in channels:
        if not visibility.get(ch.name, True):
            continue
        arr = np.asarray(ch.levels[-1].data).astype(np.float32)
        lo, hi = ch.cache.clim
        norm = np.clip((arr - lo) / max(hi - lo, 1e-6), 0, 1)
        if ch.cache.lut_array is not None:
            idx = (norm * 255).astype(np.uint8)
            layers.append(ch.cache.lut_array[idx, :3])
        else:
            layers.append(np.repeat(norm[..., None], 3, axis=-1))
    if not layers:
        if not channels:
            return np.zeros((1, 1, 3), dtype=np.uint8)
        h, w = np.asarray(channels[0].levels[-1].data).shape
        return np.zeros((h, w, 3), dtype=np.uint8)
    composite = np.clip(sum(layers), 0, 1)
    return (composite * 255).astype(np.uint8)


class MinimapWidget(QtWidgets.QWidget):
    """Static composite thumbnail + a live rectangle showing what the main
    camera currently sees, like Photoshop's Navigator or napari's overview.
    Click or drag on it to recenter the main camera there."""

    position_clicked = QtCore.Signal(float, float)  # world x, y

    def __init__(self, world_bounds: tuple[float, float, float, float]):
        super().__init__()
        self._world_bounds = world_bounds
        self._pixmap: QtGui.QPixmap | None = None
        self._view_rect: tuple[float, float, float, float] | None = None
        self.setMinimumHeight(180)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def set_world_bounds(self, world_bounds: tuple[float, float, float, float]) -> None:
        """Re-fit to new bounds — used once, the first time real data
        arrives after starting with none (see cytos.ui.main_window)."""
        self._world_bounds = world_bounds
        self.update()

    def set_image(self, rgb: np.ndarray) -> None:
        h, w, _ = rgb.shape
        rgb = np.ascontiguousarray(rgb)
        image = QtGui.QImage(rgb.tobytes(), w, h, w * 3, QtGui.QImage.Format.Format_RGB888).copy()
        self._pixmap = QtGui.QPixmap.fromImage(image)
        self.update()

    def set_view_rect(self, world_rect: tuple[float, float, float, float]) -> None:
        self._view_rect = world_rect
        self.update()

    def _image_rect(self) -> QtCore.QRectF:
        """Largest rect, centered in the widget, that draws the pixmap at its
        true aspect ratio (letterboxed/pillarboxed) instead of stretched."""
        widget_rect = QtCore.QRectF(self.rect())
        if self._pixmap is None or self._pixmap.isNull():
            return widget_rect
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw == 0 or ph == 0 or widget_rect.height() == 0:
            return widget_rect
        image_aspect = pw / ph
        widget_aspect = widget_rect.width() / widget_rect.height()
        if image_aspect > widget_aspect:
            w = widget_rect.width()
            h = w / image_aspect
        else:
            h = widget_rect.height()
            w = h * image_aspect
        x = widget_rect.x() + (widget_rect.width() - w) / 2
        y = widget_rect.y() + (widget_rect.height() - h) / 2
        return QtCore.QRectF(x, y, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(20, 20, 20))
        image_rect = self._image_rect()
        if self._pixmap is not None:
            painter.drawPixmap(image_rect, self._pixmap, QtCore.QRectF(self._pixmap.rect()))

        if self._view_rect is not None and image_rect.width() and image_rect.height():
            vminx, vminy, vmaxx, vmaxy = self._view_rect
            wminx, _, wmaxx, _ = self._world_bounds
            span_x = (wmaxx - wminx) or 1
            px0 = image_rect.x() + (vminx - wminx) / span_x * image_rect.width()
            px1 = image_rect.x() + (vmaxx - wminx) / span_x * image_rect.width()
            # World Y increases upward but pixel Y increases downward, so the
            # rect's *larger* world Y (vmaxy) maps to the *smaller* pixel Y.
            py0 = self._world_y_to_pixel_y(vmaxy, image_rect)
            py1 = self._world_y_to_pixel_y(vminy, image_rect)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 0), 2))
            painter.drawRect(QtCore.QRectF(px0, py0, px1 - px0, py1 - py0))
        painter.end()

    def _world_y_to_pixel_y(self, world_y: float, image_rect: QtCore.QRectF) -> float:
        _, wminy, _, wmaxy = self._world_bounds
        span_y = (wmaxy - wminy) or 1
        return image_rect.y() + (wmaxy - world_y) / span_y * image_rect.height()

    def _pixel_y_to_world_y(self, pixel_y: float, image_rect: QtCore.QRectF) -> float:
        _, wminy, _, wmaxy = self._world_bounds
        span_y = (wmaxy - wminy) or 1
        fy = min(max((pixel_y - image_rect.y()) / image_rect.height(), 0.0), 1.0)
        return wmaxy - fy * span_y

    def _widget_pos_to_world(self, pos: QtCore.QPointF) -> tuple[float, float] | None:
        image_rect = self._image_rect()
        if image_rect.width() == 0 or image_rect.height() == 0:
            return None
        fx = min(max((pos.x() - image_rect.x()) / image_rect.width(), 0.0), 1.0)
        wminx, _, wmaxx, _ = self._world_bounds
        wx = wminx + fx * (wmaxx - wminx)
        wy = self._pixel_y_to_world_y(pos.y(), image_rect)
        return wx, wy

    def _handle_pointer(self, event) -> None:
        world_pos = self._widget_pos_to_world(event.position())
        if world_pos is not None:
            self.position_clicked.emit(*world_pos)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._handle_pointer(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._handle_pointer(event)
