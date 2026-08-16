/**
 * The layers dock: one collapsible section per layer kind, one row of
 * controls per layer. It edits a SlideSettings object (the saved-session
 * vocabulary, see state.ts) and knows nothing about deck.gl — the viewer
 * reads the same settings and draws them.
 */

import { useState, type ReactNode } from 'react';

import './panel.css';
import {
  autocontrast,
  CHANNEL_COLORS,
  type ImageLayerSpec,
  type SegmentLayerSpec,
} from './slide';
import {
  imageKey,
  segmentsKey,
  type ImageSettings,
  type SectionSettings,
  type SegmentSettings,
  type SlideSettings,
} from './state';
import type { LoadedSlide } from './viewer';

const IMAGE_COLORMAPS = Object.keys(CHANNEL_COLORS);

interface PanelProps {
  slide: LoadedSlide;
  settings: SlideSettings;
  onChange: (key: string, patch: Partial<ImageSettings & SegmentSettings>) => void;
  onSectionChange: (name: string, patch: Partial<SectionSettings>) => void;
}

const styles = {
  panel: {
    position: 'absolute',
    top: 0,
    right: 0,
    bottom: 0,
    width: 280,
    overflowY: 'auto',
    background: 'rgba(24, 24, 28, 0.92)',
    borderLeft: '1px solid #333',
    font: '12px system-ui, sans-serif',
    color: '#ccc',
    padding: '4px 0 12px',
  },
  row: { padding: '6px 12px', borderBottom: '1px solid #26262a' },
  head: { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 },
  name: { flex: 1, color: '#eee' },
  line: { display: 'flex', alignItems: 'center', gap: 6, margin: '3px 0' },
  label: { width: 46, color: '#888' },
  num: { width: 56, color: '#aaa', textAlign: 'right' as const, fontVariantNumeric: 'tabular-nums' },
  select: { background: '#2a2a30', color: '#ccc', border: '1px solid #444', borderRadius: 3 },
  button: {
    background: '#2a2a30',
    color: '#ccc',
    border: '1px solid #444',
    borderRadius: 3,
    padding: '1px 8px',
    cursor: 'pointer',
  },
} as const;

/** One slider, two thumbs, a numeric input on each side. */
function ClimControl({
  value,
  onChange,
}: {
  value: [number, number];
  onChange: (clim: [number, number]) => void;
}) {
  const [lo, hi] = value;
  // A generous but not full-uint16 span, so the usable part of the slider
  // isn't a sliver; typing a bigger number widens it.
  const top = Math.max(1000, hi * 2);
  const pct = (v: number) => `${(100 * v) / top}%`;

  const setLo = (v: number) => onChange([Math.max(0, Math.min(v, hi - 1)), hi]);
  const setHi = (v: number) => onChange([lo, Math.max(v, lo + 1)]);

  return (
    <div style={styles.line}>
      <input
        className="panel-num"
        type="number"
        value={Math.round(lo)}
        onChange={(e) => setLo(Number(e.target.value))}
      />
      <div className="dual">
        <div className="track" />
        <div className="range" style={{ left: pct(lo), width: pct(hi - lo) }} />
        <input
          type="range"
          min={0}
          max={top}
          step={1}
          value={lo}
          onChange={(e) => setLo(Number(e.target.value))}
        />
        <input
          type="range"
          min={0}
          max={top}
          step={1}
          value={hi}
          onChange={(e) => setHi(Number(e.target.value))}
        />
      </div>
      <input
        className="panel-num"
        type="number"
        value={Math.round(hi)}
        onChange={(e) => setHi(Number(e.target.value))}
      />
    </div>
  );
}

function Section({
  title,
  settings,
  onChange,
  children,
}: {
  title: string;
  settings: SectionSettings;
  onChange: (patch: Partial<SectionSettings>) => void;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="section-head" onClick={() => onChange({ expanded: !settings.expanded })}>
        <span className="caret">{settings.expanded ? '▼' : '▶'}</span>
        <input
          type="checkbox"
          checked={settings.checked}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => onChange({ checked: e.target.checked })}
        />
        <span className="title">{title}</span>
      </div>
      {settings.expanded && children}
    </div>
  );
}

