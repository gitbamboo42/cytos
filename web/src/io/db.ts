/**
 * The browser's own store, shared by everything in a tab that has to
 * remember something between visits — sessions, and which slides were opened.
 *
 * IndexedDB rather than localStorage: localStorage is synchronous, capped at
 * a few MB, and a gene selection of a few thousand ids is already a real
 * fraction of that. Cookies would be worse still — they ride along with
 * every request to the origin and are cleared for reasons that have nothing
 * to do with this app. One database, one version, one place to add a store.
 */

const DB_NAME = 'cytos';
const DB_VERSION = 2;

export const SESSIONS = 'sessions';
export const RECENTS = 'recents';

export function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      // Every version's stores, created if absent: an upgrade from any
      // earlier version lands here, and a store that already exists is left
      // exactly as it is.
      if (!db.objectStoreNames.contains(SESSIONS)) {
        db.createObjectStore(SESSIONS, { keyPath: 'key' }).createIndex('slide', 'slide');
      }
      if (!db.objectStoreNames.contains(RECENTS)) {
        db.createObjectStore(RECENTS, { keyPath: 'slide' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** An IndexedDB request as a promise. */
export function done<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
