/**
 * Polygon layers: one deck TileLayer per segment layer, each tile drawn as
 * two sublayers from the same binary arrays — a SolidPolygonLayer fill that
 * doubles as the pick surface (kept even at opacity 0, same trick as the Qt
 * renderer, where hiding the fill would kill hover picking), and a LineLayer
 * drawing each ring edge as an independent segment (see ringSegments).
 */

import { TileLayer } from '@deck.gl/geo-layers';
import { LineLayer, SolidPolygonLayer } from '@deck.gl/layers';
import type { PickingInfo } from '@deck.gl/core';
import { Matrix4 } from '@math.gl/core';

import { colorValueRgb, rampLut, TAB20, UNASSIGNED_COLOR } from '../core/colormaps';
import { tileWorldSize } from '../core/manifest';
import type { SegmentSettings } from '../core/session';
import type { Feature, FeatureTable } from '../io/features';
import type { SegmentTileSource } from '../io/segments';
import type { LoadedSlide } from '../io/slide';

/** Outline width and alpha, matching the Qt renderer so the two viewers
 * look alike: `LineSegmentMaterial(thickness=1.5, thickness_space="screen")`
 * in `src/cytos/render/polygons.py`, coloured from a matplotlib LUT whose
 * alpha is 255. A 1 px line at alpha 220 reads as visibly fainter. */
const OUTLINE_WIDTH = 1.5;
const OUTLINE_ALPHA = 255;

/** One tile's arrays, wrapped the way deck's binary-attribute path wants
 * them — built once per tile in getTileData (see the note there). The
 * color attributes are the exception: they are swapped in place when the
 * coloring settings change (see colorizeTile), which deck picks up through
 * updateTriggers without touching the tessellated positions. */
interface DeckSegmentTile {
  fill: {
    length: number;
    startIndices: Uint32Array;
    attributes: {
      getPolygon: { value: Float32Array; size: number };
      getFillColor?: { value: Uint8Array; size: number; normalized: boolean };
    };
    cellIds: Uint32Array;
    vertexCellIds: Uint32Array;
  };
  outline: {
    length: number;
    attributes: {
      getSourcePosition: { value: Float32Array; size: number };
      getTargetPosition: { value: Float32Array; size: number };
      getColor?: { value: Uint8Array; size: number; normalized: boolean };
    };
    segmentCellIds: Uint32Array;
  };
}

/** Ring edges as independent (source, target) segment pairs — the same
 * interleaving Qt's `_make_outline` does for `LineSegmentMaterial`, and for
 * the same reason: a plain segment is far cheaper on the GPU than PathLayer's
 * miter-joined quads (4 projected positions and 6 vertices per segment, which
 * measured at ~8 fps against LineLayer during a drag over a full slide). The
 * rings arrive closed (first vertex repeated), so consecutive vertices within
 * a ring are exactly the segments and no cross-ring edge is ever emitted. */
function ringSegments(positions: Float32Array, startIndices: Uint32Array, cellIds: Uint32Array) {
  const nRings = startIndices.length - 1;
  const nSegments = startIndices[nRings] - nRings;
  const source = new Float32Array(nSegments * 2);
  const target = new Float32Array(nSegments * 2);
  const segmentCellIds = new Uint32Array(nSegments);
  let out = 0;
  for (let ring = 0; ring < nRings; ring++) {
    const from = startIndices[ring];
    const to = startIndices[ring + 1];
    source.set(positions.subarray(from * 2, (to - 1) * 2), out * 2);
    target.set(positions.subarray((from + 1) * 2, to * 2), out * 2);
    segmentCellIds.fill(cellIds[ring], out, out + (to - 1 - from));
    out += to - 1 - from;
  }
  return { source, target, segmentCellIds };
}

/** Per-vertex RGBA from a per-cell feature: the tile's vertex_cell_id
 * column indexes the feature values directly (row i of features.parquet is
 * dense cell id i). NaN — a cell with no value — draws dim, not absent. */
function vertexColors(
  vertexCellIds: Uint32Array,
  feature: Feature,
  colormap: string,
  alpha: number,
): Uint8Array {
  const n = vertexCellIds.length;
  const out = new Uint8Array(n * 4);
  const lut = rampLut(colormap);
  const [lo, hi] = feature.domain;
  const scale = 255 / (hi - lo);
  for (let i = 0; i < n; i++) {
    const v = feature.values[vertexCellIds[i]];
    let r: number;
    let g: number;
    let b: number;
    if (Number.isNaN(v)) {
      [r, g, b] = UNASSIGNED_COLOR;
    } else if (feature.categorical) {
      [r, g, b] = TAB20[((v % 20) + 20) % 20 | 0];
    } else {
      const q = Math.max(0, Math.min(255, Math.round((v - lo) * scale))) * 4;
      r = lut[q];
      g = lut[q + 1];
      b = lut[q + 2];
    }
    const j = i * 4;
    out[j] = r;
    out[j + 1] = g;
    out[j + 2] = b;
    out[j + 3] = alpha;
  }
  return out;
}

function solidColors(n: number, rgb: [number, number, number], alpha: number): Uint8Array {
  const out = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    const j = i * 4;
    out[j] = rgb[0];
    out[j + 1] = rgb[1];
    out[j + 2] = rgb[2];
    out[j + 3] = alpha;
  }
  return out;
}

