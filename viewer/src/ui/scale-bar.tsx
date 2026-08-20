/**
 * Two readouts driven by the camera rather than by React: the scale bar over
 * the canvas, and the one-line status under the panel.
 *
 * Both are the twins of Qt's — `ui/scale_bar.py` and the stats label
 * `main_window.py` puts in its dock — and both follow the same rule the
 * minimap follows: the camera is a ref the scene writes on every move, read
 * here on a slow timer. Re-rendering React on every frame of a drag would
 * spend the frame budget the renderer just bought, and neither of these
 * needs to be right sixty times a second.
 */

import { useEffect, useState } from 'react';

import { unitAbbrev } from '../core/manifest';
import type { LoadedSlide } from '../io/slide';
import { worldPerPixel, type CameraView } from '../render/scene';

/** How often the numbers are re-read. Same tick as the minimap's. */
const TICK = 100;

/** The length the bar aims for before rounding down to a nice number, and
 * the numbers it rounds to — `_TARGET_PX` and `_NICE_STEPS` in
 * `src/cytos/ui/scale_bar.py`. */
const TARGET_PX = 120;
const NICE_STEPS = [5, 2, 1];

/** The longest 1/2/5 x 10^n distance that still fits in `TARGET_PX`. */
export function niceLength(worldPerPx: number): number {
  const target = TARGET_PX * worldPerPx;
  const power = Math.pow(10, Math.floor(Math.log10(target)));
  for (const step of NICE_STEPS) {
    if (step * power <= target) return step * power;
  }
  return power;
}

/** Label for a bar of `length`, promoted to a bigger unit once the number
 * would otherwise run long — "1.5 mm" reads faster than "1500 µm". */
export function formatLength(length: number, units: string): string {
  const abbrev = unitAbbrev(units);
  if (abbrev === 'µm' && length >= 1000) return `${trim(length / 1000)} mm`;
  return `${trim(length)} ${abbrev}`;
}

/** Python's "%g" as far as this needs it: no trailing zeros, no exponent for
 * the sizes a slide is measured in. */
function trim(value: number): string {
  return String(Number(value.toPrecision(6)));
}

/** A bar of a round distance, in the canvas's bottom-left corner. Transparent
 * to the mouse, or dragging across it would quietly eat the gesture instead
 * of panning. */
export function ScaleBar({
  slide,
  camera,
}: {
  slide: LoadedSlide;
  camera: React.MutableRefObject<CameraView | null>;
}) {
  const [bar, setBar] = useState<{ px: number; label: string } | null>(null);

  useEffect(() => {
    const tick = () => {
      const view = camera.current;
      const worldPerPx = view ? worldPerPixel(slide.pixelSize, view.zoom) : 0;
      if (!worldPerPx || !Number.isFinite(worldPerPx)) return setBar(null);
      const length = niceLength(worldPerPx);
      const next = {
        px: Math.max(1, Math.round(length / worldPerPx)),
        label: formatLength(length, slide.manifest.world_units),
      };
      // Only a zoom changes these, so most ticks are asked to change nothing
      // — and must not, or every one would re-render.
      setBar((prev) => (prev && prev.px === next.px && prev.label === next.label ? prev : next));
    };
    tick();
    const timer = window.setInterval(tick, TICK);
    return () => window.clearInterval(timer);
  }, [camera, slide]);

  if (!bar) return null;
  return (
    <div style={styles.panel}>
      <div style={styles.label}>{bar.label}</div>
      {/* The bar and its end ticks are one box: a white bottom border is the
          bar, and white side borders are the ticks rising from its ends,
          which is exactly the shape Qt paints. */}
      <div style={{ ...styles.bar, width: bar.px }} />
    </div>
  );
}

/** The one line of numbers Qt keeps in its dock, cut to what a viewer (not a
 * profiler) wants: how much ground a screen pixel covers, and whether the
 * tiles behind the picture are arriving. A tile that 404s is a finding, not
 * noise, so failures are said out loud rather than left to the console. */
export function StatusLine({
  slide,
  camera,
}: {
  slide: LoadedSlide;
  camera: React.MutableRefObject<CameraView | null>;
}) {
  const [text, setText] = useState('');

  useEffect(() => {
    const tick = () => {
      const view = camera.current;
      if (!view) return;
      const worldPerPx = worldPerPixel(slide.pixelSize, view.zoom);
      const units = unitAbbrev(slide.manifest.world_units);
      // The same counters `shot.mjs` prints, kept by the tile readers.
      const stats = (window as { __tileStats?: { ok: number; failed: number } }).__tileStats;
      const parts = [`${Number(worldPerPx.toPrecision(3))} ${units}/px`];
      if (stats) parts.push(`${stats.ok.toLocaleString()} tiles`);
      if (stats?.failed) parts.push(`${stats.failed} failed`);
      const next = parts.join(' · ');
      setText((prev) => (prev === next ? prev : next));
    };
    tick();
    const timer = window.setInterval(tick, TICK);
    return () => window.clearInterval(timer);
  }, [camera, slide]);

  return <div style={styles.status}>{text}</div>;
}

const styles = {
  panel: {
    position: 'absolute',
    left: 12,
    bottom: 26,
    padding: '5px 8px 6px',
    borderRadius: 4,
    background: 'rgba(0, 0, 0, 0.55)',
    color: 'var(--text-bright)',
    font: '11px system-ui, sans-serif',
    textAlign: 'center',
    pointerEvents: 'none',
  },
  label: { marginBottom: 3 },
  bar: {
    height: 9,
    margin: '0 auto',
    borderLeft: '1px solid var(--text-bright)',
    borderRight: '1px solid var(--text-bright)',
    borderBottom: '4px solid var(--text-bright)',
    boxSizing: 'border-box',
  },
  status: {
    padding: '6px 12px 0',
    color: 'var(--text-faint)',
    fontVariantNumeric: 'tabular-nums',
  },
} as const;
