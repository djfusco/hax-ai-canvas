# Outstanding tasks

Work that is not finished yet. Tick items off as they are done.

## Not completed: needs the project owner
- [ ] **Own email sender (SMTP) in Neon Auth.** Neon's shared sender is
  rate-limited, so sign-in codes can be slow or not arrive. In the Neon
  console, open Auth → Configuration → Email server, switch it to Custom SMTP,
  enter your mail provider's details and click Send test email. Full steps:
  [`deployment.md`](deployment.md#3-your-own-email-sender-smtp--do-this-before-inviting-instructors).
  Do this before inviting instructors.
- [ ] **Clean up the Vercel project** (hax-ai-canvas-claude):
  - [ ] Delete the environment variables nothing uses any more:
    `HAX_UPLOAD_SIGNING_SECRET`, `HAX_INTERNAL_SECRET`, `CRON_SECRET`,
    `BLOB_READ_WRITE_TOKEN`.
  - [ ] Delete the Blob store connected to the project.
  - [ ] Remove the Vercel access token saved in the Claude cloud environment's
    network settings, and revoke it in Vercel (Account Settings → Tokens).
- [ ] **Remove the OpenRouter testing key** saved in the Claude cloud environment's network settings
  (host `openrouter.ai`) when testing is done, and revoke or limit it in OpenRouter.

## To test or verify
- [ ] **Real evaluation run.** With your own key:
  `CC_EVAL_API_KEY=... python3 scripts/evaluate_consultant.py --export <export> --provider openrouter --model <id>`,
  then fill in the scoring sheet in the report (about 10–25 AI requests).
- [ ] **Canvas import file (.imscc).** Import one into a Canvas sandbox course
  and check the pages, assignments (points, due dates) and rubrics.
- [ ] **Real Canvas courses.** Try exports and the browser extension on a few
  real courses, especially ones using New Quizzes.
- [ ] **HAX-theme preview in production.** Check it renders on the live site
  (it depends on `cdn.hax.cloud` allowing the sandboxed page). If it is blank,
  host HAX's build and set `HAX_CDN_URL` (see the README).
- [ ] **Firefox extension.** Run `npx web-ext lint` and try it in Firefox
  before publishing.
- [ ] **Post-deployment smoke test** on the live site: the steps in
  [`deployment.md`](deployment.md#6-smoke-test-after-each-deployment).

## Code clean-up (small)
- [ ] **Build the course: unsaved structure changes.** Clicking "Build the course" after renaming a
  week or moving an item, without "Save changes to the structure", silently builds from the last
  saved structure. Warn about unsaved changes, or save them automatically before building
  (`python/app/static/consultant.js`, build tab).
- [ ] Remove the old wizard's unused styles from `python/app/static/style.css`
  (for example `.nav-advanced`, `.nav-dropdown`).
- [ ] One browser test (`instructor journey` in
  `tests-js/consultant-ui.test.cjs`) failed once when all suites ran in
  parallel; it passed when re-run. Watch for it and lengthen its waits if it recurs.

## Not built yet
- [ ] LTI 1.3 launch from inside Canvas (needs an institution's registration).
