/**
 * The gene picker: which genes a point layer draws, and what colour each one
 * takes.
 *
 * Its own file for the same reason `color-picker.tsx` is — it carries search
 * state, browse state and a tree of its own, and `rows.tsx` is meant to be
 * one row per layer kind, not a place widgets grow in.
 */

import { useEffect, useRef, useState } from 'react';

import { presetGeneColor } from '../core/colormaps';
import type { GeneTable } from '../io/points';
import { ColorSwatch, type CustomColors } from './color-picker';
import { styles } from './controls';

/**
 * Which genes to draw, as a one-level tree.
 *
 * The root sits **inside the list**, as its first row: it is a node like any
 * other, and its checkbox is all-or-none, which is why there are no separate
 * buttons for that. It shows a dash when only some genes are on, so "partly
 * selected" has a state instead of being ambiguous.
 *
 * With the search box empty the list shows only the selected genes — 514 rows
 * is not a list anyone reads. Type to search the whole panel and add more.
 * Genes are ordered by whole-slide abundance, which `genes.parquet` ships
 * counts for, so building the list touches no transcripts.
 *
 * **Every gene has a preset colour, shown as a circle**, fixed by gene id so
 * it does not move when the selection changes — the list and the slide always
 * agree about which colour is which gene. Clicking a circle pins a different
 * one.
 */
