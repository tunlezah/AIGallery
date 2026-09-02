#!/usr/bin/env node
/* Browser smoke test for the built gallery (developer-run, not part of CI).
 *
 * Builds the test fixtures into a temp directory, serves the hosted target
 * under a GitLab-Pages-style sub-path, loads it and the standalone target in
 * headless Chromium, and checks: no console errors, tiles render, images
 * load, search/filter/sort/dialogs work, and no link resolves to a
 * javascript: URL.
 *
 * Needs Node plus Playwright with Chromium, which the disconnected CI runner
 * does not have:
 *   npm install -g playwright && npx playwright install chromium
 *   NODE_PATH="$(npm root -g)" node build/tests/browser_smoke.js
 * Set PYTHON to the interpreter that has build/requirements.txt installed.
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawnSync } = require('child_process');

let chromium;
try { ({ chromium } = require('playwright')); } catch (err) {
  console.error('playwright is not installed; see the header of this file');
  process.exit(2);
}

const ROOT = path.resolve(__dirname, '..', '..');
const PYTHON = process.env.PYTHON || 'python3';
const PREFIX = '/some-namespace/ai-gallery';
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.webp': 'image/webp',
  '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.gif': 'image/gif',
};
const failures = [];

function check(condition, message) {
  if (!condition) { failures.push(message); }
  console.log(`${condition ? 'ok  ' : 'FAIL'} ${message}`);
}

function build(tmp) {
  const result = spawnSync(PYTHON, [
    path.join(ROOT, 'build/generate.py'),
    '--content-dir', path.join(ROOT, 'build/tests/fixtures/content-valid'),
    '--topics-snapshot', path.join(ROOT, 'build/tests/fixtures/topics/topics.json'),
    '--out', path.join(tmp, 'public'),
    '--dist', path.join(tmp, 'dist'),
    '--standalone',
  ], { encoding: 'utf8', env: { ...process.env, GALLERY_CONTENT_REPO_URL: 'https://gitlab.example.com/g/content.git' } });
  if (result.status !== 0) {
    console.error(result.stdout, result.stderr);
    throw new Error('generate.py failed');
  }
}

function serve(dir) {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split('?')[0].split('#')[0]);
      if (!p.startsWith(PREFIX)) { res.writeHead(404); return res.end(); }
      p = p.slice(PREFIX.length) || '/';
      if (p.endsWith('/')) { p += 'index.html'; }
      const file = path.join(dir, p);
      if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end(); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(fs.readFileSync(file));
    });
    server.listen(0, '127.0.0.1', () => resolve({ server, url: `http://127.0.0.1:${server.address().port}${PREFIX}/` }));
  });
}

async function exercise(page, label, expectedTiles) {
  const errors = [];
  const failedRequests = [];
  page.on('console', (m) => { if (m.type() === 'error') { errors.push(m.text()); } });
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('requestfailed', (r) => failedRequests.push(r.url()));
  await page.goto(label.url);
  await page.waitForTimeout(500);

  check(errors.length === 0, `${label.name}: no console/page errors${errors.length ? ' -> ' + errors.join(' | ') : ''}`);
  check(failedRequests.length === 0, `${label.name}: no failed requests${failedRequests.length ? ' -> ' + failedRequests.join(', ') : ''}`);
  const tiles = await page.locator('.tile:visible').count();
  check(tiles === expectedTiles, `${label.name}: ${tiles} tiles rendered (expected ${expectedTiles})`);
  const images = await page.$$eval('img', (imgs) => imgs.map((i) => i.complete && i.naturalWidth > 0));
  check(images.length > 0 && images.every(Boolean), `${label.name}: all ${images.length} images loaded`);
  const scriptLinks = await page.$$eval('a', (as) => as.filter((a) => a.protocol !== 'https:' && a.protocol !== 'http:' && a.protocol !== 'file:').map((a) => a.href));
  check(scriptLinks.length === 0, `${label.name}: every link is http(s)${scriptLinks.length ? ' -> ' + scriptLinks.join(', ') : ''}`);
  check((await page.locator('.more-button:not([hidden])').count()) > 0, `${label.name}: More buttons appear on long bodies`);

  await page.fill('#search', 'zurich');
  await page.waitForTimeout(400);
  check((await page.locator('.tile:visible').count()) === 1, `${label.name}: search "zurich" folds diacritics to one tile`);
  check((await page.evaluate(() => location.hash)) === '#q=zurich', `${label.name}: search state lands in the URL hash`);
  await page.fill('#search', 'is:featured');
  await page.waitForTimeout(400);
  check((await page.locator('.tile:visible').count()) === 1, `${label.name}: is:featured shows the one featured tile`);
  await page.fill('#search', '');
  await page.waitForTimeout(400);
  await page.locator('.rail .chip[data-tag="local"]').click();
  await page.waitForTimeout(300);
  /* Two fixture items carry "local", in two different sections that also
     hold non-matching items: this catches tiles that stay on screen because
     an author display rule defeats the hidden attribute. */
  const chipVisible = await page.locator('.tile:visible').count();
  check(chipVisible === 2, `${label.name}: tag chip hides every non-matching tile (${chipVisible} visible, expected 2)`);
  const hiddenTiles = await page.$$eval('.tile[hidden]', (tiles) => tiles.filter((t) => getComputedStyle(t).display !== 'none').length);
  check(hiddenTiles === 0, `${label.name}: every [hidden] tile is display:none`);
  await page.click('#clear-filters');
  await page.waitForTimeout(300);
  check((await page.locator('.tile:visible').count()) === expectedTiles, `${label.name}: clear filters restores every tile`);

  await page.click('#settings-button');
  await page.waitForTimeout(200);
  check((await page.locator('#settings-dialog[open]').count()) === 1, `${label.name}: settings dialog opens`);
  await page.check('input[name="sort"][value="title"]');
  await page.waitForTimeout(200);
  const firstTooling = await page.getAttribute('#section-tooling .grid .tile', 'data-id');
  check(firstTooling === 'tooling/broken-image', `${label.name}: title sort reorders tiles (first tooling tile: ${firstTooling})`);
  await page.check('input[name="theme"][value="dark"]');
  check((await page.evaluate(() => document.documentElement.getAttribute('data-theme'))) === 'dark', `${label.name}: theme switch applies`);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  await page.click('#help-button');
  await page.waitForTimeout(200);
  check((await page.locator('#help-dialog[open]').count()) === 1, `${label.name}: help dialog opens`);
  await page.keyboard.press('Escape');
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ai-gallery-smoke-'));
  try {
    build(tmp);
    const index = JSON.parse(fs.readFileSync(path.join(tmp, 'public/gallery.json'), 'utf8'));
    const hosted = await serve(path.join(tmp, 'public'));
    const browser = await chromium.launch();
    const targets = [
      { name: 'hosted', url: hosted.url },
      { name: 'standalone', url: 'file://' + path.join(tmp, 'dist/index.html') },
    ];
    for (const target of targets) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      const page = await context.newPage();
      await exercise(page, target, index.items.length);
      await context.close();
    }
    await browser.close();
    hosted.server.close();
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log('\nbrowser smoke: all checks passed');
})().catch((err) => { console.error(err); process.exit(1); });