// Which coloring each tile's attribute arrays currently hold, so a settings
// change rebuilds them once and unchanged settings rebuild nothing.
const colorState = new WeakMap<object, string>();

function colorizeTile(
  tile: DeckSegmentTile,
  feature: Feature | undefined,
  colormap: string,
  fillAlpha: number,
): string {
  const key = `${feature?.name ?? ''}|${feature?.categorical ?? ''}|${colormap}|${fillAlpha}`;
  if (colorState.get(tile) !== key) {
    const ids = tile.fill.vertexCellIds;
    // No feature = one flat color for every cell: a hex colormap value as
    // itself, a ramp's top otherwise — the `flat_colors` rule from
    // src/cytos/render/polygons.py, so flat color needs no second field.
    const flat = colorValueRgb(colormap);
    const segIds = tile.outline.segmentCellIds;
    const fill = feature
      ? vertexColors(ids, feature, colormap, fillAlpha)
      : solidColors(ids.length, flat, fillAlpha);
    // LineLayer colors are per segment (per instance), not per vertex.
    const line = feature
      ? vertexColors(segIds, feature, colormap, OUTLINE_ALPHA)
      : solidColors(segIds.length, flat, OUTLINE_ALPHA);
    // `normalized: true` says these uint8 channels are 0-255 fractions of 1,
    // matching the shader attribute's unorm8 type — without the flag deck
    // assumes it and warns on every tile.
    tile.fill.attributes.getFillColor = { value: fill, size: 4, normalized: true };
    tile.outline.attributes.getColor = { value: line, size: 4, normalized: true };
    colorState.set(tile, key);
  }
  return key;
}

export function segmentTileLayer(
  slide: LoadedSlide,
  source: SegmentTileSource,
  settings: SegmentSettings,
  features: FeatureTable | null,
) {
  const spec = source.spec;
  const [minx, miny, maxx, maxy] = slide.manifest.world_bounds;
  const s = slide.pixelSize;
  // World µm -> full-res pixel coords.
  const worldToPixels = new Matrix4().scale([1 / s, 1 / s, 1]);
  const fillAlpha = settings.show_fill ? Math.round(255 * settings.fill_opacity) : 0;
  const feature = settings.color_by ? features?.get(settings.color_by) : undefined;

  return new TileLayer<DeckSegmentTile | null>({
    id: `segments-${spec.id}`,
    visible: settings.visible,
    // Our grid is a single flat level: clamping zoom to 0 with tileSize equal
    // to one tile's world size makes deck's tile index (x, y) exactly our
    // (col, row).
    tileSize: tileWorldSize(slide.manifest) / s,
    minZoom: 0,
    maxZoom: 0,
    extent: [minx / s, miny / s, maxx / s, maxy / s],
    // deck ignores function-prop identity on purpose, so a new
    // renderSubLayers closure alone never regenerates already-rendered
    // tiles — display settings must be declared here to take effect.
    updateTriggers: {
      renderSubLayers: [
        fillAlpha,
        settings.show_outline,
        settings.colormap,
        feature?.name ?? null,
      ],
    },
    // The deck-ready binary objects are built HERE, once per tile, so their
    // references stay stable. renderSubLayers runs on every deck update for
    // every visible tile; building fresh objects there made deck see
    // "changed data" for each already-loaded tile whenever a new one
    // arrived, re-tessellating everything — quadratic, and it showed.
    getTileData: async ({ index: { x, y } }) => {
      const tile = await source.tile(y, x);
      if (!tile) return null;
      const segments = ringSegments(tile.positions, tile.startIndices, tile.cellIds);
      return {
        fill: {
          length: tile.length,
          startIndices: tile.startIndices,
          attributes: { getPolygon: { value: tile.positions, size: 2 } },
          cellIds: tile.cellIds, // for the tooltip, via sourceLayer.props.data
          vertexCellIds: tile.vertexCellIds,
        },
        outline: {
          length: segments.segmentCellIds.length,
          attributes: {
            getSourcePosition: { value: segments.source, size: 2 },
            getTargetPosition: { value: segments.target, size: 2 },
          },
          segmentCellIds: segments.segmentCellIds,
        },
      };
    },
    renderSubLayers: (props) => {
      const tile = props.data;
      if (!tile) return null;
      const colorKey = colorizeTile(tile, feature, settings.colormap, fillAlpha);
      return [
        new SolidPolygonLayer({
          id: `${props.id}-fill`,
          data: tile.fill,
          _normalize: false,
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 255, 120, 140],
          modelMatrix: worldToPixels,
          updateTriggers: { getFillColor: colorKey },
        }),
        settings.show_outline &&
          new LineLayer({
            id: `${props.id}-outline`,
            data: tile.outline,
            getWidth: OUTLINE_WIDTH,
            widthUnits: 'pixels',
            modelMatrix: worldToPixels,
            updateTriggers: { getColor: colorKey },
          }),
      ];
    },
  });
}

/** Which cell is under the cursor — reads the picked sublayer's own data. */
export function segmentTooltip(info: PickingInfo): string | null {
  const data = (info.sourceLayer?.props as { data?: DeckSegmentTile['fill'] })?.data;
  if (!data?.cellIds || info.index < 0) return null;
  return `cell ${data.cellIds[info.index]}`;
}
