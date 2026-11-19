# Course Consultant

A cloud app that helps a faculty member improve their whole course, not just
add AI to it. It reads an existing Canvas course, holds a structured
pedagogical conversation with the instructor (purpose, outcomes, assessment,
content, resources, engagement), and records proposed changes, each with its
reasoning, for the instructor to decide on. AI is considered within every
topic: AI-resilient assessment, AI-integrated learning, a course AI tutor, and
AI tools for the instructor.

Design: [`docs/superpowers/specs/2026-11-09-course-consultant.md`](docs/superpowers/specs/2026-11-09-course-consultant.md)
· Phase 1 plan: [`docs/superpowers/plans/2026-11-09-consultant-phase-1.md`](docs/superpowers/plans/2026-11-09-consultant-phase-1.md)

## What an instructor does
1. **Choose an AI model.** Course Consultant uses the instructor's own AI
   account: connect OpenRouter in one click or paste an OpenRouter key (for
   someone who was given one and has no OpenRouter account), or enter an
   OpenAI, Anthropic or Gemini key. The service never pays for or resells AI.
2. **Bring in the course**, one of four ways:
   - **Upload a Canvas export.** The browser reads only the export's text and
     structure. Videos and other media never leave the instructor's computer,
     so multi-gigabyte exports become a few megabytes. One optional exception,
     off by default: the instructor can tick "Include the pictures your pages
     show", which sends the small images (PNG, JPEG, GIF, WebP; 1 MiB each,
     6 MiB a course) that pages display, so the built site shows them.
   - **Use the browser extension** (`extension/`, Chrome, Edge and Firefox)
     to send the live course from Canvas using the instructor's existing
     sign-in. No token and no admin approval needed.
   - **Connect to Canvas** when the instructor's institution has registered
     Course Consultant (OAuth developer key): sign in, choose a course.
   - **Start from documents**: a syllabus, or a description of the course.
3. **Review the course map**: how the grade is actually weighted, graded work
   by week, rubric coverage, outcomes and which items address them, resources.
4. **Consult.** The consultant opens with a snapshot, asks about students and
   goals, then works through the topics in backward-design order (the
   instructor can jump). It asks before advising, then offers two or three
   options with trade-offs, citing the course itself and a named principle.
   Returning instructors see where they left off.
5. **Plan.** Accept, edit, defer or reject each proposed change. AI drafts the
   Course Improvement Plan (vision, revised outcomes, student AI policy, open
   questions) from the consultation and the accepted changes; the instructor
   edits it and downloads it as Word or HTML.
6. **Revised documents.** Generate a revised syllabus, assignments and
   rubrics that carry out the accepted changes; preview, edit and approve
   each, **ask for changes** in plain language, or **rewrite** from the latest
   plan. Compare any revision with the original (before/after), restore
   earlier versions (the plan and each document keep their last ten), revise
   any page, or apply one instruction to many pages at once. A **course AI
   tutor kit** (system prompt, guardrails, sample questions) can be generated
   too. Download approved documents as Word + HTML (with the plan), or as a
   **Canvas import file (.imscc)**: pages, assignments (points, due dates,
   settings) and rubrics arrive as new, unpublished "(revised)" items in one
   module, and nothing already in the course changes.
7. **The course site.** The whole course in module order, using approved
   revised versions in place of originals. **Preview course site** opens it in
   a new tab; **Preview in HAX theme** renders it with HAX's Clean One theme in
   the browser (sandboxed, no HAX server); **Download course site (ZIP)** is a
   static HAX site plus an offline preview.
