/**
 * Per-layer display settings, in the saved-session vocabulary.
 *
 * The Qt viewer's rule holds here: the session file format is the API, and
 * there is no second schema. Layers are keyed "kind:id" ("image:dapi",
 * "segments:cell") with the same field names `collect_session` writes in
 * `src/cytos/core/session.py`, so a future save/load — or a remote `set`
 * command — is a plain merge, not a translation.
 */

import { imageLayers, pointLayers, segmentLayers, type SlideManifest } from './manifest';

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

/** Points, in the same vocabulary `collect_session` writes: `color_mode` is
 * "gene" (one palette colour per gene) or "flat" (`colormap` for every
 * point), `size` is screen pixels, matching Qt's `size_space="screen"`. */
export interface PointSettings {
  visible: boolean;
  /** Dense gene ids to draw, or null for every gene. A selection also
   * decides colour: the palette is spent on the genes being looked at, by
   * rank, not on the whole panel (`render/points.py`). */
  genes: number[] | null;
  /** Per-gene colour overrides, keyed by dense gene id. A gene without one
   * takes its palette colour; picking a colour pins it. */
  gene_colors: Record<number, string>;
  size: number;
  opacity: number;
  color_mode: string;
  colormap: string;
  palette: string;
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
  layers: Record<string, ImageSettings | SegmentSettings | PointSettings>;
}

export function imageKey(id: string): string {
  return `image:${id}`;
}

export function segmentsKey(id: string): string {
  return `segments:${id}`;
}

export function pointsKey(id: string): string {
  return `points:${id}`;
}

/** The slide's own defaults, from the manifest — what View > Reset to Slide
 * Defaults returns to. */
export function defaultSettings(manifest: SlideManifest): SlideSettings {
  // A slide opens showing the morphology image and nothing else. Segments
  // and points are both whole-slide overlays — 140k outlines and millions of
  // transcripts — so drawing them before being asked buries the image they
  // sit on and spends the frame budget on things nobody has chosen yet. The
  // section checkbox turns each on in one click, without touching what the
  // manifest says about individual layers. Both start folded too, so the
  // panel opens as a short list of channels rather than a wall of rows.
  const sections: SlideSettings['sections'] = {
    images: { expanded: true, checked: true },
    segments: { expanded: false, checked: false },
    points: { expanded: false, checked: false },
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
  for (const layer of pointLayers(manifest)) {
    layers[pointsKey(layer.id)] = {
      visible: layer.visible ?? true,
      // Empty, not null: null means "every gene", and every gene at once is
      // 514 of them cycling ten colours. Starting empty makes the first
      // thing you see the genes you picked.
      genes: layer.genes ?? [],
      gene_colors: layer.gene_colors ?? {},
      size: layer.size ?? 3,
      opacity: layer.opacity ?? 0.9,
      color_mode: layer.color_mode ?? 'gene',
      colormap: layer.colormap ?? 'yellow',
      palette: layer.palette ?? 'tab10',
    };
  }
  return { sections, layers };
}
