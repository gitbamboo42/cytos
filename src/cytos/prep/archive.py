"""Pack a written zarr store into a single zipped store.

The last step of writing any layer. Zarr chunks are already compressed by
their own codec, so the archive is stored uncompressed (`ZIP_STORED`) --
deflating them a second time would cost CPU on every chunk read for no space
saved. `zarr.storage.ZipStore` reads exactly this.

See `cytos.core.store` for why a slide's stores are zipped at all.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


def zip_store(src: Path, dest: Path | None = None, remove_source: bool = False) -> Path:
    """Zip the store directory `src` into one file.

    `dest` defaults to `src.zip`, next to the directory. `remove_source`
    deletes the chunk tree afterwards -- true for a store the importer just
    wrote and is done with, false when packing a source dataset's own store,
    which isn't ours to delete.
    """
    dest = dest if dest is not None else src.with_name(src.name + ".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # Sorted, so packing the same store twice gives a byte-identical archive.
    # An importer whose output differs run to run makes "is this the same
    # slide?" unanswerable.
    paths = sorted(p for p in src.rglob("*") if p.is_file())
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in paths:
            archive.write(path, str(path.relative_to(src)))
    if remove_source:
        shutil.rmtree(src)
    return dest
