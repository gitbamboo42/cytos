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
import { open } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

// Without this the app menu is titled "Electron" when run unpackaged, since
// the name only comes from the bundle's Info.plist once packaged.
app.setName('cytos');

/** Set by `npm run app:dev` to the vite server; empty means load the build. */
const DEV_URL = process.env.CYTOS_DEV_URL;

/**
 * Slide directories the user has actually opened, by dialog or on the command
 * line. The renderer names the file it wants, so main checks that the name
 * resolves inside one of these — a bug in the page can then only read the
 * slide it was given, not the rest of the disk.
 */
const allowed = new Set();

let win = null;
/** The slide to show once the page asks for it (argv, or a menu open that
 * arrives before the window has loaded). */
let pending = null;

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

function openSlide(dir) {
  const full = path.resolve(dir);
  allowed.add(full);
  pending = full;
  app.addRecentDocument(full);
  if (win) {
    win.setTitle(`cytos — ${path.basename(full)}`);
    win.webContents.send('cytos:open-slide', full);
  }
}

async function promptOpenSlide() {
  // A `.cytos` slide is a directory, so this is a directory chooser — the
  // same choice the Qt viewer's File ▸ Open Slide… makes.
  const { canceled, filePaths } = await dialog.showOpenDialog(win, {
    title: 'Open Slide',
    buttonLabel: 'Open',
    properties: ['openDirectory'],
  });
  if (!canceled && filePaths[0]) openSlide(filePaths[0]);
}

ipcMain.handle('cytos:initial-slide', () => pending);
ipcMain.handle('cytos:open-dialog', promptOpenSlide);

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
 * Four menus, and only one of them holds a command of ours. A read-only
 * viewer has almost nothing to put in a menu bar, so most of Electron's
 * stock roles were cut rather than kept for the look of it.
 *
 * Two menus. The app menu is macOS's, shown whatever we do, and Quit lives in
 * it; File holds the two commands the shell exists to give.
 *
 * What went: Window (there is one window), View (one command does not need a
 * menu of its own — Reset moved into File), Toggle Full Screen (the green
 * button already does it), page zoom (⌘+ would enlarge the panel while the
 * viewer has a zoom of its own — two meanings for one key), Reload, and
 * Undo/Redo, which a viewer that edits nothing can never do.
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
        { label: 'Open Slide…', accelerator: 'CmdOrCtrl+O', click: promptOpenSlide },
        {
          label: 'Reset to Slide Defaults',
          click: () => win?.webContents.send('cytos:reset-settings'),
        },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' },
        // Only in a checkout — a packaged app has no source to inspect, so
        // this never shows up in the app you actually use.
        ...(app.isPackaged ? [] : [{ type: 'separator' }, { role: 'toggleDevTools' }]),
      ],
    },
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

function createWindow() {
  win = new BrowserWindow({
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
  if (pending) win.setTitle(`cytos — ${path.basename(pending)}`);
  if (DEV_URL) win.loadURL(DEV_URL);
  else win.loadFile(path.join(here, '..', 'dist', 'index.html'));
}

// macOS: double-clicking a slide in Finder, or dropping one on the Dock icon.
app.on('open-file', (event, filePath) => {
  event.preventDefault();
  openSlide(filePath);
});

app.whenReady().then(() => {
  const initial = slideFromArgv();
  if (initial) openSlide(initial);
  buildMenu();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
