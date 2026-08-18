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

import { OrthographicView } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';

import { segmentsKey, type SegmentSettings, type SlideSettings } from '../core/session';
import type { FeatureTable } from '../io/features';
import type { LoadedSlide } from '../io/slide';
import { imageLayer } from './image';
import { segmentTileLayer, segmentTooltip } from './segments';

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
  ];

  const fitZoom = Math.log2(
    Math.min(window.innerWidth / width, window.innerHeight / height),
  );
  const [cx, cy, zoom] = initialView ?? [width / 2, height / 2, fitZoom];

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
      getTooltip={segmentTooltip}
      style={{ background: '#000' }}
    />
  );
}
