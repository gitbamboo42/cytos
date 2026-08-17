/**
 * The DeckGL scene: viv's MultiscaleImageLayer for the morphology image,
 * one TileLayer per segment layer for the polygons.
 *
 * View space is the image's full-resolution **pixel grid** (what viv's
 * layers render in natively); polygon coordinates arrive in world µm and are
 * scaled into it with a modelMatrix, so the geometry buffers stay exactly
 * what the store holds. Y increases downward in both spaces — deck's
 * OrthographicView already puts y down (flipY default), so like the Qt
 * viewer there is exactly one place display orientation lives: the view.
 *
 * Each polygon tile draws as two sublayers from the same binary arrays:
 * a SolidPolygonLayer fill that doubles as the pick surface (kept even at
 * opacity 0 — same trick as the Qt renderer, where hiding the fill would
 * kill hover picking), and a PathLayer with `_pathType: 'loop'` for the
 * ring outlines.
 */

import { OrthographicView, type PickingInfo } from '@deck.gl/core';
import { Matrix4 } from '@math.gl/core';
import { TileLayer } from '@deck.gl/geo-layers';
import { PathLayer, SolidPolygonLayer } from '@deck.gl/layers';
import DeckGL from '@deck.gl/react';
import { ColorPaletteExtension, MultiscaleImageLayer } from '@hms-dbmi/viv';

import { colorValueRgb, rampLut, TAB20, UNASSIGNED_COLOR } from './colormaps';
import type { Feature, FeatureTable } from './features';
import type { SegmentTileSource } from './segments';
import { tileWorldSize, type ImageLayerSpec, type SlideManifest } from './slide';
import type { stackChannels } from './slide';
import {
  imageKey,
  segmentsKey,
  type ImageSettings,
  type SegmentSettings,
  type SlideSettings,
} from './state';

export interface LoadedSlide {
  manifest: SlideManifest;
  channels: ImageLayerSpec[];
  loader: ReturnType<typeof stackChannels>;
  pixelSize: number; // µm per full-res image pixel
  segments: SegmentTileSource[];
}

const OUTLINE_ALPHA = 220;

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
      getFillColor?: { value: Uint8Array; size: number };
    };
    cellIds: Uint32Array;
    vertexCellIds: Uint32Array;
  };
  path: {
    length: number;
    startIndices: Uint32Array;
    attributes: {
      getPath: { value: Float32Array; size: number };
      getColor?: { value: Uint8Array; size: number };
    };
  };
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
    const fill = feature
      ? vertexColors(ids, feature, colormap, fillAlpha)
      : solidColors(ids.length, flat, fillAlpha);
    const line = feature
      ? vertexColors(ids, feature, colormap, OUTLINE_ALPHA)
      : solidColors(ids.length, flat, OUTLINE_ALPHA);
    tile.fill.attributes.getFillColor = { value: fill, size: 4 };
    tile.path.attributes.getColor = { value: line, size: 4 };
    colorState.set(tile, key);
  }
  return key;
}

function segmentTileLayer(
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
      return {
        fill: {
          length: tile.length,
          startIndices: tile.startIndices,
          attributes: { getPolygon: { value: tile.positions, size: 2 } },
          cellIds: tile.cellIds, // for the tooltip, via sourceLayer.props.data
          vertexCellIds: tile.vertexCellIds,
        },
        path: {
          length: tile.length,
          startIndices: tile.startIndices,
          attributes: { getPath: { value: tile.positions, size: 2 } },
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
          new PathLayer({
            id: `${props.id}-outline`,
            data: tile.path,
            _pathType: 'loop',
            positionFormat: 'XY',
            getWidth: 1,
            widthUnits: 'pixels',
            modelMatrix: worldToPixels,
            updateTriggers: { getColor: colorKey },
          }),
      ];
    },
  });
}

function tooltip(info: PickingInfo) {
  const data = (info.sourceLayer?.props as { data?: DeckSegmentTile['fill'] })?.data;
  if (!data?.cellIds || info.index < 0) return null;
  return `cell ${data.cellIds[info.index]}`;
}

export function SlideViewer({
  slide,
  settings,
  features,
  initialView,
}: {
  slide: LoadedSlide;
  settings: SlideSettings;
  /** Per-cell attribute tables, keyed by segments layer key; null until
   * that layer's features.parquet arrives. */
  features: Record<string, FeatureTable | null>;
  /** [x, y, zoom] in full-res pixel coords — the `?view=` URL param. */
  initialView?: [number, number, number];
}) {
  const [, height, width] = slide.loader[0].shape;
  const image = (id: string) => settings.layers[imageKey(id)] as ImageSettings;
  const imagesOn = settings.sections.images?.checked ?? true;
  const segmentsOn = settings.sections.segments?.checked ?? true;

  const layers = [
    new MultiscaleImageLayer({
      id: 'image',
      loader: slide.loader,
      dtype: slide.loader[0].dtype,
      selections: slide.channels.map((_, i) => ({ c: i })),
      contrastLimits: slide.channels.map((c) => image(c.id).clim),
      channelsVisible: slide.channels.map((c) => imagesOn && image(c.id).visible),
      extensions: [new ColorPaletteExtension()],
      // `colors` is the extension's prop, absent from the layer's own TS
      // props — a spread slips past the excess-property check.
      ...{
        colors: slide.channels.map(
          (c) => colorValueRgb(image(c.id).colormap),
        ),
      },
    }),
    ...slide.segments.map((source) => {
      const key = segmentsKey(source.spec.id);
      const layerSettings = settings.layers[key] as SegmentSettings;
      return segmentTileLayer(
        slide,
        source,
        { ...layerSettings, visible: segmentsOn && layerSettings.visible },
        features[key] ?? null,
      );
    }),
  ];

  const fitZoom = Math.log2(
    Math.min(window.innerWidth / width, window.innerHeight / height),
  );
  const [cx, cy, zoom] = initialView ?? [width / 2, height / 2, fitZoom];

  return (
    <DeckGL
      views={new OrthographicView({ id: 'ortho' })}
      controller={true}
      initialViewState={{
        target: [cx, cy, 0],
        zoom,
        minZoom: fitZoom - 1,
        maxZoom: 6,
      }}
      layers={layers}
      getTooltip={tooltip}
      style={{ background: '#000' }}
    />
  );
}
