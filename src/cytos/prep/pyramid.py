"""Convert one channel of a morphology OME-TIFF into a real OME-Zarr (OME-NGFF)
multiscale pyramid.

Xenium's `morphology_focus/*.ome.tif` files are OME-TIFF, not zarr — none of the
Xenium output is actually OME-NGFF (see CLAUDE.md). This produces a real one, and
it's what `cytos-import` calls to fill a bundle's `images/` when the source has
no OME-Zarr of its own yet.

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


def convert_ome_zarr(tiff_path: Path, out: Path, channel: int = 0, quiet: bool = False) -> Path:
    with tifffile.TiffFile(tiff_path) as tif:
        series = tif.series[0]
        data = series.asarray()
        if series.axes == "YX":
            # No channel axis at all (SizeC=1 collapses to a plain 2D array
            # in some Xenium pipeline versions, e.g. gene-only panels whose
            # morphology_focus.ome.tif is DAPI-only) — unlike multi-channel
            # bundles (axes "CYX"), there's nothing to index by channel.
            if channel != 0:
                raise ValueError(
                    f"{tiff_path} has no channel axis (axes={series.axes!r}, "
                    f"a single-channel file) — only channel 0 is valid."
                )
            plane = data
        else:
            plane = data[channel]  # (Y, X) uint16
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
