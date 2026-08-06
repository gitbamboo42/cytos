"""Camera math for translating a pygfx camera's state into a world-space view
rect, independent of any particular scene content."""

from __future__ import annotations


def effective_camera_view_size(
    camera_width: float, camera_height: float, viewport_w: float, viewport_h: float
) -> tuple[float, float]:
    """What the camera actually shows, accounting for `maintain_aspect`.

    `camera.width`/`camera.height` alone do NOT reflect what's visible on
    screen when the viewport's aspect ratio differs from the camera's — pygfx
    pads one dimension at render time (see
    `pygfx.cameras._perspective.PerspectiveCamera._update_projection_matrix`)
    without writing that padding back to the public `.width`/`.height`
    properties. Any code deriving a world-space view rect from the camera
    (tile-cache updates, minimap overlays, ...) must replicate that padding or
    it'll compute a rect narrower than what's actually rendered.
    """
    if viewport_h == 0 or camera_height == 0:
        return camera_width, camera_height
    view_aspect = viewport_w / viewport_h
    cam_aspect = camera_width / camera_height
    if cam_aspect < view_aspect:
        return camera_width * (view_aspect / cam_aspect), camera_height
    return camera_width, camera_height * (cam_aspect / view_aspect)
