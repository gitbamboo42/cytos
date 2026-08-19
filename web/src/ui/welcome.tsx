/**
 * What a window with no slide shows.
 *
 * What the program is, and nothing else — the same call the Qt viewer's
 * welcome window makes (`src/cytos/ui/main_window.py`). The File menu is
 * where you act, so repeating its commands as buttons would put the same
 * thing on screen twice. A browser tab has no File menu, so there it says
 * the one thing that is true there instead: name the slide in the URL.
 */

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
} as const;

export function Welcome({ desktop }: { desktop: boolean }) {
  return (
    <div style={styles.page}>
      <div style={styles.title}>cytos</div>
      <div style={styles.subtitle}>A fast viewer for spatial biology slides</div>
      <div style={styles.hint}>
        {desktop
          ? 'File ▸ Open Slide…   ⌘O'
          : 'Name a slide in the address bar:\n?slide=https://…/sample.cytos'}
      </div>
    </div>
  );
}
