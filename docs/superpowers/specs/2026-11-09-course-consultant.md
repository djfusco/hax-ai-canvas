# Course Consultant: Whole-Course Pedagogical Redesign

**Goal:** Turn the faculty mentor into a pedagogical consultant. It reads an
existing course, holds a structured conversation with the instructor about the
whole course (purpose, learners, outcomes, content, resources, assessment,
engagement, AI), and co-authors an improved, AI-enhanced version of that
course. A HAX site is an optional export at the end, not the goal.

**Constraints:** Build on this repository. Keep the existing AI client,
provider support, structured output, prompt fitting, encrypted keys, workspace
storage and approval rules. Phase 1 is local-only. Every new component must
also work behind a web server, so a hosted version can be added later without
rework (see "Getting the course in").

---

## 1. What changes, in one table

| | Current mentor | Course consultant |
|---|---|---|
| Center | Item-by-item framework scoring | A conversation about the whole course |
| Interview | One short step that produces a free-text brief | The core of the product: structured by topic, ongoing |
| Analysis unit | Individual pages | The course as a system (outcomes, activities, assessments, resources) |
| Main output | Revised pages, then a HAX site | A **Course Improvement Plan**, then revised materials |
| Frameworks | Bloom and AIAS headline every report | Lenses the consultant uses when relevant |
| HAX | The destination | One export option among several |

## 2. Getting the course in (the size problem)

### Why exports are huge
A Canvas `.imscc` export bundles every uploaded file: lecture videos, slide
decks, image banks. That is where multi-gigabyte sizes come from. The
consultant needs almost none of it. It needs **structure and text**: modules,
pages, assignment prompts, rubrics, quiz questions, due dates, points, the
syllabus, and a list of which files exist (name, type and size), not their
bytes. For a typical course that is a few megabytes, even when the export is
8 GiB.

### Option A: Slim the export in the browser (recommended first)
An `.imscc` file is a ZIP archive. ZIP stores a table of contents at the end
of the file, so the browser can read that table and pull out individual
entries with `File.slice()` **without reading or uploading the whole file**.

1. The instructor picks their normal Canvas export in the browser.
2. A small script (e.g. `zip.js`, which reads lazily from a `Blob`) lists
   the entries and extracts only `imsmanifest.xml`, `.html`, `.xml`, `.qti`
   and other text entries, under a size cap per entry.
3. Media and documents are **not** uploaded. They are recorded in a
   `media_inventory.json` (path, type, size) so the consultant still knows
   "Week 3 has a 45-minute lecture video" and can discuss it.
4. Optionally, the instructor ticks specific PDFs or slide decks (e.g. the
   syllabus or a key reading) to upload as text sources.
5. The browser uploads a small "slim course package" (typically 1 to 20 MB).

Advantages: works today, needs no Canvas admin approval, uses the export
instructors already know how to make, and makes a hosted web version
practical. It also removes most of the existing hosted machinery (Blob
multipart upload, Sandbox preparation, Workflows) for this use.
`imscc_import.py` already tolerates missing files with warnings; it gains a
"slim" mode that reads the inventory instead of the bytes.

Trade-off: media files are not available to rebuild into a HAX site. The HAX
export (Phase 3) can either link to the originals in Canvas or ask for the
specific files it needs at that point.

### Option B: Canvas API connection (recommended second)
Read the course directly from Canvas through its REST API: modules, pages,
assignments with rubrics, quizzes, discussions, syllabus, outcomes and file
metadata. Nothing large is transferred, and it is always the **current**
version of the course, not a stale export.

- **Pilot:** the instructor pastes a Canvas personal access token. Simple,
  but some institutions disable personal tokens, and tokens are powerful, so
  read-only scope and encrypted storage are required.
- **Production:** an institutional Canvas developer key (OAuth2) so
  instructors click "Connect to Canvas" and approve read access. This needs
  a Canvas admin at each institution.
- **Later:** an LTI 1.3 tool installed in Canvas, launched from inside the
  course. Best experience, most institutional setup.

The API also gives data an export does not present cleanly: due dates,
points, rubric criteria, and module requirements, all of which matter for
workload and alignment analysis.

### Option C: Selective export guidance (fallback)
Canvas lets instructors export "specific content" and leave out files. A
short help panel can walk them through this. Zero engineering, but it relies
on instructors following steps.

### Option D: Start from documents only
Upload or paste a syllabus and a few key documents, with no Canvas at all.
The current "blank course" path already supports this; it stays.

### Recommendation
Build **A** in Phase 1 (it also works locally, so nothing is wasted), keep
**D**, and add **B** when moving to a hosted web version. All sources produce
the same internal `course` model, so the consultant never cares where the
course came from.

## 3. The consultation

### Topics
The consultant works through these topics, in a sensible order but following
the instructor's lead:

1. **Purpose and learners:** who takes the course, why, prior knowledge,
   modality, class size.
2. **Outcomes:** what students should be able to do; how outcomes connect to
   the program.
