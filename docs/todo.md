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
- [ ] **Remove `v3-stage.haxiam.motore.dev`** from the Claude cloud environment's allowed hosts when
  HAX Lite publish testing is done.
- [ ] **Remove `drive.usercontent.google.com`** from the Claude cloud environment's allowed hosts
  (added to download a shared Canvas export for testing).

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

- [ ] **Publish to HAX Lite end to end on staging:** once connect and approve is switched on there and
  the app's origin is allowed, publish a built course and check the imported site.

## Not built yet
- [ ] LTI 1.3 launch from inside Canvas (needs an institution's registration).

## Testing with real courses and a real model
How: `scripts/evaluate_consultant.py` (slims the export with the browser's code, runs the whole pipeline with a
real model, writes a report and the HAX site) and `scripts/check_site.cjs` (opens every page of that site in a
browser with HAX's components). Playwright is not a project dependency: `npm i playwright` in a scratch folder
and run with `NODE_PATH=<folder>/node_modules`.

Done 2026-11-17: IST 210 course template (121 MB) with `anthropic/claude-sonnet-5.5` through OpenRouter:
33 AI requests, about 19 minutes, about $1.26; a 15-week course, 103 student pages, 56 practice questions (all
traceable, none discarded); the browser check passed on all 103 pages. Fixed from that run: import failure on a
folder name with a colon, dead links between course pages, dropped embedded documents, pages presenting a
not-yet-built AI tutor as available, an over-harsh design rating and essential-question rule.

Still open from that run:
- [ ] **Run the two CYBER exports** (339 MB and 792 MB, real taught sections). Not run: the owner asked to stop
  after IST 210.
- [ ] **Check on the live site that a course-site download over 4.5 MB works.** Downloads are now sent as a
  stream, which Vercel's documentation says is exempt from its 4.5 MB response limit; this was tested here
  with a 5 MB site but not on the deployment.
- [ ] **Practice questions about instructions, not ideas**, when the teaching content is in a textbook or videos
  and Canvas holds only lab instructions and objective lists. Consider skipping practice for a week whose pages
  have too little subject text, or saying so to the instructor.
- [ ] **"No invented instructor statements" false alarm**: fails when the AI cites "instructor" for a statement
  that something is *not* known ("class size is not stated").
- [ ] **Proposals lean towards AI**: 10 of 18 in the first run had an AI component.
- [ ] **Objective bullets** sit on the last line of a long objective on week pages (`oer-schema` is inline-block).
- [ ] **Import warnings are noise**: 98 for IST 210, nearly all "External LTI/web link resource not imported".
- [ ] **Model limits without a catalog**: a model saved without choosing it from the provider's list gets tiny
  default limits (4,096 output tokens) and every step fails with "ran out of room". The screen always goes
  through the list; the message should say to refresh the model list.
- [ ] `tests.test_canvas_oauth.ConfigTests.test_sealed_token` failed once in a full run and passed on every rerun.
- [ ] Not tested with a real course yet: the browser screens, a publish into HAX Lite, the extension on live Canvas.

## HAX-native course (see `superpowers/specs/2026-11-16-hax-native-course.md`)
- [ ] **Publish a built course with the new elements into HAX Lite** and check the questions, note and
  objectives arrive intact and the answers mark correctly.
- [ ] **Practice-question quality with a real model**: run a build and read the questions it keeps and drops.
- [ ] **Pictures beyond the export upload**: courses from the browser extension or the Canvas connection
  have no pictures yet (their images become "not included" notes); the Canvas import file does not carry
  included pictures either.
- [ ] **Storage**: included pictures are up to 6 MiB a course in the database; watch the hosting plan's limit.
- [ ] **Course-design quality with a real model**: draft a design for a real course and judge the
  understandings, questions and evidence map.
- [ ] PRAW's multimedia-principles audit (the chunking, instructional-events and UDL audits are in the Page check).
- [ ] The Quick AI cost estimate does not count the course-design request.
- [ ] Editing an understanding's wording drops its ticks in the evidence list (they are matched by text).
- [ ] More elements: `flash-card` and `lrn-vocab` for key terms, `grid-plate`; practice questions on item pages.
- [ ] Videos other than YouTube and Vimeo (Kaltura, Panopto, Canvas Studio) are still removed with their frame.
- [ ] The theme picker has no effect on HAX Lite (imported sites start on the default theme).
- [ ] When HAX's CDN build reads `data-correct`, the extra `correct` marker on answers can go.

## From the November 2026 review: still open
Done in that round: sanitizer bypass (comments, CDATA); instructor-only pages kept out of published and
shared sites; replacing a course resets stale work; course and data deletion with a 25-course cap; staff
contact details kept, more fields stripped; one time budget for AI retries; the consultant reads the syllabus
and uploaded documents; Quick AI version stops for the instructor's decisions; hand-work in Canvas flagged;
weekly hours shown to students only when confirmed; a mostly unpublished course is no longer treated as
instructor-only; the quality check no longer rewards adding AI.

Decisions and design:
- [ ] **Create and re-weight assessments.** Today such changes are only flagged as hand-work
  (`improvement_plan.manual_work`). Letting the tool add items and change points and weights touches the
  course model, the build and the Canvas package.
- [ ] **Automatic retention.** Delete courses untouched for a set period (the spec suggests 12 months).
  Needs a scheduled job and a decision on the period.
- [ ] **Delete the sign-in record.** "Delete all my data" removes courses and saved keys; the email held by
  the sign-in service (Neon Auth) stays.

Pedagogy:
- [ ] **Outcome alignment is shallow and never confirmed.** It is a yes/no link judged from a short excerpt,
  with no level (introduce, practise, assess) and no way for the instructor to confirm it
  (`course_map.propose_alignment`).
- [ ] **Check the built course against the outcomes.** Weekly objectives are written from item titles;
  nothing checks that every outcome is taught and assessed.
- [ ] **Workload.** "Workload" is a count of graded items per week; video length and reading time are
  unknown. Ask the instructor for credit hours and term length, and estimate from them.
- [ ] **Learning evidence.** Advice rests on course documents and what the instructor recalls. Consider
  de-identified score summaries per assignment.
- [ ] **Old due dates** are carried into the built course; offer to shift them to a new term.
- [ ] **Courses that are not weekly** (self-paced, modular) are still forced into weeks.
- [ ] **Image descriptions** from "Fix with AI" are written without seeing the image.

Engineering:
- [ ] **Lost updates between two tabs**: edits to the plan, documents and built pages are saved without a
  revision check (`save_consultant_fields`). AI requests are not idempotent either.
- [ ] **Database use**: several new connections and table checks on every request; no pooling
  (`hosted_data.py`, `hosted_backend.py`).
- [ ] **Shortened inputs without a notice**: uploaded documents over 60,000 characters (the warning is
  dropped at intake) and page text over 15,000-20,000 characters in document and build prompts.
- [ ] **Sanitizer keeps remote images and `id`/`class`**, so an imported page can carry a tracking image.
- [ ] **HAX-theme preview link** cannot be revoked before it expires (two hours).
- [ ] **Extension**: the collected course is held only in the service worker's memory and can be lost if the
  person pauses before importing.
- [ ] **No CI**; the Postgres storage tests are skipped unless a database is configured; tests pass on SDK
  versions other than the pinned ones, so the `anthropic==0.86.0` pin is unverified.
- [ ] **`tests-js/slim-export.test.cjs` "detects corrupted entries"** fails on Node 26 (the error text
  changed); it expects Node 22.
- [ ] **Structure**: `mentor_routes.py` is one 1,700-line function; unused code from the old wizard remains
  (`imscc_content.prepare_import_content`, `mentor_materials.fetch_url_material`, `ai_client.build_ai_client`).
