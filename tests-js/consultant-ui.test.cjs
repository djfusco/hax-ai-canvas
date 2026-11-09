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

function startApp(dataDir) {
  const child = spawn(PYTHON, ['-c', SERVER, dataDir, String(PORT)], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, HAX_EXECUTION_MODE: 'local' } });
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
      await page.click('#cc-generate');
      await page.locator('.cc-document').waitFor();
      const frame = page.frameLocator('.cc-doc-frame');
      assert.equal(await frame.locator('h1').textContent(), 'BIO 101 Syllabus');
      await page.click('.cc-document button:has-text("Approve")');
      await page.locator('.cc-document .cc-tag:has-text("Approved")').waitFor();
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