function ImageRow({
  layer,
  slide,
  settings,
  onChange,
}: {
  layer: ImageLayerSpec;
  slide: LoadedSlide;
  settings: ImageSettings;
  onChange: (patch: Partial<ImageSettings>) => void;
}) {
  const [busy, setBusy] = useState(false);
  const channel = slide.channels.findIndex((c) => c.id === layer.id);

  return (
    <div style={styles.row}>
      <div style={styles.head}>
        <input
          type="checkbox"
          checked={settings.visible}
          onChange={(e) => onChange({ visible: e.target.checked })}
        />
        <span style={styles.name}>{layer.id}</span>
        <button
          style={styles.button}
          disabled={busy}
          title="contrast from the 1st/99.5th percentile"
          onClick={async () => {
            setBusy(true);
            try {
              onChange({ clim: await autocontrast(slide.loader, channel) });
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? '…' : 'auto'}
        </button>
        <select
          style={styles.select}
          value={settings.colormap}
          onChange={(e) => onChange({ colormap: e.target.value })}
        >
          {IMAGE_COLORMAPS.map((name) => (
            <option key={name}>{name}</option>
          ))}
        </select>
      </div>
      <ClimControl value={settings.clim} onChange={(clim) => onChange({ clim })} />
    </div>
  );
}

function SegmentRow({
  layer,
  settings,
  onChange,
}: {
  layer: SegmentLayerSpec;
  settings: SegmentSettings;
  onChange: (patch: Partial<SegmentSettings>) => void;
}) {
  return (
    <div style={styles.row}>
      <div style={styles.head}>
        <input
          type="checkbox"
          checked={settings.visible}
          onChange={(e) => onChange({ visible: e.target.checked })}
        />
        <span style={styles.name}>
          {layer.id} <span style={{ color: '#777' }}>({layer.n_cells.toLocaleString()})</span>
        </span>
      </div>
      <div style={styles.line}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <input
            type="checkbox"
            checked={settings.show_outline}
            onChange={(e) => onChange({ show_outline: e.target.checked })}
          />
          outline
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 12 }}>
          <input
            type="checkbox"
            checked={settings.show_fill}
            onChange={(e) => onChange({ show_fill: e.target.checked })}
          />
          fill
        </label>
      </div>
      <div style={styles.line}>
        <span style={styles.label}>opacity</span>
        <input
          type="range"
          style={{ flex: 1, minWidth: 0 }}
          min={0.05}
          max={1}
          step={0.05}
          value={settings.fill_opacity}
          onChange={(e) => onChange({ fill_opacity: Number(e.target.value) })}
        />
        <span style={styles.num}>{settings.fill_opacity.toFixed(2)}</span>
      </div>
    </div>
  );
}

export function Panel({ slide, settings, onChange, onSectionChange }: PanelProps) {
  const images = slide.channels;
  const segments = slide.segments.map((s) => s.spec);

  return (
    <div style={styles.panel}>
      {images.length > 0 && (
        <Section
          title="Images"
          settings={settings.sections.images}
          onChange={(patch) => onSectionChange('images', patch)}
        >
          {images.map((layer) => (
            <ImageRow
              key={layer.id}
              layer={layer}
              slide={slide}
              settings={settings.layers[imageKey(layer.id)] as ImageSettings}
              onChange={(patch) => onChange(imageKey(layer.id), patch)}
            />
          ))}
        </Section>
      )}
      {segments.length > 0 && (
        <Section
          title="Segments"
          settings={settings.sections.segments}
          onChange={(patch) => onSectionChange('segments', patch)}
        >
          {segments.map((layer) => (
            <SegmentRow
              key={layer.id}
              layer={layer}
              settings={settings.layers[segmentsKey(layer.id)] as SegmentSettings}
              onChange={(patch) => onChange(segmentsKey(layer.id), patch)}
            />
          ))}
        </Section>
      )}
    </div>
  );
}
