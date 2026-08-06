# cytos

A fast, read-only viewer for spatial biology data: cell segmentation polygons
drawn over a large OME-Zarr morphology image.

## Install

```
pip install -e .
```

## Usage

```
cytos-viewer
```

Convert a Xenium morphology OME-TIFF channel to OME-Zarr first, if needed:

```
cytos-convert-ome-zarr path/to/morphology.ome.tif --channel 0 --out out.ome.zarr
```
