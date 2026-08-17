/**
 * Read a segment layer's `features.parquet` — the per-cell attribute table,
 * already permuted into dense-cell-id order at import time, so row i IS
 * cell i and a feature column indexes directly by the tile's cell ids.
 *
 * Read via parquet-wasm into Arrow IPC rather than a pure-JS parquet
 * reader, because categorical columns (clusterings) are marked by the
 * *Arrow field metadata* `cytos:categorical` (see `CATEGORICAL_META` in
 * `src/cytos/core/polygons.py` — parquet drops the dictionary type, and
 * nothing anywhere matches on column names). Only readers that surface the
 * Arrow schema can see that marker.
 */

import { tableFromIPC } from 'apache-arrow';
import initParquetWasm, { readParquet } from 'parquet-wasm/esm';
// The wasm binary as a vite-managed asset URL — letting wasm-bindgen locate
// it relative to import.meta.url breaks under the dev server's pre-bundling.
import parquetWasmUrl from 'parquet-wasm/esm/parquet_wasm_bg.wasm?url';

import type { ReadRange } from './read';

// Numeric but not a measurement of the cell: ids are arbitrary labels and
// the centroids re-encode position. Same list as the Qt viewer's
// feature_names() in src/cytos/core/polygons.py.
const NON_MEASUREMENT = new Set(['id', 'cell_id', 'x_centroid', 'y_centroid']);

export interface Feature {
  name: string;
  categorical: boolean;
  /** Value per dense cell id; NaN = no value ("unassigned"). */
  values: Float64Array;
  /** Ramp domain: the 2nd/98th percentile of real values (min/max when the
   * percentiles collapse) — same robust range as `feature_colors` in
   * `src/cytos/render/polygons.py`, so a heavy-tailed feature like
   * transcript_counts doesn't pin every cell to the ramp's bottom.
   * Meaningless when categorical. */
  domain: [number, number];
}

function robustDomain(values: Float64Array): [number, number] {
  const finite = Array.from(values.filter((v) => !Number.isNaN(v))).sort((a, b) => a - b);
  if (finite.length === 0) return [0, 1];
  let lo = finite[Math.floor(0.02 * (finite.length - 1))];
  let hi = finite[Math.ceil(0.98 * (finite.length - 1))];
  if (hi <= lo) {
    lo = finite[0];
    hi = finite[finite.length - 1];
  }
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

export class FeatureTable {
  constructor(readonly features: Map<string, Feature>) {}

  get names(): string[] {
    return [...this.features.keys()];
  }

  get(name: string): Feature | undefined {
    return this.features.get(name);
  }
}

let wasmReady: Promise<unknown> | null = null;

export async function loadFeatures(
  read: ReadRange,
  layerPath: string,
): Promise<FeatureTable | null> {
  const bytes = await read(`${layerPath}/features.parquet`);
  if (!bytes) return null;
  wasmReady ??= initParquetWasm({ module_or_path: parquetWasmUrl });
  await wasmReady;

  const table = tableFromIPC(readParquet(bytes).intoIPCStream());
  const features = new Map<string, Feature>();
  for (const field of table.schema.fields) {
    if (NON_MEASUREMENT.has(field.name)) continue;
    const categorical = field.metadata?.get('cytos:categorical') === 'true';
    const column = table.getChild(field.name);
    if (!column) continue;
    const typeId = field.typeId as number;
    // Arrow type ids: 2 = Int, 3 = Float. Anything else isn't colorable.
    if (typeId !== 2 && typeId !== 3) continue;

    const n = column.length;
    const values = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const v = column.get(i);
      values[i] = v === null || v === undefined ? NaN : Number(v);
    }
    features.set(field.name, {
      name: field.name,
      categorical,
      values,
      domain: robustDomain(values),
    });
  }
  return new FeatureTable(features);
}
