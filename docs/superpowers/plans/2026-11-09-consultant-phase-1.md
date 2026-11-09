# Course Consultant Phase 1 Implementation Plan

> **For agentic workers:** work task by task. Steps use checkbox (`- [ ]`)
> syntax for tracking. Each task ends with its tests passing.

**Goal:** A cloud-hosted consultant that takes in a Canvas course (slimmed
export upload or Chrome extension), builds a course map, and holds a
topic-based pedagogical consultation across several sessions.

**Spec:** `docs/superpowers/specs/2026-11-09-course-consultant.md`

**Architecture:** Builds on the existing hosted mode (Flask on Vercel, Neon
Postgres, Neon Auth). Two intake adapters (slim export, extension bundle)
produce one normalized **course model**. Privacy stripping, the course map and
the consultation all read only the course model.

```
Export (.imscc) ──browser slimming──▶ slim ZIP ──▶ imscc_import (slim mode) ─┐
                                                                             ├─▶ privacy strip ─▶ course model ─▶ course map ─▶ consultation
Chrome extension ──Canvas API (session)──▶ API bundle ──▶ canvas_api_import ─┘
```

**Tech stack:** Python 3.12, Flask, psycopg, existing `ai_client.py`;
browser ES modules with `zip.js` (pinned, vendored) for ZIP reading; Chrome
Manifest V3 for the extension. Tests: `unittest`, `node --test`, Playwright
(Chromium is preinstalled; extensions load in a persistent context).

## Global constraints

- Cloud only. Do not add new local-mode code paths. Local modules are left
  in place during Phase 1 and removed in a dedicated cleanup task (Task 11).
- Never send or store anything before privacy stripping (Task 5).
- No new server-side runtime dependencies except where a task names one.
- Every AI output is validated locally (types, lengths, known IDs) before it
  is stored. No raw model text in error messages.
- Keep `ai_client.py`'s call interface unchanged; add OpenRouter beside the
  existing providers.

---

## Task 1: Course model

**Files:** create `python/course_model.py`, `tests/test_course_model.py`

The single internal representation every later step uses.

- [ ] Define the model as plain dicts with a validator `validate_course(model)`:
  - `course`: title, code, term, syllabus_html, grading_scheme, late_policy
  - `modules[]`: id, title, position, items (ordered refs), requirements,
    prerequisites
  - `items[]`: id, kind (`page`, `assignment`, `quiz`, `discussion`,
    `file`, `external_link`, `external_tool`), title, html, module_id
  - `assignments{}` by item id: points, due_at, submission_types,
    grading_type, group_id, rubric_id, peer_reviews, group_work,
    allowed_attempts
  - `assignment_groups[]`: id, name, weight, position
  - `rubrics{}`: id, title, criteria[] (description, long_description,
    points, ratings[])
  - `quizzes{}` by item id: time_limit, allowed_attempts, question_count,
    question_types{}, questions[] (text, type, points)
  - `outcomes[]`: id, title, description, mastery_points, linked item ids
  - `media_inventory[]`: path, mime, bytes, referenced_by[]
  - `materials[]`: documents the instructor chose to include as text
  - `provenance`: `source` (`export` or `extension`), imported_at, counts,
    warnings
- [ ] Limits: reuse existing caps (500 items, 2 MiB per item, 16 MiB total
  instructional HTML); add 200 rubrics, 2,000 quiz questions, 5,000 media
  inventory entries.
- [ ] Tests: valid model passes; each limit and each wrong type fails with
  a field-specific message.

## Task 2: Importer reads the full export (slim mode)

**Files:** modify `python/imscc_import.py`; create
`tests/fixtures/slim_course/` (small hand-built export);
modify `tests/test_imscc_import.py`

- [ ] Add `import_slim(zip_path) -> course model`. Reuses existing archive
  safety checks (`inspect`), manifest and resource parsing.
- [ ] Parse and map into the model:
  - `course_settings/rubrics.xml`, linked via `rubric_identifierref` in
    `assignment_settings.xml`
  - `course_settings/assignment_groups.xml` (weights)
  - `course_settings/learning_outcomes.xml` (+ alignments where present)
  - `course_settings/module_meta.xml` (requirements, prerequisites)
  - `assignment_settings.xml`: add grading_type, peer_reviews,
    group_category, allowed_attempts
  - `assessment_meta.xml` + QTI: time limit, attempts, question text, types,
    points
  - `course_settings/course_settings.xml`, `grading_standards.xml`,
    `late_policy.xml`
