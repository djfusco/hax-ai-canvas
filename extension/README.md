# Course Consultant browser extension

Sends the Canvas course you are viewing to Course Consultant, using your
existing Canvas sign-in. No Canvas token or admin approval is needed.
See [PRIVACY.md](PRIVACY.md) for exactly what it reads.

## How it works
1. In Canvas, open a course and click the extension button.
2. **Read this course:** the background worker injects `collect.js` into that
   tab (allowed by `activeTab` after your click). It makes same-origin GET
   requests to an allowlist of Canvas course-content API endpoints and keeps
   only the fields Course Consultant uses.
3. Review the summary (counts and titles) and click **Send**.
4. The extension opens `<Course Consultant>/mentor/import/extension`. That
   page asks the extension for the course (`chrome.runtime.sendMessage`,
   permitted by `externally_connectable` and checked against the configured
   origin), then uploads it with the page's own session. The extension never
   holds Course Consultant credentials.

## Development
- Load `extension/` unpacked in Chrome (`chrome://extensions`, Developer mode).
  It targets `http://127.0.0.1:5050`.
- Tests: `node --test tests-js/extension-collect.test.cjs` (collector against
  a fake Canvas) and `tests-js/extension-e2e.test.cjs` (real Chromium with the
  extension loaded, hand-off to the running app).

## Release
```bash
node scripts/build-extension.cjs --origin https://your-course-consultant-site
```
Produces `dist/extension/` and `dist/course-consultant-extension.zip` for the
Chrome Web Store, with the origin set and the test hook removed.

## Not yet verified against real Canvas
The endpoint list and response fields follow Canvas's documented REST API.
Check against a real course before release, especially New Quizzes items
(`/api/quiz/v1/...`), which some institutions restrict.
