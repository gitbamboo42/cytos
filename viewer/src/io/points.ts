/**
 * Read a point (transcript) layer's tiles.
 *
 * Points are the third primitive beside the image and the polygon set, and
 * the flat one: one row per transcript, so a tile is just `coords` plus a
 * dense `gene_id` into a shared gene table (`src/cytos/core/points.py`).
 * That density is what lets colour-by-gene be a small palette lookup rather
 * than a per-point buffer rewrite.
 *
 * **The cache has detail levels, and they are the same tiles.** Level k lives
 * at grid depth `tile_depth - k`; level 0 is every real transcript, coarser
 * levels hold one weighted dot per (region, gene) — a real position, with
 * `count` saying how many it stands for. Every level has the same schema, so
 * reading one is the same read. A tile exists at a coarse depth exactly when
 * one of its children exists, so the coarse index is derived from the
 * manifest's fine one by shifting, never probed for — the same trick
 * `PointTileGrid.tiles_at` uses in Python.
 */

import * as zarr from 'zarrita';

import type { PointLayerSpec } from '../core/manifest';
import { tableFromParquet } from './features';
import { openStore, type ReadRange } from './read';

const FORMATS = { dir: 'zarr-tiles-v1', zip: 'zarr-zip-tiles-v1' };

/**
 * Read optimisation only, never a behaviour boundary — `GENE_SLICE_MAX` in
 * `src/cytos/render/points.py`. Up to this many genes, a tile is read as a
 * few contiguous slices through its gene index; past it, slicing stops being
 * cheaper than reading the tile once and filtering in memory. The same points
 * end up on screen either way, which is why the fallback below is allowed to
 * be silent.
 */
const GENE_SLICE_MAX = 256;

export interface PointTile {
  /** Flat (x, y) pairs, world µm. */
  coords: Float32Array;
  /** Dense gene id per point — indexes the gene table. */
  geneId: Uint32Array;
  /** How many transcripts each dot stands for; absent at full detail, where
   * every dot is one transcript. */
  count?: Uint32Array;
}

/** Row i describes dense gene id i. `count` is the whole-slide abundance, so
 * a gene list can be ordered without touching a single point. */
export interface GeneTable {
  names: string[];
  counts: Uint32Array;
}

export class PointTileSource {
  private root: Promise<zarr.Location<zarr.AsyncReadable>>;
  private tiles: Set<string>;
  /** Coarse indexes, derived once each, keyed by depth. */
  private coarse = new Map<number, Set<string>>();

  constructor(read: ReadRange, readonly spec: PointLayerSpec) {
    this.root = openStore(
      read,
      `point layer "${spec.id}"`,
      spec.format,
      `${spec.path}/tiles.zarr`,
      FORMATS,
    ).then((store) => zarr.root(store));
    this.tiles = new Set(spec.tiles.map(([r, c]) => `${r},${c}`));
  }

  /** How many detail levels the cache holds; 1 means full detail only. */
  get levels(): number {
    return Math.max(this.spec.levels ?? 1, 1);
  }

  /** The finest depth — where the manifest's tile index applies. */
  get fineDepth(): number {
    return this.spec.tile_depth;
  }

  has(row: number, col: number, depth: number): boolean {
    if (depth === this.fineDepth) return this.tiles.has(`${row},${col}`);
    let set = this.coarse.get(depth);
    if (!set) {
      const shift = this.fineDepth - depth;
      set = new Set<string>();
      for (const key of this.tiles) {
        const [r, c] = key.split(',');
        set.add(`${Number(r) >> shift},${Number(c) >> shift}`);
      }
      this.coarse.set(depth, set);
    }
    return set.has(`${row},${col}`);
  }

  /**
   * Null for a tile this layer never wrote.
   *
   * `genes` — dense ids — draws only those genes. A small selection is read
   * through the tile's own gene index as a handful of contiguous slices; a
   * large one, or a tile written before the index existed, reads the tile
   * once and filters in memory. Same points either way.
   */
  async tile(
    row: number,
    col: number,
    depth: number,
    genes?: number[] | null,
  ): Promise<PointTile | null> {
    if (!this.has(row, col, depth)) return null;
    if (genes && genes.length === 0) {
      return { coords: new Float32Array(0), geneId: new Uint32Array(0) };
    }
    const root = await this.root;
    const base = `tile/${depth}/${row}/${col}`;
    const open = (name: string) =>
      zarr.open.v3(root.resolve(`${base}/${name}`), { kind: 'array' }) as Promise<
        zarr.Array<zarr.NumberDataType>
      >;
    const whole = async (name: string) => (await zarr.get(await open(name))).data;
    const hasCount = depth !== this.fineDepth;

    if (genes && genes.length <= GENE_SLICE_MAX) {
      const sliced = await this.sliceGenes(open, genes, hasCount);
      if (sliced) return sliced;
    }

    const [coords, geneId] = (await Promise.all([whole('coords'), whole('gene_id')])) as [
      Float32Array,
      Uint32Array,
    ];
    let count: Uint32Array | undefined;
    if (hasCount) {
      try {
        count = (await whole('count')) as Uint32Array;
      } catch {
        count = undefined;
      }
    }
    const tile: PointTile = { coords, geneId, count };
    return genes ? filterByGene(tile, genes) : tile;
  }

