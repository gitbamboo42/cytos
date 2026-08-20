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
import { OrthographicView, type PickingInfo } from '@deck.gl/core';
import DeckGL, { type DeckGLRef } from '@deck.gl/react';

import {
  pointsKey,
  segmentsKey,
  type PointSettings,
  type SegmentSettings,
  type SlideSettings,
} from '../core/session';
import type { ViewRect } from '../core/session';
import { categoryKey, type FeatureTable } from '../io/features';
import type { GeneTable } from '../io/points';
import type { LoadedSlide } from '../io/slide';
import { unitAbbrev } from '../core/manifest';
import { imageLayer } from './image';
import { pickedPoint, pointTileLayer, selectPointLevel } from './points';
import { pickedCell, segmentTileLayer } from './segments';

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

/** How big a session's thumbnail is, longest side, in pixels — the same
 * bound `_write_snapshot` uses in `src/cytos/core/session.py`, because both
 * viewers write the same `<slug>.png` and either picker shows it. */
const THUMB_MAX = 320;

/** Takes the frame currently on screen, or null if there is nothing to take.
 * `App.tsx` holds one of these and calls it when it writes a session. */
export type TakeShot = () => Promise<Blob | null>;

/** World µm covered by one screen pixel. View space is full-resolution image
 * pixels, so a screen pixel spans 2^-zoom of them, each `pixelSize` µm wide. */
export function worldPerPixel(pixelSize: number, zoom: number): number {
  return pixelSize * Math.pow(2, -zoom);
}

/** A feature value as the readout shows it — four significant figures for a
 * measurement, the number itself for a category, "unassigned" for a cell the
 * feature says nothing about. Same three cases as `_hover_cell_text` in
 * `src/cytos/ui/main_window.py`. */
function featureText(value: number): string {
  if (Number.isNaN(value)) return 'unassigned';
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(4)));
}

