/* Finish the Canvas OAuth exchange with a same-site request, which carries our session cookie. */
(async function () {
  'use strict';
  const status = document.getElementById('canvas-status');
  const error = document.getElementById('canvas-error');
  const params = new URLSearchParams(location.search);
  const code = params.get('code');
  const state = params.get('state');
  const denied = params.get('error');
  history.replaceState(null, '', location.pathname); // keep the code out of history
  try {
    if (denied) throw new Error('Canvas access was not granted.');
    if (!code || !state) throw new Error('Canvas did not return an authorization code.');
    const session = await (await fetch('/mentor/api/session', { credentials: 'same-origin' })).json();
    const response = await fetch('/mentor/api/canvas/exchange', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Mentor-CSRF': session.csrf },
      body: JSON.stringify({ code, state }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'The connection failed.');
    status.textContent = 'Connected. Choose your course…';
    setTimeout(() => location.replace('/mentor?canvas=connected'), 600);
  } catch (e) {
    status.hidden = true;
    error.textContent = e.message;
    error.hidden = false;
  }
})();