  /** Contiguous runs for the wanted genes, or null when this tile carries no
   * gene index (an older cache) — the caller then reads it whole. */
  private async sliceGenes(
    open: (name: string) => Promise<zarr.Array<zarr.NumberDataType>>,
    genes: number[],
    hasCount: boolean,
  ): Promise<PointTile | null> {
    let geneIds: ArrayLike<number | bigint>;
    let geneStarts: ArrayLike<number | bigint>;
    try {
      geneIds = (await zarr.get(await open('gene_ids'))).data as ArrayLike<number | bigint>;
      geneStarts = (await zarr.get(await open('gene_starts'))).data as ArrayLike<number | bigint>;
    } catch {
      return null;
    }
    // The index is written as int64, so zarrita hands back BigInts — and a
    // BigInt cannot be compared or sliced with plain numbers without an
    // explicit conversion. Coerce once here rather than at every use.
    const want = new Set(genes);
    const spans: Array<[number, number]> = [];
    for (let i = 0; i < geneIds.length; i++) {
      if (want.has(Number(geneIds[i]))) {
        spans.push([Number(geneStarts[i]), Number(geneStarts[i + 1])]);
      }
    }
    if (spans.length === 0) {
      return { coords: new Float32Array(0), geneId: new Uint32Array(0) };
    }

    const coordsArr = await open('coords');
    const geneIdArr = await open('gene_id');
    const countArr = hasCount ? await open('count').catch(() => null) : null;

    const parts = await Promise.all(
      spans.map(async ([from, to]) => ({
        coords: (await zarr.get(coordsArr, [zarr.slice(from, to), null])).data as Float32Array,
        geneId: (await zarr.get(geneIdArr, [zarr.slice(from, to)])).data as Uint32Array,
        count: countArr
          ? ((await zarr.get(countArr, [zarr.slice(from, to)])).data as Uint32Array)
          : null,
      })),
    );

    const n = parts.reduce((sum, part) => sum + part.geneId.length, 0);
    const coords = new Float32Array(n * 2);
    const geneId = new Uint32Array(n);
    const count = countArr ? new Uint32Array(n) : undefined;
    let at = 0;
    for (const part of parts) {
      coords.set(part.coords, at * 2);
      geneId.set(part.geneId, at);
      if (count && part.count) count.set(part.count, at);
      at += part.geneId.length;
    }
    return { coords, geneId, count };
  }
}

/** Keep only the wanted genes. The slow path, for tiles with no gene index
 * or a selection too big for slicing to pay. */
function filterByGene(tile: PointTile, genes: number[]): PointTile {
  const want = new Set(genes);
  const keep: number[] = [];
  for (let i = 0; i < tile.geneId.length; i++) {
    if (want.has(tile.geneId[i])) keep.push(i);
  }
  const coords = new Float32Array(keep.length * 2);
  const geneId = new Uint32Array(keep.length);
  const count = tile.count ? new Uint32Array(keep.length) : undefined;
  for (let j = 0; j < keep.length; j++) {
    const i = keep[j];
    coords[j * 2] = tile.coords[i * 2];
    coords[j * 2 + 1] = tile.coords[i * 2 + 1];
    geneId[j] = tile.geneId[i];
    if (count && tile.count) count[j] = tile.count[i];
  }
  return { coords, geneId, count };
}

/** The layer's gene table. Read once per layer, not per tile. */
export async function loadGenes(
  read: ReadRange,
  layerPath: string,
): Promise<GeneTable | null> {
  const table = await tableFromParquet(read, `${layerPath}/genes.parquet`);
  if (!table) return null;
  const nameCol = table.getChild('name');
  const countCol = table.getChild('count');
  if (!nameCol) return null;
  const names: string[] = [];
  for (let i = 0; i < nameCol.length; i++) names.push(String(nameCol.get(i)));
  const counts = new Uint32Array(names.length);
  if (countCol) {
    for (let i = 0; i < counts.length; i++) counts[i] = Number(countCol.get(i) ?? 0);
  }
  return { names, counts };
}
