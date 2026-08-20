/**
 * Which slides were opened, most recent first.
 *
 * The one thing cytos remembers that belongs to no slide — Qt keeps the same
 * list in its own data directory (`src/cytos/ui/recent.py`), and so does the
 * desktop shell, which owns the file because its File ▸ Open Recent menu is
 * built from it. A tab keeps its copy in the shared IndexedDB (`io/db.ts`).
 *
 * Entries that no longer open are *kept*, not dropped: a slide on an
 * unmounted drive is not gone, and forgetting it because the drive was out
 * once would be a surprise. Missing ones are skipped where they are shown —
 * the same rule as Qt's.
 */

import { done, openDb, RECENTS } from './db';
import { desktopHost } from './host';

export interface RecentSlide {
  /** What to reopen: a directory on disk, or a slide URL. */
  id: string;
  /** What to show — the slide's own folder name. */
  name: string;
}

export interface Recents {
  list(): Promise<RecentSlide[]>;
  /** Note that a slide was opened. A no-op in the desktop shell, where the
   * main process already recorded it as it opened the window. */
  remember(slide: string): Promise<void>;
  forget(slide: string): Promise<void>;
  clear(): Promise<void>;
}

/** The last path segment, with any trailing slash gone — "sample.cytos" out
 * of either a directory path or a URL. */
export function slideName(id: string): string {
  const trimmed = id.replace(/[/\\]+$/, '');
  return trimmed.split(/[/\\]/).pop() || trimmed;
}

class HostRecents implements Recents {
  private host = desktopHost()!;

  async list(): Promise<RecentSlide[]> {
    const paths = await this.host.recentSlides().catch(() => [] as string[]);
    return paths.map((id) => ({ id, name: slideName(id) }));
  }

  async remember(): Promise<void> {}

  async forget(slide: string): Promise<void> {
    await this.host.forgetRecent(slide).catch(() => {});
  }

  async clear(): Promise<void> {
    await this.host.clearRecent().catch(() => {});
  }
}

interface RecentRecord {
  slide: string;
  opened: number;
}

const MAX_RECENT = 10;

class BrowserRecents implements Recents {
  async list(): Promise<RecentSlide[]> {
    const db = await openDb();
    const records = await done(
      db.transaction(RECENTS, 'readonly').objectStore(RECENTS).getAll() as
        IDBRequest<RecentRecord[]>,
    );
    return records
      .sort((a, b) => b.opened - a.opened)
      .slice(0, MAX_RECENT)
      .map((r) => ({ id: r.slide, name: slideName(r.slide) }));
  }

  async remember(slide: string): Promise<void> {
    const db = await openDb();
    const store = db.transaction(RECENTS, 'readwrite').objectStore(RECENTS);
    await done(store.put({ slide, opened: Date.now() } satisfies RecentRecord));
    // Trim past the cap here rather than on read, so the store cannot grow
    // without bound in a long-lived browser profile.
    const all = await done(store.getAll() as IDBRequest<RecentRecord[]>);
    for (const old of all.sort((a, b) => b.opened - a.opened).slice(MAX_RECENT)) {
      store.delete(old.slide);
    }
  }

  async forget(slide: string): Promise<void> {
    const db = await openDb();
    await done(db.transaction(RECENTS, 'readwrite').objectStore(RECENTS).delete(slide));
  }

  async clear(): Promise<void> {
    const db = await openDb();
    await done(db.transaction(RECENTS, 'readwrite').objectStore(RECENTS).clear());
  }
}

export function recentSlides(): Recents {
  return desktopHost() ? new HostRecents() : new BrowserRecents();
}