- [ ] Read `media_inventory.json` from the slim ZIP; treat absent media as
  expected (no warnings), record references from item HTML.
- [ ] Tests: fixture with two modules, three assignments (one with rubric),
  weighted groups, two outcomes, one classic quiz, one page linking a video
  and a PDF. Assert every field above lands in the model.

## Task 3: Browser-side export slimming

**Files:** create `web/slim-export.js` (ES module), vendor
`web/vendor/zip.js` (pinned version), `tests-js/slim-export.test.cjs`

- [ ] `slimExport(file, {onProgress, signal}) -> {blob, summary}`:
  1. Open with zip.js `BlobReader` (reads only the central directory and
     requested ranges; supports ZIP64).
  2. Keep entries matching: `imsmanifest.xml`, `*.html`, `*.htm`, `*.xml`,
     `*.qti`, `course_settings/*`; per-entry cap 2 MiB; total cap 32 MiB.
  3. Build `media_inventory.json` from all other entries (path, size, mime by
     extension).
  4. Offer documents (`.pdf`, `.docx`, `.pptx`, `.txt`) referenced by item
     HTML and under 10 MiB as optional includes; include only those the
     instructor ticks, under `materials/`.
  5. Write a new ZIP (store or deflate) and return it with a summary
     (kept count, skipped bytes, documents offered).
- [ ] Cancel via `AbortSignal`; progress per entry.
- [ ] Reject non-ZIP files and archives without `imsmanifest.xml` with plain
  messages.
- [ ] Tests (Node 22 has `Blob`, `DecompressionStream`): build ZIPs in the
  test with a large dummy media entry; assert the media bytes are never read
  (wrap `Blob.slice` to record ranges), kept entries round-trip, inventory is
  correct, ZIP64 fixture works.

## Task 4: Slim upload endpoint

**Files:** modify `python/app/mentor_routes.py`, `python/hosted_data.py`;
create `tests/test_slim_intake.py`

- [ ] `POST /mentor/api/workspaces/<id>/intake/export`: accepts the slim ZIP
  (max 48 MiB) with the existing CSRF and auth checks.
- [ ] Process in memory or a temp directory: `import_slim` → privacy strip
  (Task 5) → `validate_course` → store the course model. Never persist the
  uploaded ZIP.
- [ ] Return the summary: counts, warnings, what privacy stripping removed.
- [ ] Vercel request body limit: if the slim ZIP exceeds the platform limit,
  fall back to the existing Blob client upload for the slim file only
  (small, single part), then process from Blob and delete it.
- [ ] Tests: happy path, oversize, bad ZIP, missing manifest, CSRF failure,
  other user's workspace.

## Task 5: Student-information stripping

**Files:** create `python/privacy_strip.py`, `tests/test_privacy_strip.py`

Runs on the server on every intake path before anything is stored or sent
to an AI provider. One implementation, so both paths behave the same.

- [ ] Drop discussion replies (keep the instructor's prompt only).
- [ ] Redact email addresses, student ID patterns (configurable regexes),
  and phone numbers in all HTML and materials.
- [ ] Redact names in announcements and discussion prompts that follow
  patterns like "Thanks to Jane Smith", "@Jane", "Jane's post", using a
  conservative heuristic; never redact the instructor's own name (from the
  course model) or named authors of cited works where detectable.
- [ ] Return `(model, report)` where report lists counts by category and
  item ids touched; show it in the UI after intake.
- [ ] Tests: each category, false-positive guards (citations, the course
  title, instructor name), idempotence.

## Task 6: Chrome extension

**Files:** create `extension/` (`manifest.json`, `popup.html`, `popup.js`,
`collect.js`, `canvas-api.js`, `README.md`, `PRIVACY.md`); create
`tests-js/extension-collect.test.cjs`; create
`tests/browser_extension_flow.py`

Manifest V3, permissions `activeTab` and `scripting` only. No host
permissions.

- [ ] **Popup:** shows "Send this course to Course Consultant" when the
  active tab URL matches `/courses/<id>`; otherwise explains how to open a
  course.
