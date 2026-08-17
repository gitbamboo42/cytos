/**
 * The layers dock: one collapsible section per layer kind, one row per
 * layer. It edits a SlideSettings object (the saved-session vocabulary, see
 * core/session.ts) and nothing else.
 */

import type { FeatureTable } from '../io/features';
import type { LoadedSlide } from '../io/slide';
import {
  imageKey,
  segmentsKey,
  type ImageSettings,
  type SectionSettings,
  type SegmentSettings,
  type SlideSettings,
} from '../core/session';
import type { CustomColors } from './color-picker';
import { Section, styles } from './controls';
import { ImageRow, SegmentRow } from './rows';

interface PanelProps {
  slide: LoadedSlide;
  settings: SlideSettings;
  features: Record<string, FeatureTable | null>;
  /** The window's shared pool of user-created colors — one pool for every
   * picker, as in the Qt viewer. */
  custom: CustomColors;
  onChange: (key: string, patch: Partial<ImageSettings & SegmentSettings>) => void;
  onSectionChange: (name: string, patch: Partial<SectionSettings>) => void;
}

export function Panel({ slide, settings, features, custom, onChange, onSectionChange }: PanelProps) {
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
    </div>
  );
}
