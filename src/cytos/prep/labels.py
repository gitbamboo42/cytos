"""OME-Zarr label mask -> polygon boundaries.

The second way a segmentation arrives. A boundary table
(`cytos.core.polygons.polygons_from_parquet`) is what Xenium ships, but most
segmentation algorithms -- Cellpose, StarDist, Mesmer, anything that ends in a
watershed -- produce a **label mask** instead: an image the same size as the
morphology one, where a pixel's value is the id of the object covering it and 0
is background. This module traces one back into rings.

Lives in `prep`, not `core`, for two reasons: it is offline work measured in
minutes rather than a column read, and it needs scikit-image/scipy, which the
pure data model deliberately doesn't.

Two passes, because a whole-slide mask does not fit in memory (30k x 40k uint32
is 4.8 GB):

1. **Bounding boxes.** Walk the level-0 array one chunk at a time and
   accumulate, per label, its min/max row and column plus a pixel count.
   Memory is O(objects), not O(pixels).
2. **Trace.** For each label, read back only its own bounding box plus a
   one-pixel halo -- so an object spanning several chunks still comes out
   whole, and the halo guarantees marching squares sees background on every
   side rather than clipping the ring at the crop edge.

Then simplify. Marching squares emits roughly one vertex per pixel step, so a
30 px cell arrives with ~120 vertices where Xenium's own boundaries carry ~25.
Vertex count is what the tile store, the GPU buffers and the earcut loop all
scale with, so Douglas-Peucker (`approximate_polygon`) is not an optional
polish step here.

Known limits, both fixable and neither silent -- `polygons_from_labels` counts
what it dropped and says so:

* **Holes are dropped.** Only the longest contour of an object is kept, which
  is its outer ring. `cytos.prep.polygons` hands earcut a single ring anyway;
  earcut's own `rings` argument already supports holes when that changes.
* **A label split into disconnected pieces keeps its largest piece**, for the
  same reason -- one id has to become one polygon, because the dense cell id
  is what indexes the color LUT.

One thing that is inherent rather than a limit worth fixing: marching squares
bevels every corner by half a pixel, so a hard-edged synthetic shape comes back
with its corners cut (a 10x10 px square traces as a 4-gon of area 90.5, not
100). Real segmentation outlines are round at this scale and don't notice --
the round trip in `data/README.md` measures +1.8% area on 358 real cells -- but
it does mean tiny axis-aligned test shapes are a bad way to judge accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import zarr
from scipy.ndimage import find_objects
from skimage.measure import approximate_polygon, find_contours

from cytos.core.image import pyramid_levels
from cytos.core.polygons import Polygons
from cytos.core.store import open_zarr_group

# Pixels, for Douglas-Peucker. Below ~0.5 nothing is removed (marching squares
# already puts vertices on a half-pixel grid); much above 1.5 and small cells
# start visibly turning into triangles. 0.75 cuts the vertex count by roughly
# 4x on a real mask while staying under half a pixel of error.
DEFAULT_SIMPLIFY = 0.75

# Objects this small are single specks of segmentation noise -- and below 3
# vertices there is no ring to draw at all.
DEFAULT_MIN_AREA_PX = 4


def _resolve_multiscale(path: Path) -> tuple[zarr.Group, str]:
    """The multiscale group holding the mask, and a name for it.

    Handles both layouts in the wild: a plain OME-NGFF image whose pixels
    happen to be ids, and the NGFF `labels/<name>` convention that
    ome-zarr-py's `write_labels` and spatialdata write. The nested one cannot
    be reached by path alone inside a zipped store, which is why this descends
    through an open group rather than joining paths.
    """
    if not path.exists():
        raise ValueError(f"{path}: not an OME-Zarr label mask -- nothing there")
    try:
        root = open_zarr_group(path)
    except ValueError as err:  # zarr's GroupNotFoundError is a ValueError
        raise ValueError(f"{path}: not an OME-Zarr label mask -- no zarr store there") from err

    attrs = dict(root.attrs)
    ome = attrs.get("ome", attrs)
    if ome.get("multiscales"):
        return root, str(path)

    names = ome.get("labels")
    if names:
        name = str(names[0])
        try:
            group = root[f"labels/{name}"]
        except KeyError as err:
            raise ValueError(f"{path}: lists a label image '{name}' that isn't in the store") from err
        return group, f"{path}:labels/{name}"

    raise ValueError(
        f"{path}: a zarr store, but holds neither an OME-Zarr image nor a labels group"
    )


# Both passes read the mask in square blocks of about this many pixels a side,
# rounded up to whole chunks. Big enough that almost every object falls
# entirely inside one block (so pass 2 traces it without a second read), small
# enough to stay well inside memory -- 2048 is 16 MB at uint32.
BLOCK_PX = 2048


@dataclass
class _BlockGrid:
    """How the mask is cut up for reading. Kept as a grid, not just a list,
    because pass 2 has to answer "which block holds this object" for every
    object -- as arithmetic on the bounding boxes, not a search."""

    blocks: list[tuple[int, int, int, int]]  # (row0, row1, col0, col1)
    step_r: int
    step_c: int
    n_cols: int


def _block_grid(shape: tuple[int, int], chunks: tuple[int, int]) -> _BlockGrid:
    """Reading blocks covering the array, each a whole number of chunks so no
    chunk is ever decompressed for two different blocks."""
    height, width = shape
    step_r = max(chunks[0], (BLOCK_PX // chunks[0]) * chunks[0])
    step_c = max(chunks[1], (BLOCK_PX // chunks[1]) * chunks[1])
    n_cols = len(range(0, width, step_c))
    blocks = [
        (r0, min(r0 + step_r, height), c0, min(c0 + step_c, width))
        for r0 in range(0, height, step_r)
        for c0 in range(0, width, step_c)
    ]
    return _BlockGrid(blocks=blocks, step_r=step_r, step_c=step_c, n_cols=n_cols)


def _label_boxes(
    data: zarr.Array, blocks: list[tuple[int, int, int, int]]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pass 1: every label present in the mask, its (row0, col0, row1, col1)
    bounding box, and its pixel count.

    Per block this is two C-speed calls -- `bincount` for the counts and
    `find_objects` for the boxes -- and then a Python loop over only the labels
    that block actually held. Accumulating instead with `np.minimum.at` over
    every nonzero pixel would be correct but is a scatter over a billion
    elements, which `ufunc.at` is famously slow at.
    """
    height, width = data.shape
    row0 = np.full(1, height, dtype=np.int64)
    col0 = np.full(1, width, dtype=np.int64)
    row1 = np.full(1, -1, dtype=np.int64)
    col1 = np.full(1, -1, dtype=np.int64)
    count = np.zeros(1, dtype=np.int64)

    def grow(n: int) -> None:
        """Geometric, so a mask whose ids climb block by block doesn't pay a
        full copy for each one."""
        nonlocal row0, col0, row1, col1, count
        n = max(n, 2 * len(count), 1024)
        pad = n - len(count)
        row0 = np.concatenate([row0, np.full(pad, height, dtype=np.int64)])
        col0 = np.concatenate([col0, np.full(pad, width, dtype=np.int64)])
        row1 = np.concatenate([row1, np.full(pad, -1, dtype=np.int64)])
        col1 = np.concatenate([col1, np.full(pad, -1, dtype=np.int64)])
        count = np.concatenate([count, np.zeros(pad, dtype=np.int64)])

    for r0, r1, c0, c1 in blocks:
        block = np.asarray(data[r0:r1, c0:c1])
        block_counts = np.bincount(block.reshape(-1))
        present = np.flatnonzero(block_counts)
        present = present[present != 0]
        if not len(present):
            continue
        if int(present[-1]) >= len(count):
            grow(int(present[-1]) + 1)
        count[: len(block_counts)] += block_counts

        boxes = find_objects(block)
        for label in present:
            rows, cols = boxes[label - 1]
            row0[label] = min(row0[label], rows.start + r0)
            row1[label] = max(row1[label], rows.stop - 1 + r0)
            col0[label] = min(col0[label], cols.start + c0)
            col1[label] = max(col1[label], cols.stop - 1 + c0)

    count[0] = 0  # background is not an object
    labels = np.flatnonzero(count > 0)
    boxes = np.stack([row0[labels], col0[labels], row1[labels], col1[labels]], axis=1)
    return labels, boxes, count[labels]