export function SlideViewer({
  slide,
  settings,
  features,
  genes,
  initialView,
  openingRect,
  camera,
  recenter,
  shot,
}: {
  slide: LoadedSlide;
  settings: SlideSettings;
  /** Per-cell attribute tables, keyed by segments layer key; null until
   * that layer's features.parquet arrives. */
  features: Record<string, FeatureTable | null>;
  /** Gene names per point layer, keyed by points layer key — what a hovered
   * transcript is called. Null until that layer's table arrives. */
  genes: Record<string, GeneTable | null>;
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
  /** Filled in with a way to grab the current frame, for the session's
   * thumbnail. A ref for the same reason the camera is one: the scene owns
   * the canvas, and handing the function up costs no re-render. */
  shot?: React.MutableRefObject<TakeShot | null>;
}) {
  const [, height, width] = slide.loader[0].shape;
  const units = unitAbbrev(slide.manifest.world_units);
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

  const deck = useRef<DeckGLRef>(null);
  // Where the pointer is, in world units. Written straight into the DOM
  // rather than through React: it changes on every mouse move, and the panel
  // has no business re-rendering that often (the camera is a ref for the
  // same reason).
  const readout = useRef<HTMLDivElement>(null);
  // Set while a thumbnail has been asked for: the next frame drawn hands the
  // canvas to this and clears it.
  const wanted = useRef<((shot: Blob | null) => void) | null>(null);

  /** The frame deck just drew, scaled down to a thumbnail.
   *
   * Read inside `onAfterRender` on purpose. The drawing buffer is not
   * preserved (asking for that would slow every frame to serve one), so it
   * holds the picture only until the browser composites it — which is after
   * this callback, never before. Copying it into a 2D canvas here is what
   * lets the PNG encoding itself happen later, off the frame. */
  const grab = (gl: WebGL2RenderingContext) => {
    const resolve = wanted.current;
    if (!resolve) return;
    wanted.current = null;
    const source = gl.canvas as HTMLCanvasElement;
    const scale = Math.min(1, THUMB_MAX / Math.max(source.width, source.height));
    const thumb = document.createElement('canvas');
    thumb.width = Math.max(1, Math.round(source.width * scale));
    thumb.height = Math.max(1, Math.round(source.height * scale));
    const ctx = thumb.getContext('2d');
    if (!ctx) return resolve(null);
    ctx.drawImage(source, 0, 0, thumb.width, thumb.height);
    thumb.toBlob((blob) => resolve(blob), 'image/png');
  };

  useEffect(() => {
    if (!shot) return;
    shot.current = () =>
      new Promise<Blob | null>((resolve) => {
        const instance = deck.current?.deck;
        if (!instance) return resolve(null);
        wanted.current = resolve;
        // Forced, not requested: nothing may have changed since the last
        // frame, and a viewer sitting still would otherwise never draw
        // another one. `redraw` runs the render — and `onAfterRender` —
        // before it returns, so a request still pending after this line
        // means no frame was drawn at all.
        instance.redraw('session thumbnail');
        if (wanted.current === resolve) {
          wanted.current = null;
          resolve(null);
        }
      });
    return () => {
      shot.current = null;
    };
  }, [shot]);

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

  /**
   * What the cursor is over, in words.
   *
   * A transcript answers before the cell it sits in — a dot is drawn on top
   * of the cell, so on a dot the gene is the answer and beside it the cell
   * is, exactly as `on_pointer_move` decides it in the Qt viewer. deck picks
   * the topmost layer for us, so the order here only has to match the draw
   * order.
   */
  const hoverText = (info: PickingInfo): string | null => {
    const point = pickedPoint(info);
    if (point) {
      const table = genes[pointsKey(point.layerId)];
      const name = table?.names[point.gene] ?? `gene ${point.gene}`;
      // At full detail a dot is one transcript and there is nothing to
      // count; on an aggregate level it stands for several.
      return point.count === null ? name : `${name} ×${point.count.toLocaleString()}`;
    }
    const hit = pickedCell(info);
    if (!hit) return null;
    const key = segmentsKey(hit.layerId);
    const table = features[key];
    const layerSettings = settings.layers[key] as SegmentSettings | undefined;
    const colorBy = layerSettings?.color_by ?? null;
    const feature = colorBy ? table?.get(colorBy) : undefined;
    if (feature?.categorical && colorBy) {
      // A hidden category is not there to be hovered — Qt's `pick_cell`
      // refuses a cell whose colour has gone to alpha 0, and this is the
      // same refusal.
      const hidden = layerSettings?.hidden_categories?.[colorBy] ?? [];
      if (hidden.includes(categoryKey(feature.values[hit.cell]))) return null;
    }
    const id = table?.ids?.get(hit.cell) ?? hit.cell;
    const text = `cell ${id}`;
    if (!feature || !colorBy) return text;
    return `${text} · ${colorBy}: ${featureText(feature.values[hit.cell])}`;
  };

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
    <>
      <DeckGL
        ref={deck}
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
      onAfterRender={({ gl }) => grab(gl)}
      getTooltip={hoverText}
      onHover={(info) => {
        const node = readout.current;
        if (!node) return;
        // `coordinate` is view space — full-resolution image pixels — and
        // world µm is one multiply away. Off the canvas there is nothing to
        // say, so the readout empties rather than freezing on a stale spot.
        const at = info.coordinate;
        node.textContent = at
          ? `${(at[0] * slide.pixelSize).toFixed(1)}, ${(at[1] * slide.pixelSize).toFixed(1)} ${units}`
          : '';
      }}
        style={{ background: '#000' }}
      />
      {/* Where the pointer is, bottom left — the web twin of the Qt window's
          status bar. Transparent to the mouse, or it would steal the moves
          that fill it in. */}
      <div ref={readout} className="readout" style={readoutStyle} />
    </>
  );
}

const readoutStyle = {
  position: 'absolute',
  left: 8,
  bottom: 6,
  color: '#9a9a9a',
  font: '11px system-ui, sans-serif',
  fontVariantNumeric: 'tabular-nums',
  textShadow: '0 1px 2px #000',
  pointerEvents: 'none',
} as const;
