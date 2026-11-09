# Course Consultant: Whole-Course Pedagogical Redesign

**Goal:** Turn the faculty mentor into a cloud-hosted pedagogical consultant.
It reads an existing Canvas course, holds a structured conversation with the
instructor about the whole course (purpose, learners, outcomes, content,
resources, assessment, engagement), and co-authors an improved, AI-enhanced
version of the course. The main deliverable is a Course Improvement Plan plus
revised key documents. A HAX site is an optional download.

**Audience:** any instructor at any institution. One instructor per
consultation; no sharing or roles.

**Constraints:**
- **Cloud only.** One public website. The local `npx` version is retired.
- **Bring your own AI key**, as today, plus **OpenRouter**. The service never
  pays for AI usage.
- **Low-cost hosting run by the project owner** (Vercel + Neon, as already
  used by the hosted beta).
- **Store data, minimized:** slimmed course text, conversations and plans are
  stored server-side; student information is stripped before storage or any
  AI call.
- Build on this repository: keep the AI client, structured output, prompt
  fitting, workspace storage and approval rules.

---

## 1. Decisions

| Area | Decision |
|---|---|
| Sessions | Several sessions over weeks. Each returning session opens with "where we left off". The first session still delivers a course snapshot and 3-5 opportunities within about 30 minutes. |
| Opening | Hybrid: a short factual snapshot of the course, then open questions about what the export cannot show (students, goals, frustrations). |
| Advice style | Ask before advising on each topic, then offer 2-3 concrete options with trade-offs, grounded in the course's own evidence and a named principle. The instructor can adjust directness. |
| AI enhancement | All four dimensions, as a thread through every topic: AI-resilient assessment, AI-integrated learning, a course AI tutor or practice resource, and AI for the instructor. |
| Frameworks | Backward design as the backbone; UDL; plus the current set (revised Bloom, AIAS 2.1, constructive alignment, TILT, authentic assessment). |
| Deliverable | Course Improvement Plan + revised key documents (syllabus, major assignments, rubrics). HAX site optional, as a download. |
| Collaboration | Instructor only. |
| Student privacy | Automatically strip likely student information before storage or any AI call, and say so clearly in the interface. |
| Deployment | Cloud only. |
| AI providers | Bring your own key: OpenAI, Anthropic, Gemini, institutional gateways, and OpenRouter. |
| Hosting | Run by the project owner, low cost. |
| Data | Stored, minimized, with retention limits and full deletion. |
| API key storage | Deferred; not decided in this spec. |

## 2. Getting the course in

A Canvas export bundles every uploaded file, which is why exports reach
several gigabytes. The consultant needs structure and text, which are a few
megabytes. Four paths, all producing the same internal course model:

### Path 1: Export upload, slimmed in the browser (launch)
The instructor picks their normal Canvas `.imscc` export on the website.
**The file is never uploaded.**

1. An `.imscc` is a ZIP. ZIP stores its table of contents at the end of the
   file, so the browser reads only that table (typically a few hundred KB,
   even for 8 GB).
2. Using `File.slice()`, the browser reads only the text entries it needs
   from the instructor's disk: `imsmanifest.xml`, HTML, XML and QTI files,
   under a per-entry size cap. Media bytes are never read.
3. Entries are decompressed in the browser (`DecompressionStream`, with
   ZIP64 support for exports over 4 GB; e.g. `zip.js`).
4. Media and documents are recorded in `media_inventory.json` (path, type,
   size) so the consultant knows they exist.
5. The browser suggests small documents (PDF, DOCX, PPTX) that assignments
   or pages link to, and the instructor can tick them for inclusion as text.
6. The browser uploads a slim course package, typically 1-20 MB. The server
   strips student information (section 7) before storing anything.

This replaces the hosted beta's Blob multipart upload, Sandbox preparation
and Workflow chain for course intake.

**Export guidance:** a short help panel shows how to export from Canvas with
"Select specific content" and Files unticked, so large courses produce a
small export in the first place. Slimming handles anything that slips
through.

### Path 2: Chrome extension (Phase 1)
A Chrome/Edge extension (Firefox later) that reads the **live** course while
the instructor is logged into Canvas, using their existing session. No token,
no admin approval. Designed for trust:

- **No broad permissions at install.** Uses `activeTab` + `scripting`: it
  runs only on the tab where the instructor clicks its button, only at that
  moment. No "read all your data on all websites" warning, and it works on
  any Canvas domain, including custom ones.
- **Read-only, course content only.** Calls only content endpoints (modules,
  pages, assignments, rubrics, assignment groups, quizzes, discussion
  prompts, syllabus, outcomes, file metadata). Never enrollments,
  submissions, grades, users, or student discussion replies.
- **Preview before sending:** "These 84 items will be sent", with a list the
  instructor reviews. Nothing is sent until they confirm.
- **Open source**, published in the Chrome Web Store, with a plain-language
  privacy page.
- **Optional.** Export upload is always available, since some institutions
  restrict extensions.

