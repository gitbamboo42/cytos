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

import type { SegmentTileSource } from './segments';
import {
  CHANNEL_COLORS,
  tileWorldSize,
  type ImageLayerSpec,
  type SlideManifest,
} from './slide';
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

// Outline colors handed out per segment layer, in manifest order — a
// placeholder until per-cell coloring (color_by + features) is ported.
const OUTLINE_COLORS: [number, number, number][] = [
  [230, 230, 230],
  [90, 190, 255],
  [255, 190, 90],
  [190, 255, 90],
];

/** One tile's arrays, wrapped the way deck's binary-attribute path wants
 * them — built once per tile in getTileData (see the note there). */
interface DeckSegmentTile {
  fill: {
    length: number;
    startIndices: Uint32Array;
    attributes: { getPolygon: { value: Float32Array; size: number } };
    cellIds: Uint32Array;
  };
  path: {
    length: number;
    startIndices: Uint32Array;
    attributes: { getPath: { value: Float32Array; size: number } };
  };
}

function segmentTileLayer(
  slide: LoadedSlide,
  source: SegmentTileSource,
  index: number,
  settings: SegmentSettings,
) {
  const spec = source.spec;
  const [minx, miny, maxx, maxy] = slide.manifest.world_bounds;
  const s = slide.pixelSize;
  // World µm -> full-res pixel coords.
  const worldToPixels = new Matrix4().scale([1 / s, 1 / s, 1]);
  const fillAlpha = settings.show_fill ? Math.round(255 * settings.fill_opacity) : 0;

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
      renderSubLayers: [fillAlpha, settings.show_outline],
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
      const { fill: data, path: paths } = tile;
      return [
        new SolidPolygonLayer({
          id: `${props.id}-fill`,
          data,
          _normalize: false,
          getFillColor: [255, 255, 255, fillAlpha],
          pickable: true,
          autoHighlight: true,
          highlightColor: [255, 255, 120, 140],
          modelMatrix: worldToPixels,
        }),
        settings.show_outline &&
          new PathLayer({
            id: `${props.id}-outline`,
            data: paths,
            _pathType: 'loop',
            positionFormat: 'XY',
            getColor: [...OUTLINE_COLORS[index % OUTLINE_COLORS.length], 200],
            getWidth: 1,
            widthUnits: 'pixels',
            modelMatrix: worldToPixels,
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
  initialView,
}: {
  slide: LoadedSlide;
  settings: SlideSettings;
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
          (c) => CHANNEL_COLORS[image(c.id).colormap] ?? [255, 255, 255],
        ),
      },
    }),
    ...slide.segments.map((source, i) => {
      const layerSettings = settings.layers[segmentsKey(source.spec.id)] as SegmentSettings;
      return segmentTileLayer(slide, source, i, {
        ...layerSettings,
        visible: segmentsOn && layerSettings.visible,
      });
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
