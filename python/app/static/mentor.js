'use strict';
let mentorCSRF = '';
let workspace = null;
let busy = false;
let pollTimer = null;
let hosted = false;
let localMode = false;
let localHax = false;
let localSource = null;
let haxPollTimer = null;
let haxStatus = null;
let haxRequest = 0;
let haxBusy = false;
let uploadController = null;
let uploadFeedback = null;
let uploadHeartbeat = null;
let uploadChecking = false;
let uploadStatusIssue = null;
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
let renderedAssessmentKey = '';
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

function assessmentRequired() {
  return localMode && workspace?.draft?.assessment_required === true;
}
function assessmentReady() {
  const report = workspace?.draft?.assessment;
  return !assessmentRequired() || (workspace.draft.assessment_current === true &&
    report?.version === 1 && report.mode === 'ai' &&
    Array.isArray(report.items) && Array.isArray(report.frameworks));
}
function renderAssessmentState() {
  const draft = workspace?.draft;
  el('assessment-panel').hidden = !localMode || !draft;
  el('assessment-cost-help').hidden = !localMode;
  if (!localMode || !draft) return;
  const stale = draftEditsPending();
  const report = draft.assessment;
  const text = stale
    ? assessmentRequired()
      ? 'Your unsaved edits do not match this assessment. Save the draft, then complete a fresh AI final review before approval.'
      : 'Your draft has unsaved edits. Save the snapshot, then complete a fresh final review before approval. Guided mode does not generate an AI assessment.'
    : !assessmentReady() ? 'A current AI assessment is missing. Complete the final-review conversation to assess every proposed item before approving this exact draft. No neutral or invented scores are substituted.'
    : report ? 'Review the proposed assessment and its coverage below. The server verifies that the report matches the exact draft at approval and build time.'
    : provider() === 'guided'
      ? 'Guided walkthrough: no AI assessment report or framework scores are generated. To receive an AI assessment, configure a provider and model, then generate and review an AI draft.'
      : 'No AI assessment report is attached to this draft. Configure a provider and model, then generate and review an AI draft to receive an assessment.';
  setLiveText('assessment-readiness', text);
}

function assessmentReferenceURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}
function renderAssessmentReport(parent, report) {
  parent.replaceChildren();
  if (!report || report.mode !== 'ai') return;
  itemText(parent, 'p', report.summary || 'No assessment summary was provided.');
  const coverage = report.coverage || {};
  itemText(parent, 'p', `Coverage: ${coverage.items_evaluated ?? 'not reported'} of ${coverage.items_total ?? 'not reported'} instructional items assessed using bounded excerpts.`);
  if (Array.isArray(coverage.truncated_items) && coverage.truncated_items.length)
    itemText(parent, 'p', 'Truncated item excerpts: ' + coverage.truncated_items.join(', ') +
      '. The assessment cannot establish what omitted content contains.');
  const references = document.createElement('ul');
  for (const framework of report.frameworks || []) {
    const row = itemText(references, 'li', '');
    const label = `${framework.title || framework.id || 'Framework'}${framework.version ? ' · ' + framework.version : ''}`;
    const url = assessmentReferenceURL(framework.url);
    if (url) {
      const link = itemText(row, 'a', label);
      link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    } else row.textContent = label + ' (reference URL unavailable)';
  }
  parent.appendChild(references);
  const list = (node, title, values) => {
    if (!Array.isArray(values) || !values.length) return;
    itemText(node, 'h5', title);
    const items = document.createElement('ul');
    values.forEach(value => itemText(items, 'li', String(value)));
    node.appendChild(items);
  };
  list(parent, 'Report warnings', report.warnings);
  const dimensions = {ai_leverage: 'AI leverage', delegation_risk: 'Delegation risk',
    authenticity: 'Authenticity', pedagogical_quality: 'Pedagogical quality',
    transparency: 'Transparency', alignment: 'Alignment'};
  for (const item of report.items || []) {
    const details = document.createElement('details');
    itemText(details, 'summary', `${item.title || item.item_id} · ${item.item_id}`);
    if (item.content_sha256) itemText(details, 'p', 'Assessed content SHA-256: ' + item.content_sha256);
    itemText(details, 'p', `Bloom's taxonomy: ${item.blooms?.label || 'Not assessed'}${item.blooms?.level != null ? ' · level ' + item.blooms.level : ''}. ${item.blooms?.rationale || ''}`);
    itemText(details, 'p', `AIAS task design: observed ${item.aias?.observed_level ?? 'not assessed'}; recommended ${item.aias?.recommended_level ?? 'not assessed'}${item.aias?.label ? ' · ' + item.aias.label : ''}. ${item.aias?.rationale || ''}`);
    for (const [id, label] of Object.entries(dimensions)) {
      const dimension = item.dimensions?.[id];
      itemText(details, 'p', `${label} (application-defined heuristic): ${Number.isFinite(dimension?.score) ? dimension.score : 'not assessed'}. ${dimension?.rationale || ''}`);
    }
    list(details, 'Strengths', item.strengths);
    list(details, 'Concerns', item.concerns);
    list(details, 'Recommendations', item.recommendations);
    if (Array.isArray(item.evidence) && item.evidence.length) {
      itemText(details, 'h5', 'Evidence from assessed excerpts');
      for (const evidence of item.evidence) {
        itemText(details, 'p', 'Source: ' + (evidence.source_id || 'Not reported'));
        itemText(details, 'blockquote', evidence.quote || 'No quotation supplied.');
      }
    }
    list(details, 'Item limitations', item.limitations);
    parent.appendChild(details);
  }
}
function renderAssessments() {
  renderAssessmentState();
  if (!localMode || !workspace?.draft) return;
  const draft = workspace.draft;
  const key = workspace.id + ':' + workspace.draft_revision +
    ':' + JSON.stringify([draft.baseline_assessment, draft.assessment]);
  if (key === renderedAssessmentKey) return;
  renderedAssessmentKey = key;
  el('baseline-assessment').hidden = !draft.baseline_assessment;
  el('proposed-assessment').hidden = !draft.assessment;
  renderAssessmentReport(el('baseline-assessment-content'), draft.baseline_assessment);
  renderAssessmentReport(el('proposed-assessment-content'), draft.assessment);
}