def _trace(mask: np.ndarray, simplify: float) -> np.ndarray | None:
    """The object's outer ring as (row, col) float coordinates local to `mask`,
    or None if it has no traceable boundary."""
    contours = find_contours(mask.astype(np.float32), 0.5)
    if not contours:
        return None
    ring = max(contours, key=len)
    # find_contours closes its rings by repeating the first point; cytos rings
    # are open (see `polygons_from_parquet`), and so is what earcut wants.
    if len(ring) > 1 and np.array_equal(ring[0], ring[-1]):
        ring = ring[:-1]

    if simplify > 0 and len(ring) > 3:
        # Douglas-Peucker keeps the first and last point of whatever it is
        # given, and find_contours starts wherever its scan first crossed the
        # boundary -- normally the middle of a flat edge, which is the one
        # place on the ring worth keeping least. Rolling the ring to start at
        # its furthest point from the centre anchors it on a real extremity
        # instead, a vertex the simplification was going to keep anyway.
        # Worth ~1% off the vertex count and a third off the worst-case
        # centroid error on a real segmentation; not a correctness fix.
        start = int(np.argmax(((ring - ring.mean(axis=0)) ** 2).sum(axis=1)))
        ring = np.roll(ring, -start, axis=0)
        ring = approximate_polygon(np.vstack([ring, ring[:1]]), tolerance=simplify)
        if len(ring) > 1 and np.array_equal(ring[0], ring[-1]):
            ring = ring[:-1]
    return ring if len(ring) >= 3 else None


