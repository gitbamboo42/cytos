/**
 * The session bar: which saved view you are in, and the three things you do
 * with them — switch, start a new one, throw one away.
 *
 * There is no Save button. A session is written as you work (see `App.tsx`),
 * the way the Qt viewer writes one when its window closes; a viewer that can
 * only look at data has nothing to lose by saving early, and a Save button
 * that is never wrong is a Save button nobody needs.
 */

import { useState } from 'react';

import { uniqueSessionName } from '../core/session';
import type { SessionInfo } from '../io/sessions';
import { Dropdown, styles } from './controls';

export function SessionBar({
  sessions,
  current,
  inUse,
  onSwitch,
  onCreate,
  onDelete,
}: {
  sessions: SessionInfo[];
  current: string;
  /** Sessions another window holds. Shown, greyed, unpickable — a session
   * belongs to one window at a time, and Qt's picker greys them too. */
  inUse: string[];
  onSwitch: (name: string) => void;
  onCreate: (name: string) => void;
  onDelete: (name: string) => void;
}) {
  // "list" | "naming a new one" | "about to delete" — a three-state row
  // rather than dialogs, because Electron has no window.prompt at all and a
  // confirm() box for a two-key undo would be heavier than the act itself.
  const [mode, setMode] = useState<'list' | 'new' | 'delete'>('list');
  const [draft, setDraft] = useState('');

  const names = sessions.map((s) => s.name);
  const options = names.includes(current) ? names : [current, ...names];

  const startNew = () => {
    setDraft(uniqueSessionName(names));
    setMode('new');
  };
  const commitNew = () => {
    const name = draft.trim();
    setMode('list');
    if (name) onCreate(name);
  };

  return (
    <div style={{ ...styles.row, display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ color: '#888' }}>session</span>
      {mode === 'new' && (
        <>
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitNew();
              if (e.key === 'Escape') setMode('list');
            }}
            style={{
              flex: 1,
              minWidth: 0,
              background: '#1c1c20',
              color: '#eee',
              border: '1px solid #444',
              borderRadius: 3,
              padding: '2px 6px',
              font: 'inherit',
            }}
          />
          <button type="button" style={styles.button} onClick={commitNew}>
            add
          </button>
        </>
      )}
      {mode === 'delete' && (
        <>
          <span style={{ flex: 1, minWidth: 0, color: '#eee' }}>delete “{current}”?</span>
          <button
            type="button"
            style={styles.button}
            onClick={() => {
              setMode('list');
              onDelete(current);
            }}
          >
            delete
          </button>
          <button type="button" style={styles.button} onClick={() => setMode('list')}>
            keep
          </button>
        </>
      )}
      {mode === 'list' && (
        <>
          <Dropdown
            grow
            value={current}
            options={options}
            disabled={inUse}
            labels={Object.fromEntries(inUse.map((n) => [n, `${n} (open)`]))}
            onChange={onSwitch}
          />
          <button type="button" style={styles.button} title="New session" onClick={startNew}>
            +
          </button>
          <button
            type="button"
            style={styles.button}
            title="Delete this session"
            onClick={() => setMode('delete')}
          >
            −
          </button>
        </>
      )}
    </div>
  );
}
