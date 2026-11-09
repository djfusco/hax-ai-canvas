/* Finish OpenRouter OAuth with a same-site request, which carries our session cookie. */
(async function () {
  'use strict';
  const status = document.getElementById('openrouter-status');
  const error = document.getElementById('openrouter-error');
  const code = new URLSearchParams(location.search).get('code');
  history.replaceState(null, '', location.pathname); // keep the code out of history
  try {
    if (!code) throw new Error('OpenRouter did not return an authorization code.');
    const session = await (await fetch('/mentor/api/session', { credentials: 'same-origin' })).json();
    const response = await fetch('/mentor/api/openrouter/exchange', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Mentor-CSRF': session.csrf },
      body: JSON.stringify({ code }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || 'The connection failed.');
    status.textContent = 'OpenRouter is connected. Choose a model to continue.';
    setTimeout(() => location.replace('/mentor?provider=openrouter'), 800);
  } catch (e) {
    status.hidden = true;
    error.textContent = e.message;
    error.hidden = false;
  }
})();
