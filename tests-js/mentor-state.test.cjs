'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

async function harness() {
  const elements = new Map();
  function element(tag = 'div') {
    return {
      tag, children: [], value: '', style: {}, files: [],
      set id(value) { this._id = value; elements.set(value, this); },
      get id() { return this._id; },
      replaceChildren(...children) { this.children = children; },
      appendChild(child) { this.children.push(child); },
      setAttribute() {},
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
    document, console, FormData, AbortController, window: {},
    setTimeout, clearTimeout,
    fetch: async (url, options) => {
      let data;
      if (url.endsWith('/session')) data = {
        csrf: 'fixture', hosted_ready: true, login_required: true,
        authenticated: false, credentials: [],
      };
      else if (url.endsWith('/inventory')) data = {course_items: 1, materials: [], counts: {}};
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
  return {context, document, fixture, render, settle,
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
