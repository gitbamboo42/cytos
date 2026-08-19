/**
 * The layers dock: one collapsible section per layer kind, one row per
 * layer. It edits a SlideSettings object (the saved-session vocabulary, see
 * core/session.ts) and nothing else.
 *
 * Which session those settings belong to is not shown here and cannot be
 * changed here: a window is bound to one session for as long as it is open,
 * and the binding is chosen on the way in (`ui/session-picker.tsx`), exactly
 * as in the Qt viewer.
 */

import type { FeatureTable } from '../io/features';
import type { GeneTable } from '../io/points';
import type { LoadedSlide } from '../io/slide';
import {
  imageKey,
  pointsKey,
  segmentsKey,
  type ImageSettings,
  type PointSettings,
  type SectionSettings,
  type SegmentSettings,
  type SlideSettings,
} from '../core/session';
import type { CameraView } from '../render/scene';
import type { CustomColors } from './color-picker';
import { Section, styles } from './controls';
import { Minimap } from './minimap';
import { ImageRow, PointRow, SegmentRow } from './rows';

interface PanelProps {
  slide: LoadedSlide;
  settings: SlideSettings;
  features: Record<string, FeatureTable | null>;
  /** Per-point-layer gene tables, keyed by points layer key. */
  genes: Record<string, GeneTable | null>;
  /** The window's shared pool of user-created colors — one pool for every
   * picker, as in the Qt viewer. */
  custom: CustomColors;
  onChange: (
    key: string,
    patch: Partial<ImageSettings & SegmentSettings & PointSettings>,
  ) => void;
  onSectionChange: (name: string, patch: Partial<SectionSettings>) => void;
  /** The live camera and the way to move it — the navigator's two wires. */
  camera: React.MutableRefObject<CameraView | null>;
  onRecenter: (x: number, y: number) => void;
}

export function Panel({
  slide,
  settings,
  features,
  genes,
  custom,
  onChange,
  onSectionChange,
  camera,
  onRecenter,
}: PanelProps) {
  const images = slide.channels;
  const segments = slide.segments.map((s) => s.spec);
  const points = slide.points.map((s) => s.spec);

  return (
    <div style={styles.panel}>
      {images.length > 0 && (
        <Minimap
          slide={slide}
          settings={settings}
          camera={camera}
          onRecenter={onRecenter}
        />
      )}
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
              custom={custom}
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
              features={features[segmentsKey(layer.id)] ?? null}
              custom={custom}
              onChange={(patch) => onChange(segmentsKey(layer.id), patch)}
            />
          ))}
        </Section>
      )}
      {points.length > 0 && (
        <Section
          title="Points"
          settings={settings.sections.points}
          onChange={(patch) => onSectionChange('points', patch)}
        >
          {points.map((layer) => (
            <PointRow
              key={layer.id}
              layer={layer}
              settings={settings.layers[pointsKey(layer.id)] as PointSettings}
              genes={genes[pointsKey(layer.id)] ?? null}
              custom={custom}
              onChange={(patch) => onChange(pointsKey(layer.id), patch)}
            />
          ))}
        </Section>
      )}
    </div>
  );
}