- [ ] **Collect (injected on click):** runs in the Canvas page, so `fetch`
  is same-origin and uses the instructor's session. GET only.
  - `GET /api/v1/courses/:id?include[]=syllabus_body&include[]=term`
  - `GET /api/v1/courses/:id/modules?include[]=items`
  - `GET /api/v1/courses/:id/pages` then each `/pages/:url`
  - `GET /api/v1/courses/:id/assignments?include[]=rubric_settings`
    (rubrics come on each assignment)
  - `GET /api/v1/courses/:id/assignment_groups`
  - `GET /api/v1/courses/:id/quizzes` and `/quizzes/:qid/questions`
  - New Quizzes: `GET /api/quiz/v1/courses/:id/quizzes/:aid/items` where
    available; record a warning when not
  - `GET /api/v1/courses/:id/discussion_topics` (topic only, never entries)
  - `GET /api/v1/courses/:id/outcome_group_links?outcome_style=full`
  - `GET /api/v1/courses/:id/files` (metadata only, never file bodies)
  - Follow `Link: rel="next"` pagination; strip the `while(1);` JSON
    prefix; cap requests (e.g. 2,000) and concurrency (4).
  - Never call users, enrollments, submissions, grades or analytics
    endpoints. Enforce with an allowlist in `canvas-api.js`.
- [ ] **Preview:** popup lists counts and item titles; nothing is sent until
  the instructor clicks Send.
