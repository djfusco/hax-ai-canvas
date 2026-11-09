/*
 * Background service worker. Owns the collection so it survives the popup
 * closing, and hands the finished bundle only to the Course Consultant page
 * (origin checked) that asks for it. Holds no Course Consultant credentials.
 */
importScripts('config.js', 'collect.js');

const state = { status: 'idle', tabId: null, progress: null, preview: null, error: null, bundle: null };

function publicState() {
  const { bundle, ...rest } = state;
  return rest;
}

function courseFromUrl(url) {
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/courses\/(\d+)(?:\/|$)/);
    return match && /^https?:$/.test(parsed.protocol) ? { origin: parsed.origin, courseId: match[1] } : null;
  } catch (_) { return null; }
}

async function collectFromTab(tab) {
  const target = courseFromUrl(tab.url || '');
  if (!target) throw new Error('Open a course in Canvas, then click the extension again.');
  Object.assign(state, { status: 'collecting', tabId: tab.id, progress: 'starting', preview: null, error: null, bundle: null });
  // activeTab grants access to this tab only, only after the instructor's click.
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['collect.js'] });
  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: async (origin, courseId) => {
      const report = step => chrome.runtime.sendMessage({ type: 'progress', step }).catch(() => {});
      try {
        return { bundle: await self.CourseCollector.collect({ origin, courseId, onProgress: report }) };
      } catch (error) {
        return { error: error && error.message ? error.message : 'Could not read this course.' };
      }
    },
    args: [target.origin, target.courseId],
  });
  const value = result && result.result;
  if (!value || value.error) throw new Error((value && value.error) || 'Could not read this course.');
  state.bundle = value.bundle;
  state.preview = self.CourseCollector.preview(value.bundle);
  state.status = 'ready';
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (message.type === 'progress') {
    if (sender.tab && sender.tab.id === state.tabId) state.progress = message.step;
    return false;
  }
  if (message.type === 'state') { reply(publicState()); return false; }
  if (message.type === 'collect') {
    chrome.tabs.get(message.tabId).then(collectFromTab).catch(error => {
      Object.assign(state, { status: 'error', error: error.message, bundle: null });
    });
    reply(publicState());
    return false;
  }
  if (message.type === 'send') {
    if (state.status !== 'ready') { reply({ error: 'Read the course first.' }); return false; }
    const url = new URL('/mentor/import/extension', self.COURSE_CONSULTANT_ORIGIN);
    url.searchParams.set('ext', chrome.runtime.id);
    chrome.tabs.create({ url: url.href });
    reply({ ok: true });
    return false;
  }
  if (message.type === 'discard') {
    Object.assign(state, { status: 'idle', tabId: null, progress: null, preview: null, error: null, bundle: null });
    reply(publicState());
    return false;
  }
  return false;
});

// Only the configured Course Consultant origin may receive the course.
chrome.runtime.onMessageExternal.addListener((message, sender, reply) => {
  if (!sender.origin || sender.origin !== self.COURSE_CONSULTANT_ORIGIN) {
    reply({ error: 'This site is not Course Consultant.' });
    return false;
  }
  if (message && message.type === 'get-bundle') {
    reply(state.status === 'ready' ? { bundle: state.bundle, preview: state.preview } : { error: 'No course is waiting. Use the extension in Canvas first.' });
    return false;
  }
  if (message && message.type === 'imported') {
    Object.assign(state, { status: 'idle', tabId: null, progress: null, preview: null, error: null, bundle: null });
    reply({ ok: true });
    return false;
  }
  reply({ error: 'Unknown request.' });
  return false;
});

// Test hook: lets automated tests place a bundle without a Canvas tab.
self.__setBundleForTest = bundle => {
  state.bundle = bundle;
  state.preview = self.CourseCollector.preview(bundle);
  state.status = 'ready';
};
