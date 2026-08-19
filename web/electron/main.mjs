/**
 * The desktop shell — one native window around the same web viewer.
 *
 * Nothing about the viewer changes here. The renderer is the identical vite
 * build that a browser loads; the shell adds the three things a browser tab
 * cannot give: a menu bar, a file-open dialog, and slides read straight off
 * the disk with no server in between.
 *
 * Bytes go through `cytos:read`, which answers the same question
 * `httpReadRange` answers over HTTP — read [start, end) of a file, or its
 * last N bytes when start is negative. That is the whole seam: swap the
 * reader and `RangeStore`, `ZipStore`, viv and deck are untouched.
 *
 * Run it two ways:
 *   npm run app        -- loads web/dist, what the packaged app does
 *   npm run app:dev    -- loads the vite server instead, so edits hot-reload
 */

import { app, BrowserWindow, Menu, dialog, ipcMain } from 'electron';
import { mkdir, open, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// Without this the app menu is titled "Electron" when run unpackaged, since
// the name only comes from the bundle's Info.plist once packaged.
app.setName('cytos');

/** Set by `npm run app:dev` to the vite server; empty means load the build. */
const DEV_URL = process.env.CYTOS_DEV_URL;

/**
 * One process, as in the Qt viewer: launching again raises the app that is
 * already running rather than starting a second one. Not a nicety — "no two
 * windows share a session" can only be enforced among windows one process
 * can see, and two processes over one slide would quietly write the same
 * session file. It also rules out the confusion of a second copy running
 * older code than the page it loads.
 */
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => {
    const named = argv.slice(1).find((a) => a.endsWith('.cytos'));
    if (named) openSlide(path.resolve(named));
    else BrowserWindow.getAllWindows()[0]?.focus();
  });
}

/**
 * Slide directories the user has actually opened, by dialog or on the command
 * line. The renderer names the file it wants, so main checks that the name
 * resolves inside one of these — a bug in the page can then only read the
 * slide it was given, not the rest of the disk.
 */
const allowed = new Set();

/**
 * Windows, and what each is looking at.
 *
 * A window is bound to one slide and one session, exactly as in the Qt
 * viewer: two views of the same slide are two windows, and no two windows
 * share a session, because a session is one file and the second writer would
 * silently overwrite the first. Main is the authority on who holds what —
 * it can see every window, which no page can.
 */
const windows = new Map(); // BrowserWindow -> { slide, session }

function stateOf(contents) {
  for (const [w, state] of windows) {
    if (w.webContents === contents) return { window: w, state };
  }
  return null;
}

/** Sessions this slide has open in windows other than `except`. */
function sessionsInUse(slide, except) {
  const names = [];
  for (const [w, state] of windows) {
    if (w !== except && state.slide === slide && state.session) names.push(state.session);
  }
  return names;
}

/** Tell every window on this slide what the others hold, so their pickers
 * can grey those names out. */
function broadcastInUse(slide) {
  for (const [w, state] of windows) {
    if (state.slide === slide) {
      w.webContents.send('cytos:sessions-in-use', sessionsInUse(slide, w));
    }
  }
}

function retitle(w) {
  const state = windows.get(w);
  if (!state?.slide) return;
  // The session is in the title because it is what tells two windows on the
  // same slide apart — the same reason the Qt viewer puts it there.
  const name = path.basename(state.slide);
  w.setTitle(state.session ? `cytos — ${name} · ${state.session}` : `cytos — ${name}`);
}

function slideFromArgv() {
  const arg = process.argv.slice(1).find((a) => a.endsWith('.cytos'));
  return arg ? path.resolve(arg) : null;
}

function insideAllowed(full) {
  for (const root of allowed) {
    if (full === root || full.startsWith(root + path.sep)) return true;
  }
  return false;
}

/**
 * Show a slide. An empty window takes it; otherwise a new window opens, so
 * opening a second slide — or a second view of the same one — never disturbs
 * what you were already looking at. Qt's File ▸ Open Slide… does the same.
 */
function openSlide(dir, target) {
  const full = path.resolve(dir);
  allowed.add(full);
  app.addRecentDocument(full);
  const empty = target ?? [...windows].find(([, state]) => !state.slide)?.[0];
  if (!empty) {
    createWindow(full);
    return;
  }
  const state = windows.get(empty);
  state.slide = full;
  state.session = null;
  retitle(empty);
  empty.webContents.send('cytos:open-slide', full);
  broadcastInUse(full);
}

async function promptOpenSlide() {
  const focused = BrowserWindow.getFocusedWindow();
  // A `.cytos` slide is a directory, so this is a directory chooser — the
  // same choice the Qt viewer's File ▸ Open Slide… makes.
  const { canceled, filePaths } = await dialog.showOpenDialog(focused ?? undefined, {
    title: 'Open Slide',
    buttonLabel: 'Open',
    properties: ['openDirectory'],
  });
  if (!canceled && filePaths[0]) openSlide(filePaths[0]);
}

