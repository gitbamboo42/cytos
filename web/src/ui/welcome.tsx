/**
 * What a window with no slide shows: what the program is, how to open one,
 * and the slides you opened before.
 *
 * The Qt welcome window's rule holds — the File menu is where you act, so
 * its commands are named rather than repeated as buttons. Recents are the
 * exception, because a list you can only read is a list you have to retype:
 * they are also in File ▸ Open Recent, but a window standing empty is
 * exactly where you want them under the cursor.
 */

import './panel.css';
import type { RecentSlide } from '../io/recents';

const styles = {
  page: {
    position: 'absolute',
    inset: 0,
    background: '#141414',
    color: '#e8e8e8',
    font: '13px system-ui, sans-serif',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 6,
  },
  title: { fontSize: 34, fontWeight: 400 },
  subtitle: { color: '#8a8a8a' },
  // Left-aligned inside a centred block: centring each line on its own would
  // stagger them, so two lines starting with "File" wouldn't line up.
  hint: { color: '#6f6f6f', marginTop: 22, textAlign: 'left', whiteSpace: 'pre-line' },
  recents: { marginTop: 28, display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 2, minWidth: 240 },
  recentsLabel: { color: '#6f6f6f', marginBottom: 4 },
  recent: {
    background: 'none',
    border: 'none',
    borderRadius: 3,
    color: '#b9b9b9',
    font: 'inherit',
    textAlign: 'left',
    padding: '3px 6px',
    cursor: 'pointer',
  },
} as const;

export function Welcome({
  desktop,
  recents,
  onOpen,
}: {
  desktop: boolean;
  /** Slides opened before, most recent first. */
  recents: RecentSlide[];
  onOpen: (slide: string) => void;
}) {
  return (
    <div style={styles.page}>
      <div style={styles.title}>cytos</div>
      <div style={styles.subtitle}>A fast viewer for spatial biology slides</div>
      <div style={styles.hint}>
        {desktop
          ? 'File ▸ Open Slide…   ⌘O'
          : 'Name a slide in the address bar:\n?slide=https://…/sample.cytos'}
      </div>
      {recents.length > 0 && (
        <div style={styles.recents}>
          <div style={styles.recentsLabel}>Recent</div>
          {recents.map((slide) => (
            <button
              key={slide.id}
              type="button"
              className="welcome-recent"
              style={styles.recent}
              // The whole path, for two slides of the same name in different
              // folders — which is what a re-import leaves you with.
              title={slide.id}
              onClick={() => onOpen(slide.id)}
            >
              {slide.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
