/**
 * The navigator: a static composite thumbnail of the whole slide with a live
 * rectangle showing what the main camera sees, like Photoshop's Navigator,
 * napari's overview, or the Qt viewer's `ui/minimap.py`. Click or drag on it
 * to recentre the main camera there.
 *
 * Coordinates here are the scene's: full-resolution image pixels. The canvas
 * is sized to the image's own aspect ratio, so a canvas pixel maps to an
 * image pixel by one scale factor and nothing has to be letterboxed.
 */

import { useEffect, useRef, useState } from 'react';

import { colorValueRgb } from '../core/colormaps';
import { imageKey, type ImageSettings, type SlideSettings } from '../core/session';
import type { LoadedSlide } from '../io/slide';
import { coarsestRasters, compositeThumbnail, type Raster } from '../render/image';
import type { CameraView } from '../render/scene';

/** Largest canvas the panel gives it; the thumbnail fits inside, keeping the
 * slide's aspect ratio. The panel is 280 wide with 12 of padding each side. */
const MAX_WIDTH = 256;
const MAX_HEIGHT = 200;

/** How often the view rectangle is repainted, in ms. Deliberately not every
 * frame: the camera moves on the render thread and React does not need to
 * hear about it (see `render/scene.tsx`), so the rectangle is read on a slow
 * timer instead — the same 10 Hz the Qt viewer uses for the same reason. */
const TICK = 100;

export function Minimap({
  slide,
  settings,
  camera,
  onRecenter,
}: {
  slide: LoadedSlide;
  settings: SlideSettings;
  /** The live camera, written by the scene and read on the timer. */
  camera: React.MutableRefObject<CameraView | null>;
  /** Put the camera here, in full-resolution image pixels. */
  onRecenter: (x: number, y: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [rasters, setRasters] = useState<Raster[] | null>(null);
  // The composited thumbnail at its own (small) pixel size, redrawn only when
  // a channel's colour, contrast or visibility changes.
  const thumb = useRef<HTMLCanvasElement | null>(null);

  const [, imageHeight, imageWidth] = slide.loader[0].shape;
  const scale = Math.min(MAX_WIDTH / imageWidth, MAX_HEIGHT / imageHeight);
  const cssWidth = Math.round(imageWidth * scale);
  const cssHeight = Math.round(imageHeight * scale);

  useEffect(() => {
    let cancelled = false;
    coarsestRasters(slide.loader, slide.channels.length)
      .then((read) => {
        if (!cancelled) setRasters(read);
      })
      .catch((err) => console.error('minimap thumbnail:', err));
    return () => {
      cancelled = true;
    };
  }, [slide]);

  const channels = slide.channels.map((c) => settings.layers[imageKey(c.id)] as ImageSettings);
  // The master Images switch is deliberately not in here: turning the image
  // off to look at segments alone should not blank the map you navigate by.
  // Qt's minimap does the same.
  const look = JSON.stringify(channels.map((c) => [c.visible, c.clim, c.colormap]));

  useEffect(() => {
    if (!rasters) return;
    const image = compositeThumbnail(
      rasters,
      channels.map((c) => ({
        visible: c.visible,
        clim: c.clim,
        color: colorValueRgb(c.colormap),
      })),
    );
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    canvas
      .getContext('2d')!
      .putImageData(new ImageData(image.rgba, image.width, image.height), 0, 0);
    thumb.current = canvas;
    // `channels` is rebuilt every render; `look` is what actually changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rasters, look]);

  useEffect(() => {
    const paint = () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(cssWidth * dpr)) {
        canvas.width = Math.round(cssWidth * dpr);
        canvas.height = Math.round(cssHeight * dpr);
      }
      const ctx = canvas.getContext('2d')!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, cssWidth, cssHeight);
      if (thumb.current) ctx.drawImage(thumb.current, 0, 0, cssWidth, cssHeight);

      const view = camera.current;
      if (!view) return;
      // One image pixel spans 2^zoom screen pixels, so the camera sees
      // width/2^zoom of them across.
      const seen = Math.pow(2, -view.zoom);
      const halfW = (view.width * seen) / 2;
      const halfH = (view.height * seen) / 2;
      ctx.strokeStyle = '#ffd400';
      ctx.lineWidth = 1.5;
      ctx.strokeRect(
        (view.x - halfW) * scale,
        (view.y - halfH) * scale,
        halfW * 2 * scale,
        halfH * 2 * scale,
      );
    };
    paint();
    const timer = window.setInterval(paint, TICK);
    return () => window.clearInterval(timer);
  }, [camera, cssWidth, cssHeight, scale]);

  const toImage = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const clamp = (v: number) => Math.min(Math.max(v, 0), 1);
    onRecenter(
      clamp((event.clientX - rect.left) / rect.width) * imageWidth,
      clamp((event.clientY - rect.top) / rect.height) * imageHeight,
    );
  };

  return (
    <div style={{ padding: '4px 12px 8px', borderBottom: '1px solid #26262a' }}>
      <canvas
        ref={canvasRef}
        style={{ width: cssWidth, height: cssHeight, cursor: 'crosshair', display: 'block' }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          toImage(event);
        }}
        onPointerMove={(event) => {
          // Drag to scrub: only while the button is down.
          if (event.buttons & 1) toImage(event);
        }}
      />
    </div>
  );
}
