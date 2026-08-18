/**
 * Point (transcript) layers: one deck TileLayer per point layer, each tile a
 * ScatterplotLayer built from the tile's own binary arrays.
 *
 * **Which detail level to draw is a zoom question, not a tile question.**
 * `selectPointLevel` is the twin of `select_point_level` in
 * `src/cytos/core/points.py`, down to the 0.5 µm/px ladder: each aggregation
 * level bins two-by-two, so it quarters the dots and doubles the scale a dot
 * can stand for, exactly as an image pyramid's levels double their pixel
 * size. The scene picks the level and hands it down; the tile grid at that
 * level is the same flat grid, one depth coarser per level.
 *
 * Dots are sized in screen pixels (Qt draws them with `size_space="screen"`),
 * so a transcript stays the same size as you zoom rather than growing with
 * the tissue.
 */

import { TileLayer } from '@deck.gl/geo-layers';
import { ScatterplotLayer } from '@deck.gl/layers';
import { Matrix4 } from '@math.gl/core';

import { colorValueRgb, presetGeneColor } from '../core/colormaps';
import { tileWorldSize } from '../core/manifest';
import type { PointSettings } from '../core/session';
import type { PointTile, PointTileSource } from '../io/points';
import type { LoadedSlide } from '../io/slide';

/**
 * World units per screen pixel at which full detail stops being worth it —
 * `FULL_DETAIL_WORLD_PER_PX` in `src/cytos/core/points.py`. Below it every
 * real transcript is drawn; above it, one aggregate dot stands for several.
 */
const FULL_DETAIL_WORLD_PER_PX = 0.5;

/**
 * Aggregate dot diameters in screen pixels, before the layer's size scaling —
 * `_AGG_SIZE_MIN`/`_AGG_SIZE_MAX` in `src/cytos/render/points.py`. The bin
 * grid puts neighbouring dots ~3-6 px apart at the zooms a level serves, so
 * the largest dot has to stay near that spacing: sized past it, dense tissue
 * floods into one solid mass and the density reading is gone. Sizes spread
 * sqrt(count / tile max) across the range — area tracks count, normalised
 * within the tile so hotspots stand out of dense tissue instead of
 * everything saturating.
 */
const AGG_SIZE_MIN = 1.2;
const AGG_SIZE_MAX = 6.0;

/** The size a layer's `size` setting is relative to — Qt's default, so
 * `size` 3 means "the diameters above, unscaled". */
const BASE_POINT_SIZE = 3;

/**
 * Which detail level a zoom deserves: 0 is full detail, k is the level at
 * grid depth `tile_depth - k`. Same ladder as Python, so the two viewers
 * swap levels at the same zooms.
 */
export function selectPointLevel(levels: number, worldPerPx: number): number {
  const n = Math.max(levels, 1);
  for (let k = n - 1; k > 0; k--) {
    if (worldPerPx >= FULL_DETAIL_WORLD_PER_PX * (1 << k)) return k;
  }
  return 0;
}

/**
 * RGBA per point: one palette colour per gene, or one flat colour for all.
 *
 * A gene's colour is fixed by its dense id (`presetGeneColor`), so selecting
 * a second gene never recolours the first and the circle in the gene list is
 * always the dot on the slide. This departs from `render/points.py`, which
 * spends the palette by selection rank — worth knowing if the two viewers are
 * ever compared side by side. With 514 genes and ten colours, either way the
 * palette repeats; a stable mapping at least repeats predictably.
 */
function pointColors(tile: PointTile, settings: PointSettings): Uint8Array {
  const n = tile.geneId.length;
  const out = new Uint8Array(n * 4);
  const alpha = Math.round(255 * settings.opacity);
  if (settings.color_mode === 'gene') {
    // Resolved once per gene, not once per point.
    const resolved = new Map<number, [number, number, number]>();
    const colorOf = (id: number) => {
      let rgb = resolved.get(id);
      if (!rgb) {
        rgb = colorValueRgb(settings.gene_colors?.[id] ?? presetGeneColor(id));
        resolved.set(id, rgb);
      }
      return rgb;
    };
    for (let i = 0; i < n; i++) {
      const [r, g, b] = colorOf(tile.geneId[i]);
      const j = i * 4;
      out[j] = r;
      out[j + 1] = g;
      out[j + 2] = b;
      out[j + 3] = alpha;
    }
  } else {
    const [r, g, b] = colorValueRgb(settings.colormap);
    for (let i = 0; i < n; i++) {
      const j = i * 4;
      out[j] = r;
      out[j + 1] = g;
      out[j + 2] = b;
      out[j + 3] = alpha;
    }
  }
  return out;
}

/** Per-point radius in screen pixels. Full-detail dots are all one size; an
 * aggregate dot grows with the square root of what it stands for. */
