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

## Phases

1. **Done:** verified element list, sanitizer, weekly pages (essential question, objectives,
   grounded practice), plain forms, readable slugs.
2. **Media:** send the images pages use in `files/`. The browser already reads the export
   locally, so it can include them within HAX Lite's limits.
3. **Design in the site:** carry the course design (outcomes, alignment, accepted changes) as a
   page hidden from the menu, or as a UbD manifest if HAX Lite accepts `.json` in `files/`.
4. **Method:** replace the consultation's generic framework prompts with PRAW's Understanding by
   Design stages, and use PRAW's audit skills (UDL, accessibility, chunking, instructional events,
   multimedia, Bloom's) as checks on the built course.
5. **More elements** where they fit: `video-player` for linked video, `flash-card` for terms,
   `lrn-vocab`, `grid-plate`; practice on item pages in the Full build.
