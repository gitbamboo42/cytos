/**
 * Read a segment (polygon) layer's tiles over HTTP.
 *
 * Per tile the store keeps flat ring vertices (`coords`, world µm), a dense
 * cell id per vertex, and precomputed triangle indices. Ring boundaries are
 * not stored separately: each cell's vertices are one contiguous run of
 * `vertex_cell_id` (see `prep_polygons` in `src/cytos/prep/polygons.py`), so
 * run starts are ring starts. That run structure is exactly deck.gl's binary
 * attribute format (flat positions + startIndices), which is why this reader
 * returns typed arrays and never builds per-polygon JS objects.
 *
 * The triangle indices go unused for now — deck.gl's SolidPolygonLayer
 * re-triangulates internally. Skipping them also skips a third of the bytes;
 * a custom mesh layer can pick them up later.
 */

import * as zarr from 'zarrita';

import type { SegmentLayerSpec } from '../core/manifest';
import { openStore, type ReadRange } from './read';

const FORMATS = { dir: 'zarr-tiles-v1', zip: 'zarr-zip-tiles-v1' };

export interface SegmentTile {
  /** One polygon per cell: ring i is positions[startIndices[i]..startIndices[i+1]]. */
  length: number;
  startIndices: Uint32Array;
  positions: Float32Array; // flat (x, y) pairs, world µm
  /** Dense cell id per polygon — row index into features.parquet. */
  cellIds: Uint32Array;
  /** The same id per vertex — deck's binary accessors take one value per
   * vertex, so per-cell colors expand through this. */
  vertexCellIds: Uint32Array;
}

export class SegmentTileSource {
  /** Opening a zipped store reads its central directory, so the root is a
   * promise — started once here, awaited by every tile, never repeated. */
  private root: Promise<zarr.Location<zarr.AsyncReadable>>;
  private tiles: Set<string>;

  constructor(
    read: ReadRange,
    readonly spec: SegmentLayerSpec,
  ) {
    this.root = openStore(
      read,
      `segment layer "${spec.id}"`,
      spec.format,
      `${spec.path}/tiles.zarr`,
      FORMATS,
    ).then((store) => zarr.root(store));
    this.tiles = new Set(spec.tiles.map(([r, c]) => `${r},${c}`));
  }

  has(row: number, col: number): boolean {
    return this.tiles.has(`${row},${col}`);
  }

  /** Null for a tile the layer never wrote — most of the grid, since tissue
   * never fills its own bounding square. */
  async tile(row: number, col: number): Promise<SegmentTile | null> {
    const stats = ((window as any).__tileStats ??= {
      ok: 0,
      skipped: 0,
      failed: 0,
      fetchMs: 0,
      inFlight: 0,
      maxInFlight: 0,
    });
    if (!this.has(row, col)) {
      stats.skipped++;
      return null;
    }
    try {
      stats.inFlight++;
      stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
      const t0 = performance.now();
      const t = await this.tileInner(row, col);
      stats.fetchMs += performance.now() - t0;
      stats.inFlight--;
      stats.ok++;
      return t;
    } catch (err) {
      stats.inFlight--;
      stats.failed++;
      console.error(`tile ${row},${col}:`, err);
      throw err;
    }
  }

  private async tileInner(row: number, col: number): Promise<SegmentTile> {

    const root = await this.root;
    const load = async (name: string) => {
      const path = `tile/${this.spec.tile_depth}/${row}/${col}/${name}`;
      const arr = await zarr.open.v3(root.resolve(path), { kind: 'array' });
      return (await zarr.get(arr as zarr.Array<zarr.NumberDataType>)).data;
    };
    const [positions, vertexCellId] = await Promise.all([
      load('coords') as Promise<Float32Array>,
      load('vertex_cell_id') as Promise<Uint32Array>,
    ]);

    // Runs of vertex_cell_id -> ring startIndices plus one id per ring.
    const n = vertexCellId.length;
    const starts: number[] = [0];
    const ids: number[] = n ? [vertexCellId[0]] : [];
    for (let i = 1; i < n; i++) {
      if (vertexCellId[i] !== vertexCellId[i - 1]) {
        starts.push(i);
        ids.push(vertexCellId[i]);
      }
    }
    starts.push(n);

    return {
      length: ids.length,
      startIndices: Uint32Array.from(starts),
      positions,
      cellIds: Uint32Array.from(ids),
      vertexCellIds: vertexCellId,
    };
  }
}
