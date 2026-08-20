/**
 * The picker shown on the way into a slide: which saved view do you want
 * back, or start a new one.
 *
 * A window is bound to exactly one session (see `core/session.ts`), so this
 * is not an optional extra step — it is how that binding is chosen, and it
 * is the *only* place sessions are managed. There is deliberately no
 * switcher in the layers panel: one window, one session, and swapping the
 * file under a window mid-look is the thing that rule exists to prevent.
 * The Qt viewer's `ui/session_picker.py` is the same dialog, down to the
 * greyed-out rows.
 *
 * The picture beside each name is the actual frame from when the session was
 * last written — `<slide>/sessions/<slug>.png`, the file both viewers write
 * — which is what makes "the tumour edge one" recognisable without opening
 * it. A session saved before thumbnails existed, or by something that takes
 * none, gets the bare card and still lines up.
 */

import { useEffect, useRef, useState } from 'react';

import { uniqueSessionName } from '../core/session';
import type { SessionInfo } from '../io/sessions';
import './panel.css';

/** Card size, in CSS pixels. Frames come in whatever shape the window was,
 * so each is fitted inside a card of exactly this size rather than sizing
 * the row — otherwise a wide frame comes back short and a tall one narrow,
 * and no two names down the list start at the same x. Qt pads its own the
 * same way, and for the same reason. */
const THUMB = { width: 128, height: 96 };

const styles = {
  page: {
    position: 'absolute',
    inset: 0,
    background: 'var(--page)',
    color: 'var(--text)',
    font: '13px system-ui, sans-serif',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    padding: '48px 24px 24px',
  },
  title: { fontSize: 20, fontWeight: 400 },
  slide: { color: 'var(--text-muted)', marginTop: 4 },
  list: {
    marginTop: 24,
    width: 460,
    maxHeight: '55vh',
    overflowY: 'auto',
    border: '1px solid var(--border-subtle)',
    borderRadius: 4,
    background: 'var(--card)',
  },
  card: {
    width: THUMB.width,
    height: THUMB.height,
    flex: `0 0 ${THUMB.width}px`,
    // Dark grey reads as empty padding beside a rendered frame, not as part
    // of it. Same colour Qt fills its card with.
    background: '#262626',
    borderRadius: 2,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  },
  thumb: { maxWidth: '100%', maxHeight: '100%', display: 'block' },
  name: { color: 'var(--text)' },
  when: { color: 'var(--text-faint)', marginTop: 3 },
  note: { color: 'var(--text-faint)', marginTop: 3 },
  buttons: { marginTop: 20, width: 460, display: 'flex', alignItems: 'center', gap: 8 },
  button: { padding: '4px 12px', cursor: 'pointer' },
  input: {
    flex: 1,
    minWidth: 0,
    background: 'var(--input)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
    borderRadius: 3,
    padding: '4px 8px',
    font: 'inherit',
  },
  empty: { color: 'var(--text-faint)', padding: '24px 12px', textAlign: 'center' },
} as const;

