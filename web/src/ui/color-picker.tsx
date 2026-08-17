/**
 * One color picker, shared by every place a color is chosen — image channels
 * and segment layers. A web port of `src/cytos/ui/color_picker.py`, keeping
 * the behaviour that guide calls out:
 *
 * Two labelled sections — Preset (the standard set) and Custom, the user's
 * own colors. The Qt picker has a third, Colormap, offering ramps for image
 * channels; there is no web equivalent, because viv takes one flat RGB per
 * channel and a ramp would need a custom LUT shader. Custom colors live
 * in one per-window pool shared by every picker, so a color created while
 * tuning one layer is a one-click swatch on every other. Hovering a swatch
 * names it in the label at the bottom; nothing changes until a swatch is
 * clicked, and the popup **stays open** afterwards (clicking outside dismisses
 * it) so colors can be compared against the image.
 *
 * The [+] unfolds a pocket picker — shade square, hue strip, hex box, Add —
 * inline rather than a dialog, matching Qt, where a modal would have closed
 * the popup.
 */

import { useEffect, useRef, useState } from 'react';

import { COLOR_PRESETS, colorValueHex, hexToHsv, hsvToHex } from '../core/colormaps';

/** The window's pool of user-created colors — plain data, the twin of
 * `CustomColors` in the Qt picker. */
export interface CustomColors {
  colors: string[];
  add: (hex: string) => void;
  remove: (hex: string) => void;
}

const SWATCH = 18;
const PER_ROW = 8;

function Swatch({
  value,
  selected,
  name,
  onPick,
  onHover,
}: {
  value: string;
  selected: boolean;
  name: string;
  onPick: () => void;
  onHover: (name: string | null) => void;
}) {
  return (
    <button
      type="button"
      className={selected ? 'cp-swatch selected' : 'cp-swatch'}
      style={{ background: colorValueHex(value) }}
      onClick={onPick}
      onPointerEnter={() => onHover(name)}
      onPointerLeave={() => onHover(null)}
    />
  );
}

function Section({ title }: { title: string }) {
  return (
    <div className="cp-section">
      <span>{title}</span>
      <hr />
    </div>
  );
}

function Grid({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="cp-grid"
      style={{ gridTemplateColumns: `repeat(${PER_ROW}, ${SWATCH}px)` }}
    >
      {children}
    </div>
  );
}

/** Saturation left-to-right, value top-to-bottom, for the current hue. */
function ShadeSquare({
  hue,
  s,
  v,
  onChange,
}: {
  hue: number;
  s: number;
  v: number;
  onChange: (s: number, v: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const pick = (e: React.PointerEvent) => {
    if (e.buttons === 0 && e.type === 'pointermove') return;
    const r = ref.current!.getBoundingClientRect();
    onChange(
      Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
      1 - Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1),
    );
  };
  return (
    <div
      ref={ref}
      className="cp-shade"
      style={{ background: `linear-gradient(to right, #fff, ${hsvToHex(hue, 1, 1)})` }}
      onPointerDown={pick}
      onPointerMove={pick}
    >
      <div className="cp-shade-dark" />
      <div className="cp-dot" style={{ left: `${s * 100}%`, top: `${(1 - v) * 100}%` }} />
    </div>
  );
}

function HueStrip({ hue, onChange }: { hue: number; onChange: (h: number) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const pick = (e: React.PointerEvent) => {
    if (e.buttons === 0 && e.type === 'pointermove') return;
    const r = ref.current!.getBoundingClientRect();
    onChange(Math.min(Math.max((e.clientX - r.left) / r.width, 0), 0.999));
  };
  return (
    <div ref={ref} className="cp-hue" onPointerDown={pick} onPointerMove={pick}>
      <div className="cp-hue-mark" style={{ left: `${hue * 100}%` }} />
    </div>
  );
}

export function ColorSwatch({
  value,
  onChange,
  title,
  custom,
}: {
  /** Any colormap value; displayed as the color it stands for. */
  value: string;
  onChange: (value: string) => void;
  title?: string;
  custom: CustomColors;
}) {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState<string | null>(null);
  const [picker, setPicker] = useState(false);
  const [hsv, setHsv] = useState<[number, number, number]>([0, 1, 1]);
  const [hex, setHex] = useState('#ffffff');
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) {
        setOpen(false);
        setPicker(false);
      }
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        setPicker(false);
      }
    };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', key);
    return () => {
      window.removeEventListener('pointerdown', close);
      window.removeEventListener('keydown', key);
    };
  }, [open]);

  const setFromHsv = (h: number, s: number, v: number) => {
    setHsv([h, s, v]);
    setHex(hsvToHex(h, s, v));
  };

  const togglePicker = () => {
    if (picker) {
      setPicker(false);
      return;
    }
    const start = colorValueHex(value);
    setHsv(hexToHsv(start));
    setHex(start);
    setPicker(true);
  };

  const addPicked = () => {
    const chosen = /^#[0-9a-f]{6}$/i.test(hex.trim())
      ? hex.trim().toLowerCase()
      : hsvToHex(...hsv);
    custom.add(chosen);
    onChange(chosen);
    setPicker(false);
  };

  const sections: [string, [string, string][]][] = [
    ['Preset', COLOR_PRESETS],
    ['Custom', custom.colors.map((c) => [c, c] as [string, string])],
  ];

  return (
    <div className="dd" ref={ref}>
      <button
        type="button"
        className="swatch"
        style={{ background: colorValueHex(value) }}
        title={title ?? colorValueHex(value)}
        onClick={() => setOpen(!open)}
      />
      {open && (
        <div className="cp-pop">
          {sections.map(([label, entries]) => (
            <div key={label}>
              <Section title={label} />
              <Grid>
                {entries.map(([name, v]) => (
                  <Swatch
                    key={`${label}-${v}`}
                    value={v}
                    name={name}
                    selected={v === value}
                    onPick={() => onChange(v)}
                    onHover={setHover}
                  />
                ))}
              </Grid>
            </div>
          ))}
          <div className="cp-buttons">
            <button
              type="button"
              className="cp-swatch cp-text"
              disabled={!custom.colors.includes(value)}
              onClick={() => custom.remove(value)}
              onPointerEnter={() => setHover('Delete color')}
              onPointerLeave={() => setHover(null)}
            >
              −
            </button>
            <button
              type="button"
              className="cp-swatch cp-text"
              onClick={togglePicker}
              onPointerEnter={() => setHover('New color…')}
              onPointerLeave={() => setHover(null)}
            >
              +
            </button>
          </div>
          {picker && (
            <div className="cp-picker">
              <ShadeSquare
                hue={hsv[0]}
                s={hsv[1]}
                v={hsv[2]}
                onChange={(s, v) => setFromHsv(hsv[0], s, v)}
              />
              <HueStrip hue={hsv[0]} onChange={(h) => setFromHsv(h, hsv[1], hsv[2])} />
              <div className="cp-hex-row">
                <input
                  className="cp-hex"
                  value={hex}
                  spellCheck={false}
                  onChange={(e) => {
                    setHex(e.target.value);
                    if (/^#[0-9a-f]{6}$/i.test(e.target.value.trim())) {
                      setHsv(hexToHsv(e.target.value.trim()));
                    }
                  }}
                />
                <button type="button" className="cp-add" onClick={addPicked}>
                  Add
                </button>
              </div>
            </div>
          )}
          <div className="cp-hover">{hover ?? ' '}</div>
        </div>
      )}
    </div>
  );
}
