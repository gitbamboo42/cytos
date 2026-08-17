/**
 * One row of controls per layer, one function per layer kind. A row edits
 * only its own slice of the settings and knows nothing about deck.gl — the
 * renderer reads the same settings and draws them.
 */

import { useState } from 'react';

import { RAMP_NAMES } from '../core/colormaps';
import type { ImageLayerSpec, SegmentLayerSpec } from '../core/manifest';
import type { ImageSettings, SegmentSettings } from '../core/session';
import type { FeatureTable } from '../io/features';
import type { LoadedSlide } from '../io/slide';
import { autocontrast } from '../render/image';
import { ColorSwatch, type CustomColors } from './color-picker';
import { ClimControl, Dropdown, styles } from './controls';

export function ImageRow({
  layer,
  slide,
  settings,
  custom,
  onChange,
}: {
  layer: ImageLayerSpec;
  slide: LoadedSlide;
  settings: ImageSettings;
  custom: CustomColors;
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
        <ColorSwatch
          value={settings.colormap}
          title="channel color"
          custom={custom}
          onChange={(colormap) => onChange({ colormap })}
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
      </div>
      <ClimControl
        value={settings.clim}
        top={slide.intensityMax[channel] * 1.2}
        onChange={(clim) => onChange({ clim })}
      />
    </div>
  );
}

export function SegmentRow({
  layer,
  settings,
  features,
  custom,
  onChange,
}: {
  layer: SegmentLayerSpec;
  settings: SegmentSettings;
  features: FeatureTable | null;
  custom: CustomColors;
  onChange: (patch: Partial<SegmentSettings>) => void;
}) {
  const featureNames = features?.names ?? [];
  const coloring = settings.color_by ? features?.get(settings.color_by) : undefined;
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
      <div style={styles.line}>
        <span style={styles.label}>color by</span>
        <Dropdown
          grow
          value={settings.color_by ?? ''}
          options={['', ...featureNames]}
          labels={{ '': 'Flat color' }}
          onChange={(value) => onChange({ color_by: value || null })}
        />
        {!settings.color_by && (
          <ColorSwatch
            value={settings.colormap}
            title="flat cell color"
            custom={custom}
            onChange={(colormap) => onChange({ colormap })}
          />
        )}
        {coloring && !coloring.categorical && (
          <Dropdown
            value={settings.colormap}
            options={RAMP_NAMES}
            onChange={(colormap) => onChange({ colormap })}
          />
        )}
      </div>
    </div>
  );
}