function whenText(modified: number | null): string {
  if (!modified) return '';
  const d = new Date(modified);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function SessionPicker({
  slideName,
  sessions,
  inUse,
  loadShot,
  onOpen,
  onCreate,
  onDelete,
  onCancel,
}: {
  slideName: string;
  /** Most recently written first — the order the store lists them in. */
  sessions: SessionInfo[];
  /** Names another window on this slide already holds. Listed, but not
   * selectable: showing them explains the one-window rule better than
   * hiding them would. */
  inUse: string[];
  loadShot: (name: string) => Promise<Blob | null>;
  onOpen: (name: string) => void;
  /** Create a session holding nothing but the slide's defaults, and stay
   * here. Setting up three or four named views before looking at any of
   * them is a normal way to start, and that only works if creating one is
   * complete in itself. */
  onCreate: (name: string) => void;
  onDelete: (name: string) => void;
  /** Chose nothing: back to the welcome screen, no window bound to a
   * session. Qt's Cancel does not open the slide either. */
  onCancel: () => void;
}) {
  const busy = (name: string) => inUse.some((n) => n.toLowerCase() === name.toLowerCase());
  const free = sessions.filter((s) => !busy(s.name));
  const [chosen, setChosen] = useState<string | null>(free[0]?.name ?? null);
  // "list" | naming a new one | about to delete one — a row of controls that
  // changes, rather than dialogs, because Electron has no window.prompt at
  // all and a confirm() box is heavier than the act it guards.
  const [mode, setMode] = useState<'list' | 'new' | 'delete'>('list');
  const [draft, setDraft] = useState('');
  /** name -> object URL of its thumbnail. */
  const [shots, setShots] = useState<Record<string, string>>({});
  const page = useRef<HTMLDivElement>(null);

  // Enter opens and Escape cancels, which needs the keys to land somewhere:
  // the page itself takes focus, but only when the row of controls is the
  // plain list. Focusing on every render instead would pull the caret
  // straight back out of the name box the moment New Session was pressed.
  useEffect(() => {
    if (mode === 'list') page.current?.focus();
  }, [mode]);

  // Keep the selection on something openable as the list changes underneath
  // (a session created, deleted, or claimed by another window).
  useEffect(() => {
    if (chosen && sessions.some((s) => s.name === chosen) && !busy(chosen)) return;
    setChosen(free[0]?.name ?? null);
  }, [sessions, inUse]);

  // Thumbnails, one read each, and every URL handed back when the picker
  // goes — an object URL holds its blob alive until it is revoked.
  useEffect(() => {
    let cancelled = false;
    const mine: string[] = [];
    for (const session of sessions) {
      if (shots[session.name]) continue;
      loadShot(session.name)
        .then((blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob);
          if (cancelled) return URL.revokeObjectURL(url);
          mine.push(url);
          setShots((prev) => ({ ...prev, [session.name]: url }));
        })
        .catch((err) => console.warn(`no thumbnail for "${session.name}":`, err));
    }
    return () => {
      cancelled = true;
      mine.forEach(URL.revokeObjectURL);
    };
  }, [sessions]);

  const startNew = () => {
    setDraft(uniqueSessionName(sessions.map((s) => s.name)));
    setMode('new');
  };
  const commitNew = () => {
    const name = draft.trim();
    setMode('list');
    if (!name) return;
    onCreate(name);
    setChosen(name);
  };

  return (
    <div
      style={styles.page}
      tabIndex={-1}
      ref={page}
      onKeyDown={(e) => {
        if (mode !== 'list') return;
        if (e.key === 'Escape') onCancel();
        if (e.key === 'Enter' && chosen) onOpen(chosen);
      }}
    >
      <div style={styles.title}>Open session</div>
      <div style={styles.slide}>{slideName}</div>

      <div style={styles.list}>
        {sessions.length === 0 && <div style={styles.empty}>no saved views of this slide yet</div>}
        {sessions.map((session) => {
          const open = busy(session.name);
          return (
            <div
              key={session.name}
              className={
                open
                  ? 'session-row off'
                  : session.name === chosen
                    ? 'session-row selected'
                    : 'session-row'
              }
              onClick={() => !open && setChosen(session.name)}
              onDoubleClick={() => !open && onOpen(session.name)}
            >
              <div style={styles.card}>
                {shots[session.name] && (
                  <img style={styles.thumb} src={shots[session.name]} alt="" />
                )}
              </div>
              <div>
                <div style={styles.name}>{session.name}</div>
                <div style={styles.when}>{whenText(session.modified)}</div>
                {open && <div style={styles.note}>open in another window</div>}
              </div>
            </div>
          );
        })}
      </div>

      <div style={styles.buttons}>
        {mode === 'new' && (
          <>
            <input
              autoFocus
              style={styles.input}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitNew();
                if (e.key === 'Escape') setMode('list');
              }}
            />
            <button type="button" className="control" style={styles.button} onClick={commitNew}>
              Create
            </button>
            <button type="button" className="control" style={styles.button} onClick={() => setMode('list')}>
              Cancel
            </button>
          </>
        )}
        {mode === 'delete' && chosen && (
          <>
            <span style={{ flex: 1, minWidth: 0 }}>
              Delete “{chosen}”? The slide's data and defaults are untouched.
            </span>
            <button
              type="button"
              className="control" style={styles.button}
              onClick={() => {
                setMode('list');
                onDelete(chosen);
              }}
            >
              Delete
            </button>
            <button type="button" className="control" style={styles.button} onClick={() => setMode('list')}>
              Keep
            </button>
          </>
        )}
        {mode === 'list' && (
          <>
            <button type="button" className="control" style={styles.button} onClick={startNew}>
              New Session
            </button>
            <button
              type="button"
              className="control" style={styles.button}
              disabled={!chosen}
              onClick={() => setMode('delete')}
            >
              Delete
            </button>
            <span style={{ flex: 1 }} />
            <button type="button" className="control" style={styles.button} onClick={onCancel}>
              Cancel
            </button>
            <button
              type="button"
              className="control" style={styles.button}
              disabled={!chosen}
              onClick={() => chosen && onOpen(chosen)}
            >
              Open
            </button>
          </>
        )}
      </div>
    </div>
  );
}
