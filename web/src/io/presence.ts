/**
 * Who has which session open.
 *
 * One window, one session — the Qt viewer's rule (`_OPEN_WINDOWS` in
 * `src/cytos/ui/main_window.py`), and it is not a nicety: a session is one
 * file, so a second window writing the same one silently overwrites the
 * first. Sessions only pay for themselves when you can have two views of a
 * slide side by side, which means something has to say which names are taken.
 *
 * Two answers, because the two builds can see different things. The desktop
 * shell's main process sees every window, so it is asked directly and its
 * answer is exact. A browser tab can see no other tab, so tabs announce
 * themselves to each other over a BroadcastChannel — same origin, no server,
 * and a tab that never answers is simply one that is gone.
 */

import { desktopHost } from './host';

export interface SessionPresence {
  /** Sessions this slide has open elsewhere, right now. */
  inUse(): Promise<string[]>;
  /** Say what this window holds — null while it holds nothing. */
  announce(name: string | null): void;
  /** The set changed somewhere else. */
  onChange(callback: (names: string[]) => void): void;
  close(): void;
}

class HostPresence implements SessionPresence {
  private host = desktopHost()!;

  /** Every call swallows its own failure. Not knowing who holds what is a
   * reason to grey nothing out; it is never a reason to fail to open a
   * slide — including against a shell too old to answer at all. */
  async inUse(): Promise<string[]> {
    try {
      return await this.host.sessionsInUse();
    } catch (err) {
      console.warn('cannot ask the shell which sessions are open:', err);
      return [];
    }
  }

  announce(name: string | null): void {
    this.host.openedSession(name).catch(() => {});
  }

  onChange(callback: (names: string[]) => void): void {
    this.host.onSessionsInUse(callback);
  }

  close(): void {}
}

/** How long to wait for other tabs to answer a roll-call. They reply on
 * their next task, so this is a scheduling delay, not a network one — but a
 * busy tab tessellating tiles can be slow to get to it. */
const ROLL_CALL = 250;

type Message =
  | { kind: 'who'; slide: string }
  | { kind: 'here'; slide: string; id: string; session: string | null }
  | { kind: 'gone'; slide: string; id: string };

class TabPresence implements SessionPresence {
  private channel = new BroadcastChannel('cytos:sessions');
  private id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  private mine: string | null = null;
  /** Other tabs, by their id — a map so a tab that switches sessions
   * replaces its own entry instead of adding one. */
  private others = new Map<string, string>();
  private listener: ((names: string[]) => void) | null = null;

  constructor(private slide: string) {
    this.channel.onmessage = (event: MessageEvent<Message>) => {
      const message = event.data;
      if (!message || message.slide !== this.slide) return;
      if (message.kind === 'who') {
        this.say();
      } else if (message.kind === 'here') {
        if (message.session) this.others.set(message.id, message.session);
        else this.others.delete(message.id);
        this.listener?.(this.names());
      } else if (message.kind === 'gone') {
        this.others.delete(message.id);
        this.listener?.(this.names());
      }
    };
    // A tab that closes without saying so would hold a name for ever.
    window.addEventListener('pagehide', () => this.close());
    this.channel.postMessage({ kind: 'who', slide: this.slide } satisfies Message);
  }

  private names(): string[] {
    return [...this.others.values()];
  }

  private say(): void {
    this.channel.postMessage({
      kind: 'here',
      slide: this.slide,
      id: this.id,
      session: this.mine,
    } satisfies Message);
  }

  async inUse(): Promise<string[]> {
    this.channel.postMessage({ kind: 'who', slide: this.slide } satisfies Message);
    await new Promise((resolve) => setTimeout(resolve, ROLL_CALL));
    return this.names();
  }

  announce(name: string | null): void {
    this.mine = name;
    this.say();
  }

  onChange(callback: (names: string[]) => void): void {
    this.listener = callback;
  }

  close(): void {
    this.channel.postMessage({ kind: 'gone', slide: this.slide, id: this.id } satisfies Message);
    this.channel.close();
  }
}

export function presenceFor(slide: string): SessionPresence {
  return desktopHost() ? new HostPresence() : new TabPresence(slide);
}
