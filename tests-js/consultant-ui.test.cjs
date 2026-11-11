'use strict';

// End-to-end UI journey in Chromium against the real app, with a scripted
// fake AI provider (no network, no paid calls). Skips without Playwright.

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';
const PORT = 6000 + Math.floor(Math.random() * 1000);
const ORIGIN = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.CC_SCREENSHOTS || '';

let playwright = null;
try { playwright = require('playwright'); } catch (_) { /* optional */ }

const SERVER = String.raw`
import json, sys
sys.path[:0] = ["python/app", "python", "tests"]
import ai_client

PROPOSAL = {"topic": "assessment", "title": "Add a graded outline before the essay",
            "change": "Students submit a one-page outline in week 1 for feedback.",
            "rationale": "Spreads the stakes and gives early feedback.", "principle": "Formative assessment",
            "evidence": [], "affected_items": [], "effort": "small", "ai_dimension": "resilient_assessment"}

SLOW_ONCE = []

class FakeAI:
    def __init__(self, provider, model=None, **kwargs):
        self.provider, self.model, self.capabilities = provider, model, kwargs.get("capabilities")
    def preflight_structured(self, *args, **kwargs):
        return {}
    def call_structured(self, system, prompt, schema, max_tokens=4096):
        if "drafting a Course Improvement Plan" in system:
            return json.dumps({"vision": "A first-year course where students build arguments from evidence.",
                               "outcomes": [{"text": "Construct a scientific argument from evidence.", "note": "unchanged"}],
                               "ai_policy": "You may use AI to practise; cite any AI help.",
                               "open_questions": ["Keep the final exam?"]})
        if "QUICK AI VERSION" in prompt and "Topic: Engagement" in prompt and not SLOW_ONCE:
            SLOW_ONCE.append(1)  # the first Engagement turn is slow, so the test can cancel it
            import time
            time.sleep(4)
        if "QUICK AI VERSION" in prompt:
            return json.dumps({"reply": "I reviewed this topic and decided on the strongest options.",
                               "topic_updates": [], "proposed_changes": [PROPOSAL] if "Topic: Assessment" in prompt else [],
                               "next_topic": ""})
        if "INSTRUCTOR'S REQUEST" in prompt:
            return json.dumps({"title": "BIO 101 Syllabus", "html": "<h1>BIO 101 Syllabus</h1><p>Office hours added.</p>",
                               "summary_of_changes": "Added office hours.", "applied_proposals": []})
        if "revising a faculty member's course documents" in system:
            return json.dumps({"title": "BIO 101 Syllabus", "html": "<h1>BIO 101 Syllabus</h1><p>Essay outline due in week 1.</p>",
                               "summary_of_changes": "Added the outline checkpoint.", "applied_proposals": ["p-1"]})
        if "first session" in prompt:
            return json.dumps({"reply": "Here's what I see: **two modules** and weighted grading.\n- Labs are 30%\n- The essay has a rubric\nWho are your students?",
                               "topic_updates": [{"topic": "purpose", "status": "in_progress", "facts": [], "goals": [],
                                                  "open_questions": ["Who takes the course?"], "resolved_questions": []}],
                               "proposed_changes": [], "next_topic": ""})
        return json.dumps({"reply": "Thanks. One idea for the essay is an early outline.",
                           "topic_updates": [{"topic": "purpose", "status": "covered",
                                              "facts": [{"text": "First-year non-majors", "evidence": ["instructor"]}],
                                              "goals": [], "open_questions": [], "resolved_questions": ["Who takes the course?"]}],
                           "proposed_changes": [PROPOSAL], "next_topic": "outcomes"})
    def close(self):
        pass

ai_client.AIClient = FakeAI
import app as web
from model_capabilities import resolve_capabilities
web.app.config.update(MENTOR_DATA_ROOT=sys.argv[1], MODEL_DISCOVERY=lambda provider, saved: {
    "models": [{"id": "gpt-4o", "name": "GPT-4o", "compatibility": "text",
                "capabilities": resolve_capabilities("openai", "gpt-4o")}], "manual_allowed": False})
from werkzeug.serving import make_server
server = make_server("127.0.0.1", int(sys.argv[2]), web.app, threaded=True)
print("ready", flush=True)
server.serve_forever()
`;

const MAKE_EXPORT = String.raw`
import sys, zipfile
sys.path.insert(0, "tests")
from test_slim_import import text_entries
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr(zipfile.ZipInfo("web_resources/lecture one.mp4"), b"\0" * (2 * 1024 * 1024))
    z.writestr("web_resources/reading.pdf", b"%PDF-1.4 synthetic")
    for name, data in text_entries().items():
        z.writestr(name, data.replace("Welcome to cells.", "Welcome to cells. Thanks to Jane Smith.")
                   if name == "wiki_content/welcome.html" else data)
`;