8. **Build the course.** The AI turns the course, consultation, plan and revised
   documents into a finished week-by-week course that follows standard
   course-design practice (backward design, alignment, a steady weekly rhythm,
   Quality Matters, TILT, UDL, balanced workload): a home page, Start here, a
   course schedule, an overview and exact checklist (due dates, points, from
   the course, never from the AI) for every week, and an instructor section
   (guide, design decisions, weekly notes, "to do by hand in Canvas"). The
   instructor section, answer keys and unpublished items appear only in the
   instructor's own preview, the Canvas import file (unpublished) and the
   documents ZIP; they are left out of the HAX site ZIP, Publish to HAX Lite,
   the HAX-theme preview and the share link. The AI's weekly time estimate is
   shown to the instructor only, until they confirm or correct it in the
   structure. Two
   options: **Essential** (structure, front pages, weekly overviews and notes;
   course pages keep their content in a consistent layout) and **Full** (every
   course page reorganised into purpose / what to do / how it's graded / using
   AI / getting help, keeping the instructor's wording, with a "keeps N% of the
   original wording" measure). Review the proposed structure (rename weeks,
   move items) before building; afterwards edit, revise or restore any page.
   Both previews, the ZIP downloads and the Canvas import file (one module per
   section, for a new course) all use the built course.
   **Publish to HAX Lite** (step 5) copies the course site into the person's HAX Lite account
   as a new site, using HAX Lite's "connect and approve" flow: the person approves on their
   HAX Lite; Course Consultant's server exchanges the approval for a one-use pass, uploads the
   course-site ZIP and waits for the import. The pass is kept only for that request.
9. **Accessibility check.** Automatic WCAG 2.1 checks of what students would
   see (image descriptions, link text, headings, tables, frames, captions),
   with **Fix with AI** for any page. It states what automatic checks cannot
   judge.
10. **Share a read-only link** with a colleague or instructional designer:
   the plan and course site, no sign-in needed, revocable at any time
   (90-day maximum).

Before the quick AI version or bulk revisions, Course Consultant shows an
estimate of the AI requests and tokens, and the cost when the provider
publishes prices (OpenRouter).

**Course design.** On the Plan step the AI drafts the design behind the course by
Understanding by Design, as HAX's rules ask: big ideas, enduring understandings,
essential questions, predictable misunderstandings, and which of the course's
own graded work shows each understanding. The instructor edits it. A check with
no AI follows HAX's design standards and names the result (Aligned, Partial,
Activity-oriented, Coverage-bound): an understanding nothing assesses, no
authentic task on a real problem, understanding judged by tests alone, a week
that serves no understanding. The built course opens with the big ideas and
questions, and each week says which understanding it serves
(`python/course_design.py`).

**HAX-native pages.** Each week page opens with an essential question, lists its
objectives as OER Schema learning objectives, and ends with up to four practice
questions (multiple choice, true/false, self-check) written from that week's own
pages. The AI returns the questions as data and the app writes the HAX markup
from a closed, verified list of elements (`python/hax_elements.py`), so a page
never contains an invented tag. A question is kept only when the passage it
quotes is found in the course item it names; quiz items are never a source; the
instructor is told how many questions to review. HAX outputs get the real
elements; the plain preview, Word, the documents ZIP and the Canvas import file
get the same content as ordinary HTML. Page addresses are readable
(`week-1-cells`). Design and what was verified:
[`docs/superpowers/specs/2026-11-16-hax-native-course.md`](docs/superpowers/specs/2026-11-16-hax-native-course.md).

**Videos, review and the page check.** An embedded YouTube or Vimeo video becomes
HAX's video player (other embedded frames are removed, as before). Each week
brings back one practice question from the week before and one from three weeks
before, and a closing Course review page samples every week: recalling an idea
again after a gap is what makes it stick. A Page check (step 5, no AI) applies
HAX's own page audits to what students will see: paragraphs and lists too dense
to read, each week against the events of instruction ("Presentational" when
students only read), and whether students can respond, see a goal and meet more
than text (`python/page_audit.py`).

**Quick AI version.** Instead of talking it through, the instructor can have
the AI work through every consultation topic from the course evidence
(assumptions marked) and propose changes. It decides nothing: the instructor
accepts the changes they want (one by one, or all at once after reading
them), and the AI then drafts the plan and writes revised documents as
drafts for the instructor to approve.

