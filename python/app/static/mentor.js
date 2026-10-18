'use strict';
let mentorCSRF = '';
let workspace = null;
let busy = false;
let pollTimer = null;
const el = id => document.getElementById(id);
const provider = () => el('mentor-provider').value;
const endpoint = suffix => '/mentor/api/workspaces/' + workspace.id + suffix;

async function api(url, method = 'GET', body) {
  const options = {method, headers: {'X-Mentor-CSRF': mentorCSRF}};
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The action failed.');
  return data;
}

async function action(callback) {
  if (busy) return;
  busy = true;
  el('mentor-error').hidden = true;
  el('mentor-status').textContent = 'Working…';
  try { await callback(); }
  catch (error) {
    el('mentor-error').textContent = error.message;
    el('mentor-error').hidden = false;
    if (workspace) {
      try { workspace = await api(endpoint('')); renderWorkspace(); } catch (_) {}
    }
  } finally {
    busy = false;
    el('mentor-status').textContent = workspace?.active_job
      ? 'Processing. Progress is saved in this workspace.' : '';
  }
}

async function refreshList() {
  const list = await api('/mentor/api/workspaces');
  el('workspace-list').replaceChildren(new Option('Choose a workspace', ''));
  list.forEach(w => el('workspace-list').add(new Option(w.title + ' — ' + w.stage, w.id)));
  if (workspace) el('workspace-list').value = workspace.id;
}

async function openWorkspace(id) {
  if (!id) return;
  await action(async () => {
    workspace = await api('/mentor/api/workspaces/' + id);
    await renderWorkspace();
  });
}

function itemText(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.appendChild(node);
  return node;
}

