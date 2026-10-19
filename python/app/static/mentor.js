'use strict';
let mentorCSRF = '';
let workspace = null;
let busy = false;
let pollTimer = null;
let hosted = false;
let uploadController = null;
let renderedBriefWorkspace = null;
let renderedBrief = '';
let renderedDraftKey = null;
let modelVersion = null;
let modelRequest = 0;
let configuredProviders = new Set();
let sentCodeEmail = '';
const el = id => document.getElementById(id);
const provider = () => el('mentor-provider').value;
const endpoint = suffix => '/mentor/api/workspaces/' + workspace.id + suffix;
function acceptWorkspace(next) {
  workspace = next.keep_draft ? {...next, draft: workspace?.draft || null} : next;
}
async function fetchWorkspace() {
  acceptWorkspace(await api(endpoint('?draft_revision=' + (workspace.draft_revision || 0))));
}

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
el('mentor-login-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    await api('/mentor/api/login', 'POST', {code: el('mentor-invite-code').value});
    el('mentor-invite-code').value = '';
    const session = await updateCredentials();
    if (session.authenticated) await refreshList();
  });
};

el('account-code-form').onsubmit = event => {
  event.preventDefault();
  if (busy) {
    if (!el('account-send-code').disabled) {
      const status = el('account-code-status');
      status.hidden = false;
      status.className = 'banner banner-info';
      status.textContent = `Email address received: ${el('account-email').value.trim()}. Another request is still finishing; please try sending your code again in a moment.`;
    }
    return;
  }
  action(async () => {
    const email = el('account-email').value.trim();
    const button = el('account-send-code');
    const status = el('account-code-status');
    sentCodeEmail = '';
    el('account-code').value = '';
    el('account-verify-form').hidden = true;
    el('account-verify-status').hidden = true;
    button.disabled = true;
    button.textContent = 'Sending code…';
    el('account-email').readOnly = true;
    el('account-code-form').setAttribute('aria-busy', 'true');
    status.hidden = false;
    status.className = 'banner banner-info';
    status.textContent = `Email address received: ${email}. Sending your sign-in code…`;
    try {
      await api('/mentor/api/auth/send-code', 'POST', {email});
      sentCodeEmail = email;
      status.className = 'banner banner-success';
      status.textContent = `A sign-in code is on its way to ${email}. Check your email inbox and spam folder, then enter the latest six-digit code below.`;
      el('account-verify-form').hidden = false;
      el('account-code').focus();
    } catch (error) {
      status.className = 'banner banner-error';
      status.textContent = `We could not send a code to ${email}. ${error.message}`;
      throw error;
    } finally {
      button.disabled = false;
      button.textContent = sentCodeEmail ? 'Email another code' : 'Try sending again';
      el('account-email').readOnly = false;
      el('account-code-form').setAttribute('aria-busy', 'false');
    }
  });
};
el('account-email').oninput = () => {
  if (sentCodeEmail && el('account-email').value.trim() !== sentCodeEmail) {
    sentCodeEmail = '';
    el('account-code').value = '';
    el('account-verify-form').hidden = true;
    el('account-code-status').className = 'banner banner-info';
    el('account-code-status').textContent = 'Email address changed. Send a new sign-in code for this address.';
    el('account-send-code').textContent = 'Email a sign-in code';
  }
};
el('account-verify-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const email = el('account-email').value.trim();
    if (!sentCodeEmail || email !== sentCodeEmail)
      throw new Error('Send a new sign-in code for this email address first.');
    const button = el('account-verify-code');
    const status = el('account-verify-status');
    button.disabled = true;
    button.textContent = 'Verifying code…';
    status.hidden = false;
    status.className = 'banner banner-info';
    status.textContent = 'Verifying your code and signing you in…';
    try {
      await api('/mentor/api/auth/verify-code', 'POST', {
        email, code: el('account-code').value, claim_browser: el('claim-browser-work').checked,
      });
      el('account-code').value = '';
      workspace = null;
      await updateCredentials(); await refreshList();
      status.className = 'banner banner-success';
      status.textContent = 'You are signed in.';
    } catch (error) {
      status.className = 'banner banner-error';
      status.textContent = `We could not complete sign-in. ${error.message} If you already submitted this code, request a new one.`;
      throw error;
    } finally {
      button.disabled = false;
      button.textContent = 'Verify and sign in';
    }
  });
};
function signOutAccount() { action(async () => {
  try { await api('/mentor/api/auth/logout', 'POST', {}); }
  finally { window.location.reload(); }
}); }

