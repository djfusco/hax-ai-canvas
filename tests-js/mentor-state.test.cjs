'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('analysis shows stage, elapsed time and cancel beside Analyze during a long provider wait', async () => {
  const ui = await harness({authenticated: true});
  try {
    await ui.render({...ui.fixture, draft: null, active_job: {
      id: 'analysis-one', kind: 'analysis', phase: 'assessing_baseline',
      message: 'AI-assessing baseline item 1 of 3 against pedagogical frameworks.',
      created_at: new Date(Date.now() - 65000).toISOString(),
    }});
    ui.evaluate('clearTimeout(pollTimer)');
    assert.equal(ui.get('analysis-feedback').hidden, false);
    assert.match(ui.get('analysis-feedback-message').textContent, /item 1 of 3/);
    assert.match(ui.get('analysis-feedback-elapsed').textContent, /1m /);
    assert.match(ui.get('analysis-feedback-help').textContent, /Some models think/);
    assert.equal(ui.get('analysis-feedback-progress').hidden, false);
    assert.equal(ui.get('analysis-feedback-cancel').hidden, false);
    assert.match(ui.get('analyze-course').textContent, /running/);
    assert.equal(ui.evaluate('getFlowState().target'), 'analysis-feedback');
  } finally { ui.dispose(); }
});

test('analysis failure is visible near the button and no longer shows indeterminate progress', async () => {
  const ui = await harness({authenticated: true});
  try {
    await ui.render({...ui.fixture, jobs: [{
      id: 'analysis-one', kind: 'analysis', status: 'failed', revision: 1,
      error: 'AI assessment stopped: output limit. <literal fixture>',
    }]});
    assert.equal(ui.get('analysis-feedback').hidden, false);
    assert.match(ui.get('analysis-feedback-message').textContent, /output limit.*<literal fixture>/);
    assert.equal(ui.get('analysis-feedback-progress').hidden, true);
    assert.equal(ui.get('analysis-feedback-cancel').hidden, true);
    assert.equal(ui.get('analyze-course').textContent, 'Analyze & generate draft');
    await ui.render({...ui.fixture, id: 'different-course', jobs: []});
    assert.equal(ui.get('analysis-feedback').hidden, true);
  } finally { ui.dispose(); }
});

test('workspace polls reuse inventory until the input revision changes', async () => {
  const ui = await harness({authenticated: true});
  try {
    const before = ui.calls.filter(call => call.url.endsWith('/inventory')).length;
    const active = {...ui.fixture, active_job: {id: 'analysis-one', kind: 'analysis'}};
    await ui.render(active);
    await ui.render(active);
    assert.equal(ui.calls.filter(call => call.url.endsWith('/inventory')).length, before);
    await ui.render({...active, revision: 2});
    assert.equal(ui.calls.filter(call => call.url.endsWith('/inventory')).length, before + 1);
  } finally { ui.dispose(); }
});

test('clicking Analyze gives immediate feedback before job admission and prevents duplicate submission', async () => {
  let complete;
  const ui = await harness({authenticated: true, request: (url, options) => {
    if (url.endsWith('/analyze')) return new Promise(resolve => { complete = resolve; });
  }});
  try {
    ui.get('mentor-provider').value = 'guided';
    await ui.render({...ui.fixture, draft: null, draft_revision: 0, direction_revision: 1});
    ui.context.analyzeCourse();
    assert.match(ui.get('analysis-feedback-title').textContent, /Starting guided draft/);
    assert.equal(ui.get('analysis-feedback').hidden, false);
    ui.context.analyzeCourse();
    assert.equal(ui.calls.filter(call => call.url.endsWith('/analyze')).length, 1);
    complete({ok: true, json: async () => ({...ui.fixture, draft: null, active_job: {
      id: 'new-job', kind: 'analysis', message: 'Assessing the first item.'}})});
    await ui.settle();
    assert.match(ui.get('analysis-feedback-message').textContent, /first item/);
  } finally { ui.dispose(); }
});

