"""Console-script entry points — see pyproject.toml [project.scripts]. Kept as
a single dispatch file so "what commands does cytos install, and what do they
run" is answerable by reading one place, not by hunting through the package.

Two commands do the real work: `cytos-import` builds a `.cytos` slide from a
source dataset, `cytos-viewer` opens one. `cytos-ctl` drives a running
viewer over its control socket — the scriptable face of the same app. The
per-layer prep steps are no longer commands of their own — a layer's world
grid is the slide's, so preparing one in isolation would put it on a grid
nothing else shares.

Each entry imports its module inside the function, not at the top: importing
this module must not cost more than the command being run. `cytos-ctl` is
called once per command by scripts and AI agents, and eager imports here made
every call pay the viewer's full Qt/pygfx startup (~2 s) to send one line of
JSON down a socket.
"""


def viewer() -> None:
    """`cytos-viewer`: open the GUI app (starts on a welcome window)."""
    from cytos.ui.main_window import main

    main()


def ctl() -> None:
    """`cytos-ctl`: drive a running viewer over its control socket."""
    from cytos.remote.ctl import main

    main()


def mcp_server() -> None:
    """`cytos-mcp`: the same control surface served over MCP. The import
    guard for the optional MCP SDK lives in `cytos.remote`."""
    from cytos.remote import mcp_server_main

    mcp_server_main()


def import_slide() -> None:
    """`cytos-import`: build a `.cytos` slide from a source dataset."""
    from cytos.prep.slide import main

    main()


def convert_ome_zarr() -> None:
    """`cytos-convert-ome-zarr`: standalone OME-TIFF → OME-Zarr utility."""
    from cytos.prep.pyramid import main

    main()


__all__ = ["convert_ome_zarr", "ctl", "import_slide", "mcp_server", "viewer"]
