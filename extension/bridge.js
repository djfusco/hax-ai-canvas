/*
 * Hand-off bridge for browsers without `externally_connectable` (Firefox).
 * Runs only on Course Consultant's import page (see the build's
 * content_scripts) and relays two requests from that page to the extension:
 * get the waiting course, and mark it imported. Nothing else is relayed.
 */
(function () {
  'use strict';
  const ext = globalThis.browser || globalThis.chrome;
  const allowedOrigin = globalThis.COURSE_CONSULTANT_ORIGIN;
  if (!allowedOrigin || location.origin !== allowedOrigin || location.pathname !== '/mentor/import/extension') return;

  function send(message) {
    try {
      const result = ext.runtime.sendMessage(message);
      if (result && typeof result.then === 'function') return result;
    } catch (_) { /* fall through to callback style */ }
    return new Promise(resolve => ext.runtime.sendMessage(message, resolve));
  }

  window.addEventListener('message', async event => {
    const data = event.data;
    if (event.source !== window || event.origin !== allowedOrigin || !data
        || data.source !== 'course-consultant-page' || !['get-bundle', 'imported'].includes(data.type)
        || typeof data.nonce !== 'string') return;
    let response;
    try {
      response = await send({ type: 'bridge-' + data.type });
    } catch (_) {
      response = { error: 'The extension did not respond. Try sending the course again.' };
    }
    window.postMessage({ source: 'course-consultant-extension', nonce: data.nonce, response }, allowedOrigin);
  });
  window.postMessage({ source: 'course-consultant-extension', type: 'ready' }, allowedOrigin);
})();