async function harness({authenticated = false, inventory = {course_items: 1, materials: [], counts: {}},
  session = {}, request, XMLHttpRequest} = {}) {
  const elements = new Map();
  const calls = [];
  const timers = new Set();
  function element(tag = 'div') {
    return {
      tag, tagName: tag.toUpperCase(), children: [], value: '', style: {}, files: [], dataset: {},
      set id(value) { this._id = value; elements.set(value, this); },
      get id() { return this._id; },
      replaceChildren(...children) { this.children = children; },
      appendChild(child) { this.children.push(child); },
      setAttribute() {},
      removeAttribute() {},
      after() {},
      scrollIntoView() {},
      focus() { document.activeElement = this; },
      querySelector() {
        if (this.id === 'source-form') return document.getElementById('upload-course-button');
        if (this.id === 'material-file-form') return document.getElementById('upload-material-button');
        return this.button || (this.button = element('button'));
      },
      add(child) { this.children.push(child); },
    };
  }
  const document = {
    activeElement: null,
    createElement: element,
    getElementById(id) {
      if (!elements.has(id)) element().id = id;
      return elements.get(id);
    },
  };
  const context = vm.createContext({
    document, console, FormData, AbortController, DOMException, URL, XMLHttpRequest,
    window: {matchMedia: () => ({matches: true}), location: {reload() {}}},
    Option: function(text, value) { return {text, value}; },
    setTimeout(callback, delay) {
      const timer = setTimeout(callback, delay);
      timers.add(timer);
      return timer;
    },
    clearTimeout,
    fetch: async (url, options) => {
      calls.push({url, method: options.method, body: options.body, headers: options.headers});
      if (request) {
        const response = await request(url, options, context);
        if (response) return response;
      }
      let data;
      if (url.endsWith('/session')) data = {
        csrf: 'fixture', hosted_ready: true, login_required: !authenticated,
        authenticated, credentials: [], ...session,
      };
      else if (url === '/mentor/api/workspaces') data = [];
      else if (url.endsWith('/inventory')) data = inventory;
      else if (url.endsWith('/brief')) {
        context.serverWorkspace.brief = JSON.parse(options.body).brief.trim();
        data = context.serverWorkspace;
      } else data = context.serverWorkspace;
      return {ok: true, json: async () => structuredClone(data)};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../python/app/static/mentor.js'), 'utf8'), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  const fixture = {
    id: 'workspace-one', title: 'Fixture', stage: 'draft', revision: 1,
    draft_revision: 1, brief: 'Saved brief', messages: [], materials: [], jobs: [],
    active_job: null, artifact: null,
    draft: {summary: 'Fixture draft', warnings: [], items: [
      {id: 'item-one', title: 'Saved title', html: '<p>Saved content</p>'},
    ]},
  };
  async function render(workspace = fixture) {
    context.serverWorkspace = structuredClone(workspace);
    await vm.runInContext('workspace = serverWorkspace; renderWorkspace()', context);
  }
  await render();
  return {context, document, fixture, render, settle, calls,
    dispose: () => timers.forEach(clearTimeout),
    evaluate: code => vm.runInContext(code, context),
    get: id => document.getElementById(id)};
}

test('workspace polling preserves dirty brief after blur and unchanged draft edits', async () => {
  const ui = await harness();
  ui.get('design-brief').value = 'Unsaved brief';
  ui.document.activeElement = ui.get('mentor-message');
  ui.get('mentor-message').value = 'Unsent chat';
  ui.get('draft-title-0').value = 'Unsaved title';
  ui.get('draft-html-0').value = '<p>Unsaved HTML</p>';
  await ui.render();
  await ui.render();
  assert.equal(ui.get('design-brief').value, 'Unsaved brief');
  assert.equal(ui.get('mentor-message').value, 'Unsent chat');
  assert.equal(ui.get('draft-title-0').value, 'Unsaved title');
  assert.equal(ui.get('draft-html-0').value, '<p>Unsaved HTML</p>');
});

test('explicit brief save accepts server normalization without restoring stale text', async () => {
  const ui = await harness();
  ui.get('design-brief').value = '  New saved brief  ';
  ui.context.saveBrief();
  await ui.settle();
  assert.equal(ui.get('design-brief').value, 'New saved brief');
  await ui.render({...ui.fixture, brief: 'New saved brief'});
  assert.equal(ui.get('design-brief').value, 'New saved brief');
});

test('workspace switch and new draft revision load their own server values', async () => {
  const ui = await harness();
  ui.get('design-brief').value = 'Unsaved old workspace text';
  ui.get('draft-title-0').value = 'Unsaved old title';
  await ui.render({...ui.fixture, id: 'workspace-two', brief: 'Second brief'});
  assert.equal(ui.get('design-brief').value, 'Second brief');
  assert.equal(ui.get('draft-title-0').value, 'Saved title');
  ui.get('draft-title-0').value = 'Dirty second title';
  await ui.render({...ui.fixture, id: 'workspace-two', brief: 'Second brief', draft_revision: 2});
  assert.equal(ui.get('draft-title-0').value, 'Saved title');
});

test('new courses require an explicit mentor mode and an intentional source choice', async () => {
  const ui = await harness({authenticated: true, inventory: {course_items: 0, materials: [], counts: {}}});
  await ui.render({...ui.fixture, draft: null, draft_revision: 0, brief: '', stage: 'brief'});
  assert.equal(ui.evaluate('getFlowState().number'), 2);
  assert.equal(ui.get('send-mentor-message').disabled, true);
  assert.match(ui.get('ai-setup-state').textContent, /Choose an AI provider/);
  ui.get('mentor-provider').value = 'guided';
  await ui.context.loadModels();
  assert.equal(ui.evaluate('getFlowState().number'), 3);
  assert.match(ui.get('source-readiness').textContent, /No course content is imported/);
  assert.equal(ui.get('send-mentor-message').disabled, true);
  const before = ui.calls.length;
  ui.context.startBlankCourse();
  assert.equal(ui.calls.length, before, 'Choosing a blank course must not send any request');
  assert.equal(ui.evaluate('getFlowState().number'), 4);
  assert.equal(ui.get('send-mentor-message').disabled, false);
  assert.match(ui.get('mentor-mode').textContent, /no AI requests/);
  assert.equal(ui.get('approve-direction').disabled, true);
});

test('AI chat requires a saved key and model, and unsaved model choices are explained', async () => {
  const ui = await harness({authenticated: true});
  await ui.render({...ui.fixture, draft: null, draft_revision: 0, stage: 'brief'});
  ui.get('mentor-provider').value = 'openai';
  ui.context.renderFlow();
  assert.match(ui.get('ai-setup-state').textContent, /API key/);
  ui.evaluate("configuredProviders.add('openai'); renderFlow()");
  assert.match(ui.get('ai-setup-state').textContent, /Choose a model/);
  ui.evaluate("selectedModels.set('openai', 'future-text')");
  ui.get('mentor-model').value = 'future-text';
  ui.context.renderFlow();
  assert.equal(ui.get('send-mentor-message').disabled, false);
  assert.match(ui.get('mentor-mode').textContent, /OpenAI · future-text/);
  ui.get('mentor-model').value = 'different-unsaved-model';
  ui.get('mentor-model').onchange();
  assert.equal(ui.get('send-mentor-message').disabled, true);
  assert.match(ui.get('ai-setup-state').textContent, /unsaved changes/);
});

test('polling a completed interview clears stale processing and guides explicit direction approval', async () => {
  const ui = await harness({authenticated: true});
  ui.get('mentor-provider').value = 'guided';
  const work = {...ui.fixture, draft: null, draft_revision: 0, stage: 'interviewing',
    active_job: {id: 'job-one', kind: 'interview'}};
  await ui.render(work);
  ui.evaluate('clearTimeout(pollTimer)');
  assert.match(ui.get('mentor-status').textContent, /Preparing the mentor reply/);
  assert.equal(ui.get('send-mentor-message').disabled, true);
  await ui.render({...work, active_job: null, stage: 'brief',
    jobs: [{id: 'job-one', kind: 'interview', status: 'completed', revision: 1}]});
  assert.equal(ui.get('mentor-status').textContent, '');
  assert.equal(ui.get('mentor-status').hidden, true);
  assert.equal(ui.evaluate('getFlowState().target'), 'approve-direction');
  assert.equal(ui.get('approve-direction').disabled, false);
  const before = ui.calls.length;
  ui.context.goToNextStep();
  assert.equal(ui.calls.length, before, 'Next-step navigation must not grant approval');
  assert.equal(ui.document.activeElement, ui.get('approve-direction'));
  ui.get('design-brief').value = 'Unsaved changed brief';
  ui.get('design-brief').oninput();
  assert.equal(ui.get('approve-direction').disabled, true);
  assert.equal(ui.evaluate('getFlowState().target'), 'design-brief');
});

test('draft review, approval and building remain separate and edits require re-review', async () => {
  const ui = await harness({authenticated: true});
  ui.get('mentor-provider').value = 'guided';
  await ui.render({...ui.fixture, reviewed_draft_revision: null, build_revision: null});
  assert.equal(ui.evaluate('getFlowState().target'), 'draft-panel');
  assert.equal(ui.get('approve-build').disabled, true);
  await ui.render({...ui.fixture, reviewed_draft_revision: 1, build_revision: null});
  assert.equal(ui.evaluate('getFlowState().target'), 'approve-build');
  assert.equal(ui.get('approve-build').disabled, false);
  ui.get('draft-title-0').value = 'Unsaved revision';
  ui.get('draft-title-0').oninput();
  assert.equal(ui.evaluate('getFlowState().target'), 'save-draft');
  assert.equal(ui.get('approve-build').disabled, true);
  await ui.render({...ui.fixture, draft_revision: 2, reviewed_draft_revision: null, build_revision: null});
  assert.equal(ui.get('approve-build').disabled, true);
  assert.equal(ui.evaluate('getFlowState().target'), 'draft-panel');
  await ui.render({...ui.fixture, draft_revision: 2, reviewed_draft_revision: 2, build_revision: 2});
  assert.equal(ui.evaluate('getFlowState().target'), 'build-course');
  assert.equal(ui.get('build-course').disabled, false);
});

test('returning users can download completed content without setting up a new AI request', async () => {
  const ui = await harness({authenticated: true});
  await ui.render({...ui.fixture, stage: 'built', artifact: {kind: 'approved-course-content'}});
  assert.equal(ui.get('mentor-provider').value, '');
  assert.equal(ui.evaluate('getFlowState().target'), 'artifact-links');
  assert.equal(ui.evaluate('getFlowState().number'), 6);
  assert.match(ui.get('next-step-description').textContent, /does not publish/);
});

test('inventory errors offer a read-only retry instead of pretending the course is empty', async () => {
  const ui = await harness({authenticated: true, inventory: {course_items: 0, materials: [], counts: {}}});
  ui.get('mentor-provider').value = 'guided';
  await ui.render({...ui.fixture, draft: null, draft_revision: 0, stage: 'brief'});
  ui.evaluate("inventories.clear(); inventoryErrors.set(workspace.id, true); renderFlow()");
  assert.equal(ui.evaluate('getFlowState().retry'), true);
  assert.match(ui.get('next-step-title').textContent, /Reload/);
  const before = ui.calls.length;
  ui.context.goToNextStep();
  await ui.settle();
  assert(ui.calls.slice(before).every(call => call.method === 'GET'));
  assert.equal(ui.evaluate('getFlowState().number'), 3);
  assert.equal(ui.get('send-mentor-message').disabled, true);
});

test('unchanged polling preserves conversation nodes and the reader scroll position', async () => {
  const ui = await harness();
  const work = {...ui.fixture, messages: [{id: 'message-one', role: 'assistant',
    kind: 'interview', content: 'Discuss your learning goals.'}]};
  await ui.render(work);
  const box = ui.get('mentor-messages');
  const message = box.children[0];
  box.scrollTop = 100;
  await ui.render(work);
  assert.equal(box.children[0], message);
  assert.equal(box.scrollTop, 100);
});

test('late polling does not replace a newly selected workspace', async () => {
  const ui = await harness();
  const originalFetch = ui.context.fetch;
  let release;
  ui.context.fetch = (url, options) => url.includes('workspace-one?')
    ? new Promise(resolve => { release = resolve; }) : originalFetch(url, options);
  const pending = ui.context.fetchWorkspace();
  await ui.render({...ui.fixture, id: 'workspace-two'});
  release({ok: true, json: async () => structuredClone(ui.fixture)});
  await pending;
  assert.equal(ui.evaluate('workspace.id'), 'workspace-two');
});

test('late inventory responses cannot overwrite a newer input revision', async () => {
  const ui = await harness();
  const originalFetch = ui.context.fetch;
  let release;
  let first = true;
  ui.context.fetch = (url, options) => {
    if (url.endsWith('/inventory') && first) {
      first = false;
      return new Promise(resolve => { release = resolve; });
    }
    return originalFetch(url, options);
  };
  const pending = ui.render(ui.fixture);
  await ui.render({...ui.fixture, revision: 2});
  release({ok: true, json: async () => ({course_items: 0, materials: [], counts: {}})});
  await pending;
  assert.equal(ui.evaluate('inventories.get(workspace.id).revision'), 2);
  assert.equal(ui.evaluate('courseInputs().info.course_items'), 1);
});

const localSession = {hosted_ready: false, local_mode: true, local_hax: true};
const jsonResponse = data => ({ok: true, json: async () => structuredClone(data)});

const reportFixture = () => ({
  version: 1, scope: 'draft', mode: 'ai',
  frameworks: [{id: 'blooms', title: 'Bloom taxonomy', url: 'https://framework.example.test/blooms', version: 'revised'},
    {id: 'aias', title: 'AIAS', url: 'javascript:alert(1)', version: '2024'}],
  coverage: {items_total: 1, items_evaluated: 1, truncated_items: ['item-one']},
  summary: '<img src=x onerror=alert(1)> Literal assessment text',
  warnings: ['Bounded excerpt warning'],
  items: [{item_id: 'item-one', title: 'Saved title', content_sha256: 'synthetic-hash',
    blooms: {level: 4, label: 'Analyze', rationale: 'Compare evidence claims.'},
    aias: {observed_level: 1, recommended_level: 2, label: 'AI planning', rationale: 'Keep learner judgment.'},
    dimensions: {ai_leverage: {score: 3, rationale: 'Planning can benefit.'}},
    strengths: ['Evidence comparison'], concerns: ['Explain provenance'],
    recommendations: ['Ask learners to cite evidence'],
    evidence: [{source_id: 'course:item-one', quote: '<script>not executable</script>'}],
    limitations: ['Excerpt cannot establish grading practice']}],
});
const nodeText = node => [node.textContent || '', ...(node.children || []).map(nodeText)].join(' ');
const descendants = node => [node, ...(node.children || []).flatMap(descendants)];

test('local framework reports separate baseline/proposed and render references and evidence as text', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  const report = reportFixture();
  await ui.render({...ui.fixture, draft: {...ui.fixture.draft, assessment_required: true,
    assessment_current: true, assessment: report, baseline_assessment: {...report, scope: 'baseline'}}});
  assert.equal(ui.get('assessment-panel').hidden, false);
  assert.equal(ui.get('baseline-assessment').hidden, false);
  const proposed = ui.get('proposed-assessment-content');
  const text = nodeText(proposed);
  assert.match(text, /1 of 1/);
  assert.match(text, /Truncated item excerpts: item-one/);
  assert.match(text, /application-defined heuristic/);
  assert.match(text, /<script>not executable<\/script>/);
  const links = descendants(proposed).filter(node => node.tag === 'a');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://framework.example.test/blooms');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert(!descendants(proposed).some(node => ['script', 'img'].includes(node.tag)));
  const oldNodes = proposed.children;
  await ui.render({...ui.fixture, draft: {...ui.fixture.draft, assessment_required: true,
    assessment_current: true, assessment: report, baseline_assessment: {...report, scope: 'baseline'}}});
  assert.equal(proposed.children, oldNodes, 'Unchanged polls preserve report details and reader position');
});

test('local AI approval needs server-current report and dirty edits explain reassessment', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  const work = {...ui.fixture, reviewed_draft_revision: 1, build_revision: 1,
    draft: {...ui.fixture.draft, assessment_required: true, assessment_current: false}};
  await ui.render(work);
  assert.equal(ui.get('approve-build').disabled, true);
  assert.equal(ui.get('build-course').disabled, true);
  assert.match(ui.get('assessment-readiness').textContent, /missing/);
  assert.equal(ui.evaluate('getFlowState().target'), 'mentor-message');
  await ui.render({...work, draft: {...work.draft, assessment: reportFixture(), assessment_current: true}});
  assert.equal(ui.get('approve-build').disabled, false);
  assert.equal(ui.get('build-course').disabled, false);
  ui.get('draft-title-0').value = 'Unsaved new title';
  ui.context.renderFlow();
  assert.equal(ui.get('approve-build').disabled, true);
  assert.match(ui.get('assessment-readiness').textContent, /unsaved edits/);
  await ui.render({...work, draft_revision: 2, draft: {...work.draft, assessment: reportFixture(), assessment_current: false}});
  assert.equal(ui.get('approve-build').disabled, true);
});

