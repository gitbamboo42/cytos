/**
 * Read a zipped zarr store (`*.zarr.zip`) over HTTP range requests.
 *
 * `cytos-import` writes each store as one zip file rather than thousands of
 * chunk files (see `src/cytos/core/store.py`). The zip **compresses
 * nothing** — every entry is STORED, because zarr has already compressed
 * each chunk with zstd/blosc. So the bytes at an entry's offset *are* the
 * chunk: fetch that byte range and hand it straight to zarrita, with no
 * inflate step anywhere.
 *
 * Opening costs two small reads — the end-of-central-directory record, then
 * the central directory itself. After that every chunk is one ranged GET,
 * the same as a plain directory would cost.
 *
 * ZIP64 is handled: a slide store passes 4 GB well before anything else
 * breaks, and the 32-bit fields silently wrap rather than erroring.
 */

import * as zarr from 'zarrita';

import type { ReadRange } from './read';

const EOCD_SIG = 0x06054b50;
const EOCD64_LOCATOR_SIG = 0x07064b50;
const EOCD64_SIG = 0x06064b50;
const CENTRAL_SIG = 0x02014b50;
const ZIP64_EXTRA_ID = 0x0001;

/** Biggest possible end-of-central-directory record: 22 fixed bytes plus a
 * comment of up to 65535. Python's zipfile writes no comment, but reading
 * the full span costs one request either way. */
const MAX_EOCD = 22 + 0xffff;

interface ZipEntry {
  /** Offset of the entry's *local* header, not of its data — the local
   * header repeats the name and may carry its own extra field, so the data
   * offset is only known once those two lengths are read. */
  localHeaderOffset: number;
  compressedSize: number;
  /** Byte length of the name in the central directory, used to guess how
   * far past the local header the data starts. */
  nameLength: number;
  method: number;
}

function u16(v: DataView, at: number): number {
  return v.getUint16(at, true);
}

function u32(v: DataView, at: number): number {
  return v.getUint32(at, true);
}

/** 64-bit little-endian. Sizes here are byte counts well under 2^53, so a
 * JS number is exact; BigInt would only complicate the arithmetic. */
function u64(v: DataView, at: number): number {
  return v.getUint32(at, true) + v.getUint32(at + 4, true) * 0x1_0000_0000;
}

/** Pull the real size/offset out of a ZIP64 extra field, which carries only
 * the values whose 32-bit slots were saturated, in a fixed order. */
function zip64Extra(
  extra: DataView,
  want: { uncompressed: boolean; compressed: boolean; offset: boolean },
): { compressedSize?: number; localHeaderOffset?: number } {
  let at = 0;
  while (at + 4 <= extra.byteLength) {
    const id = u16(extra, at);
    const size = u16(extra, at + 2);
    if (id !== ZIP64_EXTRA_ID) {
      at += 4 + size;
      continue;
    }
    let field = at + 4;
    const out: { compressedSize?: number; localHeaderOffset?: number } = {};
    if (want.uncompressed) field += 8; // present but not needed
    if (want.compressed) {
      out.compressedSize = u64(extra, field);
      field += 8;
    }
    if (want.offset) out.localHeaderOffset = u64(extra, field);
    return out;
  }
  return {};
}

export class ZipStore implements zarr.AsyncReadable {
  private constructor(
    private read: ReadRange,
    private path: string,
    private entries: Map<string, ZipEntry>,
  ) {}

