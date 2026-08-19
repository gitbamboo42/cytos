/**
 * Where a session is kept.
 *
 * A slide is written once and only ever read — that is what lets it be
 * served as static bytes to as many viewers as you like. A session is the
 * opposite: personal, mutable, one per window. So it is never written to the
 * slide server, and this file is the one seam that decides where it does go:
 * files inside the slide folder in the desktop shell (the same
 * `sessions/<slug>.json` the Qt viewer reads and writes), the browser's own
 * IndexedDB in a tab.
 *
 * Both answer the same four questions, so `App.tsx` never learns which it
 * got — the same trick `ReadRange` plays for slide bytes. A third backend (a
 * server, behind an identity) would land here and nowhere else.
 */

import { slugify, type SavedSession, SESSION_FORMAT } from '../core/session';
import { done, openDb, SESSIONS } from './db';
import { desktopHost } from './host';

export interface SessionInfo {
  name: string;
  /** Epoch ms of the last write, or null when the store doesn't know. */
  modified: number | null;
}

export interface SessionStore {
  /** Every session for this slide, most recently written first. */
  list(): Promise<SessionInfo[]>;
  load(name: string): Promise<SavedSession | null>;
  save(name: string, doc: SavedSession): Promise<void>;
  remove(name: string): Promise<void>;
  /** The picker's thumbnail — the rendered frame from when the session was
   * last written, which is what makes "the tumour edge one" recognisable
   * without opening it. Null when the session has never been saved from a
   * viewer that takes one. */
  loadShot(name: string): Promise<Blob | null>;
  saveShot(name: string, shot: Blob): Promise<void>;
}

/** A session from a newer cytos is ignored, not guessed at — the same rule
 * `load_session` follows in Python. A broken session must never be the
 * reason a slide won't open, so every failure here is a console line and a
 * null, never a throw. */
function readable(text: string, where: string): SavedSession | null {
  try {
    const doc = JSON.parse(text) as SavedSession;
    if (!doc || typeof doc !== 'object') return null;
    if (Number(doc.cytos_session ?? 0) > SESSION_FORMAT) {
      console.warn(`${where}: ignoring session written by a newer cytos`);
      return null;
    }
    return doc;
  } catch (err) {
    console.warn(`${where}: ignoring unreadable session (${err})`);
    return null;
  }
}

/** Sessions as files in `<slide>/sessions/`, which is where the Qt viewer
 * keeps them — so the two viewers open each other's. */
class FileSessionStore implements SessionStore {
  constructor(private slideDir: string) {}

  private get host() {
    const host = desktopHost();
    if (!host) throw new Error('FileSessionStore needs the desktop shell');
    return host;
  }

  async list(): Promise<SessionInfo[]> {
    const found = await this.host.listSessions(this.slideDir);
    return found
      .map((f) => ({ name: f.name, modified: f.modified }))
      .sort((a, b) => (b.modified ?? 0) - (a.modified ?? 0));
  }

  async load(name: string): Promise<SavedSession | null> {
    const text = await this.host.readSession(this.slideDir, slugify(name));
    return text === null || text === undefined ? null : readable(text, `session "${name}"`);
  }

  async save(name: string, doc: SavedSession): Promise<void> {
    await this.host.writeSession(
      this.slideDir,
      slugify(name),
      JSON.stringify(doc, null, 2) + '\n',
    );
  }

  async remove(name: string): Promise<void> {
    await this.host.deleteSession(this.slideDir, slugify(name));
  }

  async loadShot(name: string): Promise<Blob | null> {
    const bytes = await this.host.readSessionShot(this.slideDir, slugify(name));
    // A Uint8Array over IPC, not a Blob: the bridge carries plain data, and
    // the one place that turns it back into an image is here.
    return bytes ? new Blob([bytes as BlobPart], { type: 'image/png' }) : null;
  }

