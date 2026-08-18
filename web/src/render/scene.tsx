/**
 * The DeckGL scene: assemble one layer per slide layer and hand them to deck.
 *
 * View space is the image's full-resolution **pixel grid** (what viv's
 * layers render in natively); polygon coordinates arrive in world µm and are
 * scaled into it with a modelMatrix, so the geometry buffers stay exactly
 * what the store holds. Y increases downward in both spaces — deck's
 * OrthographicView already puts y down (flipY default), so like the Qt
 * viewer there is exactly one place display orientation lives: the view.
 */

import { useState } from 'react';
import { OrthographicView } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';

import {
  pointsKey,
  segmentsKey,
  type PointSettings,
  type SegmentSettings,
  type SlideSettings,
} from '../core/session';
import type { FeatureTable } from '../io/features';
import type { LoadedSlide } from '../io/slide';
import { imageLayer } from './image';
import { pointTileLayer, selectPointLevel } from './points';
import { segmentTileLayer, segmentTooltip } from './segments';

/** World µm covered by one screen pixel. View space is full-resolution image
 * pixels, so a screen pixel spans 2^-zoom of them, each `pixelSize` µm wide. */
function worldPerPixel(pixelSize: number, zoom: number): number {
  return pixelSize * Math.pow(2, -zoom);
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
  const segmentsOn = settings.sections.segments?.checked ?? true;
  const pointsOn = settings.sections.points?.checked ?? true;

  const fitZoom = Math.log2(
    Math.min(window.innerWidth / width, window.innerHeight / height),
  );
  const [cx, cy, zoom] = initialView ?? [width / 2, height / 2, fitZoom];
  const pointLevels = Math.max(...slide.points.map((p) => p.levels), 1);

  // Which point detail level the current zoom deserves. State, not a
  // per-frame read: it changes only when a zoom crosses a level boundary,
  // and re-rendering React on every drag frame would spend the frame budget
  // the renderer just bought. Seeded from the opening zoom — starting at
  // full detail would draw every one of millions of transcripts over the
  // whole slide before the first mouse move.
  const [pointLevel, setPointLevel] = useState(() =>
    selectPointLevel(pointLevels, worldPerPixel(slide.pixelSize, zoom)),
  );

  const layers = [
    imageLayer(slide, settings),
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
    ...slide.points.flatMap((source) => {
      const key = pointsKey(source.spec.id);
      const layerSettings = settings.layers[key] as PointSettings;
      if (!layerSettings) return [];
      return [
        pointTileLayer(
          slide,
          source,
          { ...layerSettings, visible: pointsOn && layerSettings.visible },
          Math.min(pointLevel, source.levels - 1),
        ),
      ];
    }),
  ];

  return (
    <DeckGL
      views={new OrthographicView({ id: 'ortho' })}
      deviceProps={{ webgl: { antialias: false } }}
      controller={true}
      initialViewState={{
        target: [cx, cy, 0],
        zoom,
        minZoom: fitZoom - 1,
        maxZoom: 6,
      }}
      layers={layers}
      onViewStateChange={({ viewState }) => {
        const next = selectPointLevel(
          pointLevels,
          worldPerPixel(slide.pixelSize, (viewState as { zoom: number }).zoom),
        );
        if (next !== pointLevel) setPointLevel(next);
      }}
      getTooltip={segmentTooltip}
      style={{ background: '#000' }}
    />
  );
}
