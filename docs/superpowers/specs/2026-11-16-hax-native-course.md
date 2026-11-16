# HAX-native course pages

Date: 2026-11-16 · Status: phase 1 built (owner: "go ahead with the plan you decide on")

## Intent

The course Course Consultant builds should be a real HAX course, not plain text in a HAX theme:
it uses HAX's own learning elements, follows HAX's rules
([PRAW](https://github.com/haxtheweb/praw)), and is handed to HAX Lite, which assembles the site.
Course Consultant never assembles a HAX site itself and never runs the `hax` CLI.

## What HAX Lite does with what we send

From `HAX_LITE_PUBLISH_CONTRACT.md` and `src/site-import.js` in haxiam-orchestrator:

- `site.json` is read as a description only: each item's title, slug, parent, order, published,
  hide-in-menu. HAX Lite creates the site natively and adds each page through HAXcms's own page
  API, which gives pages new ids and cleans the HTML with HAXcms's sanitizer.
- `files/**` is accepted (6 MiB a file, 16 MiB a publish, 10 MiB archive by default).
- Themes, custom code and generated files are ignored; imported sites start on the default theme.

So the files that make a HAX site readable by AI agents (`llms.txt`, the Markdown copy of each
page, the search index) are HAXcms's to generate. Our job is content and structure.

## Rules taken from PRAW

| Rule | How it is met |
|---|---|
| Only HAX-capable elements that exist; never invent a tag | A closed list in `python/hax_elements.py`, each entry read from the element's source |
| Fill question feedback; plain "Correct/Incorrect" is the weakest feedback | Every question carries an explanation, a hint and the passage it came from |
| `a11y-collapse` sets `heading-button` | The sanitizer forces it on |
| Educational content carries OER Schema | Weekly objectives are `oer-schema` `LearningObjective`; questions carry `has-learning-objective` |
| Essential questions go in a `stop-note`, kept open at the top | Each week page opens with one |
| Imported content is data, never instructions | Unchanged: prompts already say so, and the AI never writes markup |
| `x/` routes are reserved; slugs are kebab-case | `course_site.slugs` |

## Design

**The AI returns data; the app writes the markup.** A weekly step now also returns an essential
question and up to four practice questions as plain data (kind, question, options, which are
correct, explanation, hint, objective, source item, quoted evidence). `hax_elements` turns that
into markup. A page can therefore never contain a misspelled tag or attribute.

**Every practice question is traceable.** A question is kept only when the passage it quotes is
found, word for word, in the course item it names. Quiz items are never a source, so graded
questions and answers are never turned into open practice. Questions that fail are discarded and
counted; the instructor notes say how many were kept and dropped, and the page card is tagged
"AI practice questions: check the answers".

**Elements used (phase 1):** `stop-note`, `oer-schema`, `multiple-choice`,
`true-false-question`, `self-check`, `a11y-collapse`.

**The sanitizer knows the list.** It keeps those elements with only the attributes and slots
each accepts, keeps an `input` only as an answer option inside a question, and unwraps every
other custom element. Instructor edits and "ask for changes" pass through the same check.

**Two forms of every page.** HAX outputs (HAX-theme preview, site ZIP, Publish to HAX Lite) get
the elements. Outputs that cannot run HAX's JavaScript (plain preview, share link, Word, the
documents ZIP, the Canvas import file) get `hax_elements.to_plain`: the same content as ordinary
HTML, with answers behind a "Show answer" disclosure.

**Readable addresses.** Page slugs are now `week-1-cells`, not a 12-character hash.

## Verified against the real thing

- Each element's attributes and slots were read from haxtheweb/webcomponents on 2026-11-16.
- HAXcms's sanitizer policy (`haxcmsSanitizePolicy.json`) keeps custom elements, `input`, and
  every attribute we write; a test checks our list against those rules.
- A built sample course was served as a static HAX site and opened in Chromium with HAX's
  components from `cdn.hax.cloud`. All six elements loaded; questions, options, objectives, the
  note and the readable address were read correctly.
- **That check found a bug the source alone would not:** the element source marks a right answer
  with `data-correct="true"`, but the build on HAX's CDN only reads the older `correct`
  attribute, so every answer showed as wrong. Both markers are now written.

## Not verified

- A real publish into HAX Lite (staging is still to be tested; see `docs/todo.md`). The contract
  and sanitizer policy say the elements survive; nothing has been published with them yet.
- Whether HAX Lite's imported pages get `llms.txt` and per-page Markdown as a native site does.
- Practice-question quality with a real model. Tests use a scripted model.

## Pictures (phase 2)

A Canvas export refers to its files as `$IMS-CC-FILEBASE$/...`, which only Canvas can resolve, so
every picture in a built HAX course was a broken image. `python/course_files.py` fixes both cases.

- **An explicit choice, off by default.** The standing promise is that only text leaves the
  instructor's computer. The upload step now offers "Include the pictures your pages show", with
  the count and size. Unticked, nothing changes and no picture is read. The note says pictures
  are not checked for student information.
- **What is taken:** only images that a course page displays, PNG/JPEG/GIF/WebP by their bytes
  (the file name is not trusted; SVG is refused), 1 MiB each, 6 MiB and 200 a course. HAX Lite's
  default archive limit is 10 MiB.
- **Where they go:** a table of their own, deleted when the course is replaced or deleted. They
  are served to the instructor's preview, the HAX-theme preview and the share link, and written
  to `files/` in the HAX site ZIP and the HAX Lite publish, where HAX Lite rewrites references to
  its own stored names. They are never sent to an AI provider.
- **Whatever is not included** (a picture left out, a video, a linked PDF, a link into Canvas)
  becomes a short note in the text. This applies whether or not pictures were included.

Verified: a sample export with a real picture was imported through the app, its HAX site ZIP
served statically, and the page opened with HAX's components: the picture loaded from `files/`
and the missing one showed its note. Not verified: a real HAX Lite publish with files.

## Course design by Understanding by Design (phase 4)

PRAW's rule: a HAX course whose goal is understanding is designed with the
`hax-ubd-backward-design` skills (desired results, acceptable evidence, learning plan) and checked
against `design-standards.md`. `python/course_design.py` holds that design for the whole course.

- **Stage 1, desired results:** big ideas, enduring understandings ("Students will understand
  that ..."), overarching and topical essential questions, knowledge, skills, and predictable
  misunderstandings with how to catch and confront each.
- **Stage 2, acceptable evidence:** differs from PRAW on purpose. The course already exists, so the
  design does not invent assessments. It classifies the course's own graded work (performance
  task, open response, quiz or test, informal check; a real problem or an exercise; which facets)
  and records which understandings each piece shows. What is missing is reported, and adding it
  is flagged as hand-work in Canvas like any other new item.
- **Stage 3, the learning plan:** the built course. Each week names the understandings it serves;
  its essential question comes from the design; a practice question surfaces a listed
  misunderstanding where the week's text supports it.

**The check (no AI)** follows PRAW's standards as far as a rule can: big ideas present (1),
understandings are propositions (2), essential questions open and of both kinds (3),
misunderstandings caught and confronted (4), every understanding has evidence (5), an authentic
performance task that is a problem (6), more than one facet (7), no understanding evidenced by
tests alone (8, the mechanical part of the validity self-test), many measures over time (9), every
understanding served by a week (10), no week without a purpose (12), plus retrieval practice in
every built week and the revised-Bloom level of weekly objectives. It gives PRAW's ratings:
Aligned, Partial, Activity-oriented, Coverage-bound. It says what it cannot judge (whether a
question is a good one, each assessment's validity, depth over coverage, standard 11).

**Where it shows:** a Course design card on the Plan step (draft with AI, edit, tick which graded
work shows which understanding, see the check); a student page "Big ideas and essential
questions" in Start here, written by the app in HAX elements; an instructor-only page with the
misunderstandings, evidence map and check.

**The consultation** now works by the same method: its topic prompts ask about big ideas,
understandings and misunderstandings (outcomes), what would show understanding and whether a
student could do well without it (assessment), and whether each week serves an understanding
(content); it names the two ways design goes wrong.

Not verified: the quality of an AI-drafted design with a real model (tests use a scripted one).

## Phases

1. **Done:** verified element list, sanitizer, weekly pages (essential question, objectives,
   grounded practice), plain forms, readable slugs.
2. **Done (pictures):** see "Pictures" below. Still open: pictures for courses brought in through
   the browser extension or the Canvas connection; pictures in the Canvas import file.
3. **Design in the site:** carry the course design (outcomes, alignment, accepted changes) as a
   page hidden from the menu, or as a UbD manifest if HAX Lite accepts `.json` in `files/`.
4. **Done (method):** see "Course design by Understanding by Design" below. Still open from this
   phase: PRAW's page-level audits (UDL beyond "students respond", chunking, instructional events,
   multimedia principles) as checks on built pages.
5. **More elements** where they fit: `video-player` for linked video, `flash-card` for terms,
   `lrn-vocab`, `grid-plate`; practice on item pages in the Full build.
