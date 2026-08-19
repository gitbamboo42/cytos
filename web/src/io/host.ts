/**
 * The desktop shell, as the page sees it.
 *
 * In a browser this is simply absent, and every caller has to cope with that —
 * the viewer runs in a tab with no shell at all, and that is the normal case,
 * not a degraded one. In Electron the object arrives from
 * `electron/preload.cjs`, whose exposed functions are the twin of the
 * interface below. **Change one, change the other** — nothing checks.
 *
 * Keeping it here, in `io/`, is deliberate: the shell's job is to hand over
 * bytes and to say which slide to read. `render/` and `ui/` never touch it.
 */

export interface DesktopHost {
  /** Read `[start, end)` of `path` under `base`, the whole file when both are
   * omitted, or the last `-start` bytes when `start` is negative — the same
   * contract as `ReadRange`, one directory deeper. */
  readRange(
    base: string,
    path: string,
    start?: number,
    end?: number,
  ): Promise<Uint8Array | undefined>;

  /** The slide named on the command line, if there was one. */
  initialSlide(): Promise<string | null>;

  /** Show the native open dialog; the choice arrives via `onOpenSlide`. */
  openSlideDialog(): Promise<void>;

  /** File ▸ Open Slide… picked a directory. */
  onOpenSlide(callback: (dir: string) => void): void;

  /** View ▸ Reset to Slide Defaults. */
  onResetSettings(callback: () => void): void;

  /** Slides opened before, most recent first, skipping any that are not
   * there right now. The shell owns this list — its File ▸ Open Recent menu
   * is built from the same file. */
  recentSlides(): Promise<string[]>;

  /** Open one of them in this window. */
  openRecent(slide: string): Promise<void>;

  forgetRecent(slide: string): Promise<void>;

  clearRecent(): Promise<void>;

  /** Say which session this window now holds, and get back the ones its
   * siblings hold. One window, one session — as in Qt, because a session is
   * one file and the second writer would overwrite the first. */
  openedSession(name: string | null): Promise<string[]>;

  /** Sessions open in the *other* windows on this window's slide. */
  sessionsInUse(): Promise<string[]>;

  /** That set changed — a sibling opened, switched or closed. */
  onSessionsInUse(callback: (names: string[]) => void): void;

  /** Every `<slide>/sessions/*.json`, as the picker needs to list them:
   * the name inside the file and when it was last written. */
  listSessions(base: string): Promise<{ name: string; modified: number | null }[]>;

  /** `<slide>/sessions/<slug>.json` as text, or null if there is none. */
  readSession(base: string, slug: string): Promise<string | null>;

  /** Write that file, replacing it atomically — a slide can be open in two
   * windows, and a half-written session is one that no longer loads. */
  writeSession(base: string, slug: string, text: string): Promise<void>;

  deleteSession(base: string, slug: string): Promise<void>;
}

declare global {
  interface Window {
    cytos?: DesktopHost;
  }
}

/** The shell if we are running inside it, otherwise null (a browser tab). */
export function desktopHost(): DesktopHost | null {
  return window.cytos ?? null;
}
