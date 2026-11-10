'use strict';

// HAX-theme preview with HAX's real web components. HAX's public build (from
// the @haxtheweb/haxcms-nodejs npm package, dist/public) is served locally as a
// stand-in for cdn.hax.cloud. Set HAX_TEST_CDN_DIR to that folder; otherwise
// the test is skipped.

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';
const CDN_DIR = process.env.HAX_TEST_CDN_DIR;
const PORT = 7000 + Math.floor(Math.random() * 1000);
const ORIGIN = `http://127.0.0.1:${PORT}`;
let playwright = null;
try { playwright = require('playwright'); } catch (_) { /* optional */ }

const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.json': 'application/json', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff',
  '.html': 'text/html' };

function cdnServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const file = path.join(CDN_DIR, path.normalize(decodeURIComponent(url.pathname.replace(/^\/cdn\//, ''))));
    if (!file.startsWith(CDN_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404, { 'Access-Control-Allow-Origin': '*' }).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream',
      'Access-Control-Allow-Origin': '*' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(resolve => server.listen(0, 'localhost', () => resolve(server)));
}

function startApp(dataDir, cdnUrl) {
  const code = String.raw`
import sys
sys.path[:0] = ["python/app", "python"]
import app as web
from werkzeug.serving import make_server
web.app.config.update(MENTOR_DATA_ROOT=sys.argv[1])
server = make_server("127.0.0.1", int(sys.argv[2]), web.app, threaded=True)
print("ready", flush=True)
server.serve_forever()
`;
  const child = spawn(PYTHON, ['-c', code, dataDir, String(PORT)], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, HAX_EXECUTION_MODE: 'local', HAX_CDN_URL: cdnUrl } });
  return new Promise((resolve, reject) => {
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.stdout.on('data', chunk => { if (String(chunk).includes('ready')) resolve(child); });
    child.on('exit', status => reject(new Error(`app exited ${status}: ${stderr}`)));
  });
}

test('course renders as a HAX site with the Clean One theme, sandboxed', { skip: !playwright || !CDN_DIR, timeout: 120000 },
  async () => {
    const cdn = await cdnServer();
    const cdnUrl = `http://localhost:${cdn.address().port}/cdn/`;
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-hax-'));
    const exportPath = path.join(dataDir, 'bio101.imscc');
    execFileSync(PYTHON, ['-c', String.raw`
import sys, zipfile
sys.path.insert(0, "tests")
from test_slim_import import text_entries
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in text_entries().items():
        z.writestr(name, data)
`, exportPath], { cwd: ROOT });
    const app = await startApp(dataDir, cdnUrl);
    const browser = await playwright.chromium.launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      await page.goto(`${ORIGIN}/mentor`);
      await page.locator('#cc-intake').waitFor({ state: 'visible' });
      await page.setInputFiles('#cc-export', exportPath);
      await page.locator('#cc-slim-import').waitFor({ state: 'visible' });
      await page.click('#cc-slim-import');
      await page.locator('#cc-course').waitFor({ state: 'visible' });
      const [preview] = await Promise.all([page.context().waitForEvent('page'),
        page.click('#cc-course .cc-hax-preview')]);
      await preview.waitForURL(/\/mentor\/hax\/[^/]+\/$/);
      const url = preview.url();
      assert.equal(await preview.evaluate(() => window.opener), null);
      const failures = [];
      preview.on('requestfailed', r => failures.push(r.url()));
      if (process.env.HAX_DEBUG) {
        preview.on('console', m => console.log('CONSOLE', m.type(), m.text().slice(0, 300)));
        preview.on('pageerror', e => console.log('PAGEERROR', (e.stack || e.message).slice(0, 900).replace(/\n/g, ' | ')));
        preview.on('requestfailed', r => console.log('FAILED', r.url().slice(0, 200), r.failure() && r.failure().errorText));
        preview.on('response', r => { if (r.status() >= 400) console.log('HTTP', r.status(), r.url().slice(0, 200)); });
      }
      if (process.env.HAX_DEBUG) preview.on('request', r => { if (r.url().includes('/mentor/hax/')) console.log('REQ', r.url().split('/').slice(-2).join('/')); });
      await preview.reload();
      if (process.env.HAX_DEBUG) {
        await preview.waitForTimeout(12000);
        if (process.env.CC_SCREENSHOTS) await preview.screenshot({ path: path.join(process.env.CC_SCREENSHOTS, '9-hax-debug.png') });
        console.log('DOM', (await preview.evaluate(() => {
          const builder = document.querySelector('haxcms-site-builder');
          const tags = [...new Set([...document.querySelectorAll('*')].map(e => e.tagName.toLowerCase()).filter(t => t.includes('-')))];
          return JSON.stringify({ tags: tags.slice(0, 40), builderChildren: builder ? [...builder.children].map(c => c.tagName) : null,
            shadow: builder && builder.shadowRoot ? builder.shadowRoot.innerHTML.slice(0, 300) : null,
            text: document.body.innerText.slice(0, 300) });
        })));
      }
      // HAX's site builder loads the theme declared in site.json and renders the course (in shadow DOM;
      // Playwright's text locators pierce it).
      await preview.waitForFunction(() => document.querySelector('clean-one') && customElements.get('clean-one'),
        null, { timeout: 60000 });
      await preview.getByText('Office hours Tuesday').first().waitFor({ timeout: 30000 });
      await preview.getByText('Week 1', { exact: true }).first().waitFor();
      await preview.getByText('Week 2', { exact: true }).first().waitFor();
      // Deep link to an assignment page, as HAX's router addresses it.
      const manifest = await (await preview.request.get(url + 'site.json')).json();
      assert.equal(manifest.metadata.theme.element, 'clean-one');
      const essay = manifest.items.find(item => item.title === 'Essay');
      assert.ok(essay.parent, 'assignments sit under their module');
      await preview.goto(url + essay.slug);
      await preview.getByText('Argue a position on cell theory').first().waitFor({ timeout: 30000 });
      // Isolated origin: the third-party scripts cannot see the app's cookies or storage.
      assert.equal(await preview.evaluate(() => origin), 'null');
      if (process.env.CC_SCREENSHOTS) await preview.screenshot({ path: path.join(process.env.CC_SCREENSHOTS, '9-hax-preview.png') });
      // A tampered link is refused.
      const bad = await preview.request.get(url.replace(/\/hax\/[^/]+\//, '/hax/tampered/'));
      assert.equal(bad.status(), 403);
    } finally {
      await browser.close();
      app.kill();
      cdn.close();
    }
  });

test('downloaded course site renders with HAX from a static host, and offline as plain pages',
  { skip: !playwright || !CDN_DIR, timeout: 120000 }, async () => {
    const cdn = await cdnServer();
    const cdnUrl = `http://localhost:${cdn.address().port}/cdn/`;
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-hax-'));
    const exportPath = path.join(dataDir, 'bio101.imscc');
    execFileSync(PYTHON, ['-c', String.raw`
import sys, zipfile
sys.path.insert(0, "tests")
from test_slim_import import text_entries
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in text_entries().items():
        z.writestr(name, data)
`, exportPath], { cwd: ROOT });
    const app = await startApp(dataDir, cdnUrl);
    const browser = await playwright.chromium.launch();
    let site;
    try {
      const page = await browser.newPage();
      await page.goto(`${ORIGIN}/mentor`);
      await page.locator('#cc-intake').waitFor({ state: 'visible' });
      await page.setInputFiles('#cc-export', exportPath);
      await page.locator('#cc-slim-import').waitFor({ state: 'visible' });
      await page.click('#cc-slim-import');
      await page.locator('#cc-course').waitFor({ state: 'visible' });
      const wsId = await page.evaluate(async () => (await (await fetch('/mentor/api/workspaces')).json())[0].id);
      const zip = await page.context().request.get(`${ORIGIN}/mentor/api/workspaces/${wsId}/documents/export?format=hax`);
      assert.equal(zip.status(), 200);
      const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-site-'));
      fs.writeFileSync(path.join(folder, 'site.zip'), await zip.body());
      execFileSync(PYTHON, ['-I', '-c', 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])',
        path.join(folder, 'site.zip'), path.join(folder, 'site')]);
      const root = path.join(folder, 'site');
      assert.ok(fs.existsSync(path.join(root, 'README.txt')));
      // Any static host: here a minimal one.
      site = http.createServer((req, res) => {
        const url = new URL(req.url, 'http://localhost');
        let file = path.join(root, path.normalize(decodeURIComponent(url.pathname)));
        if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
        if (!file.startsWith(root) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
        res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'text/plain' });
        fs.createReadStream(file).pipe(res);
      });
      await new Promise(resolve => site.listen(0, '127.0.0.1', resolve));
      const hax = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      await hax.goto(`http://127.0.0.1:${site.address().port}/`);
      await hax.waitForFunction(() => customElements.get('clean-one'), null, { timeout: 60000 });
      await hax.getByText('Office hours Tuesday').first().waitFor({ timeout: 30000 });
      await hax.getByText('Week 2', { exact: true }).first().waitFor();
      if (process.env.CC_SCREENSHOTS) await hax.screenshot({ path: path.join(process.env.CC_SCREENSHOTS, '10-hax-download.png') });
      // Offline plain preview straight from disk.
      const offline = await browser.newPage();
      await offline.goto('file://' + path.join(root, 'preview', 'index.html'));
      assert.match(await offline.locator('body').textContent(), /Biology 101: Cells.*Essay/s);
    } finally {
      await browser.close();
      if (site) site.close();
      app.kill();
      cdn.close();
    }
  });