function getFlowState() {
  const step = (number, title, description, target, button) => ({number, title, description, target, button});
  if (!workspace) return step(1, 'Choose or create a course workspace',
    'Give your course a name, or resume an existing workspace. Your work is saved separately for each course.', 'new-title', 'Choose a workspace');
  const upload = uploadPresentation();
  if (upload && (uploadController || workspace.active_job?.kind === 'prepare' || upload.needsCheck))
    return step(3, upload.title, upload.description, 'upload-feedback', 'View upload status');
  if (workspace.active_job || uploadController) {
    const kind = workspace.active_job?.kind;
    const number = uploadController || kind === 'prepare' ? 3 : kind === 'build' ? 6 : kind === 'interview' ? 4 : 5;
    return step(number, uploadController ? 'Your file is uploading' : 'Your course job is running',
      'Wait for this job to finish before changing its inputs. Progress is saved; job details and recovery controls are below.',
      uploadController ? 'upload-progress-label' : 'processing-details', 'View progress');
  }
  if (draftEditsPending()) return step(5, 'Save your draft edits before approval',
    'Your edited items are not saved yet. Save the snapshot, then complete a fresh final review before approving it.', 'save-draft', 'Go to save draft edits');
  if (assessmentRequired() && !assessmentReady())
    return step(5, 'Refresh the AI assessment before approval',
      'Complete a fresh final-review conversation for this exact draft. Missing or stale assessments are never replaced with invented scores.',
      'mentor-message', 'Go to final review');
  if (renderedBriefWorkspace === workspace.id && el('design-brief').value !== workspace.brief)
    return step(4, 'Save your changed design brief',
      'Your edits are not saved. Saving the brief invalidates earlier direction and draft approvals.', 'design-brief', 'Review and save the brief');
  if (localHax && workspace.artifact?.hax_site && workspace.stage === 'built')
    return step(6, 'Your approved HAX site is built',
      'Open the verified running site below, or start it again. Your site and its output directory stay on this computer.',
      'hax-site-panel', 'Go to HAX site');
  if (workspace.artifact && workspace.stage === 'built')
    return step(6, 'Your approved content is ready',
      'Download the private content package or open its preview. This does not publish a public HAX site.', 'artifact-links', 'Go to download');
  if (workspace.draft && workspace.build_revision === workspace.draft_revision &&
      workspace.reviewed_draft_revision === workspace.draft_revision)
    return step(6, localHax ? 'Build & open HAX site' : 'Build your approved content',
      localHax ? 'The exact draft is approved. Build a new local HAX site; its server starts after a successful build. Nothing is published.'
        : 'The exact draft is approved. Use Build approved content below to prepare your download.', 'build-course', 'Go to build');
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

function formatUploadBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1000000) return `${(bytes / 1000).toFixed(1)} KB`;
  if (bytes < 1000000000) return `${(bytes / 1000000).toFixed(1)} MB`;
  return `${(bytes / 1000000000).toFixed(2)} GB`;
}
function localUploadID() {
  return (uploadFeedback?.workspaceId === workspace?.id && uploadFeedback.uploadId) ||
    workspace?.upload?.upload_id || workspace?.upload?.id;
}

