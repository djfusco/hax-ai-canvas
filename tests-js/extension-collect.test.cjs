'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const Collector = require('../extension/collect.js');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.PYTHON || 'python3';
const ORIGIN = 'https://canvas.example.edu';
const FIXTURE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests/fixtures/extension_bundle.json'), 'utf8'));

// A fake Canvas built from the same course as the Python fixture, with extra
// fields Canvas returns that the collector must drop.
function fakeCanvas(overrides = {}) {
  const c = '/api/v1/courses/101';
  const routes = {
    [c]: { ...FIXTURE.course, students: [{ name: 'Student One' }], total_students: 30 },
    [`${c}/modules`]: FIXTURE.modules,
    [`${c}/pages`]: FIXTURE.pages.map(({ body, ...rest }) => rest),
    [`${c}/pages/welcome`]: { ...FIXTURE.pages[0], last_edited_by: { display_name: 'Ana Lopez' } },
    [`${c}/assignments`]: FIXTURE.assignments.map(a => ({ ...a, needs_grading_count: 7, has_submitted_submissions: true })),
    [`${c}/assignment_groups`]: FIXTURE.assignment_groups,
    [`${c}/quizzes`]: FIXTURE.quizzes.map(({ questions, ...rest }) => rest),
    [`${c}/quizzes/31/questions`]: FIXTURE.quizzes[0].questions.map(q => ({ ...q, answers: [{ text: 'Nucleus', weight: 100 }] })),
    '/api/quiz/v1/courses/101/quizzes/205/items': FIXTURE.new_quizzes[0].items,
    [`${c}/discussion_topics`]: FIXTURE.discussion_topics.map(t => ({ ...t, author: { display_name: 'Ana Lopez' } })),
    [`${c}/outcome_group_links`]: FIXTURE.outcomes.map(o => ({ outcome: { ...o, url: '/x' } })),
    [`${c}/files`]: FIXTURE.files.map(f => ({ ...f, url: 'https://files/secret-token' })),
    [`${c}/late_policy`]: { late_policy: { ...FIXTURE.late_policy } },
    ...overrides,
  };
  const requests = [];
  async function fetchImpl(url, init) {
    const parsed = new URL(url);
    requests.push({ url: parsed, init });
    const route = routes[parsed.pathname];
    if (route === undefined) return new Response('{"errors":[]}', { status: 404 });
    if (typeof route === 'function') return route(parsed);
    return new Response('while(1);' + JSON.stringify(route), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }
  return { fetchImpl, requests };
}

async function run(canvas) {
  return Collector.collect({ origin: ORIGIN, courseId: '101', fetchImpl: canvas.fetchImpl });
}

test('collects the course with only allowlisted GET requests', async () => {
  const canvas = fakeCanvas();
  const bundle = await run(canvas);
  assert.equal(bundle.version, 1);
  assert.equal(bundle.course.name, 'Biology 101: Cells');
  for (const { url, init } of canvas.requests) {
    assert.equal(url.origin, ORIGIN);
    assert.equal(init.method, 'GET');
    assert.equal(init.credentials, 'same-origin');
    assert.ok(Collector.ALLOWED.some(p => p.test(url.pathname)), url.pathname);
    assert.doesNotMatch(url.pathname, /users|enrollments|submissions|grades|entries|analytics/);
  }
  assert.equal(bundle.pages[0].body.includes('Welcome to cells'), true);
  assert.equal(bundle.quizzes[0].questions.length, 2);
  assert.equal(bundle.new_quizzes[0].items[0].entry.interaction_type_slug, 'hot-spot');
  assert.equal(bundle.outcomes.length, 2);
  assert.equal(bundle.late_policy.late_submission_deduction, 10);
  assert.deepEqual(bundle.warnings, []);
});

test('drops fields Course Consultant does not use', async () => {
  const bundle = await run(fakeCanvas());
  const text = JSON.stringify(bundle);
  for (const forbidden of ['Student One', 'total_students', 'needs_grading_count', 'has_submitted_submissions',
    'STUDENT REPLY', 'last_edited_by', 'Ana Lopez', 'secret-token', '"answers"', 'weight":100']) {
    assert.ok(!text.includes(forbidden), forbidden);
  }
  assert.equal(bundle.discussion_topics.length, 1, 'announcements are excluded');
});

test('follows pagination and strips the while(1) prefix', async () => {
  const c = '/api/v1/courses/101';
  const first = FIXTURE.assignments.slice(0, 3);
  const second = FIXTURE.assignments.slice(3);
  const canvas = fakeCanvas({
    [`${c}/assignments`]: url => url.searchParams.get('page') === '2'
      ? new Response('while(1);' + JSON.stringify(second))
      : new Response('while(1);' + JSON.stringify(first), {
        headers: { Link: `<${ORIGIN}${c}/assignments?page=2&per_page=100>; rel="next", <${ORIGIN}${c}/assignments?page=1>; rel="first"` },
      }),
  });
  const bundle = await run(canvas);
  assert.equal(bundle.assignments.length, FIXTURE.assignments.length);
});

test('refuses requests outside course content, even via pagination links', async () => {
  const c = '/api/v1/courses/101';
  for (const link of [`<${ORIGIN}/api/v1/users/self/profile>; rel="next"`,
    `<https://evil.example/api/v1/courses/101/assignments?page=2>; rel="next"`,
    `<${ORIGIN}${c}/discussion_topics/61/entries>; rel="next"`]) {
    const canvas = fakeCanvas({
      [`${c}/assignments`]: () => new Response(JSON.stringify(FIXTURE.assignments), { headers: { Link: link } }),
    });
    await assert.rejects(run(canvas), Collector.CollectError);
    assert.ok(!canvas.requests.some(r => /users|evil|entries/.test(r.url.href)));
  }
});

test('reports unreadable parts as warnings, and an unreadable course as an error', async () => {
  const c = '/api/v1/courses/101';
  const partial = await run(fakeCanvas({ [`${c}/quizzes/31/questions`]: () => new Response('{}', { status: 403 }) }));
  assert.equal(partial.quizzes[0].questions.length, 0);
  assert.match(partial.warnings[0], /Questions for "Cell quiz".*403/);
  await assert.rejects(run(fakeCanvas({ [c]: () => new Response('{}', { status: 401 }) })), /signed in as its instructor/);
  await assert.rejects(Collector.collect({ origin: ORIGIN, courseId: 'abc', fetchImpl: fetch }), /Open a Canvas course/);
});

test('collected bundle imports in Python', async () => {
  const bundle = await run(fakeCanvas());
  const preview = Collector.preview(bundle);
  assert.equal(preview.counts.quizzes, 2);
  assert.equal(preview.counts.rubrics, 1);
  const result = JSON.parse(execFileSync(PYTHON, ['-c', String.raw`
import json, sys
sys.path.insert(0, "python")
from canvas_api_import import import_api_bundle
model = import_api_bundle(json.load(sys.stdin))
print(json.dumps({"counts": model["provenance"]["counts"], "weighted": model["course"]["weighted_groups"],
                  "outcome_links": sorted(len(o["item_ids"]) for o in model["outcomes"])}))
`], { cwd: ROOT, input: JSON.stringify(bundle), encoding: 'utf8' }));
  assert.equal(result.counts.assignments, 3);
  assert.equal(result.counts.quizzes, 2);
  assert.equal(result.counts.discussions, 1);
  assert.equal(result.weighted, true);
  assert.deepEqual(result.outcome_links, [0, 1]);
});