test('Guided blank courses have no fabricated baseline or assessment and hosted gates stay unchanged', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  ui.get('mentor-provider').value = 'guided';
  await ui.render({...ui.fixture, reviewed_draft_revision: 1, build_revision: 1});
  assert.equal(ui.get('baseline-assessment').hidden, true);
  assert.equal(ui.get('proposed-assessment').hidden, true);
  assert.match(ui.get('assessment-readiness').textContent, /Guided walkthrough/);
  assert.equal(ui.get('build-course').disabled, false);
  ui.evaluate('localMode = false');
  await ui.render({...ui.fixture, reviewed_draft_revision: 1, build_revision: 1,
    draft: {...ui.fixture.draft, assessment_required: true, assessment_current: false}});
  assert.equal(ui.get('assessment-panel').hidden, true);
  assert.equal(ui.get('build-course').disabled, false);
});

test('local AI assessment phases are visible during asynchronous baseline and final review jobs', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, active_job: {id: 'assessment-job', kind: 'analysis', phase: 'assessing_baseline'}});
  assert.match(ui.get('mentor-status').textContent, /imported instructional baseline/);
  await ui.render({...ui.fixture, active_job: {id: 'assessment-job', kind: 'review', phase: 'assessing_draft'}});
  assert.match(ui.get('mentor-status').textContent, /every proposed draft item/);
  assert.equal(ui.get('approve-build').disabled, true);
  await ui.render({...ui.fixture, active_job: {id: 'assessment-job', kind: 'analysis', phase: 'drafting'}});
  assert.match(ui.get('mentor-status').textContent, /Generating your proposed draft/);
});