/** File ▸ New Window: a second view of the slide you are on. */
function newWindow() {
  const focused = BrowserWindow.getFocusedWindow();
  createWindow(windows.get(focused)?.slide ?? null);
}

ipcMain.handle('cytos:initial-slide', (event) => stateOf(event.sender)?.state.slide ?? null);
ipcMain.handle('cytos:open-dialog', promptOpenSlide);

/** Which session this window holds. Windows announce it as they open one,
 * and main answers "what are the others holding?" from the same book. */
ipcMain.handle('cytos:session-open', (event, name) => {
  const found = stateOf(event.sender);
  if (!found) return [];
  found.state.session = name;
  retitle(found.window);
  broadcastInUse(found.state.slide);
  return sessionsInUse(found.state.slide, found.window);
});

ipcMain.handle('cytos:sessions-in-use', (event) => {
  const found = stateOf(event.sender);
  return found ? sessionsInUse(found.state.slide, found.window) : [];
});

ipcMain.handle('cytos:read', async (_event, base, rel, start, end) => {
  const full = path.resolve(base, rel);
  if (!insideAllowed(full)) {
    throw new Error(`refusing to read outside an opened slide: ${full}`);
  }

  let handle;
  try {
    handle = await open(full, 'r');
  } catch (err) {
    // A missing chunk is normal — zarr asks for chunks that were never
    // written. The HTTP reader answers 404 with undefined; so does this.
    if (err.code === 'ENOENT' || err.code === 'ENOTDIR') return undefined;
    throw err;
  }

  try {
    const size = (await handle.stat()).size;
    let from;
    let length;
    if (start === undefined) {
      from = 0;
      length = size;
    } else if (start < 0 && end === undefined) {
      // Suffix read: the last -start bytes. A zip's central directory is
      // found by reading backwards from the end (see io/zip.ts).
      length = Math.min(-start, size);
      from = size - length;
    } else {
      from = start;
      length = Math.max(0, Math.min(end, size) - start);
    }
    const buffer = Buffer.allocUnsafe(length);
    const { bytesRead } = await handle.read(buffer, 0, length, from);
    return bytesRead === length ? buffer : buffer.subarray(0, bytesRead);
  } finally {
    await handle.close();
  }
});

/**
 * Sessions: the same `<slide>/sessions/<slug>.json` files the Qt viewer
 * writes, so the two viewers open each other's saved views. The page never
 * names a path — it names a slide it already had open and a slug, and the
 * three checks below (inside an opened slide, under `sessions/`, a slug of
 * safe characters) are what keep it to that folder.
 *
 * The slug rule is `slugify` in `src/cytos/core/session.py`; anything else
 * is refused rather than sanitized, because a silently renamed session is a
 * session someone cannot find again.
 */
const SLUG = /^[a-z0-9._-]+$/;

function sessionFile(base, slug) {
  if (!SLUG.test(slug)) throw new Error(`bad session name: ${slug}`);
  const dir = path.resolve(base, 'sessions');
  if (!insideAllowed(dir)) {
    throw new Error(`refusing to touch sessions outside an opened slide: ${dir}`);
  }
  return path.join(dir, `${slug}.json`);
}

ipcMain.handle('cytos:sessions:list', async (_event, base) => {
  const dir = path.resolve(base, 'sessions');
  if (!insideAllowed(dir)) return [];
  let names;
  try {
    names = await readdir(dir);
  } catch {
    return []; // no sessions folder yet — a slide nobody has saved a view of
  }
  const found = [];
  for (const file of names) {
    if (!file.endsWith('.json')) continue;
    const full = path.join(dir, file);
    try {
      const doc = JSON.parse(await readFile(full, 'utf8'));
      const { mtimeMs } = await stat(full);
      found.push({ name: String(doc.name ?? path.basename(file, '.json')), modified: mtimeMs });
    } catch {
      // An unreadable session is skipped, never fatal: a broken file must
      // not be why a slide won't open. Same rule as `list_sessions`.
      console.warn(`${full}: ignoring unreadable session`);
    }
  }
  return found;
});

ipcMain.handle('cytos:sessions:read', async (_event, base, slug) => {
  try {
    return await readFile(sessionFile(base, slug), 'utf8');
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    throw err;
  }
});

ipcMain.handle('cytos:sessions:write', async (_event, base, slug, text) => {
  const full = sessionFile(base, slug);
  await mkdir(path.dirname(full), { recursive: true });
  // Temp file then rename, the rule `write_manifest` follows: the slide may
  // be open in another window, and replacing a good session with half a one
  // loses the view it held.
  const temp = `${full}.tmp`;
  await writeFile(temp, text);
  await rename(temp, full);
});

