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
}

/**
 * Sessions in the browser's IndexedDB, one record per (slide, session).
 *
 * localStorage would have been shorter, but it is synchronous, shared with
 * everything else on the origin and capped at a few MB — a gene selection of
 * a few thousand ids is already a real fraction of that. IndexedDB is
 * per-origin too, so a session does not follow you to another machine; that
 * is what export and, one day, a server store are for.
 */
const DB_NAME = 'cytos';
const DB_VERSION = 1;
const STORE = 'sessions';

interface SessionRecord {
  key: string; // slide + "\n" + slug — the primary key
  slide: string;
  name: string;
  modified: number;
  doc: SavedSession;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'key' }).createIndex('slide', 'slide');
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function done<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

class BrowserSessionStore implements SessionStore {
  constructor(private slide: string) {}

  private key(name: string): string {
    return `${this.slide}\n${slugify(name)}`;
  }

  private async records(): Promise<SessionRecord[]> {
    const db = await openDb();
    const index = db.transaction(STORE, 'readonly').objectStore(STORE).index('slide');
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
      db.transaction(STORE, 'readonly').objectStore(STORE).get(this.key(name)) as
        IDBRequest<SessionRecord | undefined>,
    );
    if (!record) return null;
    // Stored as an object, not text, so there is nothing to parse — but it
    // still goes through the same version check.
    return readable(JSON.stringify(record.doc), `session "${name}"`);
  }

  async save(name: string, doc: SavedSession): Promise<void> {
    const db = await openDb();
    const record: SessionRecord = {
      key: this.key(name),
      slide: this.slide,
      name,
      modified: Date.now(),
      doc,
    };
    const tx = db.transaction(STORE, 'readwrite');
    await done(tx.objectStore(STORE).put(record));
  }

  async remove(name: string): Promise<void> {
    const db = await openDb();
    const tx = db.transaction(STORE, 'readwrite');
    await done(tx.objectStore(STORE).delete(this.key(name)));
  }
}

/** The store this build can use: files on disk in the desktop shell, the
 * browser's own database in a tab. */
export function sessionStoreFor(slide: string): SessionStore {
  return desktopHost() ? new FileSessionStore(slide) : new BrowserSessionStore(slide);
}
