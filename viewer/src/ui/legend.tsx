/**
 * The legend for a categorical feature — a clustering, not a measurement.
 *
 * It is a legend and a control at once, exactly as in Qt's
 * `ui/segment_panel.py`: the swatch says what colour a category is drawn in
 * and opens the colour picker, the checkbox shows or hides those cells, and
 * the count says how many there are. Its own file for the same reason
 * `gene-picker.tsx` is one — a scrolling list of coloured, checkable rows is
 * a widget, not a row of a panel.
 *
 * Everything it edits lives in the *session*, never in the slide: the slide
 * records what the cells are, the session records how you like them shown.
 */

import { categoryColor, rgbToHex, UNASSIGNED_COLOR } from '../core/colormaps';
import type { SegmentSettings } from '../core/session';
import { UNASSIGNED_KEY, type Feature } from '../io/features';
import { ColorSwatch, type CustomColors } from './color-picker';
import { styles } from './controls';

/** The colour a category takes before anyone pins one — straight from its
 * number, the same rule the renderer follows, which is what keeps the swatch
 * in the list and the cells on the slide the same colour. */
function presetColor(key: string, palette: string): string {
  if (key === UNASSIGNED_KEY) return rgbToHex(UNASSIGNED_COLOR);
  return rgbToHex(categoryColor(Number(key), palette));
}

export function CategoryLegend({
  feature,
  settings,
  custom,
  onChange,
}: {
  feature: Feature;
  settings: SegmentSettings;
  custom: CustomColors;
  onChange: (patch: Partial<SegmentSettings>) => void;
}) {
  const colors = settings.category_colors[feature.name] ?? {};
  const hidden = settings.hidden_categories[feature.name] ?? [];
  const shown = feature.categories.length - hidden.length;

  const setHidden = (keys: string[]) =>
    onChange({
      hidden_categories: { ...settings.hidden_categories, [feature.name]: keys },
    });

  const toggle = (key: string) =>
    setHidden(hidden.includes(key) ? hidden.filter((k) => k !== key) : [...hidden, key]);

  const setColor = (key: string, hex: string) =>
    onChange({
      category_colors: {
        ...settings.category_colors,
        [feature.name]: { ...colors, [key]: hex },
      },
    });

  return (
    <div style={{ ...styles.line, alignItems: 'flex-start' }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={legendStyles.list}>
          <div style={{ ...legendStyles.item, color: '#aaa' }}>
            <input
              type="checkbox"
              checked={hidden.length === 0}
              // Half the categories off reads as neither on nor off, and the
              // box says so rather than guessing.
              ref={(box) => {
                if (box) box.indeterminate = shown > 0 && hidden.length > 0;
              }}
              onChange={() =>
                setHidden(hidden.length ? [] : feature.categories.map((c) => c.key))
              }
            />
            <span style={{ flex: 1, minWidth: 0 }}>{feature.name}</span>
            <span style={{ color: '#777' }}>{feature.categories.length}</span>
          </div>
          {feature.categories.map((category) => (
            <div key={category.key} style={{ ...legendStyles.item, paddingLeft: 16 }}>
              <input
                type="checkbox"
                checked={!hidden.includes(category.key)}
                onChange={() => toggle(category.key)}
              />
              <ColorSwatch
                value={colors[category.key] ?? presetColor(category.key, settings.palette)}
                title={`${category.key} color`}
                custom={custom}
                onChange={(hex) => setColor(category.key, hex)}
              />
              <span style={{ flex: 1, minWidth: 0 }}>{category.key}</span>
              <span style={{ color: '#777' }}>{category.count.toLocaleString()}</span>
            </div>
          ))}
        </div>
        <div style={legendStyles.footer}>
          {shown} of {feature.categories.length} shown
        </div>
      </div>
    </div>
  );
}

const legendStyles = {
  list: {
    maxHeight: 180,
    overflowY: 'auto' as const,
    border: '1px solid #26262a',
    borderRadius: 3,
  },
  item: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    padding: '1px 4px',
  },
  footer: { color: '#777', marginTop: 3 },
};
