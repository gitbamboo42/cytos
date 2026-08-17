/**
 * The `.cytos` manifest: what a slide says about itself.
 *
 * Pure model — types, the layer selectors, and the world-space arithmetic.
 * Nothing here reads bytes or knows about deck.gl; `io/` does the reading,
 * and both viewers agree on the shapes described here.
 */

export interface ImageLayerSpec {
  kind: 'image';
  id: string;
  path: string;
  format: string;
  colormap: string;
  clim?: [number, number];
  /** Largest value in the channel, measured at import. Absent on slides
   * written before it was recorded, which makes the reader measure it. */
  intensity_max?: number;
  visible?: boolean;
}

export interface SegmentLayerSpec {
  kind: 'segments';
  id: string;
  path: string;
  format: string;
  tile_depth: number;
  tiles: [number, number][];
  n_cells: number;
  show_outline?: boolean;
  show_fill?: boolean;
  fill_opacity?: number;
  visible?: boolean;
  color_by?: string | null;
  colormap?: string;
}

export interface SlideManifest {
  cytos_format: number;
  name: string;
  world_units: string;
  world_bounds: [number, number, number, number];
  tile_depth: number;
  layers: Array<
    ImageLayerSpec | SegmentLayerSpec | { kind: string; id: string; path: string }
  >;
}

/** The largest slide format this reader understands. Bumped in lockstep
 * with `CYTOS_FORMAT` in `src/cytos/core/slide.py`. */
export const CYTOS_FORMAT = 1;

export function imageLayers(manifest: SlideManifest): ImageLayerSpec[] {
  return manifest.layers.filter((l): l is ImageLayerSpec => l.kind === 'image');
}

export function segmentLayers(manifest: SlideManifest): SegmentLayerSpec[] {
  return manifest.layers.filter((l): l is SegmentLayerSpec => l.kind === 'segments');
}

/** Side length of one vector tile in world units — same formula as
 * `tile_world_size` in `src/cytos/core/tiling.py`: a square grid over the
 * slide's longest axis. */
export function tileWorldSize(manifest: SlideManifest): number {
  const [minx, miny, maxx, maxy] = manifest.world_bounds;
  return Math.max(maxx - minx, maxy - miny) / (1 << manifest.tile_depth);
}