function startApp(dataDir, extraEnv = {}) {
  const child = spawn(PYTHON, ['-c', SERVER, dataDir, String(PORT)], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, HAX_EXECUTION_MODE: 'local', ...extraEnv } });
  return new Promise((resolve, reject) => {
    let stderr = '';
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.stdout.on('data', chunk => { if (String(chunk).includes('ready')) resolve(child); });
    child.on('exit', code => reject(new Error(`app exited ${code}: ${stderr}`)));
  });
}

async function shot(page, name) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: true });
}

test('instructor journey: model, export upload, course map, consultation, proposals',
  { skip: !playwright, timeout: 120000 }, async () => {
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ui-'));
    const exportPath = path.join(dataDir, 'bio101.imscc');
    execFileSync(PYTHON, ['-c', MAKE_EXPORT, exportPath], { cwd: ROOT });
    const app = await startApp(dataDir);
    const browser = await playwright.chromium.launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`${ORIGIN}/mentor`);
      await page.locator('#cc-app').waitFor({ state: 'visible' });
      await page.locator('#cc-intake').waitFor({ state: 'visible' });
      assert.match(await page.locator('#cc-ai-label').textContent(), /Choose an AI model/);

      // AI model
      await page.click('#cc-ai-toggle');
      await page.click('.cc-provider[data-provider="openai"]');
      await page.fill('#cc-key', 'sk-test-only');
      await page.click('#cc-key-form button[type=submit]');
      await page.locator('#cc-models').waitFor({ state: 'visible' });
      await page.selectOption('#cc-model', 'gpt-4o');
      await page.click('#cc-model-use');
      await page.waitForFunction(() => document.querySelector('#cc-ai-label').textContent.includes('gpt-4o'));
      await shot(page, '1-intake');

      // Export upload, slimmed in the page
      await page.setInputFiles('#cc-export', exportPath);
      await page.locator('#cc-slim-import').waitFor({ state: 'visible' });
      assert.match(await page.locator('#cc-slim-text').textContent(), /media files .* stay on your computer/);
      await page.check('#cc-docs-list input[value="web_resources/reading.pdf"]');
      await page.click('#cc-slim-import');
      await page.locator('#cc-course').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#cc-course-title').textContent(), 'Biology 101: Cells');
      assert.match(await page.locator('#cc-privacy').textContent(), /removed 1 likely student detail/);
      const mapText = await page.locator('#cc-map').textContent();
      assert.match(mapText, /Essays 40%/);
      assert.match(mapText, /Lab.*30%/);
      assert.match(mapText, /Construct a scientific argument/);
      await shot(page, '2-course-map');

      // Accessibility check finds the uncaptioned video; Fix with AI drafts a revision.
      await page.click('#cc-a11y summary');
      await page.click('#cc-a11y-run');
      await page.locator('.cc-a11y-item').first().waitFor();
      assert.match(await page.locator('#cc-a11y-results').textContent(), /Video or audio has no captions track/);
      await page.click('.cc-a11y-item button:has-text("Fix with AI")');
      await page.waitForFunction(() => document.querySelector('#cc-status').textContent.includes('is in Revised documents'));

      // Consultation
      await page.click('#cc-start-consult');
      await page.click('#cc-begin-btn');
      await page.locator('#cc-composer').waitFor({ state: 'visible' });
      const opening = page.locator('.cc-msg.assistant').first();
      assert.equal(await opening.locator('strong').textContent(), 'two modules');
      assert.equal(await opening.locator('li').count(), 2);
      await page.fill('#cc-message', 'Mostly first-year non-majors.');
      await page.click('#cc-composer button[type=submit]');
      await page.waitForFunction(() => document.querySelectorAll('.cc-msg.assistant').length === 2);
      assert.match(await page.locator('#cc-topics').textContent(), /Purpose and learnerscovered/);
      assert.equal(await page.locator('#cc-topics button[aria-current=true] span').first().textContent(), 'Outcomes');
      assert.equal(await page.locator('#cc-plan-count').textContent(), '1');
      // Done with Outcomes: move on without more questions.
      await page.click('#cc-next-topic');
      await page.waitForFunction(() => document.querySelectorAll('.cc-msg.assistant').length === 3);
      assert.match(await page.locator('.cc-msg.user').last().textContent(), /I'm done with Outcomes/);
      assert.match(await page.locator('#cc-topics').textContent(), /Outcomescovered/);
      await shot(page, '3-consultation');

      // Plan: decide, draft, edit
      await page.click('#tab-plan');
      await page.locator('.cc-proposal').first().waitFor();
      assert.match(await page.locator('#cc-proposals').textContent(), /Add a graded outline before the essay.*AI-resilient assessment/s);
      await page.click('.cc-proposal button:has-text("Accept")');
      await page.locator('.cc-proposal .cc-tag:has-text("Accepted")').waitFor();
      assert.equal(await page.locator('#cc-plan-count').isHidden(), true);
      await page.click('#cc-plan-draft');
      await page.waitForFunction(() => document.querySelector('#cc-plan-vision').value.includes('build arguments'));
      await page.fill('#cc-plan-questions', 'Keep the final exam?\nAdd a lab practical?');
      await page.click('#cc-plan-form button[type=submit]');
      await page.waitForFunction(() => document.querySelector('#cc-status').textContent.includes('Plan saved'));
      const planHtml = await page.evaluate(async () => (await fetch(document.querySelector('#cc-plan-html').href)).text());
      assert.match(planHtml, /Add a lab practical\?/);
      assert.match(planHtml, /Add a graded outline before the essay/);
      await shot(page, '4-plan');

      // Revised documents
      await page.click('#tab-docs');
      await page.locator('#cc-target option').first().waitFor({ state: 'attached' });
      assert.match(await page.locator('#cc-target option').first().textContent(), /Revised syllabus \(applies 1 accepted change\)/);
      const existing = await page.locator('.cc-document').count(); // the accessibility fix draft
      await page.click('#cc-generate');
      await page.waitForFunction(n => document.querySelectorAll('.cc-document').length === n + 1, existing);
      const newest = page.locator('.cc-document').first(); // newest first
      assert.equal(await newest.frameLocator('.cc-doc-frame').locator('h1').textContent(), 'BIO 101 Syllabus');
      await newest.locator('button:has-text("Approve")').click();
      await page.locator('.cc-document').first().locator('.cc-tag:has-text("Approved")').waitFor();
      const zip = await page.evaluate(async () => {
        const response = await fetch(document.querySelector('#cc-docs-zip').href);
        return { status: response.status, type: response.headers.get('content-type'), size: (await response.arrayBuffer()).byteLength };
      });
      assert.deepEqual([zip.status, zip.type], [200, 'application/zip']);
      assert.ok(zip.size > 1000);
      await shot(page, '5-documents');

      // Returning later shows where we left off
      await page.reload();
      await page.locator('#cc-course').waitFor({ state: 'visible' });
      await page.click('#tab-consult');
      assert.match(await page.locator('#cc-messages').textContent(), /Welcome back.*Covered so far: Purpose and learners/s);
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
      app.kill();
    }
  });