- [ ] **Hand-off without credentials:** the extension never holds Course
  Consultant credentials. On Send it opens
  `https://<app>/mentor/import/extension`; that page (listed in the
  extension's `externally_connectable.matches`) requests the bundle from the
  extension with `chrome.runtime.sendMessage`, then uploads it with the
  instructor's own app session and CSRF token.
- [ ] Tests: allowlist rejects disallowed paths; pagination; `while(1);`
  stripping; discussion entries never requested. Playwright: load the
  unpacked extension in Chromium against a stub Canvas server and the app;
  assert the end-to-end hand-off.
- [ ] Packaging script and Chrome Web Store listing text (from
  `PRIVACY.md`).

## Task 7: Extension bundle adapter

**Files:** create `python/canvas_api_import.py`,
`tests/test_canvas_api_import.py`; modify `python/app/mentor_routes.py`

- [ ] `import_api_bundle(bundle) -> course model`, mapping Canvas API JSON
  into the same model as Task 2.
- [ ] `POST /mentor/api/workspaces/<id>/intake/extension` (max 16 MiB JSON):
  adapter → privacy strip → validate → store. Same response shape as Task 4.
- [ ] Tests: fixture bundle mirroring the Task 2 fixture course produces an
  equivalent model (same assignments, rubrics, weights, outcomes).

## Task 8: Course map

**Files:** create `python/course_map.py`, `tests/test_course_map.py`

- [ ] Computed (no AI): module structure; assessment inventory (points,
  group weight, effective course weight, due week, type, rubric yes/no and
  criteria count); workload by week; resources (external links and tools
  by domain, media inventory totals by type, readings detected in the
  syllabus); stated outcomes.
- [ ] AI step (one structured call, validated): propose outcome-to-item
  alignment and classify assessment types where unclear. Every link must
  reference known ids. Marked as `proposed` until the instructor confirms
  in conversation.
- [ ] `compact_map(map, budget)` renders a token-bounded text summary for
  prompts.
- [ ] Tests: weights computed correctly with and without group weighting;
  workload by week; AI step rejects unknown ids; compact map respects the
  budget.

## Task 9: Consultation engine

**Files:** create `python/consultation.py`, `tests/test_consultation.py`;
modify `python/mentor_store.py` / `python/hosted_data.py` for storage

- [ ] Topic state as in the spec (six topics; status; facts; goals; open
  questions; evidence; directness).
- [ ] `opening(workspace, client)` for the first session: snapshot from the
  course map, then open questions. `resume(workspace, client)` for returning
  sessions: "where we left off".
- [ ] `turn(workspace, message, client)`: one structured call returning
  `reply`, `topic_updates`, `proposed_changes` (stored as `proposed` for
  Phase 2), `next_topic`. Validate topics, evidence ids, lengths; merge;
  save.
- [ ] Prompt: backward-design order as a suggestion; ask before advising;
  2-3 options with trade-offs; cite course evidence and a named principle;
  AI dimensions considered within each topic; respects directness.
- [ ] Grounding: compact course map + topic state + relevant items via the
  existing `_select_chunks`; fit with the existing prompt-fitting approach.
- [ ] Tests with mock clients: state merge; invalid topic or evidence id
  rejected; opening vs resume; prompt stays within budget for a 500-item
  course.

## Task 10: OpenRouter provider

**Files:** modify `python/ai_client.py`, `python/provider_models.py`,
`python/model_capabilities.py`; add tests in
`tests/test_model_client_matrix.py`, `tests/test_provider_models.py`

- [ ] Provider `openrouter`: OpenAI-compatible client with base URL
  `https://openrouter.ai/api/v1`.
- [ ] Model list from OpenRouter's models endpoint: use context length,
  pricing and supported parameters (structured outputs) to fill
  capabilities; unknown support falls back to prompt-only JSON with the
  existing warning.
- [ ] One-click connect: OAuth PKCE flow (`/auth` redirect, code exchange
  for a user key). Key storage follows whatever the deferred key decision
  is; until then, reuse the existing encrypted vault.
- [ ] Tests with synthetic fixtures (no paid calls).

## Task 11: UI and cleanup

**Files:** modify `python/app/templates/mentor.html`,
`python/app/static/mentor.js`, `python/app/static/style.css`;
create `web/intake.js`

- [ ] Intake screen: export upload (with slimming progress and the optional
  documents checklist), "Export without files" guide, extension install
  link, documents-only option. After intake: privacy report.
- [ ] Consultation screen: chat in the center; topic progress and course map
  beside it; directness control.
- [ ] Split `mentor.js` into modules (`intake.js`, `consult.js`, `map.js`)
  rather than growing it.
- [ ] Remove local-only code paths: `bin/index.js` launcher, `local_*.py`,
  `sqlite_client.py`, `require_local_host`, and their tests; remove the
  large-file Blob multipart and Sandbox intake chain; update README.
- [ ] Browser test: export upload → privacy report → course map → opening
  message → one turn → reload → resume summary.

## Task 12: Real-course validation

- [ ] Run Tasks 3-8 against at least two real large exports (one over
  4 GB) and the same courses through the extension. Record: slim size,
  slimming time, missing data, New Quizzes coverage, privacy-strip false
  positives. Fix gaps before Phase 2.

## Order and dependencies

1 → 2 → 3 → 4 → 5 (5 blocks storing anything) → 7 → 6 → 8 → 9 → 10 → 11 → 12

Tasks 3 and 6 are browser work and can proceed in parallel with 8-10 once
1 and 5 are done.

---

## Status (2026-11-09)

Tasks 1-11 are implemented and tested. Task 12 is done to the extent possible
without an instructor's own course; see below.

### Task 12 results
- **Real Canvas exports.** The 32 Canvas/Common Cartridge fixture archives
  from Instructure's own test suite were run through browser slimming, import,
  privacy stripping and the course map
  ([report](../validation/2026-11-09-canvas-fixtures.md)). 27 import; the 5
  rejections are 4 archives that are not course exports and 1 deliberately
  malformed manifest. This found and fixed six real-world problems:
  1. an empty `course_settings/media_tracks.xml` (Canvas writes these) failed
     the whole import;
  2. a manifest declared UTF-16 but encoded as UTF-8 failed to parse;
  3. courses re-zipped inside a folder, with macOS `__MACOSX`/`.DS_Store`
     entries, were rejected;
  4. archives made on Windows (backslash paths, in names and references)
     were rejected;
  5. Canvas quiz questions in `non_cc_assessments/*.xml.qti` were never read,
     so real quizzes imported with no questions;
  6. question banks (named and unfiled) were ignored; they are now in the
     course model and the course map.
- **Over 4 GiB.** A virtual 5 GiB ZIP64 export with course text beyond the
  4 GiB mark slims correctly while reading about 1 KB and never touching the
  video.
- **Scale.** A 500-item course (80 rubric-graded assignments, 200 quiz
  questions, 30 videos) imports, strips and maps in about half a second, and
  consultation prompts fit both a 32k-token fallback model and a large model.
- **PostgreSQL.** Course models, chunked uploads and consultation state were
  run against PostgreSQL 16 (`tests/test_postgres_consultant.py`).

### Still to verify with real courses
- An instructor's own large export, through the deployed site.
- The browser extension against a live Canvas instance (the hand-off is tested
  in real Chromium; the Canvas API calls are tested against a fake Canvas).
- New Quizzes content in exports and through the extension.

Run `node scripts/validate_exports.cjs <folder> --report report.md` on any
folder of exports; it reports counts only and uploads nothing.

### Deviations from the plan
- Task 4: instead of a Vercel Blob fallback, uploads are always sent in pieces
  under 4 MB and assembled server-side (simpler, works on any host).
- Task 11: the earlier wizard moved to `/mentor/legacy` rather than being
  deleted, because Phase 3 reuses its drafting and approval flow. The legacy
  hosted raw-upload pipeline and local HAX build modules remain until then.
- Added a documents-only intake route and question banks (found in Task 12).
