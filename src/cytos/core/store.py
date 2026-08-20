"""Opening a slide's zarr stores, whether a store is a directory or a `.zip`.

A slide is a build artifact: `cytos-import` writes it once and the viewer
only ever reads it. Nothing rewrites a chunk, so the many-small-files layout a
directory store needs buys nothing here and costs plenty -- a 1.4 GB slide is
~4000 files, which makes copying it to another machine slow and makes a
half-finished copy look fine until you pan into the missing part. A zip of the
same store is one file, refuses to open if truncated, and reads just as fast
(the same or a little faster for the small-array tile layers, since a chunk
fetch is a seek on an already-open handle rather than an open/read/close).

What zip gives up is in-place writes, which a read-only viewer never does.

Both layouts stay readable: which one a layer used is in the manifest's
per-layer `format` field, so opening one is a lookup here, not a probe of the
filesystem, and old slides keep working.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import zarr
from zarr.storage import ZipStore

from cytos.core.slide import TILES_FORMAT, TILES_ZIP_FORMAT

ZIP_SUFFIX = ".zip"

# Tile-store filename per vector-layer format.
_TILES_STORE_NAME = {
    TILES_FORMAT: "tiles.zarr",
    TILES_ZIP_FORMAT: "tiles.zarr.zip",
}


def is_zipped(path: Path | str) -> bool:
    return Path(path).suffix == ZIP_SUFFIX


def open_zarr_group(path: Path | str) -> zarr.Group:
    """Open a zarr group for reading from either a store directory or a
    zipped store. Raises ValueError naming the path if it is neither."""
    path = Path(path)
    if not is_zipped(path):
        return zarr.open_group(str(path), mode="r")
    try:
        store = ZipStore(path, mode="r")
    except zipfile.BadZipFile as err:
        # A truncated or half-copied archive lands here. Say which file, since
        # the message underneath is just "File is not a zip file".
        raise ValueError(f"{path}: not a zarr store -- not a readable zip file") from err
    return zarr.open_group(store=store, mode="r")


def open_tile_store(layer_dir: Path, tile_format: str, kind: str) -> zarr.Group:
    """The tile store of a vector layer (`kind` is "segment" or "point", used
    only for the error message)."""
    name = _TILES_STORE_NAME.get(tile_format)
    if name is None:
        raise ValueError(f"{layer_dir}: unsupported {kind} tile format {tile_format!r}")
    return open_zarr_group(layer_dir / name)
