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
let modelsLoadingProvider = '';
let configuredProviders = new Set();
let sentCodeEmail = '';
let mentorAuthenticated = false;
let renderedMessagesKey = '';
const selectedModels = new Map();
const blankCourses = new Set();
const inventories = new Map();
const inventoryErrors = new Map();
const el = id => document.getElementById(id);
const provider = () => el('mentor-provider').value;
const endpoint = suffix => '/mentor/api/workspaces/' + workspace.id + suffix;

function mentorMode() {
  const chosen = provider();
  if (!chosen) return {ready: false, message: 'Choose an AI provider or explicitly choose Guided mode.', target: 'mentor-provider'};
  if (chosen === 'guided') return {ready: true, message: 'Guided walkthrough — no AI requests or semantic analysis.'};
  if (!configuredProviders.has(chosen))
    return {ready: false, message: 'Enter and save your provider API key to fetch models.', target: 'mentor-key'};
  if (modelsLoadingProvider === chosen)
    return {ready: false, message: 'Fetching available models. Wait for the list, then choose and save a model.', target: 'mentor-model'};
  const saved = selectedModels.get(chosen);
  if (!saved) return {ready: false, message: 'Choose a model, then click Use selected model.', target: 'mentor-model'};
  const manual = chosen === 'nebula' && el('manual-model-enabled').checked;
  const pending = manual ? el('manual-model').value.trim() : el('mentor-model').value;
  if (pending !== saved)
    return {ready: false, message: 'Your model choice has unsaved changes. Click Use selected model.', target: 'save-model'};
  const names = {openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Gemini', nebula: 'NebulaONE'};
  return {ready: true, message: `${names[chosen] || chosen} · ${saved}`};
}

function draftEditsPending() {
  return !!workspace?.draft && renderedDraftKey === workspace.id + ':' + workspace.draft_revision &&
    workspace.draft.items.some((item, index) =>
      el('draft-title-' + index).value !== item.title || el('draft-html-' + index).value !== item.html);
}

function courseInputs() {
  const cached = workspace && inventories.get(workspace.id);
  const info = cached && cached.revision === workspace?.revision ? cached.info : null;
  const ready = !!workspace && (!!info?.course_items || workspace.materials.length > 0 || blankCourses.has(workspace.id));
  return {ready, info, error: workspace && inventoryErrors.get(workspace.id)};
}

function getFlowState() {
  const step = (number, title, description, target, button) => ({number, title, description, target, button});
  if (!workspace) return step(1, 'Choose or create a course workspace',
    'Give your course a name, or resume an existing workspace. Your work is saved separately for each course.', 'new-title', 'Choose a workspace');
  if (workspace.active_job || uploadController) {
    const kind = workspace.active_job?.kind;
    const number = uploadController || kind === 'prepare' ? 3 : kind === 'build' ? 6 : kind === 'interview' ? 4 : 5;
    return step(number, uploadController ? 'Your file is uploading' : 'Your course job is running',
      'Wait for this job to finish before changing its inputs. Progress is saved; job details and recovery controls are below.',
      uploadController ? 'upload-progress-label' : 'processing-details', 'View progress');
  }
  if (draftEditsPending()) return step(5, 'Save your draft edits before approval',
    'Your edited items are not saved yet. Save the snapshot, then complete a fresh final review before approving it.', 'save-draft', 'Go to save draft edits');
  if (renderedBriefWorkspace === workspace.id && el('design-brief').value !== workspace.brief)
    return step(4, 'Save your changed design brief',
      'Your edits are not saved. Saving the brief invalidates earlier direction and draft approvals.', 'design-brief', 'Review and save the brief');
  if (workspace.artifact && workspace.stage === 'built')
    return step(6, 'Your approved content is ready',
      'Download the private content package or open its preview. This does not publish a public HAX site.', 'artifact-links', 'Go to download');
  if (workspace.draft && workspace.build_revision === workspace.draft_revision &&
      workspace.reviewed_draft_revision === workspace.draft_revision)
    return step(6, 'Build your approved content',
      'The exact draft is approved. Use Build approved content below to prepare your download.', 'build-course', 'Go to build');
  if (workspace.draft && workspace.reviewed_draft_revision === workspace.draft_revision)
    return step(5, 'Approve the exact draft you reviewed',
      'Review the items once more, save any edits, then explicitly approve this exact version. Changes require another review.', 'approve-build', 'Go to draft approval');
  const mode = mentorMode();
  if (!mode.ready) return step(2, 'Configure your mentor before continuing', mode.message, mode.target, 'Finish AI setup');
  if (workspace.draft) return step(5, 'Inspect the draft, then discuss it',
    'Open the proposed items below, then use the final-review conversation to request changes or confirm your review. Approval is a separate action.', 'draft-panel', 'Review draft items');
  const inputs = courseInputs();
  if (inputs.error && !inputs.ready) return {...step(3, 'Reload the course inventory',
    'The saved inventory could not be loaded. Retry before re-uploading your course or discussing its contents.',
    'course-sources', 'Retry loading inventory'), retry: true};
  if (!inputs.ready) return step(3, 'Add course content, or choose to start from scratch',
    'No usable content is confirmed yet. Upload a Canvas export, add supporting sources, or explicitly continue without a file.', 'course-sources', 'Choose your course source');
  if (workspace.direction_revision === workspace.revision)
    return step(5, 'Generate a draft from your approved direction',
      'Your direction is approved. Generate the draft below, then review it before approving a build.', 'analyze-course', 'Go to draft generation');
  const interviewed = (workspace.jobs || []).some(job => job.kind === 'interview' &&
    job.status === 'completed' && job.revision === workspace.revision);
  if (!interviewed) return step(4, 'Discuss your audience, goals and constraints',
    'Tell the mentor what students should learn, what must stay unchanged, and what you want to improve. This conversation comes before direction approval.', 'mentor-message', 'Start the conversation');
  if (el('design-brief').value !== workspace.brief || !workspace.brief.trim())
    return step(4, 'Refine and save your design brief',
      'The brief records your goals and constraints. Save any edits before approving its direction.', 'design-brief', 'Review the brief');
  return step(4, 'Review your brief, then approve the direction',
    'Your interview is complete. Read the saved brief and explicitly approve it below before generating a draft.', 'approve-direction', 'Go to direction approval');
}

function goToNextStep() {
  const flow = getFlowState();
  if (flow.retry) {
    action(async () => { await fetchWorkspace(); await renderWorkspace(); });
    return;
  }
  const target = el(flow.target);
  if (target.tagName === 'DETAILS') target.open = true;
  target.scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center'});
  target.focus({preventScroll: true});
}

function startBlankCourse() {
  if (!workspace || workspace.active_job || busy) return;
  blankCourses.add(workspace.id);
  renderFlow();
  goToNextStep();
}

function updateProcessingStatus() {
  const status = el('mentor-status');
  let text = '';
  if (uploadController) text = `Uploading ${Math.round(el('hosted-upload-progress').value || 0)}% directly to private storage…`;
  else if (workspace?.active_job) {
    const labels = {prepare: 'Preparing your uploaded content', interview: 'Preparing the mentor reply',
      analysis: 'Generating your draft', review: 'Reviewing your draft', build: 'Building your download'};
    text = (labels[workspace.active_job.kind] || 'Processing your course') + '… Progress is saved.';
  } else text = busy ? 'Working…' : '';
  setLiveText('mentor-status', text);
  status.hidden = !text;
}

function setLiveText(id, text) {
  if (el(id).textContent !== text) el(id).textContent = text;
}
function renderFlow() {
  updateProcessingStatus();
  el('mentor-guide').hidden = !mentorAuthenticated;
  if (!mentorAuthenticated) return;
  const mode = mentorMode();
  const flow = getFlowState();
  el('guide-workspace').textContent = workspace ? 'Course: ' + workspace.title : 'Start a course workspace';
  el('next-step-number').textContent = `Your next step · ${flow.number} of 6`;
  setLiveText('next-step-title', flow.title);
  el('next-step-description').textContent = flow.description;
  el('next-step-button').textContent = flow.button;
  el('next-step-button').disabled = busy && !workspace?.active_job && !uploadController;
  setLiveText('mentor-mode', 'Mentor mode: ' + mode.message);
  setLiveText('ai-setup-state', mode.message);
  el('ai-setup-state').className = 'banner ' + (mode.ready && provider() !== 'guided' ? 'banner-success' : 'banner-info');
  const chosenAI = !!provider() && provider() !== 'guided';
  el('credential-form').hidden = !chosenAI;
  el('gateway-settings').hidden = provider() !== 'nebula';
  el('model-settings').hidden = !chosenAI || !configuredProviders.has(provider());
  el('model-settings').setAttribute('aria-busy', String(modelsLoadingProvider === provider() && !!provider()));
  for (let number = 1; number <= 6; number++) {
    const node = el('flow-step-' + number);
    node.dataset.state = number < flow.number ? 'past' : number === flow.number ? 'current' : 'upcoming';
    if (number === flow.number) node.setAttribute('aria-current', 'step');
    else node.removeAttribute('aria-current');
  }
  const waiting = busy || !!workspace?.active_job || !!uploadController;
  el('mentor-provider').disabled = waiting;
  el('mentor-model').disabled = waiting;
  el('manual-model-enabled').disabled = waiting;
  el('manual-model').disabled = waiting;
  ['save-provider-key', 'forget-provider-key', 'refresh-models', 'save-model'].forEach(id => {
    el(id).disabled = waiting;
  });
  el('workspace-list').disabled = busy;
  el('new-workspace').querySelector('button').disabled = busy;
  if (!workspace) return;
  const inputs = courseInputs();
  setLiveText('source-readiness', inputs.info?.course_items
    ? `${inputs.info.course_items} instructional items are ready for discussion.`
    : workspace.materials.length ? `${workspace.materials.length} supporting sources are ready. No supported course items were found.`
    : blankCourses.has(workspace.id) ? 'Starting a new course without an imported file. The mentor will not claim to have analyzed existing course content.'
    : inputs.error ? 'Course inventory could not be loaded. Use Retry loading inventory above before continuing.'
    : inputs.info && workspace.source_type && workspace.source_type !== 'blank'
      ? 'The imported package has no supported published instructional items. Review its warnings, add a different source, or explicitly start a new course.'
    : inputs.info ? 'No course content is imported. Upload a file or explicitly choose to start a new course.'
    : 'Reading the saved course inventory…');
  el('start-blank-course').hidden = !!inputs.info?.course_items || workspace.materials.length > 0;
  el('start-blank-course').disabled = waiting || blankCourses.has(workspace.id);
  el('start-blank-course').textContent = blankCourses.has(workspace.id)
    ? 'New course selected — no imported file' : "I'm designing a new course — continue without a file";
  const reason = !mode.ready ? mode.message : !inputs.ready
    ? 'Add course content or choose to start from scratch before discussing the course.'
    : waiting ? 'Wait for the current request or job to finish.'
    : `Ready to discuss with ${mode.message}`;
  setLiveText('discussion-readiness', reason);
  el('send-mentor-message').disabled = waiting || !mode.ready || (!workspace.draft && !inputs.ready);
  el('send-mentor-message').title = el('send-mentor-message').disabled ? reason : 'Send your message using the displayed mentor mode.';
  ['source-form', 'mentor-canvas-form', 'material-file-form', 'material-url-form', 'material-text-form'].forEach(id => {
    el(id).querySelector('button').disabled = waiting;
  });
  const interviewed = (workspace.jobs || []).some(job => job.kind === 'interview' &&
    job.status === 'completed' && job.revision === workspace.revision);
  el('approve-direction').disabled = waiting || !interviewed || !workspace.brief.trim() ||
    el('design-brief').value !== workspace.brief;
  el('analyze-course').disabled = waiting || !mode.ready || !inputs.ready ||
    el('design-brief').value !== workspace.brief ||
    workspace.direction_revision !== workspace.revision;
  el('approve-build').disabled = waiting || !workspace.draft || draftEditsPending() ||
    workspace.reviewed_draft_revision !== workspace.draft_revision;
  el('build-course').disabled = waiting || !workspace.draft || draftEditsPending() ||
    workspace.reviewed_draft_revision !== workspace.draft_revision ||
    workspace.build_revision !== workspace.draft_revision;
}

function requireMentorReady() {
  if (!workspace) throw new Error('Choose a workspace first.');
  const mode = mentorMode();
  if (!mode.ready) throw new Error(mode.message);
  if (!workspace.draft && !courseInputs().ready)
    throw new Error('Upload course content or explicitly choose to start from scratch first.');
}
function acceptWorkspace(next) {
  workspace = next.keep_draft ? {...next, draft: workspace?.draft || null} : next;
}
async function fetchWorkspace() {
  const id = workspace.id;
  const next = await api(endpoint('?draft_revision=' + (workspace.draft_revision || 0)));
  if (workspace?.id !== id) return;
  if (next.revision < workspace.revision ||
      (next.revision === workspace.revision && next.draft_revision < workspace.draft_revision)) return;
  acceptWorkspace(next);
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
el('manual-model').oninput = renderFlow;
el('mentor-model').onchange = renderFlow;
el('design-brief').oninput = renderFlow;

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
  selectedModels.delete(selectedProvider);
  const requestId = ++modelRequest;
  modelVersion = null;
  el('mentor-model').replaceChildren(new Option('Choose a model', ''));
  el('model-status').textContent = '';
  el('model-settings').hidden = selectedProvider === 'guided';
  el('manual-model-label').hidden = selectedProvider !== 'nebula';
  el('manual-model-enabled').checked = false;
  el('manual-model').hidden = true;
  el('manual-model').value = '';
  renderFlow();
  if (!selectedProvider || selectedProvider === 'guided') return;
  if (!configuredProviders.has(selectedProvider)) {
    el('model-status').textContent = 'Save this provider key to fetch its models.';
    return;
  }
  const url = '/mentor/api/providers/' + selectedProvider + '/models';
  modelsLoadingProvider = selectedProvider;
  el('model-status').textContent = refresh ? 'Fetching available models from your provider…' : 'Loading saved provider settings…';
  renderFlow();
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
  if (data.selected) selectedModels.set(selectedProvider, data.selected);
  modelsLoadingProvider = '';
  el('model-status').textContent = (data.selected ? 'Saved model: ' + data.selected + '. ' : '') +
    (data.expired ? 'Refresh the catalog before making a new selection. ' : '') + (data.warning || '');
  renderFlow();
}
function refreshModels() { action(() => loadModels(true)); }
function saveModel() { action(async () => {
  const selectedProvider = provider();
  const manual = selectedProvider === 'nebula' && el('manual-model-enabled').checked;
  el('model-status').textContent = 'Saving your model choice…';
  const result = await api('/mentor/api/providers/' + selectedProvider + '/model', 'PUT', {
    model: manual ? el('manual-model').value.trim() : el('mentor-model').value,
    version: modelVersion, manual,
  });
  el('model-status').textContent = 'Saved model: ' + result.selected;
  selectedModels.set(selectedProvider, result.selected);
  if (manual) {
    el('mentor-model').add(new Option(result.selected + ' (manual)', result.selected));
    el('manual-model-enabled').checked = false;
    el('manual-model').hidden = true;
  }
  el('mentor-model').value = result.selected;
  renderFlow();
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
  renderFlow();
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
  renderFlow();
  try { await callback(); }
  catch (error) {
    el('mentor-error').textContent = error.message;
    el('mentor-error').hidden = false;
    if (workspace) {
      try { await fetchWorkspace(); await renderWorkspace(); } catch (_) {}
    }
  } finally {
    busy = false;
    modelsLoadingProvider = '';
    renderFlow();
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
  const renderId = workspace.id;
  const renderRevision = workspace.revision;
  clearTimeout(pollTimer);
  el('workspace-content').hidden = false;
  el('workspace-title').textContent = workspace.title;
  const stages = {brief: 'Prepare your inputs and discuss your goals.', uploading: 'Uploading course content.',
    preparing: 'Preparing your course content.', interviewing: 'The mentor is preparing a reply.',
    direction_approved: 'Direction approved. Ready to generate a draft.', analyzing: 'Generating the draft.',
    draft: 'Draft ready for your review.', reviewing: 'Reviewing the draft.',
    build_approved: 'Draft approved. Ready to build.', building: 'Building your download.',
    built: 'Approved content is ready to download.'};
  el('workspace-stage').textContent = stages[workspace.stage] || 'Your workspace is saved.';
  el('workspace-revisions').textContent =
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
  const messageBox = el('mentor-messages');
  const lastMessage = workspace.messages[workspace.messages.length - 1];
  const messagesKey = workspace.id + ':' + workspace.messages.length + ':' + (lastMessage?.id || lastMessage?.content || '');
  if (messagesKey !== renderedMessagesKey) {
    const followLatest = !renderedMessagesKey.startsWith(workspace.id + ':') ||
      messageBox.scrollHeight - messageBox.scrollTop - messageBox.clientHeight < 40;
    messageBox.replaceChildren();
    workspace.messages.forEach(message => {
      const box = document.createElement('div');
      box.style.cssText = 'padding:0.8rem;margin:0.5rem 0;border-left:3px solid #1565c0;background:#f5f7fa;white-space:pre-wrap';
      itemText(box, 'strong', `${message.role} · ${message.kind}`);
      itemText(box, 'p', message.content);
      messageBox.appendChild(box);
    });
    if (followLatest) messageBox.scrollTop = messageBox.scrollHeight;
    renderedMessagesKey = messagesKey;
  }
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
      details.open = index === 0;
      itemText(details, 'summary', `${item.changed ? 'Proposed/edited' : 'Retained'}: ${item.title}`);
      itemText(details, 'p', item.reason || '');
      itemText(details, 'p', 'Sources: ' + (item.source_ids || []).join(', '));
      const titleLabel = itemText(details, 'label', 'Item title');
      titleLabel.htmlFor = 'draft-title-' + index;
      const title = document.createElement('input');
      title.id = titleLabel.htmlFor;
      title.className = 'field-input'; title.value = item.title;
      title.oninput = renderFlow;
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
      html.oninput = renderFlow;
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
    link.href = endpoint('/download'); link.className = 'btn btn-ghost';
    const preview = itemText(el('artifact-links'), 'a', 'Preview student view (opens in a new tab)');
    preview.href = endpoint('/preview/index.html'); preview.target = '_blank';
    preview.rel = 'noopener noreferrer'; preview.className = 'btn btn-primary';
    el('artifact-links').replaceChildren(preview, link);
  }
  el('mentor-jobs').textContent = (workspace.jobs || []).map(j =>
    `${j.kind}: ${j.status}${j.error ? ' — ' + j.error : ''}`).join('\n') || 'No processing jobs yet.';
  el('cancel-mentor-job').hidden = !workspace.active_job;
  renderFlow();
  try {
    const info = await api('/mentor/api/workspaces/' + renderId + '/inventory');
    if (workspace?.id !== renderId || workspace.revision !== renderRevision) return;
    inventories.set(renderId, {revision: renderRevision, info});
    inventoryErrors.delete(renderId);
    const box = el('course-inventory');
    const warningsOpen = box.querySelector('details')?.open || false;
    box.replaceChildren();
    itemText(box, 'p', `${info.course_items} published instructional items · ${info.materials.length} supplemental sources`);
    if (info.course_items) itemText(box, 'p', Object.entries(info.counts).filter(([, count]) => count)
      .map(([kind, count]) => `${count} ${kind}`).join(' · '));
    const coverage = document.createElement('details');
    coverage.open = warningsOpen;
    itemText(coverage, 'summary', (info.warnings || []).length
      ? `Review ${info.warnings.length} import warnings and coverage limits` : 'Coverage and limitations');
    const warnings = document.createElement('ul');
    (info.warnings || []).slice(0, 30).forEach(w => itemText(warnings, 'li', w));
    (info.limitations || []).forEach(w => itemText(warnings, 'li', w));
    if ((info.warnings || []).length > 30)
      itemText(warnings, 'li', `${info.warnings.length - 30} further import warnings are recorded with the source.`);
    coverage.appendChild(warnings);
    box.appendChild(coverage);
  } catch (error) {
    if (workspace?.id !== renderId) return;
    inventories.delete(renderId);
    inventoryErrors.set(renderId, true);
    el('course-inventory').textContent = error.message;
  }
  renderFlow();
  if (workspace.active_job) pollTimer = setTimeout(async () => {
    try { await fetchWorkspace(); await renderWorkspace(); }
    catch (error) { el('mentor-status').textContent = error.message; }
  }, 1000);
}

async function updateCredentials() {
  const data = await api('/mentor/api/session');
  mentorCSRF = data.csrf;
  hosted = data.hosted_ready;
  mentorAuthenticated = data.authenticated;
  configuredProviders = new Set(data.credentials.map(item => item.provider));
  for (const chosen of selectedModels.keys()) if (!configuredProviders.has(chosen)) selectedModels.delete(chosen);
  el('workspace-choice').hidden = !mentorAuthenticated;
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
  renderFlow();
  return data;
}

el('new-workspace').onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  let created = false;
  await action(async () => {
    workspace = await api('/mentor/api/workspaces', 'POST', {title: el('new-title').value});
    await refreshList(); await renderWorkspace();
    created = true;
  });
  if (created) {
    el('mentor-guide').scrollIntoView({block: 'start'});
    el('next-step-button').focus({preventScroll: true});
  }
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
  selectedModels.delete(provider());
  modelVersion = null; ++modelRequest;
  el('mentor-model').replaceChildren(new Option('Save a key, then fetch models', ''));
  el('model-status').textContent = 'Provider key and cached catalog removed.';
}); }
el('source-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const file = el('source-file').files[0];
    if (!file) throw new Error('Choose a course export or prepared package.');
    blankCourses.delete(workspace.id);
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
    requireMentorReady();
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
  requireMentorReady();
  if (el('design-brief').value !== workspace.brief)
    throw new Error('Save your design brief changes and approve the updated direction before generating a draft.');
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
