"""Convert one channel of a morphology OME-TIFF into a real OME-Zarr (OME-NGFF)
multiscale pyramid.

Xenium's `morphology_focus/*.ome.tif` files are OME-TIFF, not zarr — none of the
Xenium output is actually OME-NGFF (see skills/developers.md). This produces a real one, and
it's what `cytos-import` calls to fill a slide's `images/` when the source has
no OME-Zarr of its own yet.

Handles both morphology layouts Xenium has shipped: one file per channel
(pre-2.0, e.g. `ch0000_dapi.ome.tif`), and XOA 2.0's *multi-file* OME-TIFF
(`morphology_focus_0000.ome.tif` … `_0003.ome.tif`, one file per stain) —
open any one of those and tifffile assembles the whole 4-channel series from
the OME metadata, so `channel` indexes stains across files. Those files are
JPEG-2000 compressed, which is why `imagecodecs` is a real dependency.

Usage (installed as a console script, see pyproject.toml [project.scripts]):
    cytos-convert-ome-zarr \
        data/human_kidney_tiny/morphology_focus/ch0000_dapi.ome.tif \
        --channel 0 \
        --out data/human_kidney_tiny/dapi.ome.zarr
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import tifffile
from ome_zarr.io import parse_url
from ome_zarr.writer import write_image
from zarr import group as zarr_group

_OME_NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


def ome_channel_names(tiff_path: Path) -> list[str]:
    """The channel names an OME-TIFF declares, in channel order — one entry
    per channel even when the metadata names none of them (`ch0`, `ch1`, …),
    so the list's length is always the channel count. What `cytos-import`
    uses to name one image layer per stain of an XOA 2.0 morphology set."""
    import xml.etree.ElementTree as ET

    with tifffile.TiffFile(tiff_path) as tif:
        series = tif.series[0]
        n = series.shape[series.axes.index("C")] if "C" in series.axes else 1
        named = [None] * n
        if tif.ome_metadata:
            root = ET.fromstring(tif.ome_metadata)
            channels = root.findall(".//ome:Pixels/ome:Channel", _OME_NS)
            for i, ch in enumerate(channels[:n]):
                named[i] = ch.get("Name")
    return [name or f"ch{i}" for i, name in enumerate(named)]


def _read_plane(series, channel: int, tiff_path: Path):
    """One (Y, X) channel plane, decoding only that channel's pages when the
    layout allows. `series.asarray()[channel]` decompresses *every* channel —
    for XOA 2.0's four JPEG-2000 whole-slide stains that is 4x the work and
    4x the peak memory per converted channel, since the importer converts
    channels one at a time."""
    axes = series.axes
    if "C" not in axes:
        # SizeC=1 collapses to a plain 2D array in some Xenium pipeline
        # versions (e.g. gene-only panels' DAPI-only morphology_focus.ome.tif)
        # — there's nothing to index by channel.
        if channel != 0:
            raise ValueError(
                f"{tiff_path} has no channel axis (axes={axes!r}, "
                f"a single-channel file) — only channel 0 is valid."
            )
        return series.asarray()
    n = series.shape[axes.index("C")]
    if not 0 <= channel < n:
        raise ValueError(f"{tiff_path} has channels 0..{n - 1}, not {channel}")
    # One page per channel plane (both Xenium layouts: single-file CYX, and
    # 2.0's one-file-per-stain set) — read just that page. Any layout where
    # pages and channels don't line up falls back to the full read.
    if axes.startswith("C") and len(series.pages) == n:
        page = series.pages[channel]
        # A multi-file set's sibling files are parsed once and their handles
        # closed; reading a page from one later auto-reopens with a
        # UserWarning. Reopen it on purpose instead, and close it again.
        fh = page.parent.filehandle
        if fh.closed:
            fh.open()
            try:
                return page.asarray()
            finally:
                fh.close()
        return page.asarray()
    return series.asarray()[channel]


def convert_ome_zarr(tiff_path: Path, out: Path, channel: int = 0, quiet: bool = False) -> Path:
    with tifffile.TiffFile(tiff_path) as tif:
        series = tif.series[0]
        plane = _read_plane(series, channel, tiff_path)  # (Y, X) uint16
        px_size_x = px_size_y = None
        if tif.ome_metadata:
            import xml.etree.ElementTree as ET

            ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
            root = ET.fromstring(tif.ome_metadata)
            pixels = root.find(".//ome:Pixels", ns)
            px_size_x = float(pixels.get("PhysicalSizeX"))
            px_size_y = float(pixels.get("PhysicalSizeY"))

    if not quiet:
        print(f"plane shape: {plane.shape}, dtype: {plane.dtype}")
        print(f"physical pixel size: {px_size_x} x {px_size_y} um")

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    store = parse_url(str(out), mode="w").store
    root = zarr_group(store=store)
    write_image(
        image=plane,
        group=root,
        axes="yx",
        scale={"y": px_size_y, "x": px_size_x},
        axes_units={"y": "micrometer", "x": "micrometer"},
        storage_options=dict(chunks=(1024, 1024)),
    )

    if not quiet:
        print(f"wrote {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tiff_path", type=Path)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    convert_ome_zarr(args.tiff_path, args.out, channel=args.channel)


if __name__ == "__main__":
    main()
