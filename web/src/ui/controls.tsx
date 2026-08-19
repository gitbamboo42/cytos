/**
 * The panel's building blocks: a color swatch, a dropdown, a two-thumb
 * contrast slider, a collapsible section — plus the shared row styles.
 *
 * Nothing here knows what a layer is. The rows in `rows.tsx` compose these.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';

import './panel.css';
import type { SectionSettings } from '../core/session';

export const styles = {
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
  button: {
    background: '#2a2a30',
    color: '#ccc',
    border: '1px solid #444',
    borderRadius: 3,
    padding: '1px 8px',
    cursor: 'pointer',
  },
} as const;

/** A web-rendered dropdown. The native <select> popup is drawn by the OS
 * at system font size, immune to page CSS — so the list is ours instead. */
export function Dropdown({
  value,
  options,
  labels,
  disabled,
  onChange,
  grow,
}: {
  value: string;
  options: string[];
  /** Display text per option; defaults to the option itself. */
  labels?: Record<string, string>;
  /** Options that are shown but cannot be picked — the session picker greys
   * out a name another window already has open, as Qt's does. */
  disabled?: string[];
  onChange: (value: string) => void;
  grow?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', key);
    return () => {
      window.removeEventListener('pointerdown', close);
      window.removeEventListener('keydown', key);
    };
  }, [open]);

  return (
    <div className="dd" ref={ref} style={grow ? { flex: 1 } : undefined}>
      <button type="button" className="dd-button" onClick={() => setOpen(!open)}>
        <span className="dd-value">{labels?.[value] ?? value}</span>
        <span className="dd-caret">▼</span>
      </button>
      {open && (
        <div className="dd-list">
          {options.map((option) => {
            const off = disabled?.includes(option) && option !== value;
            return (
              <div
                key={option}
                className={
                  off ? 'dd-item off' : option === value ? 'dd-item selected' : 'dd-item'
                }
                onClick={() => {
                  if (off) return;
                  onChange(option);
                  setOpen(false);
                }}
              >
                {labels?.[option] ?? option}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** One slider, two thumbs, a numeric input on each side.
 *
 * `top` is the channel's own maximum with 20% headroom, the same bound the
 * Qt panel uses (`intensity_max` in `src/cytos/ui/main_window.py`), and it
 * limits the number boxes as well as the slider. Dragging far past a
 * channel's brightest pixel only produces black frames, and a fixed span
 * would squeeze every handle on a dim channel into the first few percent of
 * the groove. */
export function ClimControl({
  value,
  top,
  onChange,
}: {
  value: [number, number];
  top: number;
  onChange: (clim: [number, number]) => void;
}) {
  const [lo, hi] = value;
  const pct = (v: number) => `${(100 * Math.min(v, top)) / top}%`;
  const clamp = (v: number) => Math.max(0, Math.min(v, top));

  const setLo = (v: number) => onChange([Math.min(clamp(v), hi - 1), hi]);
  const setHi = (v: number) => onChange([lo, Math.max(clamp(v), lo + 1)]);

  return (
    <div style={styles.line}>
      <input
        className="panel-num"
        type="number"
        min={0}
        max={top}
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
        min={0}
        max={top}
        value={Math.round(hi)}
        onChange={(e) => setHi(Number(e.target.value))}
      />
    </div>
  );
}

export function Section({
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