The extension produces the same slim course package as Path 1, with richer
live data (exact due dates, rubric associations, New Quizzes where the API
allows).

### Path 3: Institutional connection (later)
For institutions that adopt the tool: a Canvas developer key (OAuth2, scoped
read-only) or an LTI 1.3 install, so instructors click "Connect to Canvas" or
launch from inside their course. Requires a Canvas admin at each
institution, so it is a partnership step, not a launch requirement.

### Path 4: Documents only (kept)
Upload or paste a syllabus and a few documents, with no Canvas. The existing
blank-course path already supports this.

### Not planned: personal access tokens
Pasting a Canvas token gives the tool full access as the instructor,
including grades, and some institutions forbid it. The extension gives the
same live-course benefit without handling a credential.

### Known gap: New Quizzes
Canvas New Quizzes may not appear fully in exports. Test with real courses
early; the extension may cover more through the API.

## 3. Importer: read the full export

The slim package keeps every XML file, so all course metadata survives. The
current `imscc_import.py` extracts pages, syllabus, modules, quizzes,
discussions, and per-assignment points, due dates and submission types. It
**ignores** data the consultant needs. Phase 1 adds:

| Data | Source | Why it matters |
|---|---|---|
| Rubrics (criteria, levels, points) | `course_settings/rubrics.xml`, linked from `assignment_settings.xml` | Shows what is actually valued |
| Grade weights | `course_settings/assignment_groups.xml` | Shows where student effort goes |
| Learning outcomes | `course_settings/learning_outcomes.xml` | Backbone of backward design |
| Module requirements and prerequisites | `course_settings/module_meta.xml` | Pacing and sequence |
| Assignment settings (grading type, peer review, attempts, group work) | `assignment_settings.xml` | Assessment design |
| Quiz settings (time limit, attempts, question types) | `assessment_meta.xml`, QTI | Assessment design |
| Grading scheme, late policy | `course_settings/grading_standards.xml`, `late_policy.xml` | Course policy |
| Media inventory | `media_inventory.json` from the slim package | Resources discussion |

The importer gains a **slim mode** that reads the inventory instead of file
bytes and treats missing media as expected, not as warnings.

## 4. The consultation

### Topics
1. **Purpose and learners:** who takes the course, why, prior knowledge,
   modality, class size.
2. **Outcomes:** what students should be able to do; program alignment.
3. **Content and sequence:** modules, pacing, workload by week.
4. **Resources:** texts (cost, currency, accessibility) and technology.
5. **Assessment and feedback:** what, how, how often; alignment with
   outcomes; feedback loops; integrity.
6. **Engagement and inclusion:** active learning, interaction,
   accessibility, UDL.

AI is not a separate topic. Each topic considers all four AI dimensions
where relevant (e.g. Assessment covers AI-resilient design; Resources covers
a course AI tutor).

The consultant suggests an order (backward design: purpose, outcomes,
assessment, then content) but follows the instructor if they jump.

### Opening a session
- **First session:** course snapshot from the course map (structure,
  assessment weights, workload, outcomes found), then open questions about
  students, goals and frustrations. Within about 30 minutes, it proposes
  3-5 high-value opportunities.
- **Returning sessions:** "where we left off" summary of topic progress,
  accepted changes and open questions.

### Topic state
Stored per workspace:

```json
{
  "topics": {
    "assessment": {
      "status": "in_progress",
      "facts": ["Three exams worth 70% combined", "No rubric for the project"],
      "instructor_goals": ["Less high-stakes testing"],
      "open_questions": ["How is the final project currently graded?"],
      "evidence": ["course:assignment-12", "material:syllabus"]
    }
  },
  "current_topic": "assessment",
  "directness": "balanced"
}
```

Status values: `not_started`, `in_progress`, `covered`, `skipped`.
Directness: `gentle`, `balanced`, `direct`.

### A turn
1. The instructor sends a message.
2. One structured AI call returns `reply`, `topic_updates` (facts, goals,
   open questions, each citing evidence IDs where it relies on course
   content), optional `proposed_changes` (section 6) and optional
   `next_topic`.
3. The server validates (known topics, known evidence IDs, length limits),
   merges and saves.

### Grounding
Every call gets the course map (section 5) and topic state, not the full
course. Topic-relevant items are pulled in with the existing
`_select_chunks` relevance selection, so calls stay small at any course
size.

## 5. Course map

Built after import, mostly without AI:

- **Structure:** modules, items per module, item types, requirements.
- **Assessment inventory:** every graded item with points, group weight, due
  week, type, rubric (yes/no and criteria).
- **Workload by week.**
- **Resources:** texts and readings mentioned, external tools and links,
  media inventory.
- **Outcomes:** stated outcomes and which items address them. The AI
  proposes the mapping; the instructor confirms it in conversation.

Shown as a readable overview beside the chat, and used as compact context
for every call.

## 6. Course Improvement Plan

The main output. A living document:

