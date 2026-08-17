/**
 * Open a slide's image layers as a viv-compatible pyramid.
 *
 * cytos stores each channel as its own single-channel OME-Zarr pyramid (they
 * are spatially registered). viv wants one loader with a channel dimension,
 * so `stackChannels` presents the per-channel sources as a single `(c, y, x)`
 * source and routes each tile request to the right channel's store.
 */

import { ZarrPixelSource } from '@hms-dbmi/viv';
import * as zarr from 'zarrita';

import type { ImageLayerSpec } from '../core/manifest';
import { openStore, type ReadRange } from './read';

const FORMATS = { dir: 'ome-ngff-0.5', zip: 'ome-ngff-0.5-zip' };

export interface ImagePyramid {
  levels: ZarrPixelSource<[]>[]; // full resolution first
  /** World units (µm) per full-resolution pixel, from the NGFF scale. */
  pixelSize: number;
}

export async function openImagePyramid(
  read: ReadRange,
  layer: ImageLayerSpec,
): Promise<ImagePyramid> {
  const store = await openStore(
    read, `image layer "${layer.id}"`, layer.format, layer.path, FORMATS,
  );
  const root = zarr.root(store);
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
export class ChannelStackSource {
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