async function loadModels(refresh = false) {
  const selectedProvider = provider();
  const requestId = ++modelRequest;
  modelVersion = null;
  el('mentor-model').replaceChildren(new Option('Choose a model', ''));
  el('model-status').textContent = '';
  el('model-settings').hidden = selectedProvider === 'guided';
  el('manual-model-label').hidden = selectedProvider !== 'nebula';
  el('manual-model-enabled').checked = false;
  el('manual-model').hidden = true;
  el('manual-model').value = '';
  if (selectedProvider === 'guided') return;
  if (!configuredProviders.has(selectedProvider)) {
    el('model-status').textContent = 'Save this provider key to fetch its models.';
    return;
  }
  const url = '/mentor/api/providers/' + selectedProvider + '/models';
  let data = await api(url);
  if (requestId !== modelRequest || provider() !== selectedProvider) return;
  modelVersion = data.version;
  if (refresh) {
    try { data = await api(url + '/refresh', 'POST', {}); }
    catch (error) {
      if (requestId !== modelRequest || provider() !== selectedProvider) return;
      data = {...data, warning: error.message + (selectedProvider === 'nebula'
        ? ' You can explicitly enter your gateway deployment/model ID below.' : '')};
    }
  }
  if (requestId !== modelRequest || provider() !== selectedProvider) return;
  modelVersion = data.version;
  data.models.forEach(model => el('mentor-model').add(new Option(model.name + ' — ' + model.id, model.id)));
  if (data.selected && !data.models.some(model => model.id === data.selected))
    el('mentor-model').add(new Option(data.selected + ' (saved selection)', data.selected));
  el('mentor-model').value = data.selected || '';
  el('model-status').textContent = (data.selected ? 'Saved model: ' + data.selected + '. ' : '') +
    (data.expired ? 'Refresh the catalog before making a new selection. ' : '') + (data.warning || '');
}
function refreshModels() { action(() => loadModels(true)); }
function saveModel() { action(async () => {
  const selectedProvider = provider();
  const manual = selectedProvider === 'nebula' && el('manual-model-enabled').checked;
  const result = await api('/mentor/api/providers/' + selectedProvider + '/model', 'PUT', {
    model: manual ? el('manual-model').value.trim() : el('mentor-model').value,
    version: modelVersion, manual,
  });
  el('model-status').textContent = 'Saved model: ' + result.selected;
}); }
el('mentor-provider').onchange = () => {
  el('mentor-key').value = '';
  ++modelRequest; modelVersion = null;
  el('mentor-model').replaceChildren(new Option('Choose a model', ''));
  el('model-status').textContent = 'Provider changed. Refresh models after the active request finishes.';
  el('manual-model-label').hidden = provider() !== 'nebula';
  el('manual-model').hidden = true;
  el('manual-model-enabled').checked = false;
  action(() => loadModels());
};
el('manual-model-enabled').onchange = () => {
  el('manual-model').hidden = !el('manual-model-enabled').checked;
};