ipcMain.handle('cytos:sessions:delete', async (_event, base, slug) => {
  const full = sessionFile(base, slug);
  await rm(full, { force: true });
  await rm(full.replace(/\.json$/, '.png'), { force: true }); // the Qt picker's thumbnail
});

/**
 * Three menus. A read-only viewer has little to put in a menu bar, so most
 * of Electron's stock roles were cut rather than kept for the look of it.
 *
 * The app menu is macOS's, shown whatever we do, and Quit lives in it; File
 * holds the commands the shell exists to give; Window is macOS's own, and it
 * earns its place now that a slide can be open in several windows at once —
 * it is how you find the other one.
 *
 * What went: View (one command does not need a menu of its own — Reset moved
 * into File), Toggle Full Screen (the green button already does it), page
 * zoom (⌘+ would enlarge the panel while the viewer has a zoom of its own —
 * two meanings for one key), Reload, and Undo/Redo, which a viewer that edits
 * nothing can never do.
 *
 * **Edit went too, and that is why `clipboardKeys` exists.** macOS appends
 * Writing Tools, Start Dictation and Emoji & Symbols to any menu it takes for
 * the Edit menu, and it does so below our items where Electron cannot see or
 * remove them. The documented opt-outs (`NSDisabledDictationMenuItem`,
 * `NSDisabledCharacterPaletteMenuItem`) were tried and left Writing Tools
 * standing. Since the menu was only ever there to make ⌘C and ⌘V reach the
 * text fields, the shortcuts are handled directly instead and the menu is
 * gone — verified by pasting into the picker's hex box with no Edit menu
 * present.
 */
function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        // Ids so a test can click them; nothing in the app looks them up.
        { id: 'open-slide', label: 'Open Slide…', accelerator: 'CmdOrCtrl+O', click: promptOpenSlide },
        { id: 'new-window', label: 'New Window', accelerator: 'CmdOrCtrl+N', click: newWindow },
        {
          label: 'Reset to Slide Defaults',
          click: () =>
            BrowserWindow.getFocusedWindow()?.webContents.send('cytos:reset-settings'),
        },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
        // Only in a checkout — a packaged app has no source to inspect, so
        // this never shows up in the app you actually use.
        ...(app.isPackaged ? [] : [{ type: 'separator' }, { role: 'toggleDevTools' }]),
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/**
 * ⌘X / ⌘C / ⌘V / ⌘A without an Edit menu.
 *
 * On macOS those keys normally reach a web page only because a menu item
 * claims them, so removing the menu would leave the color picker's hex box
 * and the contrast fields unable to paste. Catching the keys before the page
 * sees them and calling the same WebContents methods the menu roles call
 * gets the behaviour back and adds no menu for macOS to decorate.
 */
function clipboardKeys(contents) {
  const actions = { x: 'cut', c: 'copy', v: 'paste', a: 'selectAll' };
  contents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || !input.meta || input.control || input.alt) return;
    const action = actions[input.key.toLowerCase()];
    if (!action) return;
    contents[action]();
    event.preventDefault();
  });
}

function createWindow(slide = null) {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    backgroundColor: '#000',
    title: 'cytos',
    webPreferences: {
      // The preload is CommonJS on purpose: Electron only allows an ESM
      // preload when the sandbox is off, and the sandbox is worth more than
      // the import syntax.
      preload: path.join(here, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // The page carries its own <title>, which Electron adopts on load and
  // would use to overwrite the slide name. The title bar is the shell's to
  // set, so refuse the update.
  win.on('page-title-updated', (event) => event.preventDefault());
  clipboardKeys(win.webContents);
  windows.set(win, { slide, session: null });
  retitle(win);
  win.on('closed', () => {
    const slideWas = windows.get(win)?.slide;
    windows.delete(win);
    // The session it held is free again, so the other windows' pickers
    // stop greying that name out.
    if (slideWas) broadcastInUse(slideWas);
  });
  // Windows cascade rather than stack exactly, so a second view of a slide
  // is visibly a second window. Qt offsets its own the same way.
  const [x, y] = BrowserWindow.getFocusedWindow()?.getPosition() ?? [];
  if (x !== undefined) win.setPosition(x + 32, y + 32);
  if (DEV_URL) win.loadURL(DEV_URL);
  else win.loadFile(path.join(here, '..', 'dist', 'index.html'));
  return win;
}

// macOS: double-clicking a slide in Finder, or dropping one on the Dock icon.
app.on('open-file', (event, filePath) => {
  event.preventDefault();
  openSlide(filePath);
});

app.whenReady().then(() => {
  const initial = slideFromArgv();
  if (initial) allowed.add(initial);
  buildMenu();
  createWindow(initial);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