  async saveShot(name: string, shot: Blob): Promise<void> {
    const bytes = new Uint8Array(await shot.arrayBuffer());
    await this.host.writeSessionShot(this.slideDir, slugify(name), bytes);
  }
}

/**
 * Sessions in the browser's IndexedDB, one record per (slide, session).
 * Per-origin, so a session does not follow you to another machine; that is
 * what export and, one day, a server store are for. The database itself is
 * `io/db.ts`, shared with the recent-slides list.
 */
interface SessionRecord {
  key: string; // slide + "\n" + slug — the primary key
  slide: string;
  name: string;
  modified: number;
  doc: SavedSession;
  /** The picker's thumbnail, as a PNG. IndexedDB stores a Blob as it is, so
   * this is the same bytes the shell writes to `<slug>.png` — no base64
   * detour, which would cost a third more space for nothing. */
  shot?: Blob;
}

class BrowserSessionStore implements SessionStore {
  constructor(private slide: string) {}

  private key(name: string): string {
    return `${this.slide}\n${slugify(name)}`;
  }

  private async records(): Promise<SessionRecord[]> {
    const db = await openDb();
    const index = db.transaction(SESSIONS, 'readonly').objectStore(SESSIONS).index('slide');
    return done(index.getAll(IDBKeyRange.only(this.slide)) as IDBRequest<SessionRecord[]>);
  }

  async list(): Promise<SessionInfo[]> {
    const records = await this.records();
    return records
      .map((r) => ({ name: r.name, modified: r.modified }))
      .sort((a, b) => b.modified - a.modified);
  }

  async load(name: string): Promise<SavedSession | null> {
    const db = await openDb();
    const record = await done(
      db.transaction(SESSIONS, 'readonly').objectStore(SESSIONS).get(this.key(name)) as
        IDBRequest<SessionRecord | undefined>,
    );
    if (!record) return null;
    // Stored as an object, not text, so there is nothing to parse — but it
    // still goes through the same version check.
    return readable(JSON.stringify(record.doc), `session "${name}"`);
  }

  async save(name: string, doc: SavedSession): Promise<void> {
    // Read then write inside one transaction, because the record holds the
    // thumbnail too: a plain put would drop the preview every time a slider
    // moved, and the two are written by different callers.
    await this.update(name, (record) => ({ ...record, doc }));
  }

  async remove(name: string): Promise<void> {
    const db = await openDb();
    const tx = db.transaction(SESSIONS, 'readwrite');
    await done(tx.objectStore(SESSIONS).delete(this.key(name)));
  }

  async loadShot(name: string): Promise<Blob | null> {
    const db = await openDb();
    const record = await done(
      db.transaction(SESSIONS, 'readonly').objectStore(SESSIONS).get(this.key(name)) as
        IDBRequest<SessionRecord | undefined>,
    );
    return record?.shot ?? null;
  }

  async saveShot(name: string, shot: Blob): Promise<void> {
    await this.update(name, (record) => ({ ...record, shot }));
  }

  /** One record, changed and written back without a gap another writer
   * could slip into. A session that isn't there yet starts empty. */
  private async update(
    name: string,
    change: (record: SessionRecord) => SessionRecord,
  ): Promise<void> {
    const db = await openDb();
    const tx = db.transaction(SESSIONS, 'readwrite');
    const store = tx.objectStore(SESSIONS);
    const found = await done(store.get(this.key(name)) as IDBRequest<SessionRecord | undefined>);
    const base: SessionRecord = found ?? {
      key: this.key(name),
      slide: this.slide,
      name,
      modified: 0,
      doc: { cytos_session: SESSION_FORMAT, name },
    };
    await done(store.put({ ...change(base), name, modified: Date.now() }));
  }
}

/** The store this build can use: files on disk in the desktop shell, the
 * browser's own database in a tab. */
export function sessionStoreFor(slide: string): SessionStore {
  return desktopHost() ? new FileSessionStore(slide) : new BrowserSessionStore(slide);
}