test('local preparation failures expose safe details in upload feedback and a visible banner', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  const error = 'An instructional item exceeds the 2 MiB mentoring review limit.';
  const failed = {...ui.fixture, jobs: [{id: 'failed-prepare', kind: 'prepare', status: 'failed', error}],
    upload: {id: 'failed-prepare', filename: 'oversize.imscc', size: 100, status: 'failed',
      message: 'Inspecting local content.', error}};
  await ui.render(failed);
  assert.equal(ui.get('local-job-error').hidden, false);
  assert.match(ui.get('local-job-error').textContent, /2 MiB/);
  assert.match(ui.get('local-job-error').textContent, /original file is unchanged/);
  assert.equal(ui.get('upload-feedback-status').textContent, error);
  await ui.render({...failed, jobs: [...failed.jobs,
    {id: 'retry-prepare', kind: 'prepare', status: 'completed'}], upload: {...failed.upload, status: 'ready'}});
  assert.equal(ui.get('local-job-error').hidden, true);
});

test('local build failure banner preserves approval and stays hidden for hosted jobs', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  const failed = {...ui.fixture, reviewed_draft_revision: 1, build_revision: 1, stage: 'build_approved',
    jobs: [{id: 'failed-build', kind: 'build', status: 'failed',
      error: 'Managed HAX CLI missing. Reinstall the local application.'}]};
  await ui.render(failed);
  assert.equal(ui.get('local-job-error').hidden, false);
  assert.match(ui.get('local-job-error').textContent, /Managed HAX CLI missing/);
  assert.match(ui.get('local-job-error').textContent, /approved draft is preserved/);
  assert.equal(ui.get('build-course').disabled, false);
  assert.equal(ui.evaluate('workspace.build_revision'), 1);
  ui.evaluate('localMode = false');
  await ui.render(failed);
  assert.equal(ui.get('local-job-error').hidden, true);
  ui.evaluate('localMode = true');
  await ui.render({...failed, active_job: {id: 'retry-build', kind: 'build'}});
  assert.equal(ui.get('local-job-error').hidden, true);
});

