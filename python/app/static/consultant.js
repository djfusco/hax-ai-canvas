/* Course Consultant front end: sign-in, AI model, course intake, course map,
 * consultation and proposed changes. DOM is built with textContent only. */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const PROVIDER_NAMES = { openrouter: 'OpenRouter', anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Google Gemini' };
  const TOPIC_STATE = { not_started: 'not started', in_progress: 'in progress', covered: 'covered', skipped: 'skipped' };
  const AI_DIMENSIONS = {
    none: 'No AI component', resilient_assessment: 'AI-resilient assessment', integrated_learning: 'AI-integrated learning',
    course_tutor: 'Course AI tutor', instructor_tools: 'AI for the instructor',
  };
  const store = {
    get(key) { try { return localStorage.getItem('cc-' + key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem('cc-' + key, value); } catch (_) { /* optional */ } },
  };

  const state = {
    session: null, workspaces: [], workspace: null, provider: store.get('provider'), models: null,
    consult: null, map: null, scan: null, slimAbort: null, busy: false, tab: 'course', showResume: true,
  };

  // ── helpers ──────────────────────────────────────────────────────────────

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child);
    return node;
  }

  function status(text) { $('cc-status').textContent = text || ''; }
  function showError(message) {
    $('cc-error').textContent = message || '';
    $('cc-error').hidden = !message;
    if (message) $('cc-error').scrollIntoView({ block: 'nearest' });
  }

  async function api(path, options = {}) {
    const init = { credentials: 'same-origin', method: options.method || 'GET', headers: {} };
    if (init.method !== 'GET') init.headers['X-Mentor-CSRF'] = state.session ? state.session.csrf : '';
    if (options.json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.json);
    } else if (options.form) {
      init.body = options.form;
    }
    const response = await fetch(path, init);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  async function run(label, task) {
    if (state.busy) return;
    state.busy = true;
    showError('');
    status(label);
    document.body.setAttribute('aria-busy', 'true');
    try { await task(); status(''); } catch (error) { status(''); showError(error.message); } finally {
      state.busy = false;
      document.body.removeAttribute('aria-busy');
    }
  }

  function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

  function bytes(n) {
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  /** Minimal, safe rendering of the consultant's text: paragraphs, bullets, **bold**. */
  function richText(text) {
    const fragment = document.createDocumentFragment();
    let list = null;
    const inline = line => {
      const parts = line.split(/(\*\*[^*]+\*\*)/g).filter(Boolean);
      return parts.map(part => part.startsWith('**') && part.endsWith('**') ? el('strong', { text: part.slice(2, -2) }) : part);
    };
    for (const raw of String(text).split('\n')) {
      const line = raw.trim();
      const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        if (!list) { list = el('ul', { class: 'cc-list' }); fragment.append(list); }
        list.append(el('li', {}, inline(bullet[1])));
      } else {
        list = null;
        if (line) fragment.append(el('p', {}, inline(line)));
      }
    }
    return fragment;
  }

  // ── session and sign-in ──────────────────────────────────────────────────

  async function loadSession() {
    state.session = await api('/mentor/api/session');
    const s = state.session;
    const needsInvite = s.invite_required;
    const needsAccount = !needsInvite && (s.account_required || s.login_required);
    $('cc-signin').hidden = !(needsInvite || needsAccount);
    $('cc-invite-form').hidden = !needsInvite;
    $('cc-email-form').hidden = !needsAccount;
    $('cc-app').hidden = needsInvite || needsAccount;
    return !(needsInvite || needsAccount);
  }

  $('cc-invite-form').addEventListener('submit', event => {
    event.preventDefault();
    run('Checking invitation…', async () => {
      await api('/mentor/api/login', { method: 'POST', json: { code: $('cc-invite').value } });
      await start();
    });
  });
  $('cc-email-form').addEventListener('submit', event => {
    event.preventDefault();
    run('Sending code…', async () => {
      await api('/mentor/api/auth/send-code', { method: 'POST', json: { email: $('cc-email').value.trim() } });
      $('cc-code-form').hidden = false;
      $('cc-code').focus();
    });
  });
  $('cc-code-form').addEventListener('submit', event => {
    event.preventDefault();
    run('Signing in…', async () => {
      await api('/mentor/api/auth/verify-code', { method: 'POST',
        json: { email: $('cc-email').value.trim(), code: $('cc-code').value.trim() } });
      await start();
    });
  });

  // ── AI provider and model ────────────────────────────────────────────────

  function configured(provider) {
    return (state.session.credentials || []).some(c => c.provider === provider && c.configured);
  }

  function renderAi() {
    const provider = state.provider;
    for (const card of document.querySelectorAll('.cc-provider')) {
      card.setAttribute('aria-checked', String(card.dataset.provider === provider));
    }
    const ready = provider && state.models && state.models.selected;
    $('cc-ai-dot').classList.toggle('ready', Boolean(ready));
    $('cc-ai-label').textContent = ready ? `${PROVIDER_NAMES[provider]} · ${state.models.selected}` : 'Choose an AI model';
    $('cc-ai-connect').hidden = !provider || configured(provider);
    $('cc-openrouter-connect').hidden = provider !== 'openrouter';
    $('cc-key-form').hidden = !provider || provider === 'openrouter';
    $('cc-models').hidden = !provider || !configured(provider);
    if (provider && configured(provider) && state.models) {
      const select = $('cc-model');
      select.replaceChildren(el('option', { value: '', text: state.models.models.length ? 'Choose a model…' : 'Refresh to list models' }));
      for (const model of state.models.models) {
        const price = model.pricing && model.pricing.input_per_million !== undefined
          ? ` · $${model.pricing.input_per_million}/$${model.pricing.output_per_million} per M tokens` : '';
        select.append(el('option', { value: model.id, text: (model.name || model.id) + price, selected: model.id === state.models.selected }));
      }
      const capabilities = state.models.selected_capabilities;
      $('cc-model-note').textContent = capabilities
        ? `${capabilities.structured_outputs ? 'Supports structured answers. ' : 'Structured answers not verified; responses are checked strictly. '}`
          + (capabilities.input_limit ? `Reads up to about ${Math.round(capabilities.input_limit / 1000)}k tokens.` : '')
        : (state.models.expired ? 'The model list has expired; refresh it to change models.' : '');
    }
  }

  async function loadModels() {
    state.models = null;
    if (state.provider && configured(state.provider)) {
      state.models = await api(`/mentor/api/providers/${state.provider}/models`);
      if (!state.models.models.length || state.models.expired) {
        state.models = await api(`/mentor/api/providers/${state.provider}/models/refresh`, { method: 'POST', json: {} });
      }
    }
    renderAi();
  }

  function aiReady() { return Boolean(state.provider && state.models && state.models.selected); }

  function requireAi() {
    if (aiReady()) return true;
    $('cc-ai').hidden = false;
    $('cc-ai-toggle').setAttribute('aria-expanded', 'true');
    throw new Error('Choose an AI provider and model first.');
  }

  $('cc-ai-toggle').addEventListener('click', () => {
    const open = $('cc-ai').hidden;
    $('cc-ai').hidden = !open;
    $('cc-ai-toggle').setAttribute('aria-expanded', String(open));
  });
  for (const card of document.querySelectorAll('.cc-provider')) {
    card.addEventListener('click', () => run('Loading models…', async () => {
      state.provider = card.dataset.provider;
      store.set('provider', state.provider);
      await loadModels();
    }));
  }
  $('cc-openrouter').addEventListener('click', () => run('Opening OpenRouter…', async () => {
    const { url } = await api('/mentor/api/openrouter/start', { method: 'POST', json: {} });
    window.location.assign(url);
  }));
  $('cc-key-form').addEventListener('submit', event => {
    event.preventDefault();
    run('Saving key…', async () => {
      await api('/mentor/api/credentials', { method: 'POST', json: {
        provider: state.provider, api_key: $('cc-key').value.trim(), remember: $('cc-remember').checked } });
      $('cc-key').value = '';
      await loadSession();
      await loadModels();
    });
  });
  $('cc-model-refresh').addEventListener('click', () => run('Refreshing models…', async () => {
    state.models = await api(`/mentor/api/providers/${state.provider}/models/refresh`, { method: 'POST', json: {} });
    renderAi();
  }));
  $('cc-model-use').addEventListener('click', () => run('Saving model…', async () => {
    const model = $('cc-model').value;
    if (!model) throw new Error('Choose a model from the list.');
    await api(`/mentor/api/providers/${state.provider}/model`, { method: 'PUT',
      json: { model, version: state.models.version } });
    state.models = await api(`/mentor/api/providers/${state.provider}/models`);
    renderAi();
    $('cc-ai').hidden = true;
    $('cc-ai-toggle').setAttribute('aria-expanded', 'false');
  }));
  $('cc-forget').addEventListener('click', () => run('Disconnecting…', async () => {
    await api(`/mentor/api/credentials/${state.provider}`, { method: 'DELETE' });
    await loadSession();
    await loadModels();
  }));

  // ── workspaces ───────────────────────────────────────────────────────────

  function renderWorkspaces() {
    const select = $('cc-workspace');
    select.replaceChildren(...state.workspaces.map(w =>
      el('option', { value: w.id, text: w.title, selected: state.workspace && w.id === state.workspace.id })));
    select.hidden = !state.workspaces.length;
  }

  async function loadWorkspaces() {
    const values = await api('/mentor/api/workspaces');
    state.workspaces = Array.isArray(values) ? values : (values.workspaces || []);
    if (!state.workspaces.length) {
      const created = await api('/mentor/api/workspaces', { method: 'POST', json: { title: 'My course' } });
      state.workspaces = [created];
    }
    const remembered = store.get('workspace');
    const id = (state.workspaces.find(w => w.id === remembered) || state.workspaces[0]).id;
    await openWorkspace(id);
  }

  async function openWorkspace(id) {
    state.workspace = await api(`/mentor/api/workspaces/${encodeURIComponent(id)}`);
    store.set('workspace', id);
    state.showResume = true; // "where we left off" greets a returning instructor once
    renderWorkspaces();
    await refreshCourse();
  }

  $('cc-workspace').addEventListener('change', event => run('Opening course…', () => openWorkspace(event.target.value)));
  $('cc-new').addEventListener('click', () => run('Creating…', async () => {
    const title = window.prompt('Name this course', 'New course');
    if (!title) return;
    const created = await api('/mentor/api/workspaces', { method: 'POST', json: { title: title.slice(0, 200) } });
    state.workspaces.unshift(created);
    await openWorkspace(created.id);
    selectTab('course');
  }));

  // ── tabs ─────────────────────────────────────────────────────────────────

  function selectTab(name) {
    state.tab = name;
    for (const tab of ['course', 'consult', 'plan']) {
      $('tab-' + tab).setAttribute('aria-selected', String(tab === name));
      $('view-' + tab).hidden = tab !== name;
    }
    if (name === 'consult') renderConsult();
    if (name === 'plan') renderPlan();
  }
  for (const tab of ['course', 'consult', 'plan']) $('tab-' + tab).addEventListener('click', () => selectTab(tab));

  // ── course intake ────────────────────────────────────────────────────────

  async function refreshCourse() {
    const course = state.workspace.course;
    $('cc-intake').hidden = Boolean(course);
    $('cc-course').hidden = !course;
    state.map = course ? await api(`/mentor/api/workspaces/${state.workspace.id}/course-map`) : null;
    state.consult = await api(`/mentor/api/workspaces/${state.workspace.id}/consult`);
    if (course) renderCourse();
    renderPlanCount();
    if (state.tab === 'consult') renderConsult();
    if (state.tab === 'plan') renderPlan();
  }

  function intakeForm() {
    const form = new FormData();
    form.append('revision', String(state.workspace.revision));
    form.append('instructor_name', $('cc-instructor').value.trim());
    return form;
  }

  async function afterImport(workspace) {
    state.workspace = workspace;
    const index = state.workspaces.findIndex(w => w.id === workspace.id);
    if (index >= 0) state.workspaces[index] = workspace;
    renderWorkspaces();
    resetSlim();
    await refreshCourse();
  }

  function resetSlim() {
    if (state.slimAbort) state.slimAbort.abort();
    state.scan = null;
    state.slimAbort = null;
    $('cc-slim').hidden = true;
    $('cc-export').value = '';
  }

  $('cc-export').addEventListener('change', () => {
    const file = $('cc-export').files[0];
    if (!file) return;
    showError('');
    state.slimAbort = new AbortController();
    $('cc-slim').hidden = false;
    $('cc-slim-import').hidden = true;
    $('cc-docs').hidden = true;
    $('cc-slim-progress').value = 0;
    $('cc-slim-text').textContent = `Reading ${file.name} (${bytes(file.size)}) on your computer…`;
    SlimExport.scanExport(file, {
      signal: state.slimAbort.signal,
      onProgress: p => {
        if (p.phase === 'reading' && p.total) $('cc-slim-progress').value = p.done / p.total;
      },
    }).then(scan => {
      state.scan = { file, scan };
      $('cc-slim-progress').value = 1;
      $('cc-slim-text').textContent = `Found ${scan.stats.keptEntries} course text files. ${scan.inventory.length} media files `
        + `(${bytes(scan.stats.skippedBytes)}) stay on your computer and are listed by name only.`;
      const list = $('cc-docs-list');
      list.replaceChildren(...scan.documents.map(doc => el('label', {},
        el('input', { type: 'checkbox', value: doc.path }), `${doc.name} (${bytes(doc.bytes)})`)));
      $('cc-docs').hidden = !scan.documents.length;
      $('cc-slim-import').hidden = false;
    }).catch(error => {
      if (!/cancelled/.test(error.message)) showError(error.message);
      resetSlim();
    });
  });
  $('cc-slim-cancel').addEventListener('click', resetSlim);
  $('cc-slim-import').addEventListener('click', () => run('Importing your course…', async () => {
    const { file, scan } = state.scan;
    const include = [...$('cc-docs-list').querySelectorAll('input:checked')].map(i => i.value);
    const { blob, summary } = await SlimExport.buildSlimPackage(file, scan, { includeDocuments: include });
    status(`Uploading ${bytes(summary.slimBytes)} (from a ${bytes(summary.archiveBytes)} export)…`);
    const form = intakeForm();
    form.append('package', blob, 'course-slim.zip');
    await afterImport(await api(`/mentor/api/workspaces/${state.workspace.id}/intake/export`, { method: 'POST', form }));
  }));
  $('cc-docs-form').addEventListener('submit', event => {
    event.preventDefault();
    run('Reading your documents…', async () => {
      const form = intakeForm();
      form.append('title', $('cc-docs-title').value.trim());
      form.append('notes', $('cc-docs-notes').value.trim());
      for (const file of $('cc-docs-files').files) form.append('documents', file);
      await afterImport(await api(`/mentor/api/workspaces/${state.workspace.id}/intake/documents`, { method: 'POST', form }));
    });
  });
  $('cc-replace').addEventListener('click', () => {
    if (!window.confirm('Replace this course? Your consultation notes are kept.')) return;
    $('cc-intake').hidden = false;
    $('cc-course').hidden = true;
  });
  $('cc-start-consult').addEventListener('click', () => selectTab('consult'));

  // ── course map ───────────────────────────────────────────────────────────

  function section(title, ...children) {
    return el('article', { class: 'cc-card' }, el('h2', { text: title }), ...children);
  }

  function renderCourse() {
    const course = state.workspace.course;
    const map = state.map;
    $('cc-course-title').textContent = course.title;
    const sourceLabel = { export: 'Canvas export', extension: 'Canvas (extension)', documents: 'Your documents' }[course.source] || course.source;
    const counts = course.counts || {};
    $('cc-course-meta').textContent = [sourceLabel, map.course.code, map.course.term,
      ['pages', 'assignments', 'quizzes', 'discussions'].filter(k => counts[k]).map(k => `${counts[k]} ${k}`).join(', ')]
      .filter(Boolean).join(' · ');
    const removed = course.privacy ? course.privacy.total : 0;
    $('cc-privacy').textContent = removed
      ? `We removed ${removed} likely student detail${removed === 1 ? '' : 's'} (such as emails and names in class posts) before saving.`
      : 'No student details were found to remove. Grades, submissions and student replies are never imported.';

    const container = $('cc-map');
    container.replaceChildren();
    const grid = el('div', { class: 'cc-map' });
    container.append(grid);

    // Grading
    const grading = map.grading;
    const rows = map.assessments.slice(0, 40).map(a => el('tr', {},
      el('td', {}, a.title, ' ', el('span', { class: 'cc-tag', text: a.type }),
        a.rubric ? ' ' : null, a.rubric ? el('span', { class: 'cc-tag', text: 'rubric' }) : null),
      el('td', { class: 'num', text: a.grade_share === null ? '–' : `${a.grade_share}%` }),
      el('td', {}, el('div', { class: 'cc-bar-track' },
        el('div', { class: 'cc-bar-fill', style: `width:${Math.min(100, (a.grade_share || 0) * 2)}%` })))));
    grid.append(section('How the grade is made up',
      el('p', { class: 'cc-muted', text: grading.weighted_groups
        ? 'Weighted groups: ' + grading.groups.filter(g => g.weight !== null).map(g => `${g.name} ${g.weight}%`).join(', ')
        : 'Total points' }),
      map.assessments.length ? el('table', { class: 'cc-table' },
        el('thead', {}, el('tr', {}, el('th', { text: 'Graded item' }), el('th', { class: 'num', text: 'Share' }), el('th', { text: '' }))),
        el('tbody', {}, rows)) : el('p', { text: 'No graded items were found.' }),
      map.assessment_summary.high_stakes.length
        ? el('p', {}, el('span', { class: 'cc-tag warn', text: 'High stakes' }), ' ' + map.assessment_summary.high_stakes.join(', ')) : null));

    // Workload
    if (map.workload.length) {
      // Fill empty weeks between the first and last due week so gaps are visible.
      const byWeek = Object.fromEntries(map.workload.map(w => [w.week, w]));
      const first = map.workload[0].week;
      const last = map.workload[map.workload.length - 1].week;
      const weeks = [];
      for (let n = first; n <= last && weeks.length < 40; n++) {
        weeks.push(byWeek[n] || { week: n, graded_items: 0, grade_share: 0, titles: [] });
      }
      const peak = Math.max(...weeks.map(w => w.grade_share || w.graded_items), 1);
      grid.append(section('Graded work by week',
        el('div', { class: 'cc-weeks', role: 'img', 'aria-label': map.workload.map(w =>
          `Week ${w.week}: ${w.graded_items} items, ${w.grade_share}% of grade`).join('; ') },
          weeks.map(w => el('div', { class: 'cc-week', title: `${w.titles.join(', ')}` },
            el('div', { style: `height:${Math.max(4, 90 * (w.grade_share || w.graded_items) / peak)}px` }), `W${w.week}`))),
        map.assessment_summary.undated.length
          ? el('p', { class: 'cc-muted', text: `No due date: ${map.assessment_summary.undated.join(', ')}` }) : null));
    }

    // Outcomes
    const outcomes = map.outcomes;
    grid.append(section('Learning outcomes',
      outcomes.length ? el('ul', { class: 'cc-list' }, outcomes.map(o => el('li', {}, o.title, ' ',
        el('span', { class: 'cc-tag' + (o.aligned_items.length + o.proposed_items.length ? '' : ' warn'),
          text: plural(o.aligned_items.length + o.proposed_items.length, 'linked item')
            + (o.proposed_items.length ? ' (proposed)' : '') }))))
        : el('p', { text: 'No learning outcomes were found in Canvas.' }),
      el('p', { class: 'cc-muted', text: map.alignment_status === 'proposed'
        ? 'Links marked “proposed” were suggested by AI; you’ll confirm them in the consultation.'
        : 'AI can propose which assignments address each outcome, or find outcomes stated in your syllabus.' }),
      el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Map outcomes with AI',
        onclick: () => run('Mapping outcomes…', async () => {
          requireAi();
          state.map = await api(`/mentor/api/workspaces/${state.workspace.id}/course-map/align`,
            { method: 'POST', json: { provider: state.provider } });
          renderCourse();
        }) })));

    // Structure and resources
    const resources = map.resources;
    const media = Object.entries(resources.media).map(([kind, v]) => `${v.files} ${kind} (${bytes(v.bytes)})`);
    grid.append(section('Structure and resources',
      el('ul', { class: 'cc-list' },
        el('li', { text: `${map.structure.modules.length} modules` + (map.structure.items_outside_modules
          ? `; ${map.structure.items_outside_modules} items outside modules` : '') }),
        media.length ? el('li', { text: 'Media: ' + media.join(', ') }) : null,
        Object.keys(resources.link_domains).length ? el('li', { text: 'Links to: ' + Object.keys(resources.link_domains).slice(0, 8).join(', ') }) : null,
        resources.external_tools.length ? el('li', { text: 'Tools: ' + resources.external_tools.slice(0, 8).join(', ') }) : null,
        ...resources.syllabus_readings.slice(0, 4).map(r => el('li', { text: 'Reading: ' + r })))));
  }

  // ── consultation ─────────────────────────────────────────────────────────

  function renderConsult() {
    const view = state.consult;
    if (!view) return;
    const hasCourse = Boolean(state.workspace && state.workspace.course);
    const consultState = view.state;
    const current = consultState ? consultState.current_topic : 'purpose';
    $('cc-topics').replaceChildren(...view.topics.map(topic => {
      const topicState = consultState ? consultState.topics[topic.id].status : 'not_started';
      return el('li', {}, el('button', { type: 'button', 'aria-current': String(topic.id === current),
        onclick: () => run('Switching topic…', async () => {
          state.consult = await api(`/mentor/api/workspaces/${state.workspace.id}/consult/settings`,
            { method: 'POST', json: { topic: topic.id } });
          renderConsult();
        }) }, el('span', { text: topic.label }), el('span', { class: 'cc-topic-state ' + topicState, text: TOPIC_STATE[topicState] })));
    }));
    $('cc-directness').value = consultState ? consultState.directness : 'balanced';
    const messages = $('cc-messages');
    messages.replaceChildren();
    if (!hasCourse) {
      messages.append(el('p', { class: 'cc-banner', text: 'Bring in your course first (step 1).' }));
    }
    if (state.showResume && view.resume && view.messages.length) {
      messages.append(el('div', { class: 'cc-msg assistant' }, richText(view.resume)));
    }
    for (const message of view.messages) {
      messages.append(el('div', { class: 'cc-msg ' + (message.role === 'user' ? 'user' : 'assistant') }, richText(message.content)));
    }
    const started = consultState && consultState.turns > 0;
    $('cc-begin').hidden = !hasCourse || started;
    $('cc-composer').hidden = !hasCourse || !started;
  }

  async function consultTurn(body, pendingText) {
    requireAi();
    const messages = $('cc-messages');
    if (pendingText) messages.append(el('div', { class: 'cc-msg user' }, richText(pendingText)));
    const pending = el('div', { class: 'cc-msg pending', text: 'The consultant is thinking…' });
    messages.append(pending);
    pending.scrollIntoView({ block: 'nearest' });
    try {
      state.consult = await api(`/mentor/api/workspaces/${state.workspace.id}/consult`,
        { method: 'POST', json: { provider: state.provider, ...body } });
      state.showResume = false;
    } finally {
      pending.remove();
    }
    renderConsult();
    renderPlanCount();
    const last = $('cc-messages').lastElementChild;
    if (last) last.scrollIntoView({ block: 'nearest' });
  }

  $('cc-begin-btn').addEventListener('click', () => run('Starting the consultation…', () => consultTurn({ mode: 'opening' })));
  $('cc-composer').addEventListener('submit', event => {
    event.preventDefault();
    const message = $('cc-message').value.trim();
    if (!message) return;
    run('Waiting for the consultant…', async () => {
      await consultTurn({ message }, message);
      $('cc-message').value = '';
    });
  });
  $('cc-message').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) $('cc-composer').requestSubmit();
  });
  $('cc-directness').addEventListener('change', event => run('Saving…', async () => {
    state.consult = await api(`/mentor/api/workspaces/${state.workspace.id}/consult/settings`,
      { method: 'POST', json: { directness: event.target.value } });
  }));

  // ── proposed changes ─────────────────────────────────────────────────────

  function proposals() {
    return (state.consult && state.consult.state && state.consult.state.proposals) || [];
  }

  function renderPlanCount() {
    const count = proposals().length;
    $('cc-plan-count').textContent = String(count);
    $('cc-plan-count').hidden = !count;
  }

  function renderPlan() {
    const list = proposals();
    const topics = Object.fromEntries((state.consult ? state.consult.topics : []).map(t => [t.id, t.label]));
    $('cc-proposals').replaceChildren(...(list.length ? list.map(p => el('article', { class: 'cc-card cc-proposal' },
      el('h2', { text: p.title }),
      el('p', { class: 'cc-muted' }, topics[p.topic] || p.topic, ' · ', `${p.effort} effort`,
        p.ai_dimension !== 'none' ? ` · ${AI_DIMENSIONS[p.ai_dimension]}` : ''),
      el('p', { text: p.change }),
      el('dl', {}, el('dt', { text: 'Why' }), el('dd', { text: p.rationale }),
        el('dt', { text: 'Principle' }), el('dd', { text: p.principle })))) :
      [el('p', { class: 'cc-banner', text: 'No proposed changes yet. They appear here as the consultation goes on.' })]));
  }

  // ── start ────────────────────────────────────────────────────────────────

  async function start() {
    if (!(await loadSession())) return;
    if (new URLSearchParams(location.search).get('provider') === 'openrouter') {
      state.provider = 'openrouter';
      store.set('provider', 'openrouter');
      $('cc-ai').hidden = false;
      history.replaceState(null, '', location.pathname);
    }
    await loadModels().catch(error => showError(error.message));
    await loadWorkspaces();
  }

  run('Loading…', start);
})();
