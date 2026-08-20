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
  /** Qualitative palette for a categorical `color_by`. */
  palette?: string;
  /** feature -> {category key -> "#rrggbb"}: colours picked by hand, on top
   * of the palette. feature -> [category key, ...] for the ones drawn not at
   * all. Both are session state — the importer writes neither, so a slide's
   * defaults are empty. Same fields as `SegmentLayer` in
   * `src/cytos/core/slide.py`. */
  category_colors?: Record<string, Record<string, string>>;
  hidden_categories?: Record<string, string[]>;
}

export interface PointLayerSpec {
  kind: 'points';
  id: string;
  path: string;
  format: string;
  tile_depth: number;
  tiles: [number, number][];
  n_points: number;
  /** Detail levels in the cache: level k lives at grid depth
   * `tile_depth - k`, level 0 is every real point. 1 means full detail only
   * (a slide written before levels existed). */
  levels?: number;
  /** Qualitative palette name for colour-per-gene. */
  palette?: string;
  /** Flat colour, used when `color_mode` is not "gene". */
  colormap?: string;
  color_mode?: string;
  /** Dense gene ids the slide opens with; absent means every gene. */
  genes?: number[] | null;
  gene_colors?: Record<number, string>;
  size?: number;
  opacity?: number;
  visible?: boolean;
}

/** Short forms of the manifest's `world_units`, same table as
 * `_UNIT_ABBREV` in `src/cytos/ui/scale_bar.py`. A unit this doesn't know is
 * shown in full rather than silently relabelled. */
const UNIT_ABBREV: Record<string, string> = {
  micrometer: 'µm',
  micron: 'µm',
  millimeter: 'mm',
  nanometer: 'nm',
};

export function unitAbbrev(units: string): string {
  return UNIT_ABBREV[units] ?? units;
}

export interface SlideManifest {
  cytos_format: number;
  name: string;
  world_units: string;
  world_bounds: [number, number, number, number];
  tile_depth: number;
  layers: Array<
    | ImageLayerSpec
    | SegmentLayerSpec
    | PointLayerSpec
    | { kind: string; id: string; path: string }
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

export function pointLayers(manifest: SlideManifest): PointLayerSpec[] {
  return manifest.layers.filter((l): l is PointLayerSpec => l.kind === 'points');
}

/** Side length of one vector tile in world units — same formula as
 * `tile_world_size` in `src/cytos/core/tiling.py`: a square grid over the
 * slide's longest axis. */
export function tileWorldSize(manifest: SlideManifest): number {
  const [minx, miny, maxx, maxy] = manifest.world_bounds;
  return Math.max(maxx - minx, maxy - miny) / (1 << manifest.tile_depth);
}