test('ambiguous local chunk responses require recovery and never start preparation', async t => {
  class XHR {
    constructor() { this.upload = {}; }
    open() {}
    setRequestHeader() {}
    send() {
      this.status = 200;
      this.responseText = JSON.stringify({offset: 0});
      this.onload();
    }
  }
  const ui = await harness({authenticated: true, session: localSession, XMLHttpRequest: XHR,
    request: url => url.endsWith('/local-uploads')
      ? jsonResponse({id: 'offset-copy', offset: 0, chunk_size: 8 * 1024 ** 2}) : undefined});
  t.after(ui.dispose);
  await assert.rejects(ui.context.uploadLocal({name: 'offset.imscc', size: 100, slice: () => ({})}),
    /offset could not be confirmed/);
  assert.equal(ui.evaluate('uploadFeedback.mustCheck'), true);
  assert.equal(ui.get('upload-course-button').disabled, true);
  assert(!ui.calls.some(call => call.url.endsWith('/complete')));
});

test('unavailable HAX status removes a previously enabled Open action', async t => {
  let fail = false;
  const ui = await harness({authenticated: true, session: localSession,
    request: url => {
      if (!url.endsWith('/hax')) return;
      if (fail) return {ok: false, status: 503, json: async () => ({error: 'Synthetic local runtime unavailable'})};
      return jsonResponse({running: true, phase: 'ready', url: 'http://127.0.0.1:8123/'});
    }});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, artifact: {hax_site: {directory: '/synthetic/site'}}});
  await ui.settle();
  assert.equal(ui.get('open-hax-site').hidden, false);
  fail = true;
  await ui.context.refreshHaxSite();
  assert.equal(ui.get('open-hax-site').hidden, true);
  assert.match(ui.get('hax-site-error').textContent, /runtime unavailable/);
});

