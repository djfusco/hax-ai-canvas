/* Finish "Publish to HAX Lite" with a same-site request, which carries our session cookie.
   The server exchanges the approval code, uploads the course site and waits for the import;
   the HAX Lite pass never reaches this page. */
(async function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const code = params.get('code');
  const state = params.get('state');
  const error = params.get('error');
  history.replaceState(null, '', location.pathname); // keep the code out of history
  const started = Date.now();
  const timer = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    $('hax-lite-status').textContent = `Publishing your course site to HAX Lite and waiting for it to finish… (${s}s)`;
  }, 1000);

  function list(title, values) {
    if (!values || !values.length) return null;
    const wrap = document.createElement('div');
    const heading = document.createElement('h2');
    heading.textContent = title;
    const ul = document.createElement('ul');
    for (const value of values) {
      const li = document.createElement('li');
      li.textContent = value;
      ul.append(li);
    }
    wrap.append(heading, ul);
    return wrap;
  }

  try {
    if (!state) throw new Error('HAX Lite did not return to this page correctly. Start publishing again from Course Consultant.');
    const session = await (await fetch('/mentor/api/session', { credentials: 'same-origin' })).json();
    const response = await fetch('/mentor/api/hax-lite/finish', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Mentor-CSRF': session.csrf },
      body: JSON.stringify({ code, state, error }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Publishing failed (${response.status}).`);
    clearInterval(timer);
    $('hax-lite-status').hidden = true;
    const words = {
      complete: 'Published. Every page was added to HAX Lite.',
      partial: 'Published, but some content could not be added. See below.',
      failed: 'HAX Lite could not create the site.',
    };
    const counts = [body.report.pages != null ? `${body.report.pages} pages` : '', body.report.files != null ? `${body.report.files} files` : '']
      .filter(Boolean).join(', ');
    $('hax-lite-summary').textContent = (body.still_running
      ? 'HAX Lite is still importing the site. It will appear in My HAX shortly.'
      : (words[body.status] || `Import status: ${body.status}.`)) + (counts ? ` (${counts})` : '');
    if (body.site && body.site.url) {
      $('hax-lite-site').href = body.site.url;
      $('hax-lite-site').textContent = `Open “${body.site.title || body.site.slug}” in HAX Lite ↗`;
      $('hax-lite-link').hidden = false;
    }
    const report = body.report || {};
    $('hax-lite-report').replaceChildren(...[
      report.error ? (() => { const p = document.createElement('p'); p.className = 'banner banner-warn'; p.textContent = report.error; return p; })() : null,
      list('Left out', report.skipped), list('Warnings', report.warnings),
    ].filter(Boolean));
    $('hax-lite-result').hidden = false;
    if (body.workspace_id) $('hax-lite-back').href = '/mentor';
  } catch (e) {
    clearInterval(timer);
    $('hax-lite-status').hidden = true;
    $('hax-lite-error').textContent = e.message;
    $('hax-lite-error').hidden = false;
  }
})();
