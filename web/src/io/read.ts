/**
 * How the viewer reads bytes.
 *
 * Everything goes through one `ReadRange` function, so a slide served over
 * HTTP and a slide sitting on the local disk differ in exactly one line of
 * this file. `RangeStore`, `ZipStore`, viv and deck never learn which.
 */

import * as zarr from 'zarrita';

import { desktopHost } from './host';
import { ZipStore } from './zip';

/**
 * Read `[start, end)` of a file, the whole file when both are omitted, or —
 * when `start` is negative and `end` omitted — its last `-start` bytes.
 * That last form is HTTP's own suffix range, not an invention here: a zip's
 * central directory lives at the end of the file, and its offset can only be
 * found by reading backwards from there without knowing the total size.
 */
export type ReadRange = (
  path: string,
  start?: number,
  end?: number,
) => Promise<Uint8Array | undefined>;

export function httpReadRange(baseUrl: string): ReadRange {
  return async (path, start, end) => {
    const headers: HeadersInit = {};
    if (start !== undefined && start < 0 && end === undefined) {
      headers['Range'] = `bytes=-${-start}`;
    } else if (start !== undefined && end !== undefined) {
      headers['Range'] = `bytes=${start}-${end - 1}`;
    }
    const res = await fetch(`${baseUrl}/${path}`, { headers });
    if (res.status === 404) return undefined;
    if (!res.ok) throw new Error(`GET ${path}: HTTP ${res.status}`);
    return new Uint8Array(await res.arrayBuffer());
  };
}

/**
 * Read a slide off the local disk, through the desktop shell.
 *
 * There is no server and no HTTP: the shell reads the file and hands the
 * bytes back. Callers cannot tell this apart from `httpReadRange`, which is
 * the whole point — a local slide and a served one take the same path from
 * here on.
 */
export function fileReadRange(slideDir: string): ReadRange {
  const host = desktopHost();
  if (!host) throw new Error('fileReadRange needs the desktop shell');
  return (path, start, end) => host.readRange(slideDir, path, start, end);
}

/** Read a slide however this build can: off the disk in the desktop shell,
 * over HTTP in a browser tab. */
export function readerFor(slide: string): ReadRange {
  return desktopHost() ? fileReadRange(slide) : httpReadRange(slide);
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

/**
 * Open a layer's zarr store, plain directory or zipped, by its manifest
 * `format` tag. One place decides, so a new layer kind gets both forms for
 * free instead of carrying its own copy of the check.
 *
 * `storePath` is the store without the `.zip` suffix; the zipped form adds
 * it, except where the manifest already carries it (image layers do).
 */
export async function openStore(
  read: ReadRange,
  what: string,
  format: string,
  storePath: string,
  formats: { dir: string; zip: string },
): Promise<zarr.AsyncReadable> {
  if (format === formats.dir) return new RangeStore(read, storePath);
  if (format === formats.zip) {
    const path = storePath.endsWith('.zip') ? storePath : `${storePath}.zip`;
    return ZipStore.open(read, path);
  }
  throw new Error(
    `${what} has format "${format}" — this reader understands ` +
      `"${formats.dir}" and "${formats.zip}"`,
  );
}
