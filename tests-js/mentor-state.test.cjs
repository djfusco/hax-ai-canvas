'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

async function harness({authenticated = false, inventory = {course_items: 1, materials: [], counts: {}}} = {}) {
  const elements = new Map();
  const calls = [];
  function element(tag = 'div') {
    return {
      tag, tagName: tag.toUpperCase(), children: [], value: '', style: {}, files: [], dataset: {},
      set id(value) { this._id = value; elements.set(value, this); },
      get id() { return this._id; },
      replaceChildren(...children) { this.children = children; },
      appendChild(child) { this.children.push(child); },
      setAttribute() {},
      removeAttribute() {},
      scrollIntoView() {},
      focus() { document.activeElement = this; },
      querySelector() { return element('button'); },
      add(child) { this.children.push(child); },
    };
  }
  const document = {
    activeElement: null,
    createElement: element,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
  };
  const context = vm.createContext({
    document, console, FormData, AbortController, window: {matchMedia: () => ({matches: true})},
    Option: function(text, value) { return {text, value}; },
    setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push({url, method: options.method});
      let data;
      if (url.endsWith('/session')) data = {
        csrf: 'fixture', hosted_ready: true, login_required: !authenticated,
        authenticated, credentials: [],
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