export function GenePicker({
  genes,
  selected,
  byGene,
  colors,
  custom,
  onChange,
  onColorChange,
}: {
  genes: GeneTable;
  selected: number[] | null;
  byGene: boolean;
  colors: Record<number, string>;
  custom: CustomColors;
  onChange: (next: number[] | null) => void;
  onColorChange: (next: Record<number, string>) => void;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  // Clicking into the search box means "let me look at the panel", so the
  // whole list appears before a single character is typed. It stays up until
  // a click lands outside the picker — leaving on blur would close the list
  // the moment you reached for a checkbox in it.
  const [browsing, setBrowsing] = useState(false);
  const rootBox = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!browsing) return;
    const away = (e: PointerEvent) => {
      // Colours cannot be picked while browsing, so a click is either in
      // this list or done with it.
      if (!box.current?.contains(e.target as Node)) setBrowsing(false);
    };
    window.addEventListener('pointerdown', away);
    return () => window.removeEventListener('pointerdown', away);
  }, [browsing]);

  const all = selected === null;
  const chosen = selected ?? [];
  const isOn = (id: number) => all || chosen.includes(id);
  const count = all ? genes.names.length : chosen.length;

  // A checkbox has no "some" state in markup — it has to be set on the node.
  // The root row shows once something is selected, or while browsing — with
  // the whole panel on screen it is the only way to take all of it. With
  // nothing selected and nothing being browsed there is no group to un-group,
  // and an empty node above an empty list is just furniture.
  useEffect(() => {
    if (rootBox.current) {
      rootBox.current.indeterminate = count > 0 && count < genes.names.length;
    }
  }, [count, genes.names.length]);

  const byAbundance = genes.names
    .map((name, id) => ({ name, id, count: genes.counts[id] ?? 0 }))
    .sort((a, b) => b.count - a.count);
  const needle = query.trim().toLowerCase();
  // At rest the list holds only the genes that are on. An unchecked gene
  // shows up while browsing or searching — never otherwise, so the resting
  // list is exactly what is drawn on the slide.
  const matched = needle
    ? byAbundance.filter((g) => g.name.toLowerCase().includes(needle))
    : browsing
      ? byAbundance
      : byAbundance.filter((g) => isOn(g.id));
  // Render at most 300 rows — enough to scroll a whole panel, few enough to
  // keep typing in the search box snappy. The row below the list says what
  // the cap left off, so a truncated list never looks complete.
  const shown = matched.slice(0, 300);

  const toggle = (id: number) => {
    if (all) {
      // Dropping one gene out of "everything" needs the list spelled out.
      onChange(genes.names.map((_, i) => i).filter((i) => i !== id));
      return;
    }
    const next = chosen.includes(id) ? chosen.filter((g) => g !== id) : [...chosen, id];
    // Ticking the last one is the same request as "all", and saying it that
    // way keeps the cheap whole-tile read.
    onChange(next.length === genes.names.length ? null : next);
  };

  return (
    <div style={{ ...styles.line, alignItems: 'flex-start' }}>
      <div ref={box} style={{ flex: 1, minWidth: 0 }}>
        <input
          style={geneStyles.search}
          placeholder="search genes…"
          value={query}
          spellCheck={false}
          onFocus={() => {
            setBrowsing(true);
            setOpen(true);
          }}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div style={geneStyles.list}>
          {(count > 0 || browsing) && (
            <div style={{ ...geneStyles.item, ...geneStyles.rootItem }}>
              <input
                ref={rootBox}
                type="checkbox"
                checked={count === genes.names.length}
                onChange={() => onChange(count > 0 ? [] : null)}
              />
              <span
                style={{ flex: 1, minWidth: 0, cursor: 'pointer' }}
                onClick={() => setOpen(!open)}
              >
                {open ? '▾' : '▸'} genes
              </span>
              <span style={{ color: '#777' }}>{genes.names.length.toLocaleString()}</span>
            </div>
          )}
          {open &&
            shown.map((gene) => (
              <div key={gene.id} style={{ ...geneStyles.item, ...geneStyles.child }}>
                <input
                  type="checkbox"
                  checked={isOn(gene.id)}
                  onChange={() => toggle(gene.id)}
                />
                {!byGene ? (
                  <span style={geneStyles.dot} />
                ) : browsing ? (
                  // While browsing, the circle only reports the colour. The
                  // list is a place to pick genes then; opening a colour
                  // popup over a list you are scanning fights the thing you
                  // came to do.
                  <span
                    style={{
                      ...geneStyles.dot,
                      borderRadius: '50%',
                      background: colors[gene.id] ?? presetGeneColor(gene.id),
                    }}
                  />
                ) : (
                  <ColorSwatch
                    round
                    value={colors[gene.id] ?? presetGeneColor(gene.id)}
                    title={`${gene.name} color`}
                    custom={custom}
                    onChange={(value) => onColorChange({ ...colors, [gene.id]: value })}
                  />
                )}
                <span style={{ flex: 1, minWidth: 0 }}>{gene.name}</span>
                <span style={{ color: '#777' }}>{gene.count.toLocaleString()}</span>
              </div>
            ))}
          {open && shown.length === 0 && (
            <div style={{ color: '#777', padding: '1px 4px 1px 22px' }}>
              {needle ? 'no match' : 'none selected — click search to browse'}
            </div>
          )}
          {open && matched.length > shown.length && (
            <div style={{ color: '#777', padding: '1px 4px 1px 22px' }}>
              …{(matched.length - shown.length).toLocaleString()} more — search to
              narrow
            </div>
          )}
        </div>
        <div style={geneStyles.footer}>
          {count} of {genes.names.length} selected
        </div>
      </div>
    </div>
  );
}

const geneStyles = {
  rootItem: { color: '#aaa' },
  child: { paddingLeft: 16 },
  footer: { color: '#777', marginTop: 3 },
  search: {
    background: '#131316',
    border: '1px solid #35353b',
    borderRadius: 3,
    color: '#ddd',
    font: 'inherit',
    padding: '2px 6px',
    width: '100%',
    boxSizing: 'border-box' as const,
  },
  list: {
    maxHeight: 180,
    overflowY: 'auto' as const,
    marginTop: 4,
    border: '1px solid #26262a',
    borderRadius: 3,
  },
  item: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    padding: '1px 4px',
  },
  dot: { width: 8, height: 8, borderRadius: 2, flex: '0 0 auto' },
};
