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

/** Session file format, same as `SESSION_FORMAT` in `core/session.py`. A
 * session written by a newer cytos is ignored rather than guessed at. */
export const SESSION_FORMAT = 1;

/** Same as `DEFAULT_SESSION_NAME` in `core/session.py`. */
export const DEFAULT_SESSION_NAME = 'default';

/** The camera, in world µm — the rectangle the window was looking at.
 * Stored in world units, not pixels or zoom levels, so a session saved in a
 * small window opens on the same region in a large one, and so the Qt viewer
 * (whose camera is `show_rect`) reads exactly the same numbers. */
export interface SavedCamera {
  center: [number, number];
  size: [number, number];
}

/** A region of the slide in full-resolution image pixels: centre and extent.
 * The scene's own units — what a saved camera turns into. */
export interface ViewRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * A saved session, as it sits on disk — the same document
 * `collect_session` writes in `src/cytos/ui/main_window.py`.
 *
 * Fields this viewer does not understand (Qt's `window` geometry blob, a
 * segment row's `category_colors`) are carried through untouched: opening
 * someone's session in the web viewer and saving it must not quietly throw
 * away the half only the Qt viewer can show.
 */
export interface SavedSession {
  cytos_session: number;
  name: string;
  camera?: SavedCamera;
  sections?: Record<string, SectionSettings>;
  layers?: Record<string, Record<string, unknown>>;
  custom_colors?: string[];
  [other: string]: unknown;
}

/** A filename that still reads like the name typed — same rule as
 * `slugify` in `core/session.py`, because both viewers name the same file. */
export function slugify(name: string): string {
  const slug = name
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '');
  return slug.toLowerCase() || 'session';
}

/** `base`, or the first "base N" not already taken. */
export function uniqueSessionName(taken: string[], base = 'Session'): string {
  const used = new Set(taken.map((n) => n.toLowerCase()));
  if (!used.has(base.toLowerCase())) return base;
  let n = 2;
  while (used.has(`${base} ${n}`.toLowerCase())) n += 1;
  return `${base} ${n}`;
}

/** The slide's defaults with a session's overrides on top. Only fields the
 * defaults already name are taken, so a stray or hostile key in a session
 * file cannot reach the renderer; the rest is kept for saving by
 * `collectSession`, not merged in here. */
export function applySession(
  manifest: SlideManifest,
  saved: SavedSession | null,
): { settings: SlideSettings; customColors: string[] } {
  const settings = defaultSettings(manifest);
  if (!saved) return { settings, customColors: [] };

  for (const [name, section] of Object.entries(saved.sections ?? {})) {
    const base = settings.sections[name];
    if (!base || typeof section !== 'object') continue;
    settings.sections[name] = {
      expanded: typeof section.expanded === 'boolean' ? section.expanded : base.expanded,
      checked: typeof section.checked === 'boolean' ? section.checked : base.checked,
    };
  }
  for (const [key, layer] of Object.entries(saved.layers ?? {})) {
    const base = settings.layers[key] as unknown as Record<string, unknown>;
    if (!base || typeof layer !== 'object' || layer === null) continue;
    const merged: Record<string, unknown> = { ...base };
    for (const field of Object.keys(base)) {
      if (field in layer) merged[field] = layer[field];
    }
    settings.layers[key] = merged as unknown as SlideSettings['layers'][string];
  }
  const customColors = (saved.custom_colors ?? []).filter(
    (c): c is string => typeof c === 'string',
  );
  return { settings, customColors };
}

/** The document to write: what this viewer shows now, laid over whatever the
 * session already held. */
export function collectSession(
  name: string,
  previous: SavedSession | null,
  settings: SlideSettings,
  camera: SavedCamera | null,
  customColors: string[],
): SavedSession {
  const layers: Record<string, Record<string, unknown>> = { ...(previous?.layers ?? {}) };
  for (const [key, layer] of Object.entries(settings.layers)) {
    layers[key] = { ...(layers[key] ?? {}), ...layer };
  }
  return {
    ...(previous ?? {}),
    cytos_session: SESSION_FORMAT,
    name,
    ...(camera ? { camera } : {}),
    sections: settings.sections,
    layers,
    custom_colors: customColors,
  };
}

/**
 * Camera conversions between the session's world µm and the scene's
 * full-resolution image pixels. World = pixel × `pixelSize` with no offset
 * (`render/segments.ts` scales by exactly that), so these are one multiply
 * each — kept here, in the model, because the session file decides the units
 * and nothing in `render/` should have to know them.
 */
export function cameraFromView(
  x: number,
  y: number,
  zoom: number,
  width: number,
  height: number,
  pixelSize: number,
): SavedCamera {
  const seen = Math.pow(2, -zoom) * pixelSize;
  return { center: [x * pixelSize, y * pixelSize], size: [width * seen, height * seen] };
}

/** The saved camera as a rectangle in image pixels — what to show, with no
 * opinion about how big the window is. Which zoom that comes to is
 * `render/scene.tsx`'s to decide, because only the scene knows the canvas,
 * and at first paint the canvas is not the size it will be a frame later. */
export function rectFromCamera(camera: SavedCamera, pixelSize: number): ViewRect {
  const [cx, cy] = camera.center;
  const [w, h] = camera.size;
  return { x: cx / pixelSize, y: cy / pixelSize, width: w / pixelSize, height: h / pixelSize };
}