function uploadPresentation() {
  if (!workspace) return null;
  const local = uploadFeedback?.workspaceId === workspace.id ? uploadFeedback : null;
  const saved = workspace.upload && (!local || (localMode && local.phase === 'saved') ||
    (local.uploadId && local.uploadId === workspace.upload.id))
    ? workspace.upload : null;
  if (!local && !saved) return null;
  let phase = local?.phase || 'saved';
  if (!uploadController && saved && ['ready', 'failed', 'canceled'].includes(saved.status))
    phase = saved.status;
  if (phase === 'saved') phase = saved?.status || 'unknown';
  if (localMode) phase = ({uploading: 'awaiting_upload', complete: 'uploaded',
    cancelled: 'canceled', running: 'processing', completed: 'ready'})[phase] || phase;
  const views = {
    authorizing: ['Starting your private upload', 'Requesting upload access. No file bytes have been sent yet.', 1],
    transferring: ['Uploading your file', 'Transferring file bytes directly to private storage.', 2],
    verifying: ['Upload finished — confirming your file', 'Checking the stored file and starting the preparation job. Transfer is not the same as preparation.', 3],
    refreshing: ['Checking saved preparation progress', 'The file was sent. Loading the server’s saved status before continuing.', 3],
    awaiting_upload: ['Waiting for upload confirmation', 'No transfer is running in this tab. This saved attempt is awaiting upload confirmation; use job recovery before uploading again.', 2],
    uploaded: ['File received — preparation queued', 'The browser upload is complete. Waiting for the durable preparation job to begin.', 3],
    processing: ['Preparing your course content', 'The file is uploaded. Reading supported content and building its inventory; this may take several minutes.', 3],
    prepared: ['Saving and validating prepared content', 'Verifying the saved result before attaching it to your workspace. Raw-file cleanup follows verified attachment.', 3],
    ready: ['Your file has been prepared', 'Review the inventory and coverage warnings below, then continue with the course discussion.', 3],
    failed: ['The file could not be prepared', 'No new source was attached by this attempt. Review processing details before retrying.', 3],
    canceled: ['Upload or preparation canceled', 'Temporary cloud data is scheduled for cleanup. Your computer’s original file is unchanged.', 2],
    error: ['Upload needs your attention', local?.error || 'Check saved progress before trying another upload.', 2],
    unknown: ['Check your saved upload progress', 'The latest server status could not be confirmed. Check progress before retrying.', 3],
    not_started: ['No new upload is running', 'No active upload job was found. Your existing content is unchanged; you can retry the selected file.', 1],
  };
  if (localMode) Object.assign(views, {
    authorizing: ['Starting your local import', 'Reserving a managed local copy. No file bytes have been sent yet.', 1],
    transferring: ['Copying your file to the local app', 'Copying bounded chunks to this computer, not to cloud storage.', 2],
    verifying: ['Local copy complete — starting preparation', 'Confirming the local copy and queuing archive inspection. No AI analysis is running.', 3],
    awaiting_upload: ['Local copy interrupted', 'No transfer is running in this tab. Cancel the saved copy before selecting the file again.', 2],
    uploaded: ['Local file received — preparation queued', 'The copy is complete. Local preparation will begin shortly.', 3],
    processing: ['Preparing your course locally', 'Inspecting instructional content and retaining supported assets locally. No AI analysis is running.', 3],
    prepared: ['Validating prepared local content', 'Checking the prepared inventory before attaching it to this workspace.', 3],
    failed: ['The local file could not be prepared',
      saved?.error || saved?.message || 'No new source was attached. Review the local preparation error below before retrying.', 3],
    canceled: ['Local import canceled', 'Temporary local copy cleanup is managed by the app. Your original file is unchanged.', 2],
  });
  let [title, description, step] = views[phase] || views.unknown;
  if (phase === 'processing') {
    const phases = {
      setup: ['Starting the secure processing environment', 'Your file is uploaded. Preparing the isolated environment and its parsers before reading the archive.'],
      download: ['Loading the archive for preparation', 'The browser upload is complete. The secure processing environment is reading the private stored file.'],
      locking: ['Securing the preparation environment', 'The file is uploaded. Locking network access before parsing course content.'],
      prepare: ['Reading course content', 'Inspecting the archive and preparing supported instructional content. Excluded media is recorded in the coverage warnings.'],
      parse: ['Reading course content', 'Inspecting the archive and preparing supported instructional content.'],
    };
    if (phases[saved?.phase]) [title, description] = phases[saved.phase];
    if (localMode) {
      title = 'Preparing your course locally';
      description = saved?.message || workspace.active_job?.message ||
        'Inspecting the archive, preparing instructional content and retaining supported assets on this computer.';
      const currentPhase = saved?.phase || workspace.active_job?.phase;
      if (currentPhase) title += ' · ' + currentPhase.replace(/_/g, ' ');
    }
  }
  if (phase === 'transferring' && local?.percentage >= 100) {
    title = 'All file bytes transferred — finishing upload';
    description = localMode ? 'The local app is confirming the final chunk. Keep this tab open.'
      : 'Private storage is finalizing the file. Keep this tab open until upload confirmation finishes.';
  }
  if (['authorizing', 'verifying', 'refreshing'].includes(phase) && Date.now() - local?.changedAt > 15000)
    description += ' Still waiting for server confirmation; do not submit a duplicate upload.';
  if (phase === 'transferring' && Date.now() - local?.lastProgressAt > 30000)
    description += ' Waiting for the next transfer update; check your connection if this continues.';
  const issue = uploadStatusIssue?.workspaceId === workspace.id ? uploadStatusIssue.message : '';
  if (issue) description = issue + ' Last known status: ' + title + '. Use Check saved progress.';
  const inBrowser = ['authorizing', 'transferring', 'verifying'].includes(phase);
  return {phase, title, description, step, kind: local?.kind || saved?.kind || 'course',
    filename: local?.filename || saved?.filename || 'Selected file', size: local?.size || saved?.size,
    startedAt: local?.startedAt || (saved?.created_at ? saved.created_at * 1000 : null),
    percentage: phase === 'transferring' ? local?.percentage ?? null : phase === 'verifying' || phase === 'ready' ? 100 : null,
    loaded: local?.loaded, inBrowser, needsCheck: !!local?.mustCheck || !!issue ||
      (localMode && phase === 'awaiting_upload'),
    active: !!uploadController || ['awaiting_upload', 'uploaded', 'processing', 'prepared'].includes(phase),
    tone: issue ? 'error' : phase === 'ready' ? 'success' : ['error', 'failed', 'canceled', 'unknown'].includes(phase) ? 'error' : 'info'};
}

