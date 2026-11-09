/*
 * Receive a course from the Course Consultant browser extension and upload
 * it with this page's own session. The extension never sees our session.
 */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const status = message => { $('handoff-status').textContent = message; };
  function fail(message) {
    $('handoff-error').textContent = message;
    $('handoff-error').hidden = false;
    $('handoff-status').hidden = true;
  }

  function extensionId() {
    const id = new URLSearchParams(location.search).get('ext') || '';
    return /^[a-p]{32}$/.test(id) ? id : null;
  }

  const bridged = new URLSearchParams(location.search).get('bridge') === '1';

  /** Firefox: talk to the extension's bridge content script with window messages. */
  function askBridge(message) {
    return new Promise((resolve, reject) => {
      const nonce = Array.from(crypto.getRandomValues(new Uint8Array(12)), b => b.toString(16).padStart(2, '0')).join('');
      // The bridge may load after this script; re-send until it answers.
      const post = () => window.postMessage({ source: 'course-consultant-page', type: message.type, nonce }, location.origin);
      const retry = setInterval(post, 400);
      const timer = setTimeout(() => {
        clearInterval(retry);
        window.removeEventListener('message', listen);
        reject(new Error('The Course Consultant extension did not respond. Is it installed in this browser?'));
      }, 10000);
      function listen(event) {
        const data = event.data;
        if (event.source !== window || event.origin !== location.origin || !data
            || data.source !== 'course-consultant-extension' || data.nonce !== nonce) return;
        clearTimeout(timer);
        clearInterval(retry);
        window.removeEventListener('message', listen);
        const response = data.response;
        if (!response || response.error) reject(new Error((response && response.error) || 'No course was received.'));
        else resolve(response);
      }
      window.addEventListener('message', listen);
      post();
    });
  }

  function ask(id, message) {
    if (bridged) return askBridge(message);
    return new Promise((resolve, reject) => {
      if (!(window.chrome && chrome.runtime && chrome.runtime.sendMessage)) {
        reject(new Error('The Course Consultant extension is not installed in this browser.'));
        return;
      }
      chrome.runtime.sendMessage(id, message, response => {
        if (chrome.runtime.lastError) reject(new Error('The extension did not respond. Try sending the course again.'));
        else if (!response || response.error) reject(new Error((response && response.error) || 'No course was received.'));
        else resolve(response);
      });
    });
  }

  async function json(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status}).`);
    return body;
  }

  async function main() {
    const id = bridged ? 'bridge' : extensionId();
    if (!id) { fail('Open this page from the Course Consultant extension.'); return; }
    let received;
    try { received = await ask(id, { type: 'get-bundle' }); } catch (error) { fail(error.message); return; }
    const session = await json('/mentor/api/session');
    if (session.login_required || session.account_required) {
      fail('Sign in to Course Consultant in this browser, then send the course again from the extension.');
      return;
    }
    const csrf = session.csrf;
    const workspaces = await json('/mentor/api/workspaces');
    const select = $('handoff-workspace');
    for (const workspace of (workspaces.workspaces || workspaces)) {
      const option = document.createElement('option');
      option.value = workspace.id;
      option.textContent = workspace.title;
      select.append(option);
    }
    const preview = received.preview || {};
    $('handoff-course').textContent = preview.course || 'Canvas course';
    const counts = preview.counts || {};
    $('handoff-counts').textContent = ['pages', 'assignments', 'quizzes', 'discussions', 'rubrics', 'modules']
      .map(key => `${counts[key] || 0} ${key}`).join(' · ');
    $('handoff-review').hidden = false;
    status('Review and import.');

    $('handoff-import').addEventListener('click', async () => {
      const button = $('handoff-import');
      button.disabled = true;
      $('handoff-error').hidden = true;
      status('Importing…');
      $('handoff-status').hidden = false;
      try {
        let workspace;
        if (select.value) {
          workspace = await json(`/mentor/api/workspaces/${encodeURIComponent(select.value)}`);
        } else {
          workspace = await json('/mentor/api/workspaces', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Mentor-CSRF': csrf },
            body: JSON.stringify({ title: (preview.course || 'Canvas course').slice(0, 200) }),
          });
        }
        // Send in pieces under 4 MB: hosting platforms cap request size.
        const blob = new Blob([JSON.stringify(received.bundle)], { type: 'application/json' });
        const piece = 3.5 * 1024 * 1024;
        const total = Math.max(1, Math.ceil(blob.size / piece));
        const uploadId = [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
        for (let index = 0; index < total; index++) {
          status(`Uploading part ${index + 1} of ${total}…`);
          const chunk = new FormData();
          chunk.append('upload_id', uploadId);
          chunk.append('index', String(index));
          chunk.append('total', String(total));
          chunk.append('chunk', blob.slice(index * piece, (index + 1) * piece), 'part');
          await json(`/mentor/api/workspaces/${encodeURIComponent(workspace.id)}/intake/chunks`, {
            method: 'POST', headers: { 'X-Mentor-CSRF': csrf }, body: chunk });
        }
        const form = new FormData();
        form.append('revision', String(workspace.revision));
        form.append('instructor_name', $('handoff-instructor').value.trim());
        form.append('upload_id', uploadId);
        const result = await json(`/mentor/api/workspaces/${encodeURIComponent(workspace.id)}/intake/extension`, {
          method: 'POST', headers: { 'X-Mentor-CSRF': csrf }, body: form,
        });
        await ask(id, { type: 'imported' }).catch(() => {});
        const removed = result.course && result.course.privacy ? result.course.privacy.total : 0;
        status(`Imported ${result.course ? result.course.title : 'your course'}. ` +
          (removed ? `${removed} likely student details were removed.` : 'No student details were found.'));
        $('handoff-review').hidden = true;
        $('handoff-done').hidden = false;
      } catch (error) {
        fail(error.message);
        button.disabled = false;
      }
    });
  }

  main().catch(error => fail(error.message || 'Something went wrong.'));
})();
