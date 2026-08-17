/**
 * Per-layer display settings, in the saved-session vocabulary.
 *
 * The Qt viewer's rule holds here: the session file format is the API, and
 * there is no second schema. Layers are keyed "kind:id" ("image:dapi",
 * "segments:cell") with the same field names `collect_session` writes in
 * `src/cytos/core/session.py`, so a future save/load — or a remote `set`
 * command — is a plain merge, not a translation.
 */

import { imageLayers, segmentLayers, type SlideManifest } from './manifest';

export interface ImageSettings {
  visible: boolean;
  colormap: string;
  clim: [number, number];
}

export interface SegmentSettings {
  visible: boolean;
  show_outline: boolean;
  show_fill: boolean;
  fill_opacity: number;
  color_by: string | null;
  colormap: string;
}

/** One dock section (Images / Segments / Points): `checked` shows or hides
 * the whole kind at once without touching per-layer visibility, `expanded`
 * folds the rows away — both straight from the session vocabulary. */
export interface SectionSettings {
  expanded: boolean;
  checked: boolean;
}

export interface SlideSettings {
  sections: Record<string, SectionSettings>;
  layers: Record<string, ImageSettings | SegmentSettings>;
}

export function imageKey(id: string): string {
  return `image:${id}`;
}

export function segmentsKey(id: string): string {
  return `segments:${id}`;
}

/** The slide's own defaults, from the manifest — what View > Reset to Slide
 * Defaults returns to. */
export function defaultSettings(manifest: SlideManifest): SlideSettings {
  const sections: SlideSettings['sections'] = {
    images: { expanded: true, checked: true },
    segments: { expanded: true, checked: true },
  };
  const layers: SlideSettings['layers'] = {};
  for (const layer of imageLayers(manifest)) {
    layers[imageKey(layer.id)] = {
      visible: layer.visible ?? true,
      colormap: layer.colormap,
      clim: layer.clim ?? [0, 65535],
    };
  }
  for (const layer of segmentLayers(manifest)) {
    layers[segmentsKey(layer.id)] = {
      visible: layer.visible ?? true,
      show_outline: layer.show_outline ?? true,
      show_fill: layer.show_fill ?? false,
      fill_opacity: layer.fill_opacity ?? 0.35,
      color_by: layer.color_by ?? null,
      colormap: layer.colormap ?? 'viridis',
    };
  }
  return { sections, layers };
}
