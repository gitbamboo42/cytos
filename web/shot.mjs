// Screenshot the dev viewer and dump console/page errors — the web twin of
// `cytos-ctl snapshot`: check rendering changes with your own eyes.
//
// Usage: node shot.mjs <url> <out.png> [waitMs] [--headed] [--hover]
//
// --headed uses a real browser window. Headless chromium falls back to
// software GL, which makes tile tessellation/upload ~50x slower — fine for
// "does it render at all", useless for judging load speed.
// --hover parks the mouse at the viewport center before the shot, so the
// picking/tooltip path shows up in the image.
import { chromium } from 'playwright';

const args = process.argv.slice(2);
const flags = new Set(args.filter((a) => a.startsWith('--')));
const [url, out, waitMs = '6000'] = args.filter((a) => !a.startsWith('--'));

const browser = await chromium.launch({ headless: !flags.has('--headed') });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const logs = [];
page.on('console', (msg) => {
  if (msg.type() === 'error' || msg.type() === 'warning') logs.push(`${msg.type()}: ${msg.text()}`);
});
page.on('pageerror', (err) => logs.push(`pageerror: ${err.message}`));
page.on('response', (res) => {
  if (res.status() >= 400) logs.push(`${res.status()} ${res.url()}`);
});
await page.goto(url);
await page.waitForTimeout(Number(waitMs));
if (flags.has('--hover')) {
  await page.mouse.move(640, 400);
  await page.waitForTimeout(500);
}
console.log('tileStats:', JSON.stringify(await page.evaluate('window.__tileStats')));
await page.screenshot({ path: out });
await browser.close();
const interesting = logs.filter((l) => !/GL Driver|Failed to load resource/.test(l));
console.log(interesting.length ? interesting.slice(0, 20).join('\n') : 'no console errors');
