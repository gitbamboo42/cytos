/**
 * Open a whole slide: fetch the manifest, then every layer it names.
 *
 * `LoadedSlide` is what the rest of the app is handed — the manifest plus
 * one opened reader per layer. It lives here, with the readers it is built
 * from, so neither the renderer nor the panel owns it.
 */

import {
  CYTOS_FORMAT,
  imageLayers,
  segmentLayers,
  type ImageLayerSpec,
  type SlideManifest,
} from '../core/manifest';
import { openImagePyramid, stackChannels, type ChannelStackSource } from './image';
import type { ReadRange } from './read';
import { SegmentTileSource } from './segments';

export interface LoadedSlide {
  manifest: SlideManifest;
  channels: ImageLayerSpec[];
  loader: ChannelStackSource[];
  pixelSize: number; // µm per full-res image pixel
  segments: SegmentTileSource[];
}

export async function fetchManifest(read: ReadRange): Promise<SlideManifest> {
  const bytes = await read('cytos.json');
  if (!bytes) throw new Error('no cytos.json at that URL — not a cytos slide');
  const manifest = JSON.parse(new TextDecoder().decode(bytes)) as SlideManifest;
  if (manifest.cytos_format > CYTOS_FORMAT) {
    throw new Error(
      `slide format ${manifest.cytos_format} is newer than this viewer ` +
        `understands (${CYTOS_FORMAT})`,
    );
  }
  return manifest;
}

export async function loadSlide(read: ReadRange): Promise<LoadedSlide> {
  const manifest = await fetchManifest(read);
  const channels = imageLayers(manifest);
  if (channels.length === 0) throw new Error('slide has no image layers');
  const pyramids = await Promise.all(
    channels.map((layer) => openImagePyramid(read, layer)),
  );
  const segments = segmentLayers(manifest).map(
    (spec) => new SegmentTileSource(read, spec),
  );
  return {
    manifest,
    channels,
    loader: stackChannels(pyramids.map((p) => p.levels)),
    pixelSize: pyramids[0].pixelSize,
    segments,
  };
}