function pointRadii(tile: PointTile, settings: PointSettings): Float32Array {
  const n = tile.geneId.length;
  const out = new Float32Array(n);
  if (!tile.count) {
    out.fill(settings.size / 2);
    return out;
  }
  let max = 1;
  for (let i = 0; i < n; i++) if (tile.count[i] > max) max = tile.count[i];
  const scale = settings.size / BASE_POINT_SIZE;
  const span = AGG_SIZE_MAX - AGG_SIZE_MIN;
  for (let i = 0; i < n; i++) {
    const diameter = AGG_SIZE_MIN + span * Math.sqrt(tile.count[i] / max);
    out[i] = (diameter * scale) / 2;
  }
  return out;
}

/** One tile's arrays in deck's binary form. Positions are fixed at fetch;
 * colour and radius are swapped in place when the settings change (see
 * `stylePointTile`), which deck picks up through updateTriggers without
 * refetching the tile. */
interface DeckPointTile {
  length: number;
  attributes: {
    getPosition: { value: Float32Array; size: number };
    getFillColor?: { value: Uint8Array; size: number; normalized: boolean };
    getRadius?: { value: Float32Array; size: number };
  };
  /** The raw tile, kept so restyling never needs the store again. */
  source: PointTile;
}

/** Which styling each tile's attribute arrays currently hold, so a settings
 * change rebuilds them once and an unchanged one rebuilds nothing. */
const styleState = new WeakMap<DeckPointTile, string>();

/**
 * Apply the current settings to a tile, returning the key that identifies
 * them. Colour and size live here rather than in `getTileData` because a
 * tile is fetched once and restyled every time a slider moves — computing
 * them at fetch time is what made size and opacity wait for a refetch.
 */
function stylePointTile(tile: DeckPointTile, settings: PointSettings): string {
  // The gene selection is not in the key: changing it changes the layer id
  // (see `pointTileLayer`), so no tile survives it to be restyled.
  const key = `${settings.size}|${settings.opacity}|${settings.color_mode}|${settings.colormap}|${JSON.stringify(settings.gene_colors ?? {})}`;
  if (styleState.get(tile) !== key) {
    tile.attributes.getFillColor = {
      value: pointColors(tile.source, settings),
      size: 4,
      normalized: true,
    };
    tile.attributes.getRadius = { value: pointRadii(tile.source, settings), size: 1 };
    styleState.set(tile, key);
  }
  return key;
}

/** A short, stable stand-in for the selection, for use in a layer id. Joining
 * the ids themselves put 500-odd numbers in a string that deck compares on
 * every update; a count plus a checksum distinguishes selections just as well
 * at a fixed size. */
function geneSignature(genes: number[] | null): string {
  if (!genes) return 'all';
  let sum = 0;
  for (const id of genes) sum = (sum * 31 + id) >>> 0;
  return `${genes.length}.${sum.toString(36)}`;
}

export function pointTileLayer(
  slide: LoadedSlide,
  source: PointTileSource,
  settings: PointSettings,
  level: number,
) {
  const spec = source.spec;
  const [minx, miny, maxx, maxy] = slide.manifest.world_bounds;
  const s = slide.pixelSize;
  const depth = source.fineDepth - level;
  // World µm -> full-res pixel coords, the space the view works in.
  const worldToPixels = new Matrix4().scale([1 / s, 1 / s, 1]);

  return new TileLayer<DeckPointTile | null>({
    // Level and selection are both in the id: each is a different set of
    // points to fetch, so deck should cache them apart rather than reuse
    // tiles that hold the wrong genes.
    id: `points-${spec.id}-L${level}-g${geneSignature(settings.genes)}`,
    visible: settings.visible,
    // One tile of this level, in view (pixel) units. Coarser levels are the
    // same grid with bigger squares.
    tileSize: (tileWorldSize(slide.manifest) * (1 << level)) / s,
    minZoom: 0,
    maxZoom: 0,
    extent: [minx / s, miny / s, maxx / s, maxy / s],
    // deck ignores function-prop identity, so a setting that changes how a
    // tile looks has to be declared here or already-loaded tiles keep their
    // old styling. Pinned gene colours are one of those — they were missing,
    // and a colour pick only showed up on tiles fetched afterwards.
    updateTriggers: {
      renderSubLayers: [
        settings.size,
        settings.opacity,
        settings.color_mode,
        settings.colormap,
        JSON.stringify(settings.gene_colors ?? {}),
      ],
    },
    // Built here, once per tile, so the arrays keep their identity — the same
    // rule the segment layer follows, and for the same reason.
    getTileData: async ({ index: { x, y } }) => {
      const tile = await source.tile(y, x, depth, settings.genes);
      if (!tile) return null;
      return {
        length: tile.geneId.length,
        attributes: { getPosition: { value: tile.coords, size: 2 } },
        source: tile,
      };
    },
    renderSubLayers: (props) => {
      const tile = props.data;
      if (!tile || tile.length === 0) return null;
      const styleKey = stylePointTile(tile, settings);
      return new ScatterplotLayer({
        id: `${props.id}-dots`,
        data: tile,
        updateTriggers: { getFillColor: styleKey, getRadius: styleKey },
        radiusUnits: 'pixels',
        // Dots are already the size they should be; deck must not clamp them
        // back to its own default range.
        radiusMinPixels: 0,
        radiusMaxPixels: 64,
        stroked: false,
        modelMatrix: worldToPixels,
      });
    },
  });
}