test('mobile width has no horizontal scroll', { skip: !playwright, timeout: 60000 }, async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ui-'));
  const app = await startApp(dataDir);
  const browser = await playwright.chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await page.goto(`${ORIGIN}/mentor`);
    await page.locator('#cc-intake').waitFor({ state: 'visible' });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    await shot(page, '6-mobile');
    assert.ok(overflow <= 0, `page scrolls horizontally by ${overflow}px`);
  } finally {
    await browser.close();
    app.kill();
  }
});

// A fake Canvas on a different site (localhost vs 127.0.0.1): real OAuth redirects.
function fakeCanvas() {
  const http = require('node:http');
  const fixture = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/extension_bundle.json'), 'utf8'));
  const c = '/api/v1/courses/101';
  const routes = {
    '/api/v1/courses': [{ id: 101, name: 'Biology 101: Cells', course_code: 'BIO-101', term: { name: 'Fall 2026' } }],
    [c]: fixture.course, [c + '/modules']: fixture.modules, [c + '/pages']: fixture.pages, [c + '/pages/welcome']: fixture.pages[0],
    [c + '/assignments']: fixture.assignments, [c + '/assignment_groups']: fixture.assignment_groups,
    [c + '/quizzes']: fixture.quizzes, [c + '/quizzes/31/questions']: fixture.quizzes[0].questions,
    [c + '/discussion_topics']: fixture.discussion_topics, [c + '/files']: fixture.files,
    [c + '/outcome_group_links']: fixture.outcomes.map(outcome => ({ outcome })),
  };
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/login/oauth2/auth') {
      const back = new URL(url.searchParams.get('redirect_uri'));
      back.searchParams.set('code', 'good-code');
      back.searchParams.set('state', url.searchParams.get('state'));
      res.writeHead(302, { Location: back.href }).end();
      return;
    }
    if (url.pathname === '/login/oauth2/token') {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const ok = req.method === 'DELETE' || new URLSearchParams(body).get('code') === 'good-code';
        res.writeHead(ok ? 200 : 400, { 'Content-Type': 'application/json' }).end(JSON.stringify(ok ? { access_token: 'tok-1' } : {}));
      });
      return;
    }
    if (req.headers.authorization !== 'Bearer tok-1') { res.writeHead(401).end('{}'); return; }
    const route = routes[url.pathname];
    if (!route) { res.writeHead(404).end('{}'); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' }).end('while(1);' + JSON.stringify(route));
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server)));
}