function renderUploadFeedback() {
  clearTimeout(uploadHeartbeat);
  const panel = el('upload-feedback');
  const view = uploadPresentation();
  panel.hidden = !view || !mentorAuthenticated;
  if (!view || !mentorAuthenticated) return;
  const form = el(view.kind === 'material' ? 'material-file-form' : 'source-form');
  if (panel.previousElementSibling !== form) form.after(panel);
  panel.dataset.tone = view.tone;
  el('upload-feedback-title').textContent = view.title;
  el('upload-feedback-file').textContent = `${view.filename} · ${formatUploadBytes(view.size)}`;
  setLiveText('upload-feedback-status', view.description);
  for (let number = 1; number <= 3; number++)
    el('upload-step-' + number).dataset.state = view.phase === 'ready' || number < view.step ? 'past' : number === view.step ? 'current' : 'upcoming';
  const bar = el('hosted-upload-progress');
  bar.hidden = ['error', 'failed', 'canceled', 'unknown', 'not_started'].includes(view.phase);
  el('upload-progress-label').hidden = bar.hidden;
  el('upload-progress-label').textContent = view.step === 2 ? 'File transfer progress' :
    view.phase === 'ready' ? 'Preparation complete' : view.phase === 'verifying'
      ? 'File bytes transferred — waiting for confirmation' : view.step === 1
        ? localMode ? 'Starting local import' : 'Waiting for upload access'
        : 'Preparation progress — no percentage available yet';
  if (view.percentage === null) bar.removeAttribute('value');
  else bar.value = view.percentage;
  el('upload-feedback-bytes').textContent = view.phase === 'transferring'
    ? `${formatUploadBytes(view.loaded || 0)} of ${formatUploadBytes(view.size)} transferred` +
      (view.percentage === null ? '' : ` (${Math.round(view.percentage)}%)`) : '';
  const elapsed = view.active && view.startedAt ? Math.max(0, Math.floor((Date.now() - view.startedAt) / 1000)) : null;
  el('upload-feedback-elapsed').textContent = elapsed === null ? '' : `Elapsed since this attempt started: ${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;
  el('upload-feedback-help').textContent = view.inBrowser
    ? 'Keep this tab open. No AI analysis is performed during upload or preparation.'
    : view.phase === 'awaiting_upload' ? 'No transfer is active in this tab. Review the saved attempt and use job recovery before retrying.'
    : view.active ? localMode
      ? 'Preparation continues in the local app. Keep the app running; you can return to this workspace later. No AI analysis is running.'
      : 'The file is stored; preparation continues on the server. You can return to this workspace later. No AI analysis is running.'
    : 'Your original file stays on your computer. Check saved progress before submitting another attempt if the result is uncertain.';
  el('abort-upload').hidden = localMode
    ? !(uploadController || (localUploadID() && ['awaiting_upload', 'error', 'unknown'].includes(view.phase)))
    : !uploadController || !['authorizing', 'transferring'].includes(view.phase);
  el('abort-upload').textContent = view.phase === 'authorizing' ? 'Stop waiting for upload access' : 'Cancel transfer';
  el('check-upload-progress').disabled = uploadChecking;
  el('check-upload-progress').textContent = uploadChecking ? 'Checking saved progress…' : 'Check saved progress';
  el('upload-recovery').hidden = !workspace.active_job;
  const labels = {authorizing: 'Starting upload…', transferring: 'Uploading file…',
    verifying: 'Confirming upload…', refreshing: 'Checking progress…'};
  el('upload-course-button').textContent = view.kind === 'course' && uploadController
    ? localMode ? ({authorizing: 'Starting local import…', transferring: 'Copying file locally…', verifying: 'Starting preparation…'}[view.phase] || 'Import in progress…')
      : labels[view.phase] || 'Upload in progress…'
    : localMode ? 'Import & prepare locally' : 'Upload & prepare course';
  el('upload-material-button').textContent = view.kind === 'material' && uploadController ? labels[view.phase] || 'Upload in progress…' : 'Add document';
  if (view.active) uploadHeartbeat = setTimeout(renderUploadFeedback, 1000);
}

function showUploadRecovery() {
  el('processing-details').open = true;
  el('processing-details').scrollIntoView({block: 'center'});
  el('cancel-mentor-job').focus();
}

async function uploadControlRequest(context, url, method = 'GET', body, cancelable = true) {
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  if (cancelable) {
    context.controller.signal.addEventListener('abort', cancel, {once: true});
    if (context.controller.signal.aborted) cancel();
  }
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 45000);
  try {
    return await api(url, method, body, {signal: controller.signal});
  } catch (error) {
    if (cancelable && context.controller.signal.aborted) throw error;
    const failure = new Error(timedOut
      ? 'The server has not responded in 45 seconds. Check saved progress before trying another upload.'
      : error.httpStatus ? error.message : 'The upload service could not confirm this request. Check saved progress before retrying.');
    failure.name = 'UploadControlError';
    throw failure;
  } finally {
    clearTimeout(timeout);
    if (cancelable) context.controller.signal.removeEventListener('abort', cancel);
  }
}

async function checkSavedUploadProgress() {
  if (!workspace || uploadChecking) return;
  const id = workspace.id;
  const context = uploadFeedback?.workspaceId === id ? uploadFeedback : {
    workspaceId: id, controller: new AbortController(), phase: 'saved'};
  uploadChecking = true;
  renderUploadFeedback();
  try {
    const uploadId = localMode ? localUploadID() : context.uploadId || workspace.upload?.id;
    const transferNeedsCheck = !workspace.upload || !['ready', 'failed', 'canceled'].includes(workspace.upload.status) ||
      (context.uploadId && context.uploadId !== workspace.upload.id);
    if (localMode && uploadId && !uploadController && workspace.active_job?.kind !== 'prepare' && transferNeedsCheck) {
      const record = await uploadControlRequest(context, '/mentor/api/local-uploads/' + encodeURIComponent(uploadId),
        'GET', undefined, false);
      if (workspace?.id !== id) return;
      context.uploadId = uploadId;
      context.filename = record.filename;
      context.size = record.size;
      context.loaded = record.offset;
      context.phase = record.status;
      context.mustCheck = false;
      uploadFeedback = context;
    }
    const next = await uploadControlRequest(context,
      '/mentor/api/workspaces/' + id + '?draft_revision=' + (workspace.draft_revision || 0),
      'GET', undefined, false);
    if (workspace?.id !== id) return;
    acceptWorkspace(next);
    uploadStatusIssue = null;
    if (uploadFeedback?.workspaceId === id) {
      uploadFeedback.mustCheck = false;
      if (!uploadController) {
        if (uploadFeedback.uploadId && !localMode) uploadFeedback.phase = 'saved';
        else if (uploadFeedback.uploadId && next.upload &&
            (next.active_job?.kind === 'prepare' ||
              (next.upload.upload_id || next.upload.id) === uploadFeedback.uploadId)) uploadFeedback.phase = 'saved';
        else if (next.active_job?.kind === 'prepare') uploadFeedback = null;
        else if (!localMode || !uploadFeedback.uploadId) uploadFeedback.phase = 'not_started';
      }
      if (localMode && uploadFeedback?.workspaceId === id)
        uploadFeedback.mustCheck = uploadPresentation()?.phase === 'awaiting_upload';
    }
    await renderWorkspace();
  } catch (error) {
    if (workspace?.id === id) {
      context.error = error.message;
      context.phase = 'error';
      context.mustCheck = true;
      if (!uploadFeedback || uploadFeedback.workspaceId === id) uploadFeedback = context;
    }
  } finally {
    uploadChecking = false;
    renderFlow();
  }
}

function updateProcessingStatus() {
  const status = el('mentor-status');
  let text = '';
  if (uploadController) text = uploadPresentation()?.title || 'Starting upload…';
  else if (workspace?.active_job) {
    const labels = {prepare: 'Preparing your uploaded content', interview: 'Preparing the mentor reply',
      analysis: 'Generating your draft', review: 'Reviewing your draft', build: 'Building your download'};
    const assessmentPhases = {assessment_baseline: 'Assessing the imported instructional baseline with your selected AI',
      assessing_baseline: 'Assessing the imported instructional baseline with your selected AI',
      assessment_draft: 'Assessing every proposed draft item with your selected AI',
      assessing_draft: 'Assessing every proposed draft item with your selected AI',
      drafting: 'Generating your proposed draft with your selected AI'};
    text = (localMode && workspace.active_job.message ? workspace.active_job.message
      : localMode && assessmentPhases[workspace.active_job.phase] ? assessmentPhases[workspace.active_job.phase]
      : localHax && workspace.active_job.kind === 'build' ? 'Building your local HAX site'
      : labels[workspace.active_job.kind] || 'Processing your course') + '… Progress is saved.';
  } else text = busy ? 'Working…' : '';
  setLiveText('mentor-status', text);
  status.hidden = !text;
}

function setLiveText(id, text) {
  if (el(id).textContent !== text) el(id).textContent = text;
}
function renderFlow() {
  updateProcessingStatus();
  renderAssessmentState();
  renderUploadFeedback();
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
  const localNeedsRecovery = localMode && uploadPresentation()?.needsCheck;
  el('mentor-provider').disabled = waiting;
  el('mentor-model').disabled = waiting;
  el('manual-model-enabled').disabled = waiting;
  el('manual-model').disabled = waiting;
  ['save-provider-key', 'forget-provider-key', 'refresh-models', 'save-model'].forEach(id => {
    el(id).disabled = waiting;
  });
  el('workspace-list').disabled = busy;
  el('new-workspace').querySelector('button').disabled = busy;
  el('import-local-source').disabled = waiting || !workspace || localNeedsRecovery;
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
    el(id).querySelector('button').disabled = waiting || localNeedsRecovery ||
      !!(uploadFeedback?.workspaceId === workspace.id && uploadFeedback.mustCheck);
  });
  const interviewed = (workspace.jobs || []).some(job => job.kind === 'interview' &&
    job.status === 'completed' && job.revision === workspace.revision);
  el('approve-direction').disabled = waiting || !interviewed || !workspace.brief.trim() ||
    el('design-brief').value !== workspace.brief;
  el('analyze-course').disabled = waiting || !mode.ready || !inputs.ready ||
    el('design-brief').value !== workspace.brief ||
    workspace.direction_revision !== workspace.revision;
  el('approve-build').disabled = waiting || !workspace.draft || draftEditsPending() ||
    !assessmentReady() ||
    workspace.reviewed_draft_revision !== workspace.draft_revision;
  el('build-course').disabled = waiting || !workspace.draft || draftEditsPending() ||
    !assessmentReady() ||
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
  if (uploadStatusIssue?.workspaceId === id) uploadStatusIssue = null;
}

async function api(url, method = 'GET', body, {signal} = {}) {
  const options = {method, headers: {'X-Mentor-CSRF': mentorCSRF}};
  if (signal) options.signal = signal;
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || 'The action failed.');
    error.httpStatus = response.status;
    throw error;
  }
  return data;
}
el('local-auth-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    await api('/mentor/api/local-auth/config', 'POST', {auth_url: el('local-auth-url').value.trim()});
    window.location.reload();
  });
};
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

function copyLocalChunk(context, file, offset, end) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const finish = callback => {
      context.controller.signal.removeEventListener('abort', abort);
      callback();
    };
    xhr.open('PUT', '/mentor/api/local-uploads/' + encodeURIComponent(context.uploadId) + '?offset=' + offset);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Mentor-CSRF', mentorCSRF);
    xhr.timeout = 120000;
    xhr.upload.onprogress = event => {
      if (uploadFeedback !== context) return;
      context.loaded = Math.min(end, offset + event.loaded);
      context.percentage = file.size ? context.loaded / file.size * 100 : 0;
      context.lastProgressAt = Date.now();
      renderUploadFeedback();
    };
    xhr.onload = () => finish(() => {
      let data;
      try { data = JSON.parse(xhr.responseText); }
      catch (_) { reject(new Error('The local app returned an invalid copy response. Check saved progress.')); return; }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(data.error || 'The local copy failed. Check saved progress.'));
      } else if (data.offset !== end) {
        reject(new Error('The local copy offset could not be confirmed. Check saved progress before retrying.'));
      } else resolve(data);
    });
    xhr.onerror = () => finish(() => reject(new Error('The local app connection was interrupted. Check saved progress.')));
    xhr.ontimeout = () => finish(() => reject(new Error('The local copy timed out. Check saved progress.')));
    xhr.onabort = () => finish(() => reject(new DOMException('Local copy canceled', 'AbortError')));
    context.controller.signal.addEventListener('abort', abort, {once: true});
    if (context.controller.signal.aborted) {
      finish(() => reject(new DOMException('Local copy canceled', 'AbortError')));
      return;
    }
    xhr.send(file.slice(offset, end));
  });
}

async function uploadLocal(file) {
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > 8 * 1024 ** 3)
    throw new Error('Choose a nonempty Canvas export or prepared package no larger than 8 GiB.');
  const workspaceId = workspace.id;
  const revision = workspace.revision;
  uploadController = new AbortController();
  const context = uploadFeedback = {workspaceId, kind: 'course', filename: file.name, size: file.size,
    controller: uploadController, phase: 'authorizing', percentage: null, loaded: 0,
    startedAt: Date.now(), changedAt: Date.now(), lastProgressAt: Date.now(), mustCheck: false};
  uploadStatusIssue = null;
  renderFlow();
  try {
    const record = await uploadControlRequest(context,
      '/mentor/api/workspaces/' + workspaceId + '/local-uploads', 'POST',
      {revision, filename: file.name, size: file.size}, false);
    context.uploadId = record.id;
    if (!record.id || record.offset !== 0 || !Number.isSafeInteger(record.chunk_size) || record.chunk_size <= 0)
      throw new Error('The local app returned an invalid import reservation. Check saved progress.');
    const chunkSize = Math.min(record.chunk_size, 8 * 1024 ** 2);
    context.phase = 'transferring';
    context.changedAt = Date.now();
    renderFlow();
    for (let offset = 0; offset < file.size;) {
      if (context.controller.signal.aborted) throw new DOMException('Local copy canceled', 'AbortError');
      const end = Math.min(file.size, offset + chunkSize);
      await copyLocalChunk(context, file, offset, end);
      offset = end;
      context.loaded = end;
      context.percentage = end / file.size * 100;
    }
    if (context.controller.signal.aborted) throw new DOMException('Local copy canceled', 'AbortError');
    context.phase = 'verifying';
    context.changedAt = Date.now();
    renderFlow();
    // Allow completion to settle even if cancellation is requested; DELETE then fences its result.
    const next = await uploadControlRequest(context, '/mentor/api/local-uploads/' +
      encodeURIComponent(context.uploadId) + '/complete', 'POST', {}, false);
    if (context.controller.signal.aborted) throw new DOMException('Local copy canceled', 'AbortError');
    if (workspace?.id === workspaceId) {
      acceptWorkspace(next);
      context.phase = 'saved';
      await renderWorkspace();
    }
  } catch (error) {
    context.phase = 'error';
    context.mustCheck = true;
    context.error = error.message;
    if (context.controller.signal.aborted && context.uploadId) {
      try {
        await uploadControlRequest(context, '/mentor/api/local-uploads/' + encodeURIComponent(context.uploadId),
          'DELETE', undefined, false);
        context.phase = 'canceled';
        context.mustCheck = false;
        context.error = '';
        if (workspace?.id === workspaceId) { await fetchWorkspace(); await renderWorkspace(); }
      } catch (_) {
        context.error = 'Copy stopped in this browser. Check saved progress to confirm local cancellation.';
      }
    }
    if (context.phase !== 'canceled') {
      const failure = new Error(context.error);
      failure.uploadHandled = true;
      throw failure;
    }
  } finally {
    if (uploadController === context.controller) uploadController = null;
    renderFlow();
  }
}

function importLocalSource() { action(async () => {
  if (!localMode || !localSource?.id || !workspace) return;
  const id = workspace.id;
  uploadFeedback = {workspaceId: id, filename: localSource.filename, size: localSource.size,
    kind: 'course', phase: 'processing', startedAt: Date.now()};
  renderFlow();
  try {
    workspace = await api(endpoint('/local-source'), 'POST', {revision: workspace.revision, source_id: localSource.id});
    blankCourses.delete(id);
    uploadFeedback = null;
    await renderWorkspace();
  } catch (error) {
    uploadFeedback = null;
    throw error;
  }
}); }

async function uploadHosted(file, kind, role = 'reference') {
  const workspaceId = workspace.id;
  const base = '/mentor/api/workspaces/' + workspaceId;
  uploadController = new AbortController();
  const context = uploadFeedback = {workspaceId, kind, filename: file.name, size: file.size,
    controller: uploadController, phase: 'authorizing', percentage: null, loaded: 0,
    startedAt: Date.now(), changedAt: Date.now(), lastProgressAt: Date.now(), mustCheck: false};
  let authorizationStarted = false;
  uploadStatusIssue = null;
  renderFlow();
  try {
    if (!window.HaxHostedUpload)
      throw new Error('Hosted upload support is missing. Run the hosted build before deployment.');
    authorizationStarted = true;
    const intent = await uploadControlRequest(context, base + '/uploads', 'POST', {
      revision: workspace.revision, filename: file.name, size: file.size, kind, role,
    });
    context.uploadId = intent.upload_id;
    if (context.controller.signal.aborted) throw new DOMException('Upload stopped', 'AbortError');
    context.phase = 'transferring';
    context.changedAt = Date.now();
    renderFlow();
    // Start the SDK immediately, without another workspace/inventory GET.
    await window.HaxHostedUpload.upload(file, intent, {
      signal: context.controller.signal,
      onProgress: progress => {
        if (uploadFeedback !== context) return;
        const percent = Number.isFinite(progress.percentage) ? Math.max(0, Math.min(100, progress.percentage)) : null;
        const loaded = Number.isFinite(progress.loaded) ? Math.max(0, Math.min(file.size, progress.loaded))
          : percent === null ? context.loaded : file.size * percent / 100;
        if (loaded !== context.loaded || percent !== context.percentage) context.lastProgressAt = Date.now();
        context.percentage = percent;
        context.loaded = loaded;
        renderUploadFeedback();
      },
    });
    context.phase = 'verifying';
    context.percentage = 100;
    context.loaded = file.size;
    context.changedAt = Date.now();
    renderFlow();
    // Browser data is not proof: the server HEADs the registered private path.
    await uploadControlRequest(context, '/mentor/api/uploads/' + intent.upload_id + '/complete', 'POST', {});
    context.phase = 'refreshing';
    context.changedAt = Date.now();
    renderFlow();
    const next = await uploadControlRequest(context,
      base + '?draft_revision=' + (workspace.draft_revision || 0));
    if (workspace?.id === workspaceId) {
      acceptWorkspace(next);
      context.phase = 'saved';
      await renderWorkspace();
    }
  } catch (error) {
    context.phase = 'error';
    context.mustCheck = authorizationStarted;
    let message;
    if (context.controller.signal.aborted) {
      message = context.uploadId
        ? 'Transfer stopped in this browser. Check saved progress to confirm cancellation.'
        : 'Stopped waiting for upload access. Check saved progress for an interrupted reservation before retrying.';
      if (context.uploadId) {
        try {
          const next = await uploadControlRequest(context, base + '/cancel-job',
            'POST', {job_id: context.uploadId}, false);
          if (workspace?.id === workspaceId) acceptWorkspace(next);
          context.mustCheck = false;
          context.phase = 'saved';
          message = next.upload?.status === 'ready' ? 'The file had already finished processing before cancellation.'
            : 'Transfer canceled. Temporary upload data is scheduled for cleanup.';
        } catch (_) {}
      }
    } else {
      message = error.name === 'UploadControlError' || !authorizationStarted ? error.message
        : 'The file transfer did not finish. Check your connection and saved progress before retrying.';
    }
    context.error = message;
    const failure = new Error(message);
    failure.uploadHandled = true;
    throw failure;
  } finally {
    if (uploadController === context.controller) uploadController = null;
    renderFlow();
  }
}
function abortHostedUpload() {
  if (uploadController) uploadController.abort();
  else if (localMode && localUploadID()) action(async () => {
    await api('/mentor/api/local-uploads/' + encodeURIComponent(localUploadID()), 'DELETE');
    uploadFeedback = null;
    await fetchWorkspace();
    await renderWorkspace();
  });
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
    if (workspace && !error.uploadHandled) {
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

function renderLocalJobError() {
  const latest = (workspace?.jobs || []).filter(job => ['prepare', 'build'].includes(job.kind)).at(-1);
  const failed = localMode && !workspace?.active_job && latest?.status === 'failed';
  el('local-job-error').hidden = !failed;
  if (!failed) {
    el('local-job-error').textContent = '';
    return;
  }
  const label = latest.kind === 'prepare' ? 'Local course preparation failed.' : 'Local HAX build failed.';
  const guidance = latest.kind === 'prepare'
    ? ' Your original file is unchanged. Resolve the issue before importing again.'
    : ' Your approved draft is preserved. Resolve the issue before building again.';
  setLiveText('local-job-error', label + ' ' + (latest.error || 'Check local processing details and retry.') + guidance);
}

function verifiedHaxURL(status) {
  if (!status?.running || !status.url) return null;
  try {
    const url = new URL(status.url);
    if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) ||
        url.username || url.password) return null;
    return url.href;
  } catch (_) { return null; }
}

function renderHaxStatus() {
  const site = workspace?.artifact?.hax_site;
  const shown = localHax && !!site;
  el('hax-site-panel').hidden = !shown;
  if (!shown) return;
  const current = haxStatus?.workspaceId === workspace.id && haxStatus.directory === site.directory
    ? haxStatus : null;
  const url = verifiedHaxURL(current);
  const starting = !!current && ['starting', 'preparing', 'prepare_runtime', 'verify', 'verifying', 'launching'].includes(current.phase);
  el('hax-site-directory').textContent = site.directory || current?.directory || 'Not available';
  setLiveText('hax-site-status', haxBusy ? 'Updating local site lifecycle…'
    : url ? 'Your local HAX site is running and ready.'
    : current?.phase ? 'Local site: ' + current.phase.replace(/_/g, ' ')
    : 'Checking saved local site status…');
  el('hax-site-error').hidden = !current?.error;
  el('hax-site-error').textContent = current?.error || '';
  el('open-hax-site').hidden = !url;
  if (url) el('open-hax-site').href = url;
  else el('open-hax-site').removeAttribute('href');
  el('start-hax-site').hidden = !current;
  el('start-hax-site').textContent = current?.running ? 'Restart HAX site' : 'Start HAX site';
  el('start-hax-site').disabled = haxBusy || starting || !!workspace.active_job;
  el('stop-hax-site').hidden = !current || (!current.running && !starting);
  el('stop-hax-site').disabled = haxBusy;
  el('refresh-hax-site').disabled = haxBusy;
}

async function refreshHaxSite() {
  clearTimeout(haxPollTimer);
  if (haxBusy) return;
  const site = workspace?.artifact?.hax_site;
  if (!localHax || !site) { renderHaxStatus(); return; }
  const id = workspace.id;
  const directory = site.directory;
  const request = ++haxRequest;
  try {
    const status = await api('/mentor/api/workspaces/' + id + '/hax');
    if (request !== haxRequest || workspace?.id !== id || workspace.artifact?.hax_site?.directory !== directory) return;
    haxStatus = {...status, workspaceId: id, directory};
  } catch (error) {
    if (request !== haxRequest || workspace?.id !== id || workspace.artifact?.hax_site?.directory !== directory) return;
    haxStatus = {workspaceId: id, directory, running: false, phase: 'status unavailable', error: error.message};
  }
  renderHaxStatus();
  haxPollTimer = setTimeout(refreshHaxSite, 2000);
}

async function changeHaxSite(command) {
  const site = workspace?.artifact?.hax_site;
  if (!localHax || !site || haxBusy || workspace.active_job) return;
  const id = workspace.id;
  const directory = site.directory;
  ++haxRequest;
  clearTimeout(haxPollTimer);
  haxBusy = true;
  haxStatus = {workspaceId: id, directory, running: false, phase: command === 'stop' ? 'stopping' : 'starting'};
  renderHaxStatus();
  try {
    const base = '/mentor/api/workspaces/' + id + '/hax/';
    if (command === 'restart') await api(base + 'stop', 'POST', {});
    const status = await api(base + (command === 'stop' ? 'stop' : 'start'), 'POST', {});
    if (workspace?.id !== id || workspace.artifact?.hax_site?.directory !== directory) return;
    haxStatus = {...status, workspaceId: id, directory};
  } catch (error) {
    if (workspace?.id === id && workspace.artifact?.hax_site?.directory === directory)
      haxStatus = {workspaceId: id, directory, running: false, phase: 'failed', error: error.message};
  } finally {
    haxBusy = false;
    renderHaxStatus();
    if (workspace?.id === id && workspace.artifact?.hax_site?.directory === directory)
      haxPollTimer = setTimeout(refreshHaxSite, 1000);
  }
}
function startHaxSite() { return changeHaxSite(haxStatus?.workspaceId === workspace?.id && haxStatus.running ? 'restart' : 'start'); }
function stopHaxSite() { return changeHaxSite('stop'); }

async function renderWorkspace() {
  if (!workspace) return;
  const renderId = workspace.id;
  const renderRevision = workspace.revision;
  clearTimeout(pollTimer);
  el('workspace-content').hidden = false;
  renderLocalJobError();
  el('workspace-title').textContent = workspace.title;
  const stages = {brief: 'Prepare your inputs and discuss your goals.', uploading: 'Uploading course content.',
    preparing: 'Preparing your course content.', interviewing: 'The mentor is preparing a reply.',
    direction_approved: 'Direction approved. Ready to generate a draft.', analyzing: 'Generating the draft.',
    draft: 'Draft ready for your review.', reviewing: 'Reviewing the draft.',
    build_approved: 'Draft approved. Ready to build.', building: 'Building your download.',
    built: 'Approved content is ready to download.'};
  el('workspace-stage').textContent = stages[workspace.stage] || 'Your workspace is saved.';
  if (localHax && workspace.stage === 'building') el('workspace-stage').textContent = 'Building and starting your local HAX site.';
  if (localHax && workspace.stage === 'built') el('workspace-stage').textContent = 'Approved content is built. Open or start the local HAX site below.';
  el('workspace-revisions').textContent =
    `Stage: ${workspace.stage} · input revision ${workspace.revision} · draft revision ${workspace.draft_revision}`;
  if (workspace.upload) {
    const upload = workspace.upload;
    el('upload-state').textContent =
      `Source: ${upload.filename} · ${upload.status}${upload.phase ? ' / ' + upload.phase : ''} · ` +
      (localMode ? 'Course content and supported assets stay on this computer. Your original file is unchanged.'
        : upload.cleanup_status === 'deleted' ? 'Original cloud upload deletion accepted.'
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
  renderAssessments();
  el('artifact-links').replaceChildren();
  if (workspace.artifact && workspace.stage === 'built') {
    const link = itemText(el('artifact-links'), 'a', 'Download approved course content');
    link.href = endpoint('/download'); link.className = 'btn btn-ghost';
    const preview = itemText(el('artifact-links'), 'a', 'Preview student view (opens in a new tab)');
    preview.href = endpoint('/preview/index.html'); preview.target = '_blank';
    preview.rel = 'noopener noreferrer'; preview.className = 'btn btn-primary';
    el('artifact-links').replaceChildren(preview, link);
    if (localHax) {
      preview.className = 'btn btn-ghost';
      preview.textContent = 'Static preview (secondary; opens in a new tab)';
      if (workspace.artifact.omitted_preview_assets > 0)
        itemText(el('artifact-links'), 'p',
          `Compact static preview omits ${workspace.artifact.omitted_preview_assets} large assets; open the HAX site for supported local media.`);
    }
  }
  renderHaxStatus();
  if (localHax && workspace.artifact?.hax_site) refreshHaxSite();
  else { clearTimeout(haxPollTimer); ++haxRequest; }
  el('mentor-jobs').textContent = (workspace.jobs || []).map(j =>
    `${j.kind}: ${j.status}${j.error ? ' — ' + j.error : ''}`).join('\n') || 'No processing jobs yet.';
  el('cancel-mentor-job').hidden = !workspace.active_job;
  renderFlow();
  if (workspace.active_job?.kind === 'prepare') {
    el('course-inventory').textContent = 'The new file’s inventory will appear here after preparation finishes. Existing course content is not replaced until the result is verified.';
  } else {
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
      if (workspace?.id !== renderId || workspace.revision !== renderRevision) return;
      inventories.delete(renderId);
      inventoryErrors.set(renderId, true);
      el('course-inventory').textContent = error.message;
    }
  }
  renderFlow();
  if (workspace.active_job) pollTimer = setTimeout(async () => {
    try { await fetchWorkspace(); await renderWorkspace(); }
    catch (error) {
      if (workspace?.id !== renderId) return;
      el('mentor-status').hidden = false;
      el('mentor-status').textContent = 'Progress could not be refreshed. Reopen this workspace to retry; a saved job may still be running.';
      if (workspace.active_job?.kind === 'prepare') {
        uploadStatusIssue = {workspaceId: renderId, message: 'The latest server progress could not be checked.'};
        renderUploadFeedback();
      }
    }
  }, 1000);
}

async function updateCredentials() {
  const data = await api('/mentor/api/session');
  mentorCSRF = data.csrf;
  hosted = data.hosted_ready;
  localMode = data.local_mode === true;
  localHax = localMode && data.local_hax === true;
  localSource = localMode ? data.local_source || null : null;
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
  el('local-upload-help').hidden = !localMode;
  el('local-auth-setup').hidden = !localMode || !data.local_auth_configurable;
  el('local-source-choice').hidden = !localSource;
  el('local-source-label').textContent = localSource
    ? `Startup course: ${localSource.filename} · ${formatUploadBytes(localSource.size)}` : '';
  if (localMode) {
    el('provider-privacy-help').textContent = 'Guided mode makes no AI calls and does not invent analysis scores. With AI enabled, course excerpts and your design brief are sent to the selected provider. Provider keys are encrypted and saved only on this computer, never in browser storage or chat. Neon handles identity only.';
    el('compact-package-help').textContent = 'Optional: create a compact instructional review package first (excluded assets cannot be recovered from it)';
    el('account-login-help').textContent = 'Use the existing Neon email service to verify your identity only. All course work, conversations, provider keys and generated sites remain on this computer. Sign in here with the same account to resume; another computer will not have these files.';
    el('workspace-storage-help').textContent = data.local_accounts
      ? 'Account-scoped local workspaces: sign in with the same verified email account on this computer to resume multiple courses. Neon stores identity only; it does not store or synchronize your courses, conversations, keys or sites. Never paste keys into chat.'
      : 'Browser-local fallback (not a verified email account): course work and keys stay on this computer and access is tied to this browser identity. Clearing cookies can lose access. Configure email sign-in above for account-scoped local workspaces; it does not synchronize course data.';
    el('upload-course-button').textContent = 'Import & prepare locally';
  }
  el('flow-step-6').textContent = localHax ? '6. HAX site' : '6. Download';
  el('build-course').textContent = localHax ? 'Build & open HAX site' : 'Build approved content';
  el('completion-title').textContent = localHax ? '6. Build & open HAX site' : '6. Download approved content';
  el('completion-help').textContent = localHax
    ? 'Build the exact approved snapshot into a real local HAX site and start its server. Nothing is published. Static preview and download remain secondary options.'
    : 'Building prepares a private download and preview. It does not publish a public HAX site.';
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
    if (localMode) {
      await uploadLocal(file);
      return;
    }
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
  if (!assessmentReady()) throw new Error('Complete a current AI final-review assessment before approving this draft.');
  if (workspace.draft.items.some((item, i) =>
    el('draft-title-' + i).value !== item.title || el('draft-html-' + i).value !== item.html))
    throw new Error('Save snapshot edits before approval.');
  workspace = await api(endpoint('/approve-build'), 'POST', {draft_revision: workspace.draft_revision});
  await renderWorkspace();
}); }
function buildCourse() { action(async () => {
  if (!assessmentReady()) throw new Error('Complete a current AI final-review assessment before building this draft.');
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
