// Smoke-test the web data path without a browser: manifest -> NGFF metadata
// -> chunk fetch -> zstd decode, all over HTTP against tools/serve_slides.py.
import * as zarr from 'zarrita';

const base = 'http://127.0.0.1:8787/breast_rep1_nozip.cytos';

const manifest = await (await fetch(`${base}/cytos.json`)).json();
console.log('manifest:', manifest.name, 'format', manifest.cytos_format);
const image = manifest.layers.find((l) => l.kind === 'image');
console.log('image layer:', image.id, image.format, 'clim', image.clim);

const store = new zarr.FetchStore(`${base}/${image.path}`);
const root = zarr.root(store);
const group = await zarr.open(root, { kind: 'group' });
const ome = group.attrs.ome ?? group.attrs;
const datasets = ome.multiscales[0].datasets;
console.log('levels:', datasets.map((d) => d.path).join(' '));

for (const path of [datasets[datasets.length - 1].path, datasets[0].path]) {
  const arr = await zarr.open(root.resolve(path), { kind: 'array' });
  const t0 = performance.now();
  const chunk = await arr.getChunk([path === 's0' ? 12 : 0, path === 's0' ? 12 : 0]);
  const ms = (performance.now() - t0).toFixed(1);
  const data = chunk.data;
  let max = 0;
  let nonzero = 0;
  for (const v of data) {
    if (v > max) max = v;
    if (v) nonzero++;
  }
  console.log(
    `${path}: shape [${arr.shape}] chunks [${arr.chunks}] dtype ${arr.dtype}; ` +
      `one chunk in ${ms} ms, max ${max}, ${((100 * nonzero) / data.length).toFixed(1)}% nonzero`,
  );
}

// A segment tile: rings are reconstructed from runs of vertex_cell_id, so
// verify each cell id really is one contiguous run.
const seg = manifest.layers.find((l) => l.kind === 'segments');
if (seg) {
  const segRoot = zarr.root(new zarr.FetchStore(`${base}/${seg.path}/tiles.zarr`));
  const [row, col] = seg.tiles[0];
  const tilePath = `tile/${seg.tile_depth}/${row}/${col}`;
  const t0 = performance.now();
  const [coords, vcid] = await Promise.all(
    ['coords', 'vertex_cell_id'].map(async (name) => {
      const arr = await zarr.open(segRoot.resolve(`${tilePath}/${name}`), { kind: 'array' });
      return (await zarr.get(arr)).data;
    }),
  );
  const ms = (performance.now() - t0).toFixed(1);
  const runIds = [];
  for (let i = 0; i < vcid.length; i++) {
    if (i === 0 || vcid[i] !== vcid[i - 1]) runIds.push(vcid[i]);
  }
  const contiguous = new Set(runIds).size === runIds.length;
  console.log(
    `segments "${seg.id}" ${tilePath}: ${coords.length / 2} vertices, ` +
      `${runIds.length} cells in ${ms} ms; one run per cell: ${contiguous}`,
  );
  if (!contiguous) throw new Error('vertex_cell_id runs are not one per cell');
}

// Range request through the same server, as the zip reader will use later.
const res = await fetch(`${base}/cytos.json`, { headers: { Range: 'bytes=0-15' } });
console.log('range request:', res.status, JSON.stringify(await res.text()));
