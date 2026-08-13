"""cytos-mcp — the viewer's control socket, served over MCP.

The Model Context Protocol is how AI clients that have no shell (Claude
Desktop, claude.ai, IDE assistants) call external tools. This server is a
thin adapter and nothing more: every tool below is one command on the same
local socket `cytos-ctl` uses (see `cytos.remote.ipc`), so the two never disagree
about what the viewer can do. Anything with a shell can skip this entirely
and just run `cytos-ctl`.

The one thing MCP adds beyond reach: `snapshot` returns the rendered view as
an actual image, so a model sees the slide directly instead of needing file
access to a written PNG.

Setup, once per AI client (needs `pip install cytos[mcp]`):

    claude mcp add cytos -- cytos-mcp          # Claude Code
    # or point the client's MCP config at the `cytos-mcp` command, stdio
    # transport. Use the full path to cytos-mcp if the client won't see
    # your conda env's PATH.

The viewer itself must be running (`cytos-viewer`); this server never starts
one — a GUI app belongs to the user's desktop session, not to a tool call.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from cytos.remote import ipc

# A slide open can page a whole pyramid level in; a snapshot may load every
# tile the view needs first. Both are seconds, not the default ten.
_SLOW_TIMEOUT_MS = 120_000
_TIMEOUT_MS = 15_000

server = MCPServer(
    "cytos",
    instructions=(
        "Remote control for a running cytos-viewer (a spatial-biology slide "
        "viewer). If cytos is new to you, call usage_guide first. Then: "
        "viewer_status, open_slide, describe — describe lists every layer "
        "and every legal value for set_state. World coordinates are in the "
        "slide's own units (usually micrometers) with Y increasing downward. "
        "Use snapshot to see the current view."
    ),
)


def _call(payload: dict, timeout_ms: int = _TIMEOUT_MS) -> dict | list:
    """One request over the control socket, unwrapped or raised."""
    try:
        reply = ipc.request(payload, timeout_ms=timeout_ms)
    except ipc.NotRunning:
        raise RuntimeError(
            "cytos-viewer is not running. Ask the user to start it (`cytos-viewer`); "
            "this server cannot start a GUI app for them."
        ) from None
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "unknown error"))
    return reply.get("result")


@server.tool()
def usage_guide() -> str:
    """The cytos operating guide for AI assistants — mental model, the
    command loop, the state vocabulary, coordinate conventions, session
    etiquette. Read this first if cytos is new to you."""
    from cytos.skills import guide_text

    return guide_text()


@server.tool()
def viewer_status() -> dict:
    """Is the viewer running, its version, and how many slide windows are open."""
    return _call({"cmd": "ping"})


@server.tool()
def list_sessions(slide_path: str) -> list:
    """List a .cytos slide's saved sessions (name + last modified). Reads the
    slide directory itself, so it works even before the viewer is running."""
    from cytos.core.session import list_sessions as _list

    root = Path(slide_path).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"no slide at {root}")
    return [
        {
            "name": info.name,
            "modified": info.modified.isoformat(timespec="seconds") if info.modified else None,
        }
        for info in _list(root)
    ]


@server.tool()
def open_slide(slide_path: str, session: str = "default") -> dict:
    """Open a .cytos slide in a new viewer window, under the named session
    (created if new). Fails if another window already has that session open.
    Returns the same full description as describe()."""
    return _call(
        {"cmd": "open", "path": str(Path(slide_path).expanduser().resolve()), "session": session},
        timeout_ms=_SLOW_TIMEOUT_MS,
    )


@server.tool()
def list_windows() -> list:
    """The open slide windows and their ids — pass an id as `window` to the
    other tools when more than one is open."""
    return _call({"cmd": "windows"})


@server.tool()
def describe(window: int | None = None) -> dict:
    """Everything about a window: slide, world bounds and units, camera, and
    every layer with its current state plus every legal value for set_state
    (layer keys, colormaps, color_by features, gene names)."""
    return _call({"cmd": "describe", "window": window})


@server.tool()
def get_state(window: int | None = None) -> dict:
    """The window's current view state, in the same shape set_state accepts."""
    return _call({"cmd": "state", "window": window})


@server.tool()
def set_state(changes: dict, window: int | None = None) -> dict:
    """Apply a partial state change, shaped like a saved session:
    {"camera": {...}, "sections": {...}, "layers": {"<key>": {...}}}.
    Layer entries are partial — only the fields given change. Example:
    {"layers": {"image:dapi": {"colormap": "green", "clim": [0, 40]}}}.
    describe() lists the layer keys and legal values. Returns the new state."""
    return _call({"cmd": "set", "window": window, "changes": changes})


@server.tool()
def set_camera(
    center_x: float | None = None,
    center_y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    fit: bool = False,
    window: int | None = None,
) -> dict:
    """Move the camera. World units, Y increasing downward. width or height
    alone keeps the current aspect; fit shows the whole slide. Returns the
    new state."""
    spec: dict = {}
    if fit:
        spec["fit"] = True
    if center_x is not None or center_y is not None:
        if center_x is None or center_y is None:
            raise RuntimeError("center_x and center_y go together")
        spec["center"] = [center_x, center_y]
    if width is not None:
        spec["width"] = width
    if height is not None:
        spec["height"] = height
    if not spec:
        raise RuntimeError("give fit, a center, a width, or a height")
    return _call({"cmd": "set", "window": window, "changes": {"camera": spec}})


@server.tool()
def snapshot(window: int | None = None) -> Image:
    """Render the window's current view and return it as a PNG image."""
    fd, path = tempfile.mkstemp(prefix="cytos-mcp-", suffix=".png")
    os.close(fd)
    _call({"cmd": "snapshot", "window": window, "path": path}, timeout_ms=_SLOW_TIMEOUT_MS)
    data = Path(path).read_bytes()
    Path(path).unlink(missing_ok=True)
    return Image(data=data, format="png")


@server.tool()
def reset_view(window: int | None = None) -> dict:
    """Reset a window to the slide's own defaults (camera fit, manifest layer
    settings), like View > Reset to Slide Defaults."""
    return _call({"cmd": "reset", "window": window})


@server.tool()
def save_session(window: int | None = None) -> dict:
    """Save the window's session to disk now (it also saves on close)."""
    return _call({"cmd": "save", "window": window})


@server.tool()
def quit_viewer() -> dict:
    """Quit the viewer app, saving every open window's session first."""
    return _call({"cmd": "quit"})


def main() -> None:
    # QLocalSocket needs a Qt application object to exist (not to run).
    # Created here, before any tool call, and QCoreApplication so nothing
    # GUI-ish happens in a headless MCP host.
    from PySide6 import QtCore

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(sys.argv[:1])
    assert app is not None
    server.run("stdio")


if __name__ == "__main__":
    main()