3. **Content and sequence:** modules, pacing, workload by week.
4. **Resources:** texts (cost, currency, accessibility) and technology
   (tools, platforms, what each is used for).
5. **Assessment and feedback:** what is assessed, how, how often; alignment
   with outcomes; feedback loops; academic integrity.
6. **Engagement and inclusion:** active learning, interaction, accessibility,
   Universal Design for Learning.
7. **AI's role:** where AI helps learning, where it threatens it, the course
   policy, and AI-enhanced activities and assessments.

### Topic state
Each workspace stores a `consultation` record:

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
  "current_topic": "assessment"
}
```

Status values: `not_started`, `in_progress`, `covered`, `skipped`.

### How a turn works
1. The instructor sends a message.
2. One structured AI call returns:
   - `reply`: the conversational answer (questions, observations, ideas);
   - `topic_updates`: facts, goals and open questions to merge into topic
     state, each citing evidence IDs where it relies on course content;
   - `next_topic`: optional suggestion to move on.
3. The server validates the result (known topics, known evidence IDs,
   length limits), merges it, and saves it.

The reply cites course evidence the same way assessments do today, so
claims like "your syllabus says exams are 70%" are checkable.

### Grounding
The consultant is seeded with a **course map** (section 4) and the topic
state, not the full course text. When a topic needs detail (e.g. assessment),
the relevant items are pulled in using the existing `_select_chunks`
relevance selection. This keeps every call small regardless of course size.

## 4. Course map

Built once after import, refreshed when the course changes. Mostly computed
without AI; the AI fills in what needs judgment.

- **Structure:** modules, items per module, item types.
- **Assessment inventory:** every graded item, points, weight, due week,
  type (exam, project, discussion...), and whether a rubric exists.
- **Workload by week:** count and estimated effort of activities.
- **Resources:** textbooks and readings mentioned, external tools and links,
  media inventory.
- **Outcomes:** stated outcomes (from syllabus or Canvas outcomes) and which
  items claim or appear to address them. The AI proposes the mapping; the
  instructor confirms it in conversation.

Shown to the instructor as a readable overview page, and used as compact
context for every consultant call.

## 5. Course Improvement Plan

The main output. A living document with:

- **Course vision:** a short statement agreed in conversation.
- **Revised outcomes.**
- **Proposed changes**, each with: what changes, why (pedagogical rationale
  and evidence), affected items, effort (small, medium, large), and status
  (`proposed`, `accepted`, `rejected`, `deferred`).
- **AI integration:** policy, AI-enhanced activities, assessment redesign.
- **Open questions.**

The consultant proposes changes during the conversation; the instructor
accepts, edits or rejects each one. Only accepted changes drive materials
generation. Models never accept their own proposals.

## 6. Generating materials and exporting

From accepted changes, the consultant drafts revised or new pages,
assignments, rubrics and a syllabus, reusing the existing draft, edit and
approval flow. Export options:

- **Plan document** (HTML or Word) for sharing with colleagues or a
  curriculum committee;
- **Revised materials** as HTML pages;
- **HAX site** using the existing `local_hax.py` build;
- **Canvas-ready package** (later).

## 7. Reuse map

| Keep as is | Adapt | Retire or park |
|---|---|---|
| `ai_client.py`, `model_capabilities.py`, `provider_models.py` | `mentor_service.interview` becomes the consultation turn | Hosted mode (`hosted_*.py`, Vercel, Blob, Sandbox) for now |
| `mentor_credentials.py`, `mentor_store.py` | `imscc_import.py` gains slim-package mode | Large-course draft batching (`_triage_items`, `_draft_batch`) |
| `mentor_materials.py` | `mentor_assessment.py` becomes an on-demand lens, not the headline | Legacy converter pages behind "Advanced" |
| `local_hax.py`, draft edit and approval | `mentor.html` and `mentor.js` wizard steps | |
| `bin/index.js` launcher | | |

## 8. Phases

**Phase 1: Consultation core**
- Slim course package: browser extraction plus slim import mode.
- Course map (computed parts plus AI outcome mapping).
- Topic-based consultation turn with validated topic state.
- UI: chat in the center, topic progress and course map alongside.

**Phase 2: Improvement Plan**
- Plan data model, proposal and acceptance flow, plan view and export.

**Phase 3: Materials and exports**
- Generate materials from accepted changes, reuse draft and approval.
- HAX export, linking or requesting media as needed.

**Phase 4: Web version**
- Hosted deployment using the slim package; Canvas API connection (Option B).

## 9. Open questions for the instructor-facing design

- Should the consultant follow the topic order strictly, or let the
  instructor jump around freely? (Proposal: suggest an order, allow jumping.)
- How opinionated should it be? (Proposal: ask first, then offer two or
  three concrete options with trade-offs, never a single prescription.)
- Who else reads the plan: only the instructor, or also instructional
  designers and committees? This affects the plan export format.
