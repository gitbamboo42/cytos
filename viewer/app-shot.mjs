/**
 * Launch the desktop shell, wait, screenshot it, report anything the page
 * complained about. The Electron twin of shot.mjs.
 *
 *   node app-shot.mjs <slide-dir> <out.png> [wait-ms]
 */

import { _electron as electron } from 'playwright';

const [slide, out, waitMs = '10000'] = process.argv.slice(2);

// This shell exports ELECTRON_RUN_AS_NODE=1, which makes the Electron binary
// behave as plain node — no window, no app. Strip it.
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const app = await electron.launch({
  args: ['electron/main.mjs', ...(slide ? [slide] : [])],
  env,
});

const errors = [];
const win = await app.firstWindow();
win.on('console', (m) => {
  if (m.type() === 'error') errors.push(`console: ${m.text()}`);
});
win.on('pageerror', (e) => errors.push(`page: ${e.message}`));
win.on('response', (r) => {
  if (r.status() >= 400) errors.push(`http ${r.status()}: ${r.url()}`);
});

await win.waitForTimeout(Number(waitMs));

const title = await app.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows()[0]?.getTitle(),
);
const menu = await app.evaluate(({ Menu }) =>
  Menu.getApplicationMenu()?.items.map((i) => i.label),
);
const stats = await win.evaluate(() => window.__tileStats ?? null);

await win.screenshot({ path: out });
console.log('title:', title);
console.log('menu:', JSON.stringify(menu));
console.log('tileStats:', JSON.stringify(stats));
console.log(errors.length ? `PROBLEMS:\n${errors.join('\n')}` : 'no console errors');

await app.close();