async function uploadHosted(file, kind, role = 'reference') {
  if (!window.HaxHostedUpload) throw new Error('Hosted upload support is missing. Run the hosted build before deployment.');
  const intent = await api(endpoint('/uploads'), 'POST', {
    revision: workspace.revision, filename: file.name, size: file.size, kind, role,
  });
  uploadController = new AbortController();
  el('hosted-upload-progress').hidden = false;
  el('upload-progress-label').hidden = false;
  el('abort-upload').hidden = false;
  el('hosted-upload-progress').value = 0;
  workspace = await api(endpoint(''));
  await renderWorkspace();
  try {
    await window.HaxHostedUpload.upload(file, intent, {
      signal: uploadController.signal,
      onProgress: progress => {
        el('hosted-upload-progress').value = progress.percentage || 0;
        el('mentor-status').textContent =
          `Uploading ${Math.round(progress.percentage || 0)}% directly to private storage…`;
      },
    });
    // Browser data is not proof: the server HEADs the registered private path.
    await api('/mentor/api/uploads/' + intent.upload_id + '/complete', 'POST', {});
    workspace = await api(endpoint(''));
    await renderWorkspace();
  } catch (error) {
    if (uploadController.signal.aborted) {
      try { await api(endpoint('/cancel-job'), 'POST', {job_id: intent.upload_id}); } catch (_) {}
      throw new Error('Transfer canceled. Temporary upload data is scheduled for cleanup.');
    }
    throw error;
  } finally {
    uploadController = null;
    el('abort-upload').hidden = true;
    el('hosted-upload-progress').hidden = true;
    el('upload-progress-label').hidden = true;
  }
}
function abortHostedUpload() {
  if (uploadController) uploadController.abort();
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
      try { await fetchWorkspace(); renderWorkspace(); } catch (_) {}
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
  if (workspace.upload) {
    const upload = workspace.upload;
    el('upload-state').textContent =
      `Source: ${upload.filename} · ${upload.status}${upload.phase ? ' / ' + upload.phase : ''} · ` +
      (upload.cleanup_status === 'deleted' ? 'Original cloud upload deletion accepted.'
        : upload.cleanup_status === 'error' ? 'Raw-file cleanup pending retry.'
        : 'Raw upload is temporary and will be cleaned up after processing or expiry.');
  }
  // Preserve dirty text even after focus moves to chat or upload controls.
  if (renderedBriefWorkspace !== workspace.id || el('design-brief').value === renderedBrief) {
    el('design-brief').value = workspace.brief;
  }
  renderedBriefWorkspace = workspace.id;
  renderedBrief = workspace.brief;
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
  const draftKey = workspace.draft ? workspace.id + ':' + workspace.draft_revision : null;
  const draftChanged = draftKey !== renderedDraftKey;
  if (draftChanged) el('draft-items').replaceChildren();
  if (workspace.draft) {
    el('draft-summary').textContent = workspace.draft.summary;
    el('draft-coverage').textContent = workspace.draft.coverage || '';
    el('draft-warnings').replaceChildren();
    (workspace.draft.warnings || []).forEach(w => itemText(el('draft-warnings'), 'li', w));
    if (draftChanged) workspace.draft.items.forEach((item, index) => {
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
  renderedDraftKey = draftKey;
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
    try { await fetchWorkspace(); await renderWorkspace(); }
    catch (error) { el('mentor-status').textContent = error.message; }
  }, 1000);
}

async function updateCredentials() {
  const data = await api('/mentor/api/session');
  mentorCSRF = data.csrf;
  hosted = data.hosted_ready;
  configuredProviders = new Set(data.credentials.map(item => item.provider));
  el('mentor-login-card').hidden = data.invite_required === undefined ? !data.login_required : !data.invite_required;
  el('account-login-card').hidden = !data.account_required;
  el('account-profile').hidden = !data.account;
  el('account-label').textContent = data.account ? 'Signed in as ' + data.account.email : '';
  el('claim-browser-work').disabled = !data.can_claim_browser;
  el('claim-browser-work').checked = false;
  el('workspace-list').disabled = data.login_required;
  el('new-workspace').querySelector('button').disabled = data.login_required;
  el('provider-settings').hidden = data.login_required;
  el('hosted-upload-help').hidden = !hosted;
  el('credential-status').textContent = data.credentials.map(c =>
    `${c.provider}: saved until ${new Date(c.expires_at * 1000).toLocaleString()}`).join(' · ')
    || 'No saved credentials. Guided mode is available.';
  return data;
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
      model: '', base_url: provider() === 'nebula' ? el('mentor-gateway').value : '',
      remember: el('remember-key').checked,
    });
    el('mentor-key').value = ''; await updateCredentials(); await loadModels(true);
  });
};
function forgetKey() { action(async () => {
  await api('/mentor/api/credentials/' + provider(), 'DELETE'); await updateCredentials();
  modelVersion = null; ++modelRequest;
  el('mentor-model').replaceChildren(new Option('Save a key, then fetch models', ''));
  el('model-status').textContent = 'Provider key and cached catalog removed.';
}); }
el('source-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const file = el('source-file').files[0];
    if (!file) throw new Error('Choose a course export or prepared package.');
    if (hosted) {
      await uploadHosted(file, 'course');
      return;
    }
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
    if (hosted) {
      await uploadHosted(file, 'material', el('material-role').value);
      return;
    }
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
  const submitted = el('design-brief').value;
  workspace = await api(endpoint('/brief'), 'PUT', {brief: submitted, revision: workspace.revision});
  if (el('design-brief').value === submitted) el('design-brief').value = workspace.brief;
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
action(async () => {
  const session = await updateCredentials();
  if (session.authenticated) await refreshList();
  if (session.authenticated && provider() && provider() !== 'guided') await loadModels();
});
