'use strict';

const sections = ['not-course', 'start', 'working', 'review', 'failed'];
let tabId = null;
let timer = null;

function show(id) {
  for (const name of sections) document.getElementById(name).hidden = name !== id;
}

function text(id, value) { document.getElementById(id).textContent = value; }

function send(message) { return chrome.runtime.sendMessage(message); }

const LABELS = {
  modules: 'Modules', pages: 'Pages', assignments: 'Assignments', quizzes: 'Quizzes',
  discussions: 'Discussion prompts', rubrics: 'Rubrics', outcomes: 'Outcomes', files: 'Files (names only)',
};

function render(state) {
  clearTimeout(timer);
  if (state.status === 'collecting') {
    show('working');
    text('step', state.progress || 'starting');
    timer = setTimeout(refresh, 500);
    return;
  }
  if (state.status === 'ready' && state.preview) {
    show('review');
    text('course-name', state.preview.course || 'this course');
    const counts = document.getElementById('counts');
    counts.replaceChildren();
    for (const [key, label] of Object.entries(LABELS)) {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = String(state.preview.counts[key] ?? 0);
      counts.append(dt, dd);
    }
    const items = document.getElementById('items');
    items.replaceChildren(...state.preview.items.map(title => {
      const li = document.createElement('li');
      li.textContent = title;
      return li;
    }));
    const warnings = document.getElementById('warnings');
    warnings.replaceChildren(...state.preview.warnings.slice(0, 10).map(w => {
      const li = document.createElement('li');
      li.textContent = w;
      return li;
    }));
    return;
  }
  if (state.status === 'error') {
    show('failed');
    text('error', state.error || 'Could not read this course.');
    return;
  }
  show(tabId === null ? 'not-course' : 'start');
}

async function refresh() { render(await send({ type: 'state' })); }

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // The tab URL is visible here because the click granted activeTab.
  const isCourse = tab && /^https?:\/\/[^/]+\/courses\/\d+(?:\/|$)/.test(tab.url || '');
  tabId = isCourse ? tab.id : null;
  await refresh();
}

document.getElementById('collect').addEventListener('click', async () => {
  if (tabId === null) return;
  render(await send({ type: 'collect', tabId }));
  setTimeout(refresh, 300);
});
document.getElementById('send').addEventListener('click', async () => {
  const result = await send({ type: 'send' });
  if (result && result.error) { show('failed'); text('error', result.error); } else window.close();
});
document.getElementById('discard').addEventListener('click', async () => render(await send({ type: 'discard' })));
document.getElementById('retry').addEventListener('click', async () => render(await send({ type: 'discard' })));

init();
