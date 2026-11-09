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
2. **Bring in the course**, one of three ways:
   - **Upload a Canvas export.** The browser reads only the export's text and
     structure. Videos and other media never leave the instructor's computer,
     so multi-gigabyte exports become a few megabytes.
   - **Use the browser extension** (`extension/`) to send the live course
     from Canvas using the instructor's existing sign-in. No token and no
     admin approval needed.
   - **Start from documents**: a syllabus, or a description of the course.
3. **Review the course map**: how the grade is actually weighted, graded work
   by week, rubric coverage, outcomes and which items address them, resources.
4. **Consult.** The consultant opens with a snapshot, asks about students and
   goals, then works through the topics in backward-design order (the
   instructor can jump). It asks before advising, then offers two or three
   options with trade-offs, citing the course itself and a named principle.
   Returning instructors see where they left off.
5. **Proposed changes** collect as the conversation goes. Accepting them and
   generating revised documents (syllabus, assignments, rubrics) is Phase 2-3.

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

## Development
Python 3.12 and Node 22.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt
npm ci
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
npm run build:extension -- --origin https://your-course-consultant-site
```
Produces `dist/extension/` and a Chrome Web Store zip with the origin set and
the test hook removed.

## Deployment
Vercel (Python + Node functions) with Neon Postgres and Neon Auth email
sign-in. Environment variables, Neon Auth setup and the deployment smoke test
are documented in [`docs/legacy-readme.md`](docs/legacy-readme.md#administrator-setup);
they are unchanged. Add the OpenRouter callback
`https://<your-origin>/mentor/openrouter/callback` (no registration is
required by OpenRouter for PKCE).

## Known limitations
- **Not yet tested against real Canvas.** Export parsing and the extension's
  API calls follow Canvas's formats and documentation; validate with real
  courses (see the Phase 1 plan, Task 12), especially New Quizzes.
- **Upload size.** Slim packages are sent in pieces under 4 MB (hosting
  platforms cap request size) and assembled server-side, up to 48 MiB. The
  extension's bundle is sent in one request; very large live courses may
  exceed the hosting limit and should use the export path for now.
- **Phase 2-3 not built yet:** accepting/editing proposals, the Course
  Improvement Plan document, and generating revised documents. The earlier
  draft-and-build wizard remains at `/mentor/legacy` until then.
- The legacy hosted raw-upload pipeline (Blob multipart, Sandbox preparation)
  and local HAX build modules are still in the repository for that wizard;
  they are not used by Course Consultant and will be removed in Phase 3.
