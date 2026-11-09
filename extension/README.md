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
node scripts/build-extension.cjs --origin https://your-course-consultant-site                    # Chrome, Edge
node scripts/build-extension.cjs --origin https://your-course-consultant-site --browser firefox  # Firefox 121+
```
Chrome/Edge: `dist/extension/` and `dist/course-consultant-extension.zip` for
the Chrome Web Store. Firefox: `dist/extension-firefox/` and
`dist/course-consultant-extension-firefox.zip` for addons.mozilla.org. The
origin is set and the test hook removed.

### Firefox differences
Firefox has no `externally_connectable`, so the Firefox build hands the course
to the site through `bridge.js`, a content script limited to
`/mentor/import/extension` on the configured site (it also checks the exact
origin). The background runs as event-page scripts instead of a service
worker, and APIs are called through the promise-based `browser.*` namespace.
The bridge path is tested in Chromium (`--handoff bridge`); **run
`npx web-ext lint` on the Firefox build and try it in Firefox before
publishing**, since Firefox itself is not available in the test environment.

## Not yet verified against real Canvas
The endpoint list and response fields follow Canvas's documented REST API.
Check against a real course before release, especially New Quizzes items
(`/api/quiz/v1/...`), which some institutions restrict.
