/**
 * The morphology image layer: viv's MultiscaleImageLayer, one channel per
 * cytos image layer, additively blended through a flat per-channel color.
 *
 * `colors` is the ColorPaletteExtension's prop, not the layer's, so a ramp
 * colormap can't be shown here — a ramp value renders as its top color.
 * Offering ramps needs a LUT in the shader.
 */

import { ColorPaletteExtension, MultiscaleImageLayer } from '@hms-dbmi/viv';

import { colorValueRgb } from '../core/colormaps';
import { imageKey, type ImageSettings, type SlideSettings } from '../core/session';
import type { ChannelStackSource } from '../io/image';
import type { LoadedSlide } from '../io/slide';

export function imageLayer(slide: LoadedSlide, settings: SlideSettings) {
  const image = (id: string) => settings.layers[imageKey(id)] as ImageSettings;
  const on = settings.sections.images?.checked ?? true;
  return new MultiscaleImageLayer({
    id: 'image',
    loader: slide.loader,
    dtype: slide.loader[0].dtype,
    selections: slide.channels.map((_, i) => ({ c: i })),
    contrastLimits: slide.channels.map((c) => image(c.id).clim),
    channelsVisible: slide.channels.map((c) => on && image(c.id).visible),
    extensions: [new ColorPaletteExtension()],
    // `colors` is the extension's prop, absent from the layer's own TS
    // props — a spread slips past the excess-property check.
    ...{
      colors: slide.channels.map((c) => colorValueRgb(image(c.id).colormap)),
    },
  });
}

/** Percentile-based contrast limits (1st / 99.5th), matching the Qt
 * viewer's autocontrast: fluorescence is sparse and heavy-tailed, so raw
 * min/max crushes the image to near-black. Reads the lowest-resolution
 * level — the statistics barely differ and it is already in cache. */
export async function autocontrast(
  loader: ChannelStackSource[],
  channel: number,
): Promise<[number, number]> {
  const lowest = loader[loader.length - 1];
  const { data } = await lowest.getRaster({ selection: { c: channel } });
  const stride = Math.max(1, Math.floor(data.length / 1_000_000));
  const sample: number[] = [];
  for (let i = 0; i < data.length; i += stride) sample.push(data[i]);
  sample.sort((a, b) => a - b);
  const lo = sample[Math.floor(0.01 * (sample.length - 1))];
  const hi = sample[Math.ceil(0.995 * (sample.length - 1))];
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

/** One channel's pixels at some pyramid level — what viv's `getRaster`
 * returns, narrowed to what the thumbnail needs. */
export interface Raster {
  data: ArrayLike<number>;
  width: number;
  height: number;
}

/** Every channel's coarsest pyramid level, read once. That level is already
 * small (a few hundred KB — it is what `autocontrast` reads), and it is the
 * whole slide in one array, which is exactly what a navigator thumbnail
 * needs and no tiled read can give it. */
export async function coarsestRasters(
  loader: ChannelStackSource[],
  channels: number,
): Promise<Raster[]> {
  const lowest = loader[loader.length - 1];
  return Promise.all(
    Array.from({ length: channels }, (_, c) => lowest.getRaster({ selection: { c } })),
  );
}

/** Composite the channel rasters the way the main view composites tiles —
 * normalize by clim, multiply by the channel's flat colour, add — so the
 * thumbnail can't drift from the scene. Returns straight RGBA bytes, ready
 * for `putImageData`. Same maths as viv's ColorPaletteExtension, which is
 * why a channel's colour comes from `colorValueRgb` here too.
 *
 * `Uint8ClampedArray` does the additive clamp on every store, so an
 * over-bright sum saturates to white instead of wrapping. */
export function compositeThumbnail(
  rasters: Raster[],
  channels: { visible: boolean; clim: [number, number]; color: [number, number, number] }[],
): { width: number; height: number; rgba: Uint8ClampedArray<ArrayBuffer> } {
  const { width, height } = rasters[0];
  const count = width * height;
  // Built on a plain ArrayBuffer, not the default: `ImageData` takes only
  // that one, never a shared buffer.
  const rgba = new Uint8ClampedArray(new ArrayBuffer(count * 4));
  for (let i = 0; i < count; i++) rgba[i * 4 + 3] = 255;

  rasters.forEach((raster, c) => {
    const channel = channels[c];
    if (!channel?.visible) return;
    const [lo, hi] = channel.clim;
    const span = Math.max(hi - lo, 1e-6);
    const [r, g, b] = channel.color;
    const { data } = raster;
    for (let i = 0; i < count; i++) {
      const t = (data[i] - lo) / span;
      if (t <= 0) continue;
      const level = t > 1 ? 1 : t;
      rgba[i * 4] += level * r;
      rgba[i * 4 + 1] += level * g;
      rgba[i * 4 + 2] += level * b;
    }
  });
  return { width, height, rgba };
}
