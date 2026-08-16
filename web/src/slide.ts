/**
 * Read a `.cytos` slide over HTTP.
 *
 * A slide is a plain directory: `cytos.json` names the layers, each image
 * layer is an OME-Zarr pyramid next to it. This module fetches the manifest
 * and opens each image pyramid with zarrita, then wraps every resolution
 * level in viv's ZarrPixelSource so viv's tiled renderer can pull chunks
 * straight from the store.
 *
 * cytos stores each channel as its own single-channel pyramid (they are
 * spatially registered). viv wants one loader with a channel dimension, so
 * `stackChannels` presents the per-channel sources as a single `(c, y, x)`
 * source and routes each tile request to the right channel's store.
 */

import { ZarrPixelSource } from '@hms-dbmi/viv';
import * as zarr from 'zarrita';

export interface ImageLayerSpec {
  kind: 'image';
  id: string;
  path: string;
  format: string;
  colormap: string;
  clim?: [number, number];
  visible?: boolean;
}

export interface SegmentLayerSpec {
  kind: 'segments';
  id: string;
  path: string;
  format: string;
  tile_depth: number;
  tiles: [number, number][];
  n_cells: number;
  show_outline?: boolean;
  show_fill?: boolean;
  fill_opacity?: number;
  visible?: boolean;
}

export interface SlideManifest {
  cytos_format: number;
  name: string;
  world_units: string;
  world_bounds: [number, number, number, number];
  tile_depth: number;
  layers: Array<
    ImageLayerSpec | SegmentLayerSpec | { kind: string; id: string; path: string }
  >;
}

/** Side length of one vector tile in world units — same formula as
 * `tile_world_size` in `src/cytos/core/tiling.py`: a square grid over the
 * slide's longest axis. */
export function tileWorldSize(manifest: SlideManifest): number {
  const [minx, miny, maxx, maxy] = manifest.world_bounds;
  return Math.max(maxx - minx, maxy - miny) / (1 << manifest.tile_depth);
}

/** The largest slide format this reader understands. Bumped in lockstep
 * with `CYTOS_FORMAT` in `src/cytos/core/slide.py`. */
export const CYTOS_FORMAT = 1;

/** How the viewer reads bytes: everything goes through one function, so
 * swapping HTTP for local files (Electron) later touches only this. */
export type ReadRange = (
  path: string,
  start?: number,
  end?: number,
) => Promise<Uint8Array | undefined>;

export function httpReadRange(baseUrl: string): ReadRange {
  return async (path, start, end) => {
    const headers: HeadersInit = {};
    if (start !== undefined && end !== undefined) {
      headers['Range'] = `bytes=${start}-${end - 1}`;
    }
    const res = await fetch(`${baseUrl}/${path}`, { headers });
    if (res.status === 404) return undefined;
    if (!res.ok) throw new Error(`GET ${path}: HTTP ${res.status}`);
    return new Uint8Array(await res.arrayBuffer());
  };
}

/** Adapt a ReadRange to zarrita's store interface. */
export class RangeStore implements zarr.AsyncReadable {
  constructor(
    private read: ReadRange,
    private prefix: string,
  ) {}

  async get(key: string): Promise<Uint8Array | undefined> {
    return this.read(this.prefix + key);
  }
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

export function imageLayers(manifest: SlideManifest): ImageLayerSpec[] {
  return manifest.layers.filter((l): l is ImageLayerSpec => l.kind === 'image');
}

export function segmentLayers(manifest: SlideManifest): SegmentLayerSpec[] {
  return manifest.layers.filter((l): l is SegmentLayerSpec => l.kind === 'segments');
}

export interface ImagePyramid {
  levels: ZarrPixelSource<[]>[]; // full resolution first
  /** World units (µm) per full-resolution pixel, from the NGFF scale. */
  pixelSize: number;
}

export async function openImagePyramid(
  read: ReadRange,
  layer: ImageLayerSpec,
): Promise<ImagePyramid> {
  if (layer.format !== 'ome-ngff-0.5') {
    throw new Error(
      `image layer "${layer.id}" has format "${layer.format}" — this reader ` +
        `only handles plain directories yet (re-import with --no-zip)`,
    );
  }
  const root = zarr.root(new RangeStore(read, layer.path));
  const group = await zarr.open.v3(root, { kind: 'group' });
  const attrs = group.attrs as Record<string, any>;
  const multiscale = (attrs.ome ?? attrs).multiscales[0];

  const levels: ZarrPixelSource<[]>[] = [];
  for (const dataset of multiscale.datasets) {
    const arr = await zarr.open.v3(root.resolve(dataset.path), { kind: 'array' });
    const tileSize = arr.chunks[arr.chunks.length - 1];
    levels.push(
      new ZarrPixelSource(arr as zarr.Array<zarr.NumberDataType>, ['y', 'x'], tileSize),
    );
  }

  const transforms = multiscale.datasets[0].coordinateTransformations ?? [];
  const scale = transforms.find((t: { type: string }) => t.type === 'scale');
  return { levels, pixelSize: scale ? scale.scale[scale.scale.length - 1] : 1 };
}

type Selection = Record<string, number>;

/** Present N single-channel `(y, x)` pyramids as one `(c, y, x)` source per
 * level, dispatching on the `c` of each request. */
class ChannelStackSource {
  labels: ['c', 'y', 'x'] = ['c', 'y', 'x'];

  constructor(private channels: ZarrPixelSource<[]>[]) {}

  get shape(): number[] {
    return [this.channels.length, ...this.channels[0].shape];
  }

  get dtype() {
    return this.channels[0].dtype;
  }

  get tileSize(): number {
    return this.channels[0].tileSize;
  }

  private channel(selection: Selection) {
    const c = selection.c ?? 0;
    const source = this.channels[c];
    if (!source) throw new Error(`no channel ${c}`);
    return source;
  }

  getTile(opts: { x: number; y: number; selection: Selection; signal?: AbortSignal }) {
    return this.channel(opts.selection).getTile({ ...opts, selection: {} });
  }

  getRaster(opts: { selection: Selection; signal?: AbortSignal }) {
    return this.channel(opts.selection).getRaster({ ...opts, selection: {} });
  }

  onTileError(err: Error): void {
    console.error(err);
  }
}

/** Zip per-channel pyramids into one multi-channel pyramid: level i of the
 * result stacks level i of every channel. All channels of a slide share one
 * geometry, so the level counts match; trim to the shortest to be safe. */
export function stackChannels(perChannel: ZarrPixelSource<[]>[][]): ChannelStackSource[] {
  const nLevels = Math.min(...perChannel.map((levels) => levels.length));
  return Array.from(
    { length: nLevels },
    (_, i) => new ChannelStackSource(perChannel.map((levels) => levels[i])),
  );
}

/** The black→hue ramps cytos names in manifests, as viv channel colors. */
export const CHANNEL_COLORS: Record<string, [number, number, number]> = {
  blue: [0, 0, 255],
  green: [0, 255, 0],
  red: [255, 0, 0],
  cyan: [0, 255, 255],
  magenta: [255, 0, 255],
  yellow: [255, 255, 0],
  gray: [255, 255, 255],
};
