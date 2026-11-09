'use strict';

// Real-browser check of the extension hand-off: the unpacked extension is
// loaded in Chromium, the Course Consultant app runs locally, and the
// handoff page receives the course from the extension and imports it.
// Skips when Playwright's Chromium is unavailable.

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';
const PORT = 5000 + Math.floor(Math.random() * 1000);
const ORIGIN = `http://127.0.0.1:${PORT}`;

let playwright = null;
try { playwright = require('playwright'); } catch (_) { /* optional */ }

function startApp(dataDir) {
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
    env: { ...process.env, HAX_EXECUTION_MODE: 'local' } });
  return new Promise((resolve, reject) => {
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.stdout.on('data', chunk => { if (String(chunk).includes('ready')) resolve(child); });
    child.on('exit', code => reject(new Error(`app exited ${code}: ${stderr}`)));
  });
}

function extensionCopy() {
  // Point the extension at this test's app port.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-'));
  for (const name of fs.readdirSync(path.join(ROOT, 'extension'))) {
    fs.copyFileSync(path.join(ROOT, 'extension', name), path.join(dir, name));
  }
  fs.writeFileSync(path.join(dir, 'config.js'), `self.COURSE_CONSULTANT_ORIGIN = '${ORIGIN}';\n`);
  return dir;
}

test('extension hands the course to Course Consultant', { skip: !playwright, timeout: 90000 }, async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-data-'));
  const app = await startApp(dataDir);
  const extension = extensionCopy();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-profile-'));
  let context;
  try {
    context = await playwright.chromium.launchPersistentContext(profile, {
      headless: true, channel: 'chromium', // full Chromium; the headless shell has no extensions
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    let [worker] = context.serviceWorkers();
    if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 15000 });
    const extensionId = new URL(worker.url()).host;

    // Put a collected course in the background worker, as a Canvas collection would.
    const bundle = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/extension_bundle.json'), 'utf8'));
    await worker.evaluate(value => self.__setBundleForTest(value), bundle);

    const page = await context.newPage();
    await page.goto(`${ORIGIN}/mentor/import/extension?ext=${extensionId}`);
    await page.locator('#handoff-review').waitFor({ state: 'visible', timeout: 15000 });
    assert.equal(await page.locator('#handoff-course').textContent(), 'Biology 101: Cells');
    await page.fill('#handoff-instructor', 'Ana Lopez');
    await page.click('#handoff-import');
    await page.locator('#handoff-done').waitFor({ state: 'visible', timeout: 15000 });
    assert.match(await page.locator('#handoff-status').textContent(), /Imported Biology 101: Cells/);

    const workspaces = await page.evaluate(async () => (await fetch('/mentor/api/workspaces')).json());
    assert.equal(workspaces.length, 1);
    assert.equal(workspaces[0].course.source, 'extension');
    assert.equal(workspaces[0].course.counts.quizzes, 2);

    // The extension cleared its copy after the import.
    const again = await page.evaluate(id => new Promise(resolve =>
      chrome.runtime.sendMessage(id, { type: 'get-bundle' }, resolve)), extensionId);
    assert.match(again.error, /No course is waiting/);
  } finally {
    if (context) await context.close();
    app.kill();
  }
});

test('extension refuses to hand the course to another site', { skip: !playwright, timeout: 60000 }, async () => {
  const extension = extensionCopy();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-profile-'));
  const context = await playwright.chromium.launchPersistentContext(profile, {
    headless: true, channel: 'chromium', // full Chromium; the headless shell has no extensions
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
  });
  try {
    let [worker] = context.serviceWorkers();
    if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 15000 });
    const extensionId = new URL(worker.url()).host;
    await worker.evaluate(() => self.__setBundleForTest({ version: 1, course: { name: 'X' }, modules: [], pages: [],
      assignments: [], quizzes: [], new_quizzes: [], discussion_topics: [], outcomes: [], files: [], warnings: [] }));
    // A different localhost port is a different origin; the manifest lets it
    // message the extension, but the background checks the exact origin.
    const http = require('node:http');
    const server = http.createServer((req, res) => res.end('<html><body>other</body></html>'));
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const reply = await page.evaluate(id => new Promise(resolve =>
      chrome.runtime.sendMessage(id, { type: 'get-bundle' }, resolve)), extensionId);
    assert.match(reply.error, /not Course Consultant/);
    server.close();
  } finally {
    await context.close();
  }
});

test('bridge hand-off (used by Firefox) delivers the course only to the import page',
  { skip: !playwright, timeout: 90000 }, async () => {
    const { execFileSync } = require('node:child_process');
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-data-'));
    const extension = fs.mkdtempSync(path.join(os.tmpdir(), 'ext-bridge-'));
    execFileSync('node', [path.join(ROOT, 'scripts/build-extension.cjs'), '--origin', ORIGIN, '--handoff', 'bridge',
      '--keep-test-hook', '--out', extension], { cwd: ROOT });
    const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'manifest.json'), 'utf8'));
    assert.equal(manifest.externally_connectable, undefined);
    assert.deepEqual(manifest.content_scripts[0].matches, ['http://127.0.0.1/mentor/import/extension*']);
    const app = await startApp(dataDir);
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-profile-'));
    let context;
    try {
      context = await playwright.chromium.launchPersistentContext(profile, {
        headless: true, channel: 'chromium',
        args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
      });
      let [worker] = context.serviceWorkers();
      if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 15000 });
      const bundle = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/extension_bundle.json'), 'utf8'));
      await worker.evaluate(value => self.__setBundleForTest(value), bundle);

      // Another page on the same site, outside the import page, gets no bridge.
      const other = await context.newPage();
      await other.goto(`${ORIGIN}/mentor`);
      const reply = await other.evaluate(() => new Promise(resolve => {
        window.addEventListener('message', e => { if (e.data && e.data.source === 'course-consultant-extension') resolve(e.data); });
        window.postMessage({ source: 'course-consultant-page', type: 'get-bundle', nonce: 'n1' }, location.origin);
        setTimeout(() => resolve('no answer'), 1500);
      }));
      assert.equal(reply, 'no answer');

      const page = await context.newPage();
      await page.goto(`${ORIGIN}/mentor/import/extension?bridge=1`);
      await page.locator('#handoff-review').waitFor({ state: 'visible', timeout: 15000 });
      assert.equal(await page.locator('#handoff-course').textContent(), 'Biology 101: Cells');
      await page.click('#handoff-import');
      await page.locator('#handoff-done').waitFor({ state: 'visible', timeout: 15000 });
      const workspaces = await page.evaluate(async () => (await fetch('/mentor/api/workspaces')).json());
      assert.equal(workspaces[0].course.source, 'extension');
      // The extension cleared its copy after the import.
      const again = await page.evaluate(() => new Promise(resolve => {
        window.addEventListener('message', e => {
          if (e.data && e.data.source === 'course-consultant-extension' && e.data.nonce === 'n2') resolve(e.data.response);
        });
        window.postMessage({ source: 'course-consultant-page', type: 'get-bundle', nonce: 'n2' }, location.origin);
      }));
      assert.match(again.error, /No course is waiting/);
    } finally {
      if (context) await context.close();
      app.kill();
    }
  });