**What it cannot do for you.** Course Consultant revises pages, assignments
and documents that already exist. It cannot add an assignment, split or
replace one, or change points, grade weights or due dates. Every proposed
change records whether it needs that work; such changes are flagged on the
proposal, in the plan ("To do by hand in Canvas"), on the build screen and in
the built course's instructor section, so a revised syllabus is not mistaken
for a Canvas course that already matches it.

**Deleting data.** "Delete this course" removes a course and everything made
from it; "Delete all my courses and saved AI keys" removes everything stored
for the person. An account can hold up to 25 courses. Replacing a course
keeps the consultation and the plan and clears the revised documents and the
built course (they were written for the old course's pages). Automatic
deletion of inactive courses is not built yet (see `docs/todo.md`).

## Privacy
- Grades, submissions, student discussion replies and class lists are never
  imported, by either intake path.
- Before anything is stored or sent to an AI provider, likely student
  information is removed: student IDs, links to user profiles, names in class
  contexts ("Thanks to Jane", "Priya's post"), and emails and phone numbers.
  Emails and phone numbers are kept in the syllabus and on contact and
  support pages (titles such as "Contact your instructor", "Getting help",
  "Welcome"), where they belong to staff and offices. Titles, question banks,
  rubric ratings and outcome descriptions are covered too; file names are
  not. The instructor sees what was removed. This is a safeguard, not a
  guarantee.
- Pictures are only stored when the instructor ticks the box at upload. They
  are kept with the course, never sent to an AI provider, not checked for
  student information (the upload step says so), served only to the
  instructor, in the HAX site they download or publish, and through a share
  link they create, and deleted when the course is replaced or deleted. A
  picture or file that was not included appears in the built course as a
  short note ("Image not included: …") instead of a broken image or dead link.
- Uploaded export packages are processed in a temporary directory and never
  kept. Course models, conversations and proposals are stored per user.
- The extension's permissions and exact data use are in
  [`extension/PRIVACY.md`](extension/PRIVACY.md).

## How it is built
| Part | Where |
|---|---|
| Course model (one format for every intake path) | `python/course_model.py` |
| Slim export import (rubrics, weights, outcomes, module rules, settings) | `python/slim_import.py`, `python/imscc_import.py` |
| Browser export slimming (no dependencies; ZIP64; never reads media) | `python/app/static/slim-export.js` |
| Browser extension and its import | `extension/`, `python/canvas_api_import.py` |
| Student-information stripping | `python/privacy_strip.py` |
| Course map and AI outcome alignment | `python/course_map.py` |
| Consultation engine | `python/consultation.py` |
| Improvement plan (decisions, AI draft, Word/HTML) | `python/improvement_plan.py` |
| Revised documents (syllabus, assignment, rubric; exports) | `python/revised_documents.py` |
| Institutional Canvas connection (OAuth2, server-side reader) | `python/canvas_oauth.py`, `python/canvas_collect.py` |
| Whole-course site (preview, download) | `python/course_site.py` |
| Building the finished course | `python/course_build.py` |
| Model suggestions | `python/model_advice.py` |
| Canvas import package (.imscc) | `python/canvas_package.py` |
| Publish to HAX Lite | `python/hax_lite_publish.py` |
| HAX elements a page may use, and their plain form | `python/hax_elements.py` |
| Course design (Understanding by Design) and its check | `python/course_design.py` |
| Included pictures, and notes for what cannot be shown | `python/course_files.py` |
| Page check (HAX's chunking, instructional-events and UDL audits) | `python/page_audit.py` |
| Accessibility checks | `python/accessibility.py` |
| AI cost estimate | `python/cost_estimate.py` |
| Real-course evaluation (whole pipeline, real model) and a browser check of the built site | `scripts/evaluate_consultant.py`, `scripts/slim_export.cjs`, `scripts/check_site.cjs`, `python/quality_checks.py` |
| HAX-theme preview and static HAX site files | `python/hax_preview.py` |
| AI providers (OpenAI, Anthropic, Gemini, OpenRouter, gateways) | `python/ai_client.py`, `python/model_capabilities.py`, `python/provider_models.py` |
| Web app and API | `python/app/mentor_routes.py`, `python/app/templates/consultant.html`, `python/app/static/consultant.js` |
| Storage (Postgres in the cloud, SQLite in development) | `python/mentor_store.py`, `python/hosted_data.py` |

Every AI response is validated before it is used: known topics, statuses and
course IDs only, length limits, and proposals stored as *proposed* only. Models
never accept their own proposals, and AI-written documents are drafts until
the instructor approves them. Prompts are fitted to each model's limits from
its catalog. Long texts are shortened for a prompt: the consultation and the
syllabus revision tell the model how much was left out; other steps use the
first 12,000-20,000 characters of a page (see `docs/todo.md`). AI-written
and imported HTML is sanitized, including markup hidden in comments and CDATA
sections. All AI attempts for one step share one time budget, so a retry
cannot outlive the hosting platform's request limit.

### API (all under `/mentor/api/workspaces/<id>`)
| Route | Purpose |
|---|---|
| `POST /intake/export` | Slim package (multipart `package`) |
| `POST /intake/extension` | Extension bundle (multipart `bundle`) |
| `POST /intake/documents` | Documents and/or a description |
| `GET /course-map`, `POST /course-map/align` | Course map; AI outcome alignment |
| `GET /consult`, `POST /consult`, `POST /consult/settings` | Consultation state, a turn (`mode: opening` or `message`), topic/advice style |
| `POST /consult/proposals/<id>` | Instructor decision on a proposal (accept, reject, defer, reopen; optional edits) |
| `GET/PUT /plan`, `POST /plan/draft`, `GET /plan/export?format=docx\|html` | Improvement plan |
| `GET /documents`, `POST /documents/generate`, `PUT/DELETE /documents/<id>` | Revised documents |
| `GET /documents/<id>/export?format=docx\|html`, `GET /documents/export?format=zip\|hax\|imscc` | Downloads (approved only for packages) |
| `GET /documents/<id>/compare`, `POST /documents/<id>/restore`, `POST /plan/restore` | Before/after and version history |
| `GET /accessibility` | Accessibility report |
| `GET /page-check` | HAX's page audits on what students will see |
| `GET /estimate?action=quick\|turn\|plan\|document\|bulk&count=n&provider=p` | AI use and cost estimate |
| `GET/POST/DELETE /share` → `/mentor/shared/<signed>/` | Read-only share link |
| `POST /hax-lite/start` → HAX Lite approval → `/mentor/hax-lite/callback` → `POST /mentor/api/hax-lite/finish` | Publish to HAX Lite (see `HAX_LITE_PUBLISH_CONTRACT.md` in haxiam-orchestrator) |
| `GET /build`, `POST /build/blueprint` (`mode: essential\|full`), `PUT /build/blueprint` | Build the course: state, propose the structure, edit it |
| `POST /build/step` (`step: front\|week-N\|week-N-items-K`), `DELETE /build` | Build one step; discard the built course |
| `PUT /build/pages/<id>`, `POST /build/pages/<id>/revise\|restore` | Edit, revise or restore a built page |
| `POST /intake/canvas` | Import a course through the institutional Canvas connection |
| `POST /consult` with `mode: auto` / `advance` | Quick AI version advisor turn / move on to the next topic |
| `POST /consult/proposals/accept-all` | The instructor accepts every undecided proposal |
| `POST /documents/approve-all` | The instructor approves every draft document |
| `GET/PUT /design`, `POST /design/draft` | Course design, its check against the design standards, and an AI draft |
| `DELETE /mentor/api/workspaces/<id>`, `DELETE /mentor/api/account` (`{"confirm": "DELETE"}`) | Delete a course; delete everything stored for the person |
| `POST /documents/<id>/revise` | Revise a document from an instruction |
| `GET /site/...` | Private preview of the whole course site |
| `POST /hax-preview` → `/mentor/hax/<signed>/` | HAX-theme preview (signed two-hour link, sandboxed origin) |

The HAX-theme preview loads HAX's web components from `HAX_CDN_URL`
(default `https://cdn.hax.cloud/cdn/`). Third-party scripts run under a
Content-Security-Policy `sandbox` without same-origin, so they cannot see or
use the instructor's session.

Institutional Canvas connection (under `/mentor/api/canvas`): `institutions`,
`connect`, `exchange`, `courses`, `disconnect`. Configure participating
institutions with `HAX_CANVAS_INSTITUTIONS` (JSON; see `python/canvas_oauth.py`)
and one client-secret environment variable per institution. The redirect URI
to register in Canvas is `https://<your-origin>/mentor/canvas/callback`.

## Development
Python 3.12 and Node 22.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt
HAX_NO_BROWSER=1 python3 python/app/app.py     # http://127.0.0.1:5050/mentor
```

Development mode stores data in SQLite under `python/mentor_data/`. Load
`extension/` unpacked in Chrome to try the extension against it.

### Tests
```bash
python3 -m unittest discover -s tests          # Python
npm test                                        # JS, incl. real-Chromium UI and extension tests
```
Set `CC_TEST_POSTGRES_DSN` to a disposable PostgreSQL database to also run
the storage tests against real Postgres. Browser tests use Playwright's Chromium and a scripted fake AI provider; no
test makes network or paid AI calls. Install the pinned `requirements.txt`:
newer Anthropic SDKs than `anthropic==0.86.0` break the client.

### Browser extension release
```bash
npm run build:extension -- --origin https://your-course-consultant-site                    # Chrome, Edge
npm run build:extension -- --origin https://your-course-consultant-site --browser firefox  # Firefox
```
See [`extension/README.md`](extension/README.md).

## Deployment
Vercel (one Python function) with Neon Postgres and Neon Auth email sign-in.
Step-by-step setup, environment variables, **your own email sender (SMTP)**
and the post-deployment smoke test: [`docs/deployment.md`](docs/deployment.md).
Outstanding set-up, testing and clean-up tasks: [`docs/todo.md`](docs/todo.md).

## Known limitations
- **Not yet tested against real Canvas.** Export parsing and the extension's
  API calls follow Canvas's formats and documentation; validate with real
  courses (see the Phase 1 plan, Task 12), especially New Quizzes.
- **Upload size.** Slim packages (up to 48 MiB) and extension bundles (up to
  16 MiB) are sent in pieces under 4 MB, because hosting platforms cap request
  size, and assembled server-side.
- **Firefox** is validated by build checks and the bridge hand-off tested in
  Chromium, not in Firefox itself; run `npx web-ext lint` and try it in
  Firefox before publishing.
- **HAX-theme preview** was tested with HAX's own built components served
  locally; it relies on `cdn.hax.cloud` sending CORS headers for the sandboxed
  page. If a deployment shows a blank HAX preview, host HAX's build (from the
  `@haxtheweb/haxcms-nodejs` package's `dist/public`) and set `HAX_CDN_URL`.
  Run `tests-js/hax-preview.test.cjs` with `HAX_TEST_CDN_DIR` pointing at it.
- **Institutional connection** is OAuth2 only; LTI 1.3 (launching from inside
  Canvas) needs an institution's registration to build and test.
- **Canvas import package** follows the Canvas Common Cartridge format and
  round-trips through this app's importer; try it on a sandbox course before
  importing into a live one.
- **Quality evaluation** needs a real AI key; `scripts/evaluate_consultant.py`
  runs it and writes a report for a person to review.
