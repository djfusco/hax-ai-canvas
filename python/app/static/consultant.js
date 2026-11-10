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

  /** Run one task at a time with a status line. Resolves true on success. */
  async function run(label, task, done = '') {
    if (state.busy) return false;
    state.busy = true;
    showError('');
    status(label);
    document.body.setAttribute('aria-busy', 'true');
    try { await task(); status(done); return true; } catch (error) { status(''); showError(error.message); return false; } finally {
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
    for (const tab of ['course', 'consult', 'plan', 'docs']) {
      $('tab-' + tab).setAttribute('aria-selected', String(tab === name));
      $('view-' + tab).hidden = tab !== name;
    }
    if (name === 'consult') renderConsult();
    if (name === 'plan') run('Loading the plan…', loadPlan);
    if (name === 'docs') run('Loading documents…', loadDocuments);
  }
  for (const tab of ['course', 'consult', 'plan', 'docs']) $('tab-' + tab).addEventListener('click', () => selectTab(tab));

  // ── course intake ────────────────────────────────────────────────────────

  async function refreshCourse() {
    const course = state.workspace.course;
    state.map = course ? await api(`/mentor/api/workspaces/${state.workspace.id}/course-map`) : null;
    state.consult = await api(`/mentor/api/workspaces/${state.workspace.id}/consult`);
    if (course) renderCourse();
    if (course) $('cc-preview-course').href = `/mentor/api/workspaces/${state.workspace.id}/site/index.html`;
    // Reveal only after rendering, so the panel never shows half-filled.
    $('cc-intake').hidden = Boolean(course);
    $('cc-course').hidden = !course;
    renderPlanCount();
    if (state.tab === 'consult') renderConsult();
    if (state.tab === 'plan') await loadPlan();
    if (state.tab === 'docs') await loadDocuments();
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
    // Send in pieces under 4 MB: hosting platforms cap request size.
    const piece = 3.5 * 1024 * 1024;
    const total = Math.max(1, Math.ceil(blob.size / piece));
    const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
    for (let index = 0; index < total; index++) {
      status(`Uploading ${bytes(summary.slimBytes)} (from a ${bytes(summary.archiveBytes)} export): part ${index + 1} of ${total}…`);
      const chunk = new FormData();
      chunk.append('upload_id', uploadId);
      chunk.append('index', String(index));
      chunk.append('total', String(total));
      chunk.append('chunk', blob.slice(index * piece, (index + 1) * piece), 'part');
      await api(`/mentor/api/workspaces/${state.workspace.id}/intake/chunks`, { method: 'POST', form: chunk });
    }
    status('Importing your course…');
    const form = intakeForm();
    form.append('upload_id', uploadId);
    await afterImport(await api(`/mentor/api/workspaces/${state.workspace.id}/intake/export`, { method: 'POST', form }));
  }));
  // Institutional Canvas connection (only when an administrator configured it).
  async function loadCanvas() {
    const info = await api('/mentor/api/canvas/institutions').catch(() => ({ institutions: [] }));
    $('cc-canvas-card').hidden = !info.institutions.length;
    if (!info.institutions.length) return;
    $('cc-institution').replaceChildren(...info.institutions.map(i => el('option', { value: i.id, text: i.name })));
    $('cc-canvas-connect').hidden = Boolean(info.connected);
    $('cc-canvas-pick').hidden = !info.connected;
    if (info.connected) {
      const { courses } = await api('/mentor/api/canvas/courses');
      $('cc-canvas-course').replaceChildren(...courses.map(c => el('option', { value: String(c.id),
        text: [c.name, c.term].filter(Boolean).join(' · ') })));
      $('cc-canvas-import').disabled = !courses.length;
    }
  }
  $('cc-canvas-go').addEventListener('click', () => run('Opening Canvas…', async () => {
    const { url } = await api('/mentor/api/canvas/connect', { method: 'POST', json: { institution: $('cc-institution').value } });
    window.location.assign(url);
  }));
  $('cc-canvas-import').addEventListener('click', () => run('Reading your course from Canvas… this can take a minute', async () => {
    await afterImport(await api(`/mentor/api/workspaces/${state.workspace.id}/intake/canvas`, { method: 'POST', json: {
      course_id: $('cc-canvas-course').value, revision: state.workspace.revision,
      instructor_name: $('cc-instructor').value.trim() } }));
  }));
  $('cc-canvas-disconnect').addEventListener('click', () => run('Disconnecting…', async () => {
    await api('/mentor/api/canvas/disconnect', { method: 'POST', json: {} });
    await loadCanvas();
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

  // Accessibility check (no AI); "Fix with AI" drafts a revision for review.
  function renderAccessibility(report) {
    $('cc-a11y-summary').textContent = report.items_with_issues
      ? `${report.errors} errors, ${report.warnings} warnings in ${report.items_with_issues} of ${report.items_checked} pages`
      : `No problems found in ${report.items_checked} pages`;
    const rows = report.items.map(entry => el('div', { class: 'cc-a11y-item' },
      el('div', { class: 'cc-head' }, el('strong', { text: entry.title }),
        entry.fixable ? el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Fix with AI',
          onclick: () => run(`Fixing ${entry.title}…`, async () => {
            requireAi();
            await api(`/mentor/api/workspaces/${state.workspace.id}/documents/generate`, { method: 'POST', json: {
              provider: state.provider, kind: entry.id === 'syllabus' ? 'syllabus' : 'page',
              item_id: entry.id === 'syllabus' ? null : entry.id, instruction: entry.fix_instruction } });
          }, `Draft fix for “${entry.title}” is in Revised documents. Review and approve it, then check again.`) }) : null),
      el('ul', { class: 'cc-list' }, entry.issues.map(issue => el('li', {},
        el('span', { class: 'cc-tag' + (issue.severity === 'error' ? ' warn' : ''), text: issue.severity }), ' ',
        `${issue.message} ×${issue.count} (WCAG ${issue.wcag})`)))));
    $('cc-a11y-results').replaceChildren(
      ...rows,
      report.videos_without_captions.length ? el('p', { class: 'cc-banner',
        text: `Course videos with no caption file: ${report.videos_without_captions.join(', ')}. Add captions in Canvas or your video platform.` }) : null,
      el('p', { class: 'cc-muted', text: report.limitations }));
  }
  $('cc-a11y-run').addEventListener('click', () => run('Checking accessibility…', async () => {
    renderAccessibility(await api(`/mentor/api/workspaces/${state.workspace.id}/accessibility`));
  }));

  // HAX-theme preview: a signed link to a sandboxed page that loads HAX's components.
  for (const button of document.querySelectorAll('.cc-hax-preview')) {
    button.addEventListener('click', () => {
      const tab = window.open('about:blank', '_blank'); // open now, so popup blockers allow it
      run('Preparing the HAX preview…', async () => {
        const { url } = await api(`/mentor/api/workspaces/${state.workspace.id}/hax-preview`, { method: 'POST', json: {} });
        if (tab) {
          tab.opener = null; // the preview cannot reach back to this page
          tab.location.href = url;
        } else {
          window.location.assign(url);
        }
      }).then(ok => { if (!ok && tab) tab.close(); });
    });
  }

  /** Quick AI version: the AI advises and decides; every step is a separate, resumable request. */
  async function quickVersion() {
    requireAi();
    const ws = () => `/mentor/api/workspaces/${state.workspace.id}`;
    const step = (n, total, text) => status(`Quick AI version, step ${n} of ${total}: ${text}`);
    // 1. The advisor works through every topic not yet covered.
    let view = await api(ws() + '/consult');
    const open = () => view.state ? view.topics.filter(t => !['covered', 'skipped'].includes(view.state.topics[t.id].status)).length : view.topics.length;
    let guard = 0;
    while (open() && guard++ < 8) {
      const label = view.state ? view.topics.find(t => t.id === view.state.current_topic).label : view.topics[0].label;
      step(1, 4, `advising on ${label}…`);
      view = await api(ws() + '/consult', { method: 'POST', json: { mode: 'auto', provider: state.provider } });
      state.consult = view;
    }
    // 2. Accept the advisor's proposals on the instructor's behalf (labelled AI-decided).
    step(2, 4, 'accepting the advisor\'s changes…');
    state.plan = await api(ws() + '/consult/proposals/accept-all', { method: 'POST', json: {} });
    if (state.consult && state.consult.state) state.consult.state.proposals = state.plan.proposals;
    // 3. Draft the plan.
    step(3, 4, 'drafting the improvement plan…');
    state.plan = await api(ws() + '/plan/draft', { method: 'POST', json: { provider: state.provider } });
    // 4. Write revised documents: syllabus, then assessments touched by accepted changes, with rubrics.
    const docs = await api(ws() + '/documents');
    const have = new Set(docs.documents.filter(d => d.status === 'approved').map(d => `${d.kind}:${d.item_id || ''}`));
    const wanted = docs.targets.filter(t => t.kind === 'syllabus' || t.proposals.length).slice(0, 13)
      .filter(t => !have.has(`${t.kind}:${t.item_id || ''}`));
    for (const [index, target] of wanted.entries()) {
      step(4, 4, `writing ${target.title} (${index + 1} of ${wanted.length})…`);
      state.docs = await api(ws() + '/documents/generate', { method: 'POST',
        json: { provider: state.provider, kind: target.kind, item_id: target.item_id, approve: true } });
    }
    renderPlanCount();
    selectTab('docs'); // its own loader is skipped while this task runs, so load here
    state.docs = await api(ws() + '/documents');
    renderDocuments();
  }
  $('cc-quick').addEventListener('click', () => {
    if (!window.confirm('Create the quick AI version?\n\nThe AI will answer the consultation itself, accept its own proposed '
      + 'changes (labelled AI-decided), draft the plan and write revised documents. This makes roughly 10-25 requests '
      + 'on your AI account and takes a few minutes. You can change any part afterwards.')) return;
    run('Starting the quick AI version…', quickVersion,
      'Quick AI version ready. Preview the course site, then refine anything: consult further, change decisions, or ask for changes to any document.');
  });

  // ── course map ───────────────────────────────────────────────────────────

  function section(title, ...children) {
    return el('article', { class: 'cc-card' }, el('h2', { text: title }), ...children);
  }

  function renderCourse() {
    const course = state.workspace.course;
    const map = state.map;
    $('cc-course-title').textContent = course.title;
    const sourceLabel = { export: 'Canvas export', extension: 'Canvas (extension)', canvas: 'Canvas (connected)',
      documents: 'Your documents' }[course.source] || course.source;
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
    const remaining = consultState ? view.topics.filter(t => t.id !== current &&
      ['not_started', 'in_progress'].includes(consultState.topics[t.id].status)).length : 0;
    $('cc-next-topic').hidden = !hasCourse || !started || !remaining;
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
  $('cc-next-topic').addEventListener('click', () => {
    const label = $('cc-topics').querySelector('button[aria-current=true] span');
    run('Moving to the next topic…', () => consultTurn({ mode: 'advance' },
      `I'm done with ${label ? label.textContent : 'this topic'}. Let's move on.`));
  });
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

  // ── plan ─────────────────────────────────────────────────────────────────

  const STATUS_LABELS = { proposed: 'Undecided', accepted: 'Accepted', rejected: 'Rejected', deferred: 'Deferred' };

  function proposals() {
    return (state.consult && state.consult.state && state.consult.state.proposals) || [];
  }

  function renderPlanCount() {
    const waiting = proposals().filter(p => p.status === 'proposed').length;
    $('cc-plan-count').textContent = String(waiting);
    $('cc-plan-count').hidden = !waiting;
  }

  function base() { return `/mentor/api/workspaces/${state.workspace.id}`; }

  async function loadPlan() {
    state.plan = await api(base() + '/plan');
    renderPlan();
  }

  function decide(id, status, edits = {}) {
    return run('Saving…', async () => {
      state.plan = await api(`${base()}/consult/proposals/${encodeURIComponent(id)}`, { method: 'POST', json: { status, ...edits } });
      if (state.consult && state.consult.state) state.consult.state.proposals = state.plan.proposals;
      renderPlan();
      renderPlanCount();
    });
  }

  function proposalCard(p, topics) {
    const card = el('article', { class: 'cc-card cc-proposal cc-status-' + p.status });
    const view = () => {
      card.replaceChildren(
        el('div', { class: 'cc-head' }, el('h3', { text: p.title }),
          el('span', {}, p.decided_by === 'ai' ? el('span', { class: 'cc-tag ai', text: 'AI-decided' }) : null, ' ',
            el('span', { class: 'cc-tag' + (p.status === 'accepted' ? ' good' : ''), text: STATUS_LABELS[p.status] }))),
        el('p', { class: 'cc-muted' }, topics[p.topic] || p.topic, ' · ', `${p.effort} effort`,
          p.ai_dimension !== 'none' ? ` · ${AI_DIMENSIONS[p.ai_dimension]}` : ''),
        el('p', { text: p.change }),
        el('dl', {}, el('dt', { text: 'Why' }), el('dd', { text: p.rationale }),
          el('dt', { text: 'Principle' }), el('dd', { text: p.principle })),
        el('div', { class: 'cc-row' },
          p.status !== 'accepted' ? el('button', { type: 'button', class: 'cc-btn', text: 'Accept', onclick: () => decide(p.id, 'accepted') }) : null,
          p.status !== 'deferred' ? el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Later', onclick: () => decide(p.id, 'deferred') }) : null,
          p.status !== 'rejected' ? el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Reject', onclick: () => decide(p.id, 'rejected') }) : null,
          p.status !== 'proposed' ? el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Undo', onclick: () => decide(p.id, 'proposed') }) : null,
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Edit', onclick: edit })));
    };
    const edit = () => {
      const title = el('input', { value: p.title, maxlength: 200, 'aria-label': 'Title' });
      const change = el('textarea', { rows: 3, maxlength: 2000, 'aria-label': 'Change' });
      change.value = p.change;
      const rationale = el('textarea', { rows: 2, maxlength: 2000, 'aria-label': 'Why' });
      rationale.value = p.rationale;
      card.replaceChildren(el('label', { text: 'Title' }), title, el('label', { text: 'Change' }), change,
        el('label', { text: 'Why' }), rationale,
        el('div', { class: 'cc-row' },
          el('button', { type: 'button', class: 'cc-btn', text: 'Save and accept', onclick: () =>
            decide(p.id, 'accepted', { title: title.value, change: change.value, rationale: rationale.value }) }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Cancel', onclick: view })));
      title.focus();
    };
    view();
    return card;
  }

  function renderPlan() {
    if (!state.plan) return;
    const topics = Object.fromEntries((state.consult ? state.consult.topics : []).map(t => [t.id, t.label]));
    const order = { proposed: 0, accepted: 1, deferred: 2, rejected: 3 };
    const list = [...state.plan.proposals].sort((a, b) => order[a.status] - order[b.status]);
    $('cc-proposals').replaceChildren(...(list.length ? list.map(p => proposalCard(p, topics))
      : [el('p', { class: 'cc-banner', text: 'No proposed changes yet. They appear as the consultation goes on.' })]));
    const plan = state.plan.plan;
    $('cc-plan-vision').value = plan.vision || '';
    $('cc-plan-outcomes').value = plan.outcomes.map(o => o.text).join('\n');
    $('cc-plan-policy').value = plan.ai_policy || '';
    $('cc-plan-questions').value = plan.open_questions.join('\n');
    const accepted = state.plan.proposals.filter(p => p.status === 'accepted').length;
    $('cc-plan-note').textContent = !state.plan.consulted
      ? 'Hold the consultation first; the plan is drafted from it.'
      : `${accepted} accepted change${accepted === 1 ? '' : 's'} will shape the plan.`
        + (plan.drafted_at ? ' Drafting again replaces the text below.' : '');
    $('cc-plan-draft').disabled = !state.plan.consulted;
    const history = plan.history || [];
    $('cc-plan-history-row').hidden = !history.length;
    $('cc-plan-history').replaceChildren(...history.map((v, index) => el('option', { value: String(index),
      text: `${new Date(v.at).toLocaleString()}: ${(v.vision || '').slice(0, 60) || 'earlier version'}` })).reverse());
    $('cc-plan-word').href = base() + '/plan/export?format=docx';
    $('cc-plan-html').href = base() + '/plan/export?format=html';
  }

  $('cc-plan-draft').addEventListener('click', () => run('Drafting the plan… this can take a minute', async () => {
    requireAi();
    if (state.plan.plan.vision && !window.confirm('Replace the current plan text with a new draft?')) return;
    state.plan = await api(base() + '/plan/draft', { method: 'POST', json: { provider: state.provider } });
    renderPlan();
  }));
  $('cc-plan-restore').addEventListener('click', () => run('Restoring the plan…', async () => {
    state.plan = await api(base() + '/plan/restore', { method: 'POST', json: { version: Number($('cc-plan-history').value) } });
    renderPlan();
  }, 'Earlier version restored. The text it replaced is in the history too.'));
  $('cc-plan-form').addEventListener('submit', event => {
    event.preventDefault();
    const lines = id => $(id).value.split('\n').map(l => l.trim()).filter(Boolean);
    run('Saving the plan…', async () => {
      const notes = Object.fromEntries(state.plan.plan.outcomes.map(o => [o.text, o.note]));
      state.plan = await api(base() + '/plan', { method: 'PUT', json: {
        vision: $('cc-plan-vision').value, ai_policy: $('cc-plan-policy').value,
        outcomes: lines('cc-plan-outcomes').map(text => ({ text, note: notes[text] || '' })),
        open_questions: lines('cc-plan-questions') } });
      renderPlan();
    }, 'Plan saved.');
  });

  // ── revised documents ────────────────────────────────────────────────────

  async function loadDocuments() {
    state.docs = await api(base() + '/documents');
    renderDocuments();
  }

  function renderDocuments() {
    const view = state.docs;
    const select = $('cc-target');
    const kinds = { syllabus: 'Syllabus', assignment: 'Assignment', rubric: 'Rubric', page: 'Page', tutor: 'AI tutor kit' };
    select.replaceChildren(...view.targets.map((t, index) => el('option', { value: String(index),
      text: (t.kind === 'syllabus' ? 'Revised syllabus' : t.kind === 'tutor' ? 'Course AI tutor kit (instructions for an AI tutor, student guide)'
        : `${kinds[t.kind]}: ${t.title.replace(/^Rubric: /, '')}`)
        + (t.proposals.length ? ` (applies ${plural(t.proposals.length, 'accepted change')})` : '') })));
    $('cc-generate').disabled = !view.targets.length;
    const approved = view.documents.filter(d => d.status === 'approved').length;
    $('cc-docs-zip').setAttribute('aria-disabled', String(!approved));
    $('cc-docs-zip').classList.toggle('cc-disabled', !approved);
    $('cc-docs-zip').href = base() + '/documents/export';
    $('cc-docs-preview').href = base() + '/site/index.html';
    $('cc-docs-hax').href = base() + '/documents/export?format=hax';
    const checked = new Set([...$('cc-bulk-items').querySelectorAll('input:checked')].map(i => i.value));
    $('cc-bulk-items').replaceChildren(...view.items.map(item => el('label', {},
      el('input', { type: 'checkbox', value: item.id, checked: checked.has(item.id) }), ` ${item.title} `,
      el('span', { class: 'cc-tag', text: item.kind }))));
    $('cc-documents').replaceChildren(...(view.documents.length ? [...view.documents].reverse().map(documentCard)
      : [el('p', { class: 'cc-banner', text: 'No documents yet. Accept changes in the plan, then write a revised syllabus, assignment or rubric.' })]));
  }

  function documentCard(d) {
    const card = el('article', { class: 'cc-card cc-document' });
    const preview = () => {
      const frame = el('div', { class: 'cc-doc-body' });
      // Server-sanitized HTML is shown in an isolated, script-less frame.
      const iframe = el('iframe', { sandbox: '', title: d.title, class: 'cc-doc-frame' });
      iframe.srcdoc = `<!doctype html><meta charset=utf-8><style>body{font:15px/1.5 system-ui;margin:12px;color:#1d2330}
        table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:6px;vertical-align:top;text-align:left}
        @media (prefers-color-scheme:dark){body{background:#1d2129;color:#e7eaf0}}</style>${d.html}`;
      frame.append(iframe);
      card.replaceChildren(
        el('div', { class: 'cc-head' }, el('h2', { text: d.title }),
          el('span', {}, d.auto ? el('span', { class: 'cc-tag ai', text: 'AI-approved' }) : null, ' ',
            el('span', { class: 'cc-tag' + (d.status === 'approved' ? ' good' : ''), text: d.status === 'approved' ? 'Approved' : 'Draft' }))),
        d.summary ? el('p', { class: 'cc-muted', text: d.summary }) : null,
        frame,
        askForm(d),
        el('div', { class: 'cc-row' },
          d.status !== 'approved'
            ? el('button', { type: 'button', class: 'cc-btn', text: 'Approve', onclick: () => change(d.id, { status: 'approved' }) })
            : el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Back to draft', onclick: () => change(d.id, { status: 'draft' }) }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Compare with original', onclick: compareView }),
          (d.versions || []).length ? el('button', { type: 'button', class: 'cc-btn cc-btn-quiet',
            text: `History (${d.versions.length})`, onclick: historyView }) : null,
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Edit HTML', onclick: edit }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Rewrite from latest plan', onclick: () => {
            if (window.confirm(`Rewrite “${d.title}” from scratch using your latest plan and accepted changes?`)) {
              run('Rewriting… this can take a minute', async () => {
                requireAi();
                state.docs = await api(base() + '/documents/generate', { method: 'POST', json: {
                  provider: state.provider, kind: d.kind, item_id: d.item_id, replace_id: d.id } });
                renderDocuments();
              });
            }
          } }),
          el('a', { class: 'cc-btn cc-btn-quiet', href: `${base()}/documents/${d.id}/export?format=docx`, download: '', text: 'Word' }),
          el('a', { class: 'cc-btn cc-btn-quiet', href: `${base()}/documents/${d.id}/export`, download: '', text: 'HTML' }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Delete', onclick: () => {
            if (window.confirm(`Delete “${d.title}”?`)) run('Deleting…', async () => {
              state.docs = await api(`${base()}/documents/${d.id}`, { method: 'DELETE' });
              renderDocuments();
            });
          } })));
    };
    const frameOf = (html, extra = '') => {
      const iframe = el('iframe', { sandbox: '', title: d.title, class: 'cc-doc-frame' });
      iframe.srcdoc = `<!doctype html><meta charset=utf-8><style>body{font:15px/1.6 system-ui;margin:12px;color:#1d2330}
        table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:6px;vertical-align:top;text-align:left}
        ins{background:#d9f5e3;text-decoration:none}del{background:#fde2e2;color:#7a1f1f}${extra}
        @media (prefers-color-scheme:dark){body{background:#1d2129;color:#e7eaf0}ins{background:#1f4a32}del{background:#4a1f1f;color:#ffd0d0}}</style>${html}`;
      return iframe;
    };
    const compareView = () => run('Comparing…', async () => {
      const result = await api(`${base()}/documents/${d.id}/compare`);
      const back = el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Back', onclick: preview });
      if (!result.has_original) {
        card.replaceChildren(el('h2', { text: d.title }), el('p', { class: 'cc-banner',
          text: 'This is a new document; there is no original in your course to compare with.' }), back);
        return;
      }
      let mode = 'changes';
      const holder = el('div', { class: 'cc-compare' });
      const show = () => {
        holder.replaceChildren(...(mode === 'changes' ? [frameOf(result.diff_html)]
          : [el('div', {}, el('h3', { text: 'Original' }), frameOf(result.original_html)),
             el('div', {}, el('h3', { text: 'Revised' }), frameOf(result.revised_html))]));
        holder.classList.toggle('cc-side-by-side', mode !== 'changes');
      };
      show();
      card.replaceChildren(el('div', { class: 'cc-head' }, el('h2', { text: `${d.title}: compared with the original` })),
        el('p', { class: 'cc-muted', text: `${result.words_added} words added, ${result.words_removed} removed. `
          + 'Highlighted: green added, red removed.' }),
        el('div', { class: 'cc-row' },
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Highlighted changes', onclick: () => { mode = 'changes'; show(); } }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Side by side', onclick: () => { mode = 'side'; show(); } })),
        holder, el('div', { class: 'cc-row' }, back));
    });
    const historyView = () => {
      const versions = (d.versions || []).map((v, index) => ({ ...v, index })).reverse();
      card.replaceChildren(el('h2', { text: `${d.title}: earlier versions` }),
        el('p', { class: 'cc-muted', text: 'Restoring brings a version back as a draft; the current text is kept as a version too.' }),
        ...versions.map(v => el('div', { class: 'cc-version' },
          el('div', { class: 'cc-head' }, el('strong', { text: `${v.title || d.title} · ${new Date(v.at).toLocaleString()}` }),
            el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Restore', onclick: () => run('Restoring…', async () => {
              state.docs = await api(`${base()}/documents/${d.id}/restore`, { method: 'POST', json: { version: v.index } });
              renderDocuments();
            }) })),
          frameOf(v.html || ''))),
        el('div', { class: 'cc-row' }, el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Back', onclick: preview })));
    };
    const edit = () => {
      const title = el('input', { value: d.title, maxlength: 240, 'aria-label': 'Title' });
      const html = el('textarea', { rows: 16, class: 'cc-code', 'aria-label': 'Document HTML' });
      html.value = d.html;
      card.replaceChildren(el('label', { text: 'Title' }), title,
        el('label', { text: 'Content (HTML: headings, paragraphs, lists, tables)' }), html,
        el('p', { class: 'cc-muted', text: 'Saving returns the document to draft so you can review it before approving.' }),
        el('div', { class: 'cc-row' },
          el('button', { type: 'button', class: 'cc-btn', text: 'Save', onclick: () => change(d.id, { title: title.value, html: html.value }) }),
          el('button', { type: 'button', class: 'cc-btn cc-btn-quiet', text: 'Cancel', onclick: preview })));
    };
    preview();
    return card;
  }

  function askForm(d) {
    const box = el('textarea', { rows: 2, maxlength: 4000, class: 'cc-ask-input',
      placeholder: 'Ask for changes, e.g. “Make the rubric 4 levels” or “Add a peer-review step”',
      'aria-label': `Ask for changes to ${d.title}` });
    const form = el('form', { class: 'cc-ask' }, box, el('button', { type: 'submit', class: 'cc-btn', text: 'Ask for changes' }));
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (!box.value.trim()) return;
      run('Revising… this can take a minute', async () => {
        requireAi();
        state.docs = await api(`${base()}/documents/${d.id}/revise`, { method: 'POST',
          json: { provider: state.provider, instruction: box.value } });
        renderDocuments();
      }, 'Revised. Review it, then approve it to use it in the course site.');
    });
    return form;
  }

  function change(id, body) {
    return run('Saving…', async () => {
      state.docs = await api(`${base()}/documents/${id}`, { method: 'PUT', json: body });
      renderDocuments();
    });
  }

  $('cc-bulk-all').addEventListener('click', () => $('cc-bulk-items').querySelectorAll('input').forEach(i => { i.checked = true; }));
  $('cc-bulk-none').addEventListener('click', () => $('cc-bulk-items').querySelectorAll('input').forEach(i => { i.checked = false; }));
  $('cc-bulk-run').addEventListener('click', () => {
    const ids = [...$('cc-bulk-items').querySelectorAll('input:checked')].map(i => i.value);
    const instruction = $('cc-bulk-instruction').value.trim();
    if (!instruction) { showError('Write the instruction first.'); return; }
    if (!ids.length) { showError('Choose at least one page.'); return; }
    if (ids.length > 3 && !window.confirm(`Revise ${ids.length} pages? This makes ${ids.length} requests on your AI account.`)) return;
    run('Revising pages…', async () => {
      requireAi();
      for (const [index, id] of ids.entries()) {
        status(`Revising page ${index + 1} of ${ids.length}…`);
        state.docs = await api(base() + '/documents/generate', { method: 'POST',
          json: { provider: state.provider, kind: 'page', item_id: id, instruction } });
      }
      renderDocuments();
    }, `Revised ${ids.length} page${ids.length === 1 ? '' : 's'}. Review and approve each draft below.`);
  });

  $('cc-generate').addEventListener('click', () => run('Writing the document… this can take a minute', async () => {
    requireAi();
    const target = state.docs.targets[Number($('cc-target').value)];
    state.docs = await api(base() + '/documents/generate', { method: 'POST',
      json: { provider: state.provider, kind: target.kind, item_id: target.item_id } });
    renderDocuments();
  }));
  for (const id of ['cc-docs-zip']) {
    $(id).addEventListener('click', event => {
      if ($(id).getAttribute('aria-disabled') === 'true') {
        event.preventDefault();
        showError('Approve at least one document first.');
      }
    });
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
    if (new URLSearchParams(location.search).get('canvas') === 'connected') history.replaceState(null, '', location.pathname);
    await loadWorkspaces();
    await loadCanvas().catch(error => showError(error.message));
  }

  run('Loading…', start);
})();
