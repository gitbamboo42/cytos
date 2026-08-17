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
