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

import { useEffect, useRef, useState } from 'react';
import { OrthographicView } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';

import {
  pointsKey,
  segmentsKey,
  type PointSettings,
  type SegmentSettings,
  type SlideSettings,
} from '../core/session';
import type { ViewRect } from '../core/session';
import type { FeatureTable } from '../io/features';
import type { LoadedSlide } from '../io/slide';
import { imageLayer } from './image';
import { pointTileLayer, selectPointLevel } from './points';
import { segmentTileLayer, segmentTooltip } from './segments';

/** Where the camera is now, in full-resolution image pixels, plus the size
 * of the canvas it is looking through — enough to work out the visible
 * rectangle. The minimap reads it; nothing else needs it. */
export interface CameraView {
  x: number;
  y: number;
  zoom: number;
  /** Canvas size in CSS pixels. */
  width: number;
  height: number;
}

/** A "put the camera here" request, in image pixels. `seq` counts requests:
 * deck resets its own view state only when `initialViewState` differs from
 * the last one it was given (a depth-3 deep-equal), so clicking the same
 * spot on the minimap twice — pan away in between — has to look different
 * or the second click would do nothing. */
export interface Recenter {
  x: number;
  y: number;
  zoom: number;
  seq: number;
}

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
  openingRect,
  camera,
  recenter,
}: {
  slide: LoadedSlide;
  settings: SlideSettings;
  /** Per-cell attribute tables, keyed by segments layer key; null until
   * that layer's features.parquet arrives. */
  features: Record<string, FeatureTable | null>;
  /** [x, y, zoom] in full-res pixel coords — the `?view=` URL param. An
   * explicit instruction, zoom and all, so it needs no fitting. */
  initialView?: [number, number, number];
  /** The region to open on — a session's saved camera. Fitted to the canvas
   * here rather than by the caller, because at first paint the canvas is not
   * yet the size it will be a frame later, and a restored view fitted to
   * that momentary size opens at the wrong zoom. Null fits the whole slide. */
  openingRect?: ViewRect | null;
  /** Written on every camera move, for the minimap to read on its own
   * timer. A ref, not state: the camera moves every frame of a drag and
   * re-rendering React that often would spend the frame budget the
   * renderer just bought. */
  camera?: React.MutableRefObject<CameraView | null>;
  /** Latest recentre request, or null while the camera is the user's own. */
  recenter?: Recenter | null;
}) {
  const [, height, width] = slide.loader[0].shape;
  const segmentsOn = settings.sections.segments?.checked ?? true;
  const pointsOn = settings.sections.points?.checked ?? true;

  // The canvas as deck last measured it. State, so the opening view is
  // re-fitted once the real size arrives — but only until the camera is the
  // user's, after which nothing here may move it again.
  const [canvas, setCanvas] = useState({
    width: window.innerWidth,
    height: window.innerHeight,
  });
  const moved = useRef(false);

  const rect = openingRect ?? { x: width / 2, y: height / 2, width, height };
  const fitZoom = Math.log2(
    Math.min(canvas.width / rect.width, canvas.height / rect.height),
  );
  // How far out the whole slide sits, which is as far out as anyone needs to
  // go — one step further, whatever the opening view happens to be.
  const slideZoom = Math.log2(Math.min(canvas.width / width, canvas.height / height));
  const [cx, cy, zoom] = initialView ?? [rect.x, rect.y, fitZoom];
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

  const live = useRef<CameraView>({
    x: cx,
    y: cy,
    zoom,
    width: window.innerWidth,
    height: window.innerHeight,
  });
  const publish = (patch: Partial<CameraView>) => {
    live.current = { ...live.current, ...patch };
    if (camera) camera.current = live.current;
  };
  // Deck only calls back once something moves, so the opening camera has to
  // be handed over by hand or the minimap would draw no rectangle until the
  // first drag.
  useEffect(() => publish({}), [camera]);
  // A recentre is deck overwriting its own view state, which fires no
  // `onViewStateChange` — so the rectangle would sit where the camera used
  // to be until the next drag. Report it here instead.
  useEffect(() => {
    if (recenter) publish({ x: recenter.x, y: recenter.y, zoom: recenter.zoom });
  }, [recenter]);

  const opening = {
    target: [cx, cy, 0] as [number, number, number],
    zoom,
    minZoom: slideZoom - 1,
    maxZoom: 6,
  };
  // Rebuilt every render, but deck compares by value, so an unrelated
  // re-render (a panel setting, say) can't snap the camera back.
  const viewState = recenter
    ? {
        ...opening,
        target: [recenter.x, recenter.y, 0] as [number, number, number],
        zoom: recenter.zoom,
        seq: recenter.seq,
      }
    : opening;

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
      initialViewState={viewState}
      layers={layers}
      onResize={({ width: w, height: h }) => {
        publish({ width: w, height: h });
        if (!moved.current) setCanvas({ width: w, height: h });
      }}
      onViewStateChange={({ viewState: next }) => {
        const state = next as { target: number[]; zoom: number };
        moved.current = true;
        publish({ x: state.target[0], y: state.target[1], zoom: state.zoom });
        const level = selectPointLevel(
          pointLevels,
          worldPerPixel(slide.pixelSize, state.zoom),
        );
        if (level !== pointLevel) setPointLevel(level);
      }}
      getTooltip={segmentTooltip}
      style={{ background: '#000' }}
    />
  );
}