test('local session clearly separates verified local accounts and browser fallback from hosted storage', async t => {
  const ui = await harness({authenticated: true, session: {...localSession, local_auth_configurable: true}});
  t.after(ui.dispose);
  assert.equal(ui.get('local-auth-setup').hidden, false);
  assert.match(ui.get('workspace-storage-help').textContent, /Browser-local fallback/);
  assert.match(ui.get('workspace-storage-help').textContent, /not a verified email account/);
  assert.equal(ui.get('local-upload-help').hidden, false);
  assert.equal(ui.get('hosted-upload-help').hidden, true);
  assert.equal(ui.get('build-course').textContent, 'Build & open HAX site');
  ui.context.fetch = async () => jsonResponse({...localSession, csrf: 'fixture', authenticated: true,
    credentials: [], local_accounts: true, account_required: true, account: {email: 'faculty@example.test'}});
  await ui.context.updateCredentials();
  assert.match(ui.get('workspace-storage-help').textContent, /Account-scoped local workspaces/);
  assert.match(ui.get('account-login-help').textContent, /another computer will not have these files/);
  assert.equal(ui.get('account-label').textContent, 'Signed in as faculty@example.test');
});

test('large local imports slice bounded chunks and send no cloud upload requests', async t => {
  const chunks = [];
  const slices = [];
  class XHR {
    constructor() { this.upload = {}; this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(chunk) {
      chunks.push(this);
      assert.equal(chunk.end - chunk.start <= 8 * 1024 ** 2, true);
      this.upload.onprogress({loaded: chunk.end - chunk.start});
      this.status = 200;
      this.responseText = JSON.stringify({offset: chunk.end});
      this.onload();
    }
  }
  const ui = await harness({authenticated: true, session: localSession, XMLHttpRequest: XHR,
    request: (url, options, context) => {
      if (url.endsWith('/local-uploads')) return jsonResponse({
        id: 'copy-one', filename: 'synthetic.imscc', size: 20 * 1024 ** 2, offset: 0, chunk_size: 8 * 1024 ** 2,
      });
      if (url.endsWith('/complete')) return jsonResponse({...context.serverWorkspace, stage: 'preparing',
        active_job: {id: 'prepare-one', kind: 'prepare'},
        upload: {id: 'copy-one', filename: 'synthetic.imscc', size: 20 * 1024 ** 2, status: 'processing', phase: 'inspect'}});
    }});
  t.after(ui.dispose);
  const file = {name: 'synthetic.imscc', size: 20 * 1024 ** 2, slice(start, end) {
    slices.push([start, end]); return {start, end};
  }};
  await ui.context.uploadLocal(file);
  assert.equal(slices.length, 3);
  assert.deepEqual(slices.at(-1), [16 * 1024 ** 2, 20 * 1024 ** 2]);
  assert(chunks.every(xhr => xhr.method === 'PUT' && xhr.headers['X-Mentor-CSRF'] === 'fixture' &&
    xhr.headers['Content-Type'] === 'application/octet-stream'));
  assert(chunks.every((xhr, i) => xhr.url.endsWith('?offset=' + slices[i][0])));
  assert(ui.calls.some(call => call.url.endsWith('/complete') && call.method === 'POST'));
  assert(ui.calls.every(call => call.url.startsWith('/mentor/api/')));
  assert(ui.calls.every(call => !call.url.endsWith('/uploads')));
  assert.equal(ui.evaluate('workspace.active_job.kind'), 'prepare');
  assert.match(ui.get('upload-feedback-status').textContent, /computer/);
});

test('local imports reject files beyond 8 GiB without reading or reserving them', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  const before = ui.calls.length;
  await assert.rejects(ui.context.uploadLocal({name: 'too-large.imscc', size: 8 * 1024 ** 3 + 1}),
    /8 GiB/);
  assert.equal(ui.calls.length, before);
});

