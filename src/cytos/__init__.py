"""cytos — spatial biology slides: built here, looked at in the app.

This package is the pipeline and the format. `cytos-import` turns a source
dataset into a `.cytos` slide, `cytos.core` defines what that slide is, and
nothing here draws anything: the viewer is the web/Electron app in `viewer/`,
which reads the same slides over HTTP or off the disk.

Deliberately exports nothing — cytos is an application, not a library, and
its interfaces are the console scripts and the `.cytos` format. The guides
ship in `cytos.skills` as plain markdown. Submodules are importable
explicitly when the internals are wanted; importing the package itself must
stay instant and dependency-free.
"""