- **Course vision**, agreed in conversation.
- **Revised outcomes.**
- **Proposed changes**, each with: what changes, why (rationale, evidence,
  named principle), affected items, effort (small, medium, large), AI
  dimension if any, and status (`proposed`, `accepted`, `rejected`,
  `deferred`).
- **AI integration:** course AI policy, AI-integrated activities,
  AI-resilient assessment changes, course AI tutor design, instructor AI
  workflows.
- **Open questions.**

The consultant proposes; the instructor accepts, edits or rejects. Models
never accept their own proposals.

## 7. Student privacy

Before storage or any AI call:
- Drop student discussion replies; keep instructor prompts only.
- Detect and redact likely student names, emails and IDs in announcements,
  pages and documents.
- Never request enrollments, submissions or grades (extension and API
  paths).
- Show the instructor what was removed and why.

## 8. Deliverables and exports

From accepted changes, the consultant drafts revised key documents:
syllabus, major assignments and rubrics, reusing the existing draft, edit
and approval flow. Exports:

- **Plan document** (Word and HTML).
- **Revised documents** (Word and HTML), ready to paste into Canvas.
- **HAX site package** as a download. Building and hosting live HAX sites is
  out of scope for the cloud version.

## 9. Platform

- **Hosting:** Vercel (expect Pro at $20/month once usage is real or
  commercial; Hobby forbids commercial use) and Neon Postgres (free tier
  initially). Existing Neon Auth email sign-in.
- **Long AI work** (course map outcome mapping, document drafting) runs as
  background jobs; consultation turns are short requests.
- **Public-service obligations:** privacy policy and terms; full account
  deletion; inactive-workspace retention limit (e.g. 12 months);
  per-account storage quotas.

### OpenRouter
- **One-click connect** via OpenRouter's OAuth PKCE flow: the instructor
  approves and the app receives a user-controlled key, no copy-paste.
- OpenAI-compatible API: add to `ai_client.py` with a base URL.
- Use OpenRouter's model list (context length, pricing, structured-output
  support) to drive `model_capabilities.py`.
- Instructors can set spending limits in OpenRouter.

## 10. Reuse map

| Keep | Adapt | Retire |
|---|---|---|
| `ai_client.py`, `model_capabilities.py`, `provider_models.py` (+ OpenRouter) | `mentor_service.interview` becomes the consultation turn | Local `npx` launcher (`bin/index.js`) and local-only modules (`local_*.py`, `sqlite_client.py`) |
| `mentor_materials.py` | `imscc_import.py`: slim mode + full metadata (section 3) | Blob multipart upload, Sandbox preparation, Workflow intake chain |
| `mentor_store.py`, `hosted_data.py`, `hosted_auth.py` | `mentor_assessment.py` as an on-demand lens | Live HAX build and serving (`local_hax.py`), replaced by a download |
| Draft edit and approval flow | `mentor.html`, `mentor.js`: chat, topic progress, course map, plan views | Large-course draft batching (`_triage_items`, `_draft_batch`) |
| | `export_course.py` / `build_site.py` for the HAX download | Legacy converter pages |

## 11. Phases

**Phase 1: Intake and consultation core**
(Plan: `docs/superpowers/plans/2026-11-09-consultant-phase-1.md`)
- Browser-side export slimming + slim upload; export guidance panel.
- Importer: slim mode + full metadata (rubrics, weights, outcomes, module
  requirements, settings).
- Student-information stripping.
- Course map.
- Topic-based consultation with validated state, session summaries.
- Chrome extension (section 2, Path 2) and its import adapter.
- OpenRouter provider.
- Test with real large exports and the extension, including New Quizzes.

**Phase 2: Improvement Plan**
- Plan model, propose/accept flow, plan view, Word/HTML export.

**Phase 3: Documents and exports**
- Generate revised syllabus, assignments and rubrics from accepted changes.
- HAX site package download.

**Phase 4: More browsers**
- Firefox (and Safari if demand) versions of the extension.

**Later: Institutional connection**
- OAuth developer key or LTI 1.3 for adopting institutions.

## 12. Still open
- Where instructors' AI keys are stored (deferred).
- Exact retention period and storage quotas.
- Whether the plan export needs a format for curriculum committees.

## 13. Status (2026-11-09)

| Phase | Status |
|---|---|
| 1. Intake and consultation core | Done (see the Phase 1 plan's status section) |
| 2. Improvement Plan | Done: decisions on proposals, AI draft, edits, Word/HTML export |
| 3. Documents and exports | Done: revised syllabus, assignments and rubrics; Word, HTML, ZIP and HAX site package |
| 4. More browsers | Firefox build with a bridge hand-off; tested via Chromium, not yet in Firefox |
| Later: institutional connection | OAuth2 developer-key connection done; LTI 1.3 not started |

Still to verify with real users and systems: an instructor's own large export
on a deployed site, the extension and OAuth connection against live Canvas
instances, New Quizzes, the Firefox build in Firefox, and real AI
conversations for tone and advice quality.
