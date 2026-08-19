/**
 * The only bridge between the page and the desktop shell.
 *
 * CommonJS, not ESM: Electron accepts an ESM preload only with the sandbox
 * switched off, and this stays sandboxed. Everything exposed here is a plain
 * function over IPC — the page never sees `fs`, `path` or `require`.
 *
 * The TypeScript shape of this object is `DesktopHost` in `src/io/host.ts`.
 * Change one, change the other.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cytos', {
  readRange: (base, path, start, end) =>
    ipcRenderer.invoke('cytos:read', base, path, start, end),

  initialSlide: () => ipcRenderer.invoke('cytos:initial-slide'),

  openSlideDialog: () => ipcRenderer.invoke('cytos:open-dialog'),

  onOpenSlide: (callback) => {
    ipcRenderer.on('cytos:open-slide', (_event, dir) => callback(dir));
  },

  onResetSettings: (callback) => {
    ipcRenderer.on('cytos:reset-settings', () => callback());
  },

  openedSession: (name) => ipcRenderer.invoke('cytos:session-open', name),

  sessionsInUse: () => ipcRenderer.invoke('cytos:sessions-in-use'),

  onSessionsInUse: (callback) => {
    ipcRenderer.on('cytos:sessions-in-use', (_event, names) => callback(names));
  },

  listSessions: (base) => ipcRenderer.invoke('cytos:sessions:list', base),

  readSession: (base, slug) => ipcRenderer.invoke('cytos:sessions:read', base, slug),

  writeSession: (base, slug, text) =>
    ipcRenderer.invoke('cytos:sessions:write', base, slug, text),

  deleteSession: (base, slug) => ipcRenderer.invoke('cytos:sessions:delete', base, slug),
});
