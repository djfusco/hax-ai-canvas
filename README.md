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
   account: connect OpenRouter in one click, or enter an OpenAI, Anthropic or
   Gemini key. The service never pays for or resells AI.
2. **Bring in the course**, one of four ways:
   - **Upload a Canvas export.** The browser reads only the export's text and
     structure. Videos and other media never leave the instructor's computer,
     so multi-gigabyte exports become a few megabytes.
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
8. **Accessibility check.** Automatic WCAG 2.1 checks of what students would
   see (image descriptions, link text, headings, tables, frames, captions),
   with **Fix with AI** for any page. It states what automatic checks cannot
   judge.
9. **Share a read-only link** with a colleague or instructional designer:
   the plan and course site, no sign-in needed, revocable at any time
   (90-day maximum).

Before the quick AI version or bulk revisions, Course Consultant shows an
estimate of the AI requests and tokens, and the cost when the provider
publishes prices (OpenRouter).

**Quick AI version.** Instead of talking it through, the instructor can have
the AI act as their advisor: it answers every consultation topic from the
course evidence (assumptions marked), accepts its own proposals (labelled
*AI-decided*), drafts the plan and writes revised documents (labelled
*AI-approved*). Everything stays editable: keep consulting, change any
decision, ask for changes to any document, and the site follows.

## Privacy
- Grades, submissions, student discussion replies and class lists are never
  imported, by either intake path.
- Before anything is stored or sent to an AI provider, likely student
  information is removed: emails, phone numbers, student IDs, links to user
  profiles, and names in class contexts ("Thanks to Jane", "Priya's post").
  The instructor sees what was removed. This is a safeguard, not a guarantee.
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
| Canvas import package (.imscc) | `python/canvas_package.py` |
| Accessibility checks | `python/accessibility.py` |
| AI cost estimate | `python/cost_estimate.py` |
| Consultant quality evaluation | `scripts/evaluate_consultant.py`, `python/quality_checks.py` |
| HAX-theme preview and static HAX site files | `python/hax_preview.py` |
| AI providers (OpenAI, Anthropic, Gemini, OpenRouter, gateways) | `python/ai_client.py`, `python/model_capabilities.py`, `python/provider_models.py` |
| Web app and API | `python/app/mentor_routes.py`, `python/app/templates/consultant.html`, `python/app/static/consultant.js` |
| Storage (Postgres in the cloud, SQLite in development) | `python/mentor_store.py`, `python/hosted_data.py` |

Every AI response is validated before it is used: known topics, statuses and
course IDs only, length limits, and proposals stored as *proposed* only. Models
never accept their own proposals. Prompts are fitted to each model's limits
from its catalog; nothing is silently truncated.

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
| `GET /estimate?action=quick\|turn\|plan\|document\|bulk&count=n&provider=p` | AI use and cost estimate |
| `GET/POST/DELETE /share` → `/mentor/shared/<signed>/` | Read-only share link |
| `POST /intake/canvas` | Import a course through the institutional Canvas connection |
| `POST /consult` with `mode: auto` / `advance` | Quick AI version advisor turn / move on to the next topic |
| `POST /consult/proposals/accept-all` | Quick AI version: accept proposals as AI-decided |
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