async function renderWorkspace() {
  if (!workspace) return;
  clearTimeout(pollTimer);
  el('workspace-content').hidden = false;
  el('workspace-title').textContent = workspace.title;
  el('workspace-stage').textContent =
    `Stage: ${workspace.stage} · input revision ${workspace.revision} · draft revision ${workspace.draft_revision}`;
  if (document.activeElement !== el('design-brief')) el('design-brief').value = workspace.brief;
  el('mentor-messages').replaceChildren();
  workspace.messages.forEach(message => {
    const box = document.createElement('div');
    box.style.cssText = 'padding:0.8rem;margin:0.5rem 0;border-left:3px solid #1565c0;background:#f5f7fa;white-space:pre-wrap';
    itemText(box, 'strong', `${message.role} · ${message.kind}`);
    itemText(box, 'p', message.content);
    el('mentor-messages').appendChild(box);
  });
  el('material-list').replaceChildren();
  workspace.materials.forEach(material => {
    const row = itemText(el('material-list'), 'li',
      `${material.name} — ${material.role} (${material.kind}) ${(material.warnings || []).join(' ')}`);
    const button = document.createElement('button');
    button.textContent = 'Remove';
    button.className = 'btn btn-xs btn-ghost';
    button.onclick = () => action(async () => {
      workspace = await api(endpoint('/materials/' + material.id), 'DELETE', {revision: workspace.revision});
      await renderWorkspace();
    });
    row.appendChild(button);
  });
  el('approve-direction').disabled = !!workspace.active_job ||
    !(workspace.jobs || []).some(j => j.kind === 'interview' &&
      j.status === 'completed' && j.revision === workspace.revision);
  el('analyze-course').disabled =
    !!workspace.active_job || workspace.direction_revision !== workspace.revision;
  el('draft-panel').hidden = !workspace.draft;
  el('chat-kind').value = workspace.draft ? 'review' : 'interview';
  el('draft-items').replaceChildren();
  if (workspace.draft) {
    el('draft-summary').textContent = workspace.draft.summary;
    el('draft-coverage').textContent = workspace.draft.coverage || '';
    el('draft-warnings').replaceChildren();
    (workspace.draft.warnings || []).forEach(w => itemText(el('draft-warnings'), 'li', w));
    workspace.draft.items.forEach((item, index) => {
      const details = document.createElement('details');
      itemText(details, 'summary', `${item.changed ? 'Proposed/edited' : 'Retained'}: ${item.title}`);
      itemText(details, 'p', item.reason || '');
      itemText(details, 'p', 'Sources: ' + (item.source_ids || []).join(', '));
      const titleLabel = itemText(details, 'label', 'Item title');
      titleLabel.htmlFor = 'draft-title-' + index;
      const title = document.createElement('input');
      title.id = titleLabel.htmlFor;
      title.className = 'field-input'; title.value = item.title;
      details.appendChild(title);
      const frame = document.createElement('iframe');
      frame.title = 'Sandboxed preview: ' + item.title;
      frame.setAttribute('sandbox', '');
      frame.style.cssText = 'width:100%;min-height:220px;border:1px solid #ddd';
      frame.srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'">' + item.html;
      details.appendChild(frame);
      if (item.original_html) {
        const before = document.createElement('details');
        itemText(before, 'summary', 'Compare with original content');
        const originalFrame = document.createElement('iframe');
        originalFrame.title = 'Original content: ' + item.title;
        originalFrame.setAttribute('sandbox', '');
        originalFrame.style.cssText = 'width:100%;min-height:220px;border:1px solid #ddd';
        originalFrame.srcdoc = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'">' + item.original_html;
        before.appendChild(originalFrame);
        details.appendChild(before);
      }
      const label = itemText(details, 'label', 'Edit proposed HTML (sanitized when saved)');
      label.htmlFor = 'draft-html-' + index;
      const html = document.createElement('textarea');
      html.id = label.htmlFor;
      html.className = 'field-input'; html.rows = 7; html.value = item.html;
      details.appendChild(html);
      el('draft-items').appendChild(details);
    });
    el('approve-build').disabled = !!workspace.active_job ||
      workspace.reviewed_draft_revision !== workspace.draft_revision;
    el('build-course').disabled = !!workspace.active_job ||
      workspace.build_revision !== workspace.draft_revision;
  }
  el('artifact-links').replaceChildren();
  if (workspace.artifact && workspace.stage === 'built') {
    const link = itemText(el('artifact-links'), 'a', 'Download approved course content');
    link.href = endpoint('/download'); link.className = 'btn btn-primary';
    const preview = itemText(el('artifact-links'), 'a', 'Open approved preview');
    preview.href = endpoint('/preview/index.html'); preview.target = '_blank';
    preview.rel = 'noopener'; preview.className = 'btn btn-ghost';
  }
  el('mentor-jobs').textContent = (workspace.jobs || []).map(j =>
    `${j.kind}: ${j.status}${j.error ? ' — ' + j.error : ''}`).join('\n') || 'No processing jobs yet.';
  el('cancel-mentor-job').hidden = !workspace.active_job;
  try {
    const info = await api(endpoint('/inventory'));
    const box = el('course-inventory');
    box.replaceChildren();
    itemText(box, 'p', `${info.course_items} published instructional items · ${info.materials.length} supplemental sources`);
    itemText(box, 'p', Object.entries(info.counts).map(([kind, count]) => `${count} ${kind}`).join(' · '));
    const warnings = document.createElement('ul');
    (info.warnings || []).slice(0, 30).forEach(w => itemText(warnings, 'li', w));
    (info.limitations || []).forEach(w => itemText(warnings, 'li', w));
    if ((info.warnings || []).length > 30)
      itemText(warnings, 'li', `${info.warnings.length - 30} further import warnings are recorded with the source.`);
    box.appendChild(warnings);
  } catch (error) { el('course-inventory').textContent = error.message; }
  if (workspace.active_job) pollTimer = setTimeout(async () => {
    try { workspace = await api(endpoint('')); await renderWorkspace(); }
    catch (error) { el('mentor-status').textContent = error.message; }
  }, 1000);
}

async function updateCredentials() {
  const data = await api('/mentor/api/session');
  mentorCSRF = data.csrf;
  el('credential-status').textContent = data.credentials.map(c =>
    `${c.provider}: saved until ${new Date(c.expires_at * 1000).toLocaleString()}`).join(' · ')
    || 'No saved credentials. Guided mode is available.';
}

