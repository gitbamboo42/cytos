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
