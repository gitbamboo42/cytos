"""cytos — a fast, read-only viewer for spatial biology slides.

Deliberately exports nothing: cytos is an application, not a library, and
its interfaces are the console scripts (`cytos-viewer`, `cytos-ctl`,
`cytos-import`, …), the control socket behind `cytos-ctl`/`cytos-mcp`, and
the `.cytos` slide format on disk. Start with `cytos-ctl skill`, which
prints the guide shipped in `cytos.skills`. Submodules are importable
explicitly when the internals are wanted; importing the package itself must
stay instant and dependency-free.
"""