test('reloaded durable local copies prioritize cancellation before accepting new inputs', async t => {
  const ui = await harness({authenticated: true, session: localSession});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, upload: {id: 'reloaded-copy', filename: 'saved.imscc',
    size: 100, offset: 80, status: 'awaiting_upload'}});
  assert.equal(ui.evaluate('uploadFeedback'), null);
  assert.equal(ui.evaluate('getFlowState().target'), 'upload-feedback');
  assert.equal(ui.get('abort-upload').hidden, false);
  assert.equal(ui.get('upload-course-button').disabled, true);
  assert.equal(ui.get('import-local-source').disabled, true);
});

test('interrupted local copies check their offset and remain cancelable without a preparation job', async t => {
  const ui = await harness({authenticated: true, session: localSession,
    request: (url, options) => url.endsWith('/saved-copy')
      ? jsonResponse({id: 'saved-copy', offset: 80, size: 100, filename: 'saved.imscc',
        status: options.method === 'DELETE' ? 'cancelled' : 'uploading'}) : undefined});
  t.after(ui.dispose);
  ui.evaluate(`uploadFeedback = {workspaceId: workspace.id, uploadId: 'saved-copy',
    controller: new AbortController(), phase: 'error', mustCheck: true}; renderFlow()`);
  assert.equal(ui.get('abort-upload').hidden, false);
  await ui.context.checkSavedUploadProgress();
  assert.equal(ui.evaluate('uploadFeedback.loaded'), 80);
  assert.equal(ui.evaluate('uploadPresentation().phase'), 'awaiting_upload');
  assert.equal(ui.get('abort-upload').hidden, false);
  ui.context.abortHostedUpload();
  await ui.settle();
  assert(ui.calls.some(call => call.url.endsWith('/saved-copy') && call.method === 'DELETE'));
});

test('canceling a local XHR copy deletes the reservation and never completes it', async t => {
  let copying;
  const started = new Promise(resolve => { copying = resolve; });
  class XHR {
    constructor() { this.upload = {}; }
    open() {}
    setRequestHeader() {}
    send() { copying(); }
    abort() { this.onabort(); }
  }
  const ui = await harness({authenticated: true, session: localSession, XMLHttpRequest: XHR,
    request: (url, options) => {
      if (url.endsWith('/local-uploads')) return jsonResponse({id: 'cancel-copy', offset: 0, chunk_size: 8 * 1024 ** 2});
      if (options.method === 'DELETE') return jsonResponse({id: 'cancel-copy', status: 'canceled'});
    }});
  t.after(ui.dispose);
  const pending = ui.context.uploadLocal({name: 'cancel.imscc', size: 100, slice: () => ({})});
  await started;
  ui.context.abortHostedUpload();
  await pending;
  assert(ui.calls.some(call => call.url.endsWith('/cancel-copy') && call.method === 'DELETE'));
  assert(!ui.calls.some(call => call.url.endsWith('/complete')));
  assert.equal(ui.evaluate('uploadController'), null);
  assert.equal(ui.evaluate('uploadFeedback.phase'), 'canceled');
});

