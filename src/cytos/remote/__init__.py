"""Remote control of a running viewer — one concept, three modules.

`ipc` is the wire (JSON lines over the single-instance local socket, both
the server the viewer runs and the client `request()`); `ctl` is the
command-line client (`cytos-ctl`); `mcp_server` adapts the same commands to
MCP for AI clients without a shell (`cytos-mcp`).

Sits below `ui` in the layering: `cytos.ui.main_window` imports `remote.ipc`
to serve the socket, and nothing here imports `ui` — the server is handed a
dispatch callable instead of knowing about windows.
"""


def mcp_server_main() -> None:
    """Entry point for `cytos-mcp` (wired through `cytos.cli`). Lives here,
    not in `mcp_server` itself, because that module imports the MCP SDK at
    the top — an optional extra — and the graceful install hint has to come
    from a module that imports without it."""
    try:
        from cytos.remote.mcp_server import main
    except ImportError:
        raise SystemExit(
            "cytos-mcp needs the MCP SDK, which is an optional extra:\n"
            "    pip install 'cytos[mcp]'"
        ) from None
    main()
