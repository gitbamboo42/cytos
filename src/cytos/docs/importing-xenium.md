# Importing Xenium data

What cytos does to read 10x Genomics' Xenium output bundles — one file per
supported data source lives in this folder, named for what *cytos* does
(`importing-<source>.md`), because the source is someone else's product and
its own documentation is not ours to rewrite. Facts below are only the ones
cytos's code depends on, marked *(verified)* when checked against a real
bundle; for everything else, follow the links. The vendor-free code lessons
these facts taught are in `skills/developers.md`; which datasets are on the
machine is `data/README.md` (gitignored).

10x's own references — swap `latest` in the URL for a version number (e.g.
`2.0`, `3.1`) to pin what a given bundle was produced with (10x sometimes
moves pages between versions; if a pinned link 404s, navigate from the
release notes):

- [Understanding Xenium outputs](https://www.10xgenomics.com/support/software/xenium-onboard-analysis/latest/analysis/xoa-output-understanding-outputs)
- [Xenium zarr output files](https://www.10xgenomics.com/support/software/xenium-onboard-analysis/latest/advanced/xoa-output-zarr)
- [XOA release notes](https://www.10xgenomics.com/support/software/xenium-onboard-analysis/latest/release-notes/release-notes-for-xoa) — where format changes between versions are announced
- [Public datasets](https://www.10xgenomics.com/datasets) — bundles download from
  `https://cf.10xgenomics.com/samples/xenium/<XOA-version>/<name>/<name>_outs.zip`,
  no auth *(verified)*

## What cytos reads from a bundle

Get the **full** `_outs.zip` — the `_xe_outs.zip` "Explorer subset" next to
it is zarr-only and lacks every parquet file cytos reads *(verified)*.
`_discover_xenium` (`cytos.prep.slide`) looks for:

- `cell_boundaries.parquet` / `nucleus_boundaries.parquet` — long-format
  boundary tables (`cell_id, vertex_x, vertex_y`) → segment layers.
- `cells.parquet` — per-cell attributes, joined into `features` by cell id.
- `transcripts.parquet` — the point layer (below).
- `morphology_focus` images — the image channels (below).

Coordinates are planar micrometres, row-major Y — the same convention as
cytos world space, so every loader reads them straight through.

## Morphology images — support by XOA version

The layout changed at 2.0; discovery handles all three, and must keep doing
so (a glob for only one layout finds nothing in the others and imports an
image-less slide without complaint):

- **< 2.0, gene-only panel** *(verified: breast rep1)*: single
  `morphology_focus.ome.tif`, and `SizeC=1` collapses to `axes="YX"` — no
  channel axis; `prep/pyramid.py` guards this.
- **< 2.0, protein panel** *(verified: human_kidney_tiny)*:
  `morphology_focus/ch*.ome.tif`; the first file can contain all channels
  despite its per-channel name.
- **≥ 2.0** *(verified: pancreas at 2.0.0, ovarian Prime at 3.0)*:
  `morphology_focus/morphology_focus_0000..0003.ome.tif` — one multi-file
  OME-TIFF, one file per stain, JPEG-2000 (hence the `imagecodecs`
  dependency). Discovery opens the first file, reads channel names from the
  OME metadata, and emits one image layer per stain, DAPI first. The OME
  names are raw marker lists, identical in every bundle seen so far
  *(verified: pancreas, ovarian Prime, kidney)*; `_KNOWN_STAINS` in
  `prep/slide.py` maps them to layer ids that say what the stain is for,
  with the default colors 10x's own viewer uses:

  | OME channel name | layer id | color |
  |---|---|---|
  | `DAPI` | `nuclear` | blue |
  | `ATP1A1/CD45/E-Cadherin` | `boundary` | magenta |
  | `18S` | `interior_rna` | yellow |
  | `alphaSMA/Vimentin` | `interior_protein` | green |

  Unknown channel names fall through to a slugged id and a positional
  color.

`morphology.ome.tif` (no `_focus`) is the raw 3D z-stack — ignored. Pixel
size has always been 0.2125 µm/px so far, but it is read from the OME
metadata, never assumed.

## Transcripts

`load_transcripts` uses `feature_name`, `x_location`, `y_location`, and —
when present, older bundles lack them — `qv` and `is_gene`. It filters at
QV ≥ 20 (the platform's own cutoff; their tools never display the tail) and
drops control codewords via `is_gene`. Scale note *(verified: ovarian
Prime)*: a 5K-panel run had 147.7M rows with `feature_name` as plain
`large_string`, not dictionary-encoded — handle that column in Arrow, not
as numpy strings.

`transcripts.zarr.zip` is Explorer's own store and cytos does not read it —
noted here only because it answers a design question: it holds a 6-level
transcript pyramid (each level a ¼ subsample, *(verified: pancreas)*) plus a
per-gene density raster. cytos builds its own LOD at prep time instead, so
non-Xenium sources get one too.

## Clustering

`analysis.tar.gz` → `analysis/clustering/*/clusters.csv`, two columns:
`Barcode,Cluster` — the cell id and a 1-based integer; unassigned cells are
simply absent *(verified)*. `cytos-import` joins every clustering it finds
onto the segment layers' feature tables as columns marked categorical in
the schema — `cluster` for `gene_expression_graphclust`, `kmeans_2` …
`kmeans_10` for the fixed-k ones — so they appear in "Color by" and draw
with a qualitative palette (see `cytos.core.polygons.join_categories`).
Separately, Explorer's [custom cell-groups import](https://www.10xgenomics.com/analysis-guides/importing-customized-clustering-into-xenium-explorer)
format (`cell_id, group[, color]` CSV) is a shape users already have files
in — worth accepting verbatim if cytos grows an add-features path.