el('new-workspace').onsubmit = event => {
  event.preventDefault(); action(async () => {
    workspace = await api('/mentor/api/workspaces', 'POST', {title: el('new-title').value});
    await refreshList(); await renderWorkspace();
  });
};
el('credential-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    await api('/mentor/api/credentials', 'POST', {
      provider: provider(), api_key: el('mentor-key').value,
      model: el('mentor-model').value, base_url: provider() === 'nebula' ? el('mentor-gateway').value : '',
      remember: el('remember-key').checked,
    });
    el('mentor-key').value = ''; await updateCredentials();
  });
};
function forgetKey() { action(async () => {
  await api('/mentor/api/credentials/' + provider(), 'DELETE'); await updateCredentials();
}); }
el('source-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const file = el('source-file').files[0];
    if (!file) throw new Error('Choose a course export or prepared package.');
    if (file.size > 256 * 1024 * 1024) throw new Error('Use the local preparation tool for this large export.');
    const form = new FormData(); form.append('file', file); form.append('revision', workspace.revision);
    workspace = await api(endpoint('/source'), 'POST', form); await renderWorkspace();
  });
};
el('mentor-canvas-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    workspace = await api(endpoint('/canvas'), 'POST', {
      revision: workspace.revision, course_id: el('mentor-canvas-id').value.trim(),
    });
    await renderWorkspace();
  });
};
el('material-file-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const file = el('material-file').files[0];
    if (!file) throw new Error('Choose a document.');
    const form = new FormData();
    form.append('file', file); form.append('revision', workspace.revision);
    form.append('role', el('material-role').value);
    workspace = await api(endpoint('/materials'), 'POST', form); await renderWorkspace();
  });
};
el('material-url-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    workspace = await api(endpoint('/materials'), 'POST', {revision: workspace.revision,
      role: el('material-role').value, url: el('material-url').value});
    await renderWorkspace();
  });
};
el('material-text-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    workspace = await api(endpoint('/materials'), 'POST', {revision: workspace.revision,
      role: el('material-role').value, text: el('material-text').value});
    el('material-text').value = ''; await renderWorkspace();
  });
};
el('mentor-chat-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    workspace = await api(endpoint('/chat'), 'POST', {message: el('mentor-message').value,
      kind: el('chat-kind').value, provider: provider()});
    el('mentor-message').value = ''; await renderWorkspace();
  });
};
function saveBrief() { action(async () => {
  workspace = await api(endpoint('/brief'), 'PUT', {brief: el('design-brief').value, revision: workspace.revision});
  await renderWorkspace();
}); }
function approveDirection() { action(async () => {
  if (el('design-brief').value !== workspace.brief) throw new Error('Save the design brief before approving it.');
  workspace = await api(endpoint('/approve-direction'), 'POST', {revision: workspace.revision}); await renderWorkspace();
}); }
function analyzeCourse() { action(async () => {
  workspace = await api(endpoint('/analyze'), 'POST', {revision: workspace.revision, provider: provider()});
  await renderWorkspace();
}); }
function saveDraft() { action(async () => {
  const items = workspace.draft.items.map((item, index) => ({
    id: item.id, title: el('draft-title-' + index).value, html: el('draft-html-' + index).value,
  })).filter(item => {
    const original = workspace.draft.items.find(i => i.id === item.id);
    return original.title !== item.title || original.html !== item.html;
  });
  if (!items.length) throw new Error('No snapshot edits to save.');
  workspace = await api(endpoint('/draft'), 'PUT', {draft_revision: workspace.draft_revision, draft: {items}});
  await renderWorkspace();
}); }
function approveBuild() { action(async () => {
  if (workspace.draft.items.some((item, i) =>
    el('draft-title-' + i).value !== item.title || el('draft-html-' + i).value !== item.html))
    throw new Error('Save snapshot edits before approval.');
  workspace = await api(endpoint('/approve-build'), 'POST', {draft_revision: workspace.draft_revision});
  await renderWorkspace();
}); }
function buildCourse() { action(async () => {
  workspace = await api(endpoint('/build'), 'POST', {draft_revision: workspace.draft_revision}); await renderWorkspace();
}); }
function cancelMentorJob() { action(async () => {
  if (!workspace.active_job) return;
  workspace = await api(endpoint('/cancel-job'), 'POST', {job_id: workspace.active_job.id});
  await renderWorkspace();
}); }
action(async () => { await updateCredentials(); await refreshList(); });