test('institutional Canvas connection: connect, choose a course, import', { skip: !playwright, timeout: 90000 }, async () => {
  const canvas = await fakeCanvas();
  const canvasOrigin = `http://localhost:${canvas.address().port}`;
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ui-'));
  const app = await startApp(dataDir, {
    HAX_CANVAS_INSTITUTIONS: JSON.stringify([{ id: 'state-u', name: 'State University', base_url: canvasOrigin,
      client_id: '1700001', client_secret_env: 'CANVAS_SECRET_STATE' }]),
    CANVAS_SECRET_STATE: 'secret-value',
  });
  const browser = await playwright.chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${ORIGIN}/mentor`);
    await page.locator('#cc-canvas-card').waitFor({ state: 'visible' });
    await page.click('#cc-canvas-go');
    await page.waitForURL(`${ORIGIN}/mentor`);
    await page.locator('#cc-canvas-pick').waitFor({ state: 'visible' });
    assert.match(await page.locator('#cc-canvas-course').textContent(), /Biology 101: Cells · Fall 2026/);
    await page.click('#cc-canvas-import');
    await page.locator('#cc-course').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#cc-course-title').textContent(), 'Biology 101: Cells');
    assert.match(await page.locator('#cc-course-meta').textContent(), /Canvas \(connected\)/);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    app.kill();
    canvas.close();
  }
});

test('quick AI version, site preview and asking for changes', { skip: !playwright, timeout: 120000 }, async () => {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ui-'));
  const exportPath = path.join(dataDir, 'bio101.imscc');
  execFileSync(PYTHON, ['-c', MAKE_EXPORT, exportPath], { cwd: ROOT });
  const app = await startApp(dataDir);
  const browser = await playwright.chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const dialogs = [];
    page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.accept(); });
    await page.goto(`${ORIGIN}/mentor`);
    await page.locator('#cc-intake').waitFor({ state: 'visible' });
    await page.click('#cc-ai-toggle');
    await page.click('.cc-provider[data-provider="openai"]');
    await page.fill('#cc-key', 'sk-test-only-fixture-key');
    await page.click('#cc-key-form button[type=submit]');
    await page.locator('#cc-models').waitFor({ state: 'visible' });
    await page.selectOption('#cc-model', 'gpt-4o');
    await page.click('#cc-model-use');
    await page.setInputFiles('#cc-export', exportPath);
    await page.locator('#cc-slim-import').waitFor({ state: 'visible' });
    await page.click('#cc-slim-import');
    await page.locator('#cc-course').waitFor({ state: 'visible' });

    // Quick AI version from the course screen: the progress panel shows each step and can cancel.
    await page.click('#cc-quick');
    await page.locator('#cc-progress').waitFor({ state: 'visible' });
    await page.waitForFunction(() => /now: Engagement/.test(document.querySelector('#cc-progress-steps').textContent));
    assert.match(await page.textContent('#cc-progress-steps'), /Purpose and learners ✓/);
    await page.click('#cc-progress-cancel');
    await page.waitForFunction(() => /^Cancelled after/.test(document.querySelector('#cc-progress-time').textContent));
    assert.match(await page.textContent('#cc-error'), /Cancelled\. Everything finished before you cancelled is saved/);
    await page.click('#cc-progress-cancel'); // Close
    assert.equal(await page.locator('#cc-progress').isHidden(), true);
    // The server finishes the cancelled turn; starting again resumes where it stopped.
    await page.waitForTimeout(4500);
    await page.click('#cc-quick');
    await page.locator('#view-docs').waitFor({ state: 'visible', timeout: 60000 });
    assert.match(dialogs[0], /About 8–20 AI requests and [\d,]+–[\d,]+ tokens\./, 'quick confirm shows the estimate');
    assert.match(dialogs.at(-1), /About 2–14 AI requests/, 'the second run only estimates what is left');
    await page.waitForFunction(() => document.querySelector('#cc-status').textContent.includes('Quick AI version ready'));
    assert.match(await page.textContent('#cc-progress-time'), /^Finished after/);
    assert.equal(await page.locator('#cc-progress-steps li[data-status=done]').count(), 4);
    await page.locator('.cc-document .cc-tag:has-text("AI-approved")').waitFor();
    assert.equal(await page.locator('#cc-plan-count').isHidden(), true, 'no undecided changes remain');
    await shot(page, '7-quick-docs');

    // The preview opens the whole course site in a new tab, using the approved syllabus.
    const [site] = await Promise.all([context.waitForEvent('page'), page.click('#cc-docs-preview')]);
    await site.waitForLoadState();
    assert.match(await site.content(), /Private student-view preview/);
    assert.match(await site.locator('body').textContent(), /BIO 101 Syllabus.*Week 1.*Essay/s);
    await shot(site, '8-site-preview');
    await site.close();

    // Ask for a change to the syllabus; it returns to draft for review.
    await page.fill('.cc-ask-input', 'Add my office hours');
    await page.click('.cc-ask button[type=submit]');
    await page.locator('.cc-document .cc-tag:has-text("Draft")').waitFor();
    assert.equal(await page.frameLocator('.cc-doc-frame').locator('p').first().textContent(), 'Office hours added.');

    // Compare with the original syllabus: changes are highlighted.
    await page.click('.cc-document button:has-text("Compare with original")');
    await page.locator('.cc-document .cc-compare').waitFor();
    assert.ok(await page.frameLocator('.cc-compare .cc-doc-frame').locator('ins').count() > 0);
    await page.click('.cc-document button:has-text("Side by side")');
    assert.equal(await page.locator('.cc-compare .cc-doc-frame').count(), 2);
    await page.click('.cc-document button:has-text("Back")');
    // History: restore the AI-approved version from before the requested change.
    await page.click('.cc-document button:has-text("History (1)")');
    await page.click('.cc-document .cc-version button:has-text("Restore")');
    await page.waitForFunction(() => document.querySelector('.cc-document') &&
      document.querySelector('.cc-document button') && [...document.querySelectorAll('.cc-document button')].some(b => b.textContent === 'History (1)'));
    assert.equal(await page.frameLocator('.cc-document .cc-doc-frame').first().locator('p').first().textContent(), 'Essay outline due in week 1.');

    // Bulk: revise a page with one instruction.
    const before = await page.locator('.cc-document').count();
    await page.click('.cc-bulk summary');
    await page.fill('#cc-bulk-instruction', 'Add a short why-this-matters introduction.');
    await page.check('#cc-bulk-items label:has-text("Welcome") input');
    await page.click('#cc-bulk-run');
    await page.waitForFunction(n => document.querySelectorAll('.cc-document').length === n + 1, before);

    // Share a read-only link; a visitor without the instructor's cookies can open it.
    await page.click('#cc-share summary');
    await page.click('#cc-share-start');
    await page.locator('#cc-share-on').waitFor({ state: 'visible' });
    const shared = await page.inputValue('#cc-share-url');
    const visitor = await (await browser.newContext()).newPage();
    await visitor.goto(shared);
    await visitor.click('text=Improvement plan');
    await visitor.locator('h1').first().waitFor();
    await page.click('#cc-share-stop');
    await page.locator('#cc-share-start').waitFor({ state: 'visible' });
    assert.equal((await visitor.goto(shared)).status(), 404, 'turned-off link stops working');
    await visitor.context().close();

    // Automatic quality check of the advisor's work.
    await page.click('#cc-quality summary');
    await page.click('#cc-quality-run');
    await page.locator('#cc-quality-results .cc-a11y-item').first().waitFor();
    assert.match(await page.textContent('#cc-quality-summary'), /\d+ passed, \d+ to review, \d+ failed/);

    // The plan shows the advisor's decisions as AI-decided, and they can be changed.
    await page.click('#tab-plan');
    await page.locator('.cc-proposal .cc-tag.ai').first().waitFor();
    await page.click('.cc-proposal button:has-text("Reject")');
    await page.locator('.cc-proposal .cc-tag:has-text("Rejected")').waitFor();
    assert.equal(await page.locator('.cc-proposal .cc-tag.ai').count(), 0, 'decision is now the instructor\'s');
    // The consultation shows the advisor's notes and can continue.
    await page.click('#tab-consult');
    assert.match(await page.locator('#cc-messages').textContent(), /Quick AI version: Purpose and learners/);
    assert.equal(await page.locator('#cc-composer').isVisible(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    app.kill();
  }
});