def polygons_from_labels(
    path: Path,
    simplify: float = DEFAULT_SIMPLIFY,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    progress=print,
) -> Polygons:
    """Trace an OME-Zarr label mask into the ragged-array polygon model.

    `features` carries the label value as `id`, the object's `pixel_area`, and
    its `area` in world units -- so "Color by" has something real to spread a
    ramp over without a separate attribute table, which a mask never comes
    with.
    """
    group, name = _resolve_multiscale(path)
    levels = pyramid_levels(group, name)
    level = levels[0]
    if len(level.shape) != 2:
        raise ValueError(
            f"{name}: label mask has shape {level.shape} -- expected a single 2D plane (Y, X)"
        )
    if level.data.dtype.kind not in "ui":
        raise ValueError(
            f"{name}: label mask has dtype {level.data.dtype} -- expected integer object ids"
        )

    height, width = level.shape
    grid = _block_grid(level.shape, level.data.chunks)
    progress(f"  tracing {name} ({height}x{width} px, {len(grid.blocks)} blocks)")
    labels, boxes, pixel_counts = _label_boxes(level.data, grid.blocks)
    progress(f"  {len(labels)} objects found; tracing boundaries")

    big_enough = pixel_counts >= min_area_px
    too_small = int((~big_enough).sum())
    labels, boxes, pixel_counts = labels[big_enough], boxes[big_enough], pixel_counts[big_enough]

    # Which block fully contains each object -- pure arithmetic on the boxes.
    # An object straddling a block edge gets -1 and is read on its own below;
    # at 2048 px blocks and cell-sized objects that is a handful out of many
    # thousands, so it never becomes the dominant cost.
    home = np.where(
        (boxes[:, 0] // grid.step_r == boxes[:, 2] // grid.step_r)
        & (boxes[:, 1] // grid.step_c == boxes[:, 3] // grid.step_c),
        (boxes[:, 0] // grid.step_r) * grid.n_cols + (boxes[:, 1] // grid.step_c),
        -1,
    )

    rings: list[np.ndarray | None] = [None] * len(labels)
    untraceable = 0
    traced = 0

    def trace_into(i: int, crop: np.ndarray, origin_r: int, origin_c: int) -> bool:
        # Padded rather than read with a halo, so an object touching the edge
        # of the mask behaves like every other one. Without the pad,
        # `find_contours` has no background to close against there and returns
        # an open arc, which then draws as a ring closing straight across the
        # object. The pad also means the reads below want the exact bounding
        # box and nothing around it.
        ring = _trace(np.pad(crop == labels[i], 1), simplify)
        if ring is None:
            return False
        ring[:, 0] += origin_r - 1
        ring[:, 1] += origin_c - 1
        rings[i] = ring
        return True

    # Grouped by block, so each block is read exactly once and every object
    # living inside it is traced out of that one in-memory array.
    order = np.argsort(home, kind="stable")
    # Run boundaries for values -1, 0, 1, ... n_blocks-1, plus the end: a block
    # no object lives in simply gets an empty slice.
    bounds = np.append(np.searchsorted(home[order], np.arange(-1, len(grid.blocks))), len(order))
    for bi in range(len(grid.blocks)):
        members = order[bounds[bi + 1] : bounds[bi + 2]]
        if not len(members):
            continue
        r0, r1, c0, c1 = grid.blocks[bi]
        block = np.asarray(level.data[r0:r1, c0:c1])
        for i in members:
            br0, bc0, br1, bc1 = boxes[i]
            crop = block[br0 - r0 : br1 + 1 - r0, bc0 - c0 : bc1 + 1 - c0]
            if not trace_into(int(i), crop, br0, bc0):
                untraceable += 1
            traced += 1
        if traced % 50000 < len(members):
            progress(f"    traced {traced}/{len(labels)}")

    for i in order[: bounds[1]]:  # the straddlers, read one bounding box each
        br0, bc0, br1, bc1 = boxes[i]
        crop = np.asarray(level.data[br0 : br1 + 1, bc0 : bc1 + 1])
        if not trace_into(int(i), crop, br0, bc0):
            untraceable += 1

    keep = [i for i, ring in enumerate(rings) if ring is not None]
    if not keep:
        raise ValueError(f"{name}: no traceable objects in the label mask")
    rings = [rings[i] for i in keep]
    kept_labels = [int(v) for v in labels[keep]]
    kept_pixels = [int(v) for v in pixel_counts[keep]]

    counts = np.array([len(r) for r in rings], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(counts)]).astype(np.uint32)
    stacked = np.concatenate(rings)  # (M, 2) as (row, col)

    # Pixel -> world. A plain scale and offset in both axes: world Y increases
    # downward, the same direction as pixel rows, so nothing is flipped here
    # (see `PyramidLevel.world_bounds`).
    sy, sx = level.scale
    ty, tx = level.translation
    coords = np.empty((len(stacked), 2), dtype=np.float32)
    coords[:, 0] = tx + stacked[:, 1] * sx
    coords[:, 1] = ty + stacked[:, 0] * sy
    coords = np.ascontiguousarray(coords)

    pixel_area = np.array(kept_pixels, dtype=np.int64)
    features = pa.table(
        {
            "id": pa.array(kept_labels, type=pa.int64()),
            "pixel_area": pixel_area,
            "area": pixel_area.astype(np.float64) * abs(sy * sx),
        }
    )

    dropped = []
    if too_small:
        dropped.append(f"{too_small} under {min_area_px} px")
    if untraceable:
        dropped.append(f"{untraceable} with no traceable ring")
    progress(
        f"  traced {len(rings)} objects, {len(coords)} vertices"
        + (f" (dropped {', '.join(dropped)})" if dropped else "")
    )

    return Polygons(
        coords=coords,
        offsets=offsets,
        cell_id=np.arange(len(rings), dtype=np.uint32),
        features=features,
    )


def label_mask_world_bounds(path: Path) -> tuple[float, float, float, float]:
    """The mask's own extent, without tracing it -- what the importer unions
    into the slide's world bounds before deciding anything."""
    group, name = _resolve_multiscale(path)
    return pyramid_levels(group, name)[0].world_bounds()
