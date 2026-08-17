/**
 * Colormaps for per-cell coloring: matplotlib's perceptually-uniform ramps
 * (sampled at 16 stops, linearly interpolated — visually indistinguishable
 * from the full tables at outline/fill scale) and the tab palettes for
 * categorical features. Same names the Qt viewer offers, so a session's
 * `colormap`/`palette` fields mean the same thing in both viewers.
 */

type RGB = [number, number, number];

// 16 evenly spaced stops from each matplotlib ramp.
const RAMPS: Record<string, RGB[]> = {
  viridis: [
    [68, 1, 84], [72, 26, 108], [71, 47, 125], [65, 68, 135], [57, 86, 140],
    [49, 104, 142], [42, 120, 142], [35, 136, 142], [31, 152, 139], [34, 168, 132],
    [53, 183, 121], [84, 197, 104], [122, 209, 81], [165, 219, 54], [210, 226, 27],
    [253, 231, 37],
  ],
  magma: [
    [0, 0, 4], [10, 7, 34], [28, 16, 68], [53, 15, 106], [80, 18, 123],
    [105, 28, 128], [131, 38, 129], [157, 47, 127], [184, 55, 121], [210, 66, 109],
    [232, 83, 98], [246, 110, 92], [252, 140, 99], [254, 172, 118], [254, 202, 141],
    [252, 253, 191],
  ],
  plasma: [
    [13, 8, 135], [51, 5, 151], [80, 2, 162], [106, 0, 168], [132, 5, 167],
    [156, 23, 158], [177, 42, 144], [195, 61, 128], [211, 81, 113], [225, 100, 98],
    [237, 121, 83], [246, 143, 68], [252, 166, 54], [254, 192, 41], [249, 220, 36],
    [240, 249, 33],
  ],
  inferno: [
    [0, 0, 4], [9, 6, 32], [25, 12, 64], [50, 10, 94], [74, 12, 107],
    [97, 19, 110], [120, 28, 109], [143, 37, 104], [166, 46, 95], [188, 58, 84],
    [208, 73, 69], [225, 92, 52], [238, 114, 33], [247, 140, 12], [251, 168, 13],
    [252, 255, 164],
  ],
  gray: [
    [0, 0, 0], [255, 255, 255],
  ],
};

export const RAMP_NAMES = Object.keys(RAMPS);

/** matplotlib tab20, in its interleaved dark/light order. */
export const TAB20: RGB[] = [
  [31, 119, 180], [174, 199, 232], [255, 127, 14], [255, 187, 120],
  [44, 160, 44], [152, 223, 138], [214, 39, 40], [255, 152, 150],
  [148, 103, 189], [197, 176, 213], [140, 86, 75], [196, 156, 148],
  [227, 119, 194], [247, 182, 210], [127, 127, 127], [199, 199, 199],
  [188, 189, 34], [219, 219, 141], [23, 190, 207], [158, 218, 229],
];

/** Cells with no value for the feature ("unassigned") draw dim, not absent. */
export const UNASSIGNED_COLOR: RGB = [80, 80, 80];

/** The balanced hues behind the manifest's named channel colors — same
 * values as `_COMPOSITE_HUES` in `src/cytos/render/image.py` (pure #00ff00
 * green glows ~2.5x brighter than blue and dominates every overlay). */
export const COMPOSITE_HUES: Record<string, string> = {
  blue: '#0f73e6',
  green: '#008a00',
  red: '#f00219',
  cyan: '#00a5a5',
  magenta: '#f300a5',
  yellow: '#a4a400',
};

/** The color-picker presets, same as `CHANNEL_COLOR_PRESETS` in
 * `src/cytos/render/image.py`. */
export const COLOR_PRESETS: [string, string][] = [
  ['Blue', '#0f73e6'],
  ['Green', '#008a00'],
  ['Yellow', '#a4a400'],
  ['Orange', '#f26500'],
  ['Red', '#f00219'],
  ['Magenta', '#f300a5'],
  ['Cyan', '#00a5a5'],
  ['White', '#ffffff'],
];

/** The single hex color a channel's `colormap` value stands for — a
 * "#rrggbb" as itself, a hue name as its hue, a ramp as its top color.
 * Mirrors `channel_color_hex` in `src/cytos/render/image.py`. */
export function colorValueHex(value: string): string {
  if (value.startsWith('#')) return value.toLowerCase();
  const hue = COMPOSITE_HUES[value];
  if (hue) return hue;
  return rgbToHex(rampColor(value, 1));
}

export function colorValueRgb(value: string): RGB {
  return hexToRgb(colorValueHex(value)) ?? [255, 255, 255];
}

export function hexToRgb(hex: string): RGB | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return null;
  const v = parseInt(m[1], 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

export function rgbToHex([r, g, b]: RGB): string {
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

/** Sample a ramp at t in [0, 1]. */
export function rampColor(name: string, t: number): RGB {
  const stops = RAMPS[name] ?? RAMPS.viridis;
  const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
  const i = Math.min(Math.floor(x), stops.length - 2);
  const f = x - i;
  const a = stops[i];
  const b = stops[i + 1];
  return [
    Math.round(a[0] + f * (b[0] - a[0])),
    Math.round(a[1] + f * (b[1] - a[1])),
    Math.round(a[2] + f * (b[2] - a[2])),
  ];
}

/** A 256-entry RGBA lookup table for a ramp — index by quantized value. */
export function rampLut(name: string, alpha = 255): Uint8Array {
  const lut = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const [r, g, b] = rampColor(name, i / 255);
    lut[i * 4] = r;
    lut[i * 4 + 1] = g;
    lut[i * 4 + 2] = b;
    lut[i * 4 + 3] = alpha;
  }
  return lut;
}
