/*
 * Course Consultant collector. Injected into the Canvas tab only when the
 * instructor clicks "Read this course". It reads course content through the
 * Canvas REST API using the instructor's existing session (same-origin GET
 * requests), and returns one bundle for Course Consultant.
 *
 * Guarantees, enforced in this file:
 *   - GET only, same origin only.
 *   - Only allowlisted course-content endpoints (see ALLOWED). Never users,
 *     enrollments, submissions, grades, analytics or discussion entries.
 *   - Only the fields Course Consultant uses are kept (see KEEP).
 */
(function (root) {
  'use strict';

  const MAX_REQUESTS = 2000;
  const CONCURRENCY = 4;
  const PER_PAGE = 100;

  // Allowed API paths (after the origin, without query string).
  const ALLOWED = [
    /^\/api\/v1\/courses\/\d+$/,
    /^\/api\/v1\/courses\/\d+\/modules$/,
    /^\/api\/v1\/courses\/\d+\/pages$/,
    /^\/api\/v1\/courses\/\d+\/pages\/[^/?#]+$/,
    /^\/api\/v1\/courses\/\d+\/assignments$/,
    /^\/api\/v1\/courses\/\d+\/assignment_groups$/,
    /^\/api\/v1\/courses\/\d+\/quizzes$/,
    /^\/api\/v1\/courses\/\d+\/quizzes\/\d+\/questions$/,
    /^\/api\/v1\/courses\/\d+\/discussion_topics$/,
    /^\/api\/v1\/courses\/\d+\/outcome_group_links$/,
    /^\/api\/v1\/courses\/\d+\/files$/,
    /^\/api\/v1\/courses\/\d+\/late_policy$/,
    /^\/api\/quiz\/v1\/courses\/\d+\/quizzes\/\d+\/items$/,
  ];

  // Fields kept from each response type; everything else is dropped here.
  const KEEP = {
    course: ['id', 'name', 'course_code', 'syllabus_body', 'apply_assignment_group_weights', 'term'],
    term: ['name'],
    module: ['id', 'name', 'position', 'require_sequential_progress', 'prerequisite_module_ids', 'items'],
    moduleItem: ['id', 'title', 'type', 'content_id', 'page_url', 'external_url', 'position', 'completion_requirement'],
    requirement: ['type', 'min_score'],
    page: ['page_id', 'url', 'title', 'body', 'published', 'front_page'],
    assignment: ['id', 'name', 'description', 'points_possible', 'due_at', 'submission_types', 'grading_type',
      'assignment_group_id', 'peer_reviews', 'group_category_id', 'allowed_attempts', 'published',
      'rubric', 'rubric_settings', 'quiz_id', 'discussion_topic', 'is_quiz_lti_assignment'],
    criterion: ['id', 'points', 'description', 'long_description', 'outcome_id', 'ratings'],
    rating: ['points', 'description', 'long_description'],
    rubricSettings: ['id', 'title', 'points_possible'],
    discussionRef: ['id'],
    group: ['id', 'name', 'position', 'group_weight'],
    quiz: ['id', 'title', 'description', 'quiz_type', 'time_limit', 'allowed_attempts', 'points_possible',
      'assignment_id', 'published'],
    question: ['question_text', 'question_type', 'points_possible'],
    newQuizItem: ['points_possible', 'entry'],
    newQuizEntry: ['item_body', 'interaction_type_slug'],
    topic: ['id', 'title', 'message', 'published', 'is_announcement', 'assignment_id'],
    outcome: ['id', 'title', 'display_name', 'description', 'mastery_points'],
    file: ['id', 'display_name', 'filename', 'size', 'content-type'],
    latePolicy: ['late_submission_deduction_enabled', 'late_submission_deduction', 'late_submission_interval',
      'late_submission_minimum_percent_enabled', 'late_submission_minimum_percent',
      'missing_submission_deduction_enabled', 'missing_submission_deduction'],
  };

  function objects(list) {
    return Array.isArray(list) ? list.filter(x => x && typeof x === 'object' && !Array.isArray(x)) : [];
  }

  function pick(value, keys) {
    if (!value || typeof value !== 'object') return null;
    const out = {};
    for (const key of keys) if (key in value) out[key] = value[key];
    return out;
  }

  const clean = {
    course(c) {
      const out = pick(c, KEEP.course);
      if (out && out.term) out.term = pick(out.term, KEEP.term);
      return out;
    },
    module(m) {
      const out = pick(m, KEEP.module);
      out.items = (Array.isArray(m.items) ? m.items : []).map(item => {
        const kept = pick(item, KEEP.moduleItem);
        if (kept.completion_requirement) kept.completion_requirement = pick(kept.completion_requirement, KEEP.requirement);
        return kept;
      });
      return out;
    },
    assignment(a) {
      const out = pick(a, KEEP.assignment);
      if (Array.isArray(out.rubric)) {
        out.rubric = out.rubric.map(c => {
          const kept = pick(c, KEEP.criterion);
          kept.ratings = (Array.isArray(c.ratings) ? c.ratings : []).map(r => pick(r, KEEP.rating));
          return kept;
        });
      }
      if (out.rubric_settings) out.rubric_settings = pick(out.rubric_settings, KEEP.rubricSettings);
      if (out.discussion_topic) out.discussion_topic = pick(out.discussion_topic, KEEP.discussionRef);
      return out;
    },
    newQuizItem(i) {
      const out = pick(i, KEEP.newQuizItem);
      if (out.entry) out.entry = pick(out.entry, KEEP.newQuizEntry);
      return out;
    },
  };

  class CollectError extends Error {
    constructor(message) { super(message); this.name = 'CollectError'; }
  }

  function createClient({ origin, fetchImpl, onRequest }) {
    let requests = 0;
    function check(url) {
      const parsed = new URL(url, origin);
      if (parsed.origin !== origin) throw new CollectError('Refusing a request to another site.');
      if (!ALLOWED.some(pattern => pattern.test(parsed.pathname))) {
        throw new CollectError(`Refusing a request outside course content: ${parsed.pathname}`);
      }
      return parsed;
    }
    async function getOnce(url) {
      const parsed = check(url);
      if (++requests > MAX_REQUESTS) throw new CollectError('This course needs too many requests to read.');
      if (onRequest) onRequest(parsed);
      const response = await fetchImpl(parsed.href, {
        method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' },
      });
      if (!response.ok) {
        const error = new CollectError(`Canvas returned ${response.status} for ${parsed.pathname}`);
        error.status = response.status;
        throw error;
      }
      const text = (await response.text()).replace(/^while\(1\);/, '');
      return { data: JSON.parse(text), link: response.headers.get('Link') || '' };
    }
    async function get(path, params = {}) {
      const url = new URL(path, origin);
      for (const [key, value] of Object.entries(params)) {
        for (const v of [].concat(value)) url.searchParams.append(key, v);
      }
      return (await getOnce(url.href)).data;
    }
    async function getAll(path, params = {}) {
      const url = new URL(path, origin);
      url.searchParams.set('per_page', String(PER_PAGE));
      for (const [key, value] of Object.entries(params)) {
        for (const v of [].concat(value)) url.searchParams.append(key, v);
      }
      const rows = [];
      let next = url.href;
      while (next) {
        const { data, link } = await getOnce(next);
        if (!Array.isArray(data)) throw new CollectError(`Unexpected response for ${path}`);
        rows.push(...objects(data));
        const match = link.split(',').find(part => /rel="next"/.test(part));
        next = match ? match.slice(match.indexOf('<') + 1, match.indexOf('>')) : null;
      }
      return rows;
    }
    return { get, getAll, count: () => requests };
  }

  async function pool(items, worker) {
    const results = new Array(items.length);
    let index = 0;
    async function run() {
      while (index < items.length) {
        const current = index++;
        results[current] = await worker(items[current], current);
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, items.length) }, run));
    return results;
  }

  /**
   * Collect a course. Returns the bundle for Course Consultant.
   * options: {origin, courseId, fetchImpl?, onProgress?, onRequest?}
   */
  async function collect(options) {
    const origin = options.origin;
    const courseId = String(options.courseId);
    if (!/^\d+$/.test(courseId)) throw new CollectError('Open a Canvas course first.');
    const fetchImpl = options.fetchImpl || ((url, init) => fetch(url, init));
    const progress = options.onProgress || (() => {});
    const api = createClient({ origin, fetchImpl, onRequest: options.onRequest });
    const base = `/api/v1/courses/${courseId}`;
    const warnings = [];

    async function optional(label, task, fallback) {
      try { return await task(); } catch (error) {
        if (error instanceof CollectError && !error.status) throw error; // allowlist violations are fatal
        warnings.push(`${label} could not be read (${error.status || 'error'}).`);
        return fallback;
      }
    }

    progress('course');
    let course;
    try {
      course = clean.course(await api.get(base, { 'include[]': ['syllabus_body', 'term'] }));
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        throw new CollectError('Canvas did not allow reading this course. Check that you are signed in as its instructor.');
      }
      throw error;
    }

    progress('modules');
    const modules = (await optional('Modules', () => api.getAll(`${base}/modules`, { 'include[]': 'items' }), []))
      .filter(x => x && typeof x === 'object').map(clean.module);

    progress('pages');
    const pageList = await optional('Pages', () => api.getAll(`${base}/pages`), []);
    const pages = (await pool(pageList.slice(0, 500), page => optional(
      `Page "${String(page.title || '').slice(0, 80)}"`,
      () => api.get(`${base}/pages/${encodeURIComponent(page.url)}`), null)))
      .filter(Boolean).map(p => pick(p, KEEP.page));

    progress('assignments');
    const assignments = (await optional('Assignments', () => api.getAll(`${base}/assignments`), []))
      .map(clean.assignment);
    const groups = (await optional('Assignment groups', () => api.getAll(`${base}/assignment_groups`), []))
      .map(g => pick(g, KEEP.group));

    progress('quizzes');
    const quizList = (await optional('Quizzes', () => api.getAll(`${base}/quizzes`), []));
    const quizzes = await pool(quizList.slice(0, 500), async quiz => {
      const kept = pick(quiz, KEEP.quiz);
      kept.questions = (await optional(`Questions for "${String(quiz.title || '').slice(0, 80)}"`,
        () => api.getAll(`${base}/quizzes/${Number(quiz.id)}/questions`), []))
        .map(q => pick(q, KEEP.question));
      return kept;
    });
    const newQuizAssignments = assignments.filter(a => a.is_quiz_lti_assignment === true).slice(0, 500);
    const newQuizzes = (await pool(newQuizAssignments, async assignment => {
      const items = await optional(`New Quiz "${String(assignment.name || '').slice(0, 80)}"`,
        () => api.getAll(`/api/quiz/v1/courses/${courseId}/quizzes/${Number(assignment.id)}/items`), null);
      return items ? { assignment_id: assignment.id, items: items.map(clean.newQuizItem) } : null;
    })).filter(Boolean);

    progress('discussions');
    const topics = (await optional('Discussions', () => api.getAll(`${base}/discussion_topics`), []))
      .map(t => pick(t, KEEP.topic)).filter(t => t.is_announcement !== true);

    progress('outcomes');
    const outcomeLinks = await optional('Outcomes',
      () => api.getAll(`${base}/outcome_group_links`, { outcome_style: 'full' }), []);
    const outcomes = outcomeLinks.map(link => pick(link.outcome, KEEP.outcome)).filter(Boolean);

    progress('files');
    const files = (await optional('File list', () => api.getAll(`${base}/files`), []))
      .map(f => pick(f, KEEP.file));
    const latePolicy = await optional('Late policy', async () => {
      const data = await api.get(`${base}/late_policy`);
      return pick(data && data.late_policy, KEEP.latePolicy);
    }, null);

    progress('done');
    return {
      version: 1,
      collected_at: new Date().toISOString(),
      canvas_origin: origin,
      course, modules, pages, assignments, assignment_groups: groups, quizzes,
      new_quizzes: newQuizzes, discussion_topics: topics, outcomes, files,
      late_policy: latePolicy, warnings,
      request_count: api.count(),
    };
  }

  /** Short preview for the popup: counts and titles, no content. */
  function preview(bundle) {
    const titles = list => list.map(x => x.title || x.name).filter(Boolean);
    return {
      course: bundle.course && bundle.course.name,
      counts: {
        modules: bundle.modules.length, pages: bundle.pages.length,
        assignments: bundle.assignments.filter(a => !a.quiz_id && !a.discussion_topic).length,
        quizzes: bundle.quizzes.length + bundle.new_quizzes.length,
        discussions: bundle.discussion_topics.length,
        rubrics: bundle.assignments.filter(a => Array.isArray(a.rubric) && a.rubric.length).length,
        outcomes: bundle.outcomes.length, files: bundle.files.length,
      },
      items: [...titles(bundle.pages), ...titles(bundle.assignments), ...titles(bundle.quizzes),
        ...titles(bundle.discussion_topics)].slice(0, 400),
      warnings: bundle.warnings,
    };
  }

  const api = { collect, preview, ALLOWED, KEEP, CollectError };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CourseCollector = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