  static async open(read: ReadRange, path: string): Promise<ZipStore> {
    // The central directory is at the end and its offset is only knowable
    // after reading it, so start from the tail. A suffix range is rejected
    // when it is longer than the file itself (416), which only happens for
    // a store small enough to fetch whole.
    let tail: Uint8Array | undefined;
    // Where the tail buffer starts in the file. Unknown after a suffix read
    // (we never learn the total size); derived below, or 0 if we gave up and
    // fetched the whole thing.
    let tailStart = -1;
    try {
      tail = await read(path, -MAX_EOCD);
    } catch {
      tail = await read(path);
      tailStart = 0;
    }
    if (!tail) throw new Error(`no zipped store at ${path}`);

    const view = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
    let eocd = -1;
    for (let i = tail.byteLength - 22; i >= 0; i--) {
      if (u32(view, i) === EOCD_SIG) {
        eocd = i;
        break;
      }
    }
    if (eocd < 0) throw new Error(`${path} is not a zip file (no end-of-central-directory)`);

    let entryCount = u16(view, eocd + 10);
    let cdSize = u32(view, eocd + 12);
    let cdOffset = u32(view, eocd + 16);

    // ZIP64: any saturated 32-bit field means the real values live in a
    // separate record, found through a locator just before the EOCD.
    if (cdOffset === 0xffffffff || cdSize === 0xffffffff || entryCount === 0xffff) {
      const locator = eocd - 20;
      if (locator < 0 || u32(view, locator) !== EOCD64_LOCATOR_SIG) {
        throw new Error(`${path}: ZIP64 sizes but no ZIP64 locator`);
      }
      const at = u64(view, locator + 8);
      const rec = await read(path, at, at + 56);
      if (!rec) throw new Error(`${path}: ZIP64 end-of-central-directory unreadable`);
      const rv = new DataView(rec.buffer, rec.byteOffset, rec.byteLength);
      if (u32(rv, 0) !== EOCD64_SIG) throw new Error(`${path}: bad ZIP64 record`);
      entryCount = u64(rv, 32);
      cdSize = u64(rv, 40);
      cdOffset = u64(rv, 48);
      // ZIP64 puts its own record and a locator between the directory and
      // the EOCD, so the offset arithmetic below doesn't hold — just fetch.
      tailStart = -1;
    } else if (tailStart < 0) {
      // A plain zip writes the EOCD immediately after the central
      // directory, so the tail began at (cdOffset + cdSize) - eocd. Knowing
      // that often saves the second request: for a small store the whole
      // directory is already in the bytes we fetched.
      tailStart = cdOffset + cdSize - eocd;
    }

    let cd: Uint8Array;
    const insideTail =
      tailStart >= 0 &&
      cdOffset >= tailStart &&
      cdOffset + cdSize <= tailStart + tail.byteLength;
    if (insideTail) {
      cd = tail.subarray(cdOffset - tailStart, cdOffset - tailStart + cdSize);
    } else {
      const fetched = await read(path, cdOffset, cdOffset + cdSize);
      if (!fetched) throw new Error(`${path}: central directory unreadable`);
      cd = fetched;
    }

    const entries = new Map<string, ZipEntry>();
    const cv = new DataView(cd.buffer, cd.byteOffset, cd.byteLength);
    const names = new TextDecoder();
    let at = 0;
    for (let i = 0; i < entryCount && at + 46 <= cd.byteLength; i++) {
      if (u32(cv, at) !== CENTRAL_SIG) break;
      const method = u16(cv, at + 10);
      let compressedSize = u32(cv, at + 20);
      const nameLength = u16(cv, at + 28);
      const extraLength = u16(cv, at + 30);
      const commentLength = u16(cv, at + 32);
      let localHeaderOffset = u32(cv, at + 42);
      const name = names.decode(cd.subarray(at + 46, at + 46 + nameLength));
      if (compressedSize === 0xffffffff || localHeaderOffset === 0xffffffff) {
        const extraAt = at + 46 + nameLength;
        const big = zip64Extra(
          new DataView(cd.buffer, cd.byteOffset + extraAt, extraLength),
          {
            uncompressed: u32(cv, at + 24) === 0xffffffff,
            compressed: compressedSize === 0xffffffff,
            offset: localHeaderOffset === 0xffffffff,
          },
        );
        compressedSize = big.compressedSize ?? compressedSize;
        localHeaderOffset = big.localHeaderOffset ?? localHeaderOffset;
      }
      entries.set(name, { localHeaderOffset, compressedSize, nameLength, method });
      at += 46 + nameLength + extraLength + commentLength;
    }
    return new ZipStore(read, path, entries);
  }

  /** zarrita hands keys with a leading slash; zip entry names have none. */
  async get(key: string): Promise<Uint8Array | undefined> {
    const entry = this.entries.get(key.replace(/^\//, ''));
    if (!entry) return undefined;
    if (entry.method !== 0) {
      throw new Error(
        `${this.path}: entry "${key}" is compressed (method ${entry.method}); ` +
          `cytos writes STORED entries only`,
      );
    }
    // One request covers the local header and the data. The local extra
    // field is empty in what cytos writes, so the guess is right and the
    // second read below never happens — but a zip written elsewhere may
    // differ, and a short buffer is not a corrupt chunk.
    const guess = 30 + entry.nameLength;
    const start = entry.localHeaderOffset;
    const buf = await this.read(this.path, start, start + guess + entry.compressedSize);
    if (!buf || buf.byteLength < 30) return undefined;
    const v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const dataAt = 30 + u16(v, 26) + u16(v, 28);
    if (buf.byteLength >= dataAt + entry.compressedSize) {
      return buf.subarray(dataAt, dataAt + entry.compressedSize);
    }
    return this.read(this.path, start + dataAt, start + dataAt + entry.compressedSize);
  }
}