test('startup file import sends only its registered id and preserves approval gates', async t => {
  const ui = await harness({authenticated: true, session: {...localSession,
    local_source: {id: 'registered-source', filename: 'startup.imscc', size: 100}}});
  t.after(ui.dispose);
  assert.equal(ui.get('local-source-choice').hidden, false);
  ui.context.importLocalSource();
  await ui.settle();
  const call = ui.calls.find(call => call.url.endsWith('/local-source'));
  assert.deepEqual(JSON.parse(call.body), {revision: 1, source_id: 'registered-source'});
  assert(!call.body.includes('path'));
  assert.equal(ui.get('approve-build').disabled, true);
  assert.equal(ui.get('build-course').disabled, true);
});

test('HAX status only opens verified loopback URLs and does not edit draft, brief or chat', async t => {
  let status = {running: false, phase: 'starting', url: 'http://127.0.0.1:8123/'};
  const ui = await harness({authenticated: true, session: localSession,
    request: url => url.endsWith('/hax') ? jsonResponse(status) : undefined});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, stage: 'built', artifact: {hax_site: {directory: '/synthetic/site-v1'}}});
  await ui.settle();
  ui.get('design-brief').value = 'Dirty brief';
  ui.get('draft-title-0').value = 'Dirty title';
  ui.get('mentor-message').value = 'Unsent message';
  assert.equal(ui.get('open-hax-site').hidden, true);
  status = {running: true, phase: 'ready', url: 'http://127.0.0.1:8123/'};
  await ui.context.refreshHaxSite();
  assert.equal(ui.get('open-hax-site').hidden, false);
  assert.equal(ui.get('open-hax-site').href, status.url);
  assert.equal(ui.get('start-hax-site').textContent, 'Restart HAX site');
  assert.equal(ui.get('design-brief').value, 'Dirty brief');
  assert.equal(ui.get('draft-title-0').value, 'Dirty title');
  assert.equal(ui.get('mentor-message').value, 'Unsent message');
  status = {running: true, phase: 'ready', url: 'https://external.example.test/'};
  await ui.context.refreshHaxSite();
  assert.equal(ui.get('open-hax-site').hidden, true);
  assert.equal(ui.calls.filter(call => /\/hax\/(start|stop)$/.test(call.url)).length, 0,
    'Viewing or polling a saved site must never start it');
});

test('HAX restart explicitly stops then starts without rebuilding or granting approval', async t => {
  const ui = await harness({authenticated: true, session: localSession,
    request: url => /\/hax(?:\/start|\/stop)?$/.test(url)
      ? jsonResponse({running: !url.endsWith('/stop'), phase: 'ready', url: 'http://localhost:8123/'}) : undefined});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, stage: 'built', artifact: {hax_site: {directory: '/synthetic/site-v1'}}});
  await ui.settle();
  const before = ui.calls.length;
  await ui.context.startHaxSite();
  assert.deepEqual(ui.calls.slice(before).map(call => [call.method, call.url]), [
    ['POST', '/mentor/api/workspaces/workspace-one/hax/stop'],
    ['POST', '/mentor/api/workspaces/workspace-one/hax/start'],
  ]);
  assert.equal(ui.evaluate('workspace.build_revision'), undefined);
  await ui.context.stopHaxSite();
  assert.equal(ui.get('open-hax-site').hidden, true);
});

test('late HAX polling cannot expose a different workspace site URL', async t => {
  let release;
  let delayed = false;
  const ui = await harness({authenticated: true, session: localSession,
    request: url => {
      if (!url.endsWith('/hax')) return;
      if (delayed) return new Promise(resolve => { release = resolve; });
      return jsonResponse({running: false, phase: 'stopped'});
    }});
  t.after(ui.dispose);
  await ui.render({...ui.fixture, artifact: {hax_site: {directory: '/synthetic/site-one'}}});
  await ui.settle();
  delayed = true;
  const pending = ui.context.refreshHaxSite();
  await ui.render({...ui.fixture, id: 'workspace-two'});
  release(jsonResponse({running: true, url: 'http://127.0.0.1:8123/', phase: 'ready'}));
  await pending;
  assert.equal(ui.get('hax-site-panel').hidden, true);
  assert.equal(ui.evaluate('workspace.id'), 'workspace-two');
});
