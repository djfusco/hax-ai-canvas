# hax-ai-canvas

## Faculty Mentor workspace — local implementation milestone

Launch `python3 python/app/app.py` from this checkout and open
`http://127.0.0.1:5050/mentor` (or choose **Faculty Mentor** in navigation).
Install updated dependencies with `python3 -m pip install -r python/requirements.txt`
if your interpreter does not have them. These changes are not automatically
available through the public npm package until a release is published.

The mentoring workspace is separate from the legacy direct converter:

1. Create a named workspace, starting from scratch, importing `.imscc` /
   prepared `.haxcourse` content, or acquiring a course with the existing local
   Canvas API configuration. Acquisition alone does not run analysis. Canvas
   API credentials remain local configuration in this milestone; per-account
   hosted Canvas authorization is a production prerequisite.
2. Add supplementary TXT, Markdown, CSV, HTML, PDF, DOCX or PPTX documents,
   public URLs, or teaching notes. Label them reference, policy, or
   student-facing source. They inform the conversation; files are not
   automatically added to the student website.
3. Review the inventory and limitations. Discuss audience, goals, constraints,
   current difficulties, what must not change, and appropriate AI involvement.
4. Refine the editable design brief and complete an interview for that input
   revision. Explicitly **approve direction**, then request draft generation.
5. Inspect actual proposed HTML and reasons/source references. Originals not
   revised by the model are retained and are not claimed as AI-reviewed.
6. Discuss the draft in the final-review conversation; revise through chat or
   edit individual snapshot items. Every changed draft requires a fresh review
   and explicit approval of its current version.
7. **Approve this exact draft**, then **build approved content**. Download the
   course content ZIP or view its protected static preview.

The build contains static review pages, a HAX-compatible content manifest
(`site.json`), bundled retained assets and an approval/version record. It is
**not a full installed HAX CMS runtime or an automatic public deployment**.
The existing direct converter still provides the earlier HAX CLI workflow,
but does not enforce the new mentoring gates.

Select a configured AI provider for actual conversational mentoring and draft
proposals. **Guided mode** is an offline walkthrough: it inventories inputs and
retains original content/design-brief text; it does not pretend to provide
LLM analysis, recommendations, baseline readiness scores or a generated full
curriculum. The new mentor currently uses bounded source excerpts and targeted
proposals, not a full-course retrieval/indexing or eight-dimension batch-scoring
engine. Large text courses must be split into review scopes (maximum 500 items,
16 MiB instructional JSON per workspace; context excerpts are further bounded).

Workspaces, conversation history, source provenance, approval revisions and
job outcomes persist in `python/mentor_data/` when using the source checkout,
or `~/.canvas-course-builder/mentor_data/` for an installed package. Running
jobs use local threads in this milestone. A stopped/restarted process cannot
resume an active request: use **Cancel / recover interrupted job** to reject
late results and retry. Cancellation cannot undo a provider request already
sent or its associated cost.

### Large IMSCC exports: prepare locally instead of uploading bulk media

A 2 GB export should not be sent through the ordinary web upload endpoint.
Run the local preparation tool against the original archive:

```bash
python3 python/prepare_course.py "/path/course.imscc" --output "/path/course.haxcourse"
```

After distribution, the npm launcher also supports:

```bash
hax-ai-canvas --prepare "/path/course.imscc" --output "/path/course.haxcourse"
```

The tool inspects ZIP metadata and reads selected instructional HTML/XML and
optional small documents. It does **not open or inflate excluded bulk media**.
It records exclusions and missing references, leaves the original untouched
and refuses to overwrite an existing output. Import the resulting `.haxcourse`
on the mentor page. This is a **review package, not a course backup**: omitted
videos/images are neither analyzed nor reconstructed.

Preparation limits are intentionally distinct from web import limits:
8 GiB original archive, 32 GiB declared expansion, 50,000 original entries,
64 MiB retained source budget, 10 MiB per XML/instructional entry, optional
documents up to 4 MiB each, and 10,000 retained normalizer entries.
The final package is limited to 64 MiB compressed / 128 MiB expanded.
Use `--no-documents` for instructional HTML/XML only. Text-heavy courses over
these limits should be split rather than silently truncated.

The test suite includes a sparse ZIP containing a 2 GiB media entry and asserts
that entry is never opened while a compact package is produced.

### Credentials, browser identity and privacy

The new mentor does not use shared `.env` provider keys. It uses explicit
per-browser provider credentials, encrypted in a local vault. The browser holds
only a signed opaque HttpOnly/SameSite identity cookie and a CSRF token; raw
provider keys are never placed in localStorage, sessionStorage, URLs or chat.
Opt in to 30-day retention, or use the default 12-hour expiry, and use
**Forget selected provider key** to delete it. This is this-browser continuity,
not an email account, SSO or cross-device account. Clearing the identity cookie
loses access to that browser's workspaces.

Local encryption keys are owner-readable files within the private data directory.
They protect against accidental plaintext disclosure, not a compromised computer
or administrator. Back up/secure that directory appropriately; losing keys
requires re-entering provider credentials. Known saved keys are redacted at
workspace text ingress, but do not paste secrets into files or conversations.
Previously exposed credentials should be rotated; existing `.env` values are
not automatically migrated.

Direct provider endpoints are pinned and inherited SDK auth/custom headers are
removed for mentoring calls. Nebula endpoints must use HTTPS on Azure APIM or
an administrator-listed `HAX_ALLOWED_AI_HOSTS` hostname. Custom gateways should
be reviewed before adding them to that administrator list.

Supplemental files are limited to 10 MiB and extracted text to 60,000 characters,
with explicit coverage warnings. Scanned PDFs are not OCR'd. Public URL ingestion
pins validated public IP addresses, verifies HTTPS hostnames, rechecks redirects
and applies time/size limits; it does not bypass login, fetch subresources or
execute webpage JavaScript. At most 30 supplementary sources are supported.

### Recommended production architecture for Vercel

This milestone is **not publicly deployable unchanged**. When `VERCEL` is set,
dynamic routes fail closed with `HOSTED_SETUP_REQUIRED`; this prevents exposing
the legacy global credentials, local-file launch controls and incomplete
multi-user persistence as a hosted product.

Recommended production components:

- **Vercel web UI / thin API:** authentication, workspace APIs, chat progress
  and signed upload/download authorization. Do not proxy large files through
  ordinary Functions (documented request limit 4.5 MB).
- **Private object storage**, such as a private Vercel Blob store: direct
  authenticated multipart/client uploads, original/prepared inputs and output
  artifacts, deletion/retention policies and owner-scoped access.
- **Managed PostgreSQL:** accounts/ownership, conversations, design briefs,
  materials, revision-bound approvals and durable job leases. Replace local
  SQLite repository operations without weakening transactional checks.
- **Durable processing worker:** bounded extraction, document processing,
  optional future transcription, batch analysis and actual HAX runtime builds.
  Use a Python worker service with a durable queue, or evaluate Vercel
  Workflow/Sandbox for the required binaries/time/disk. A background Flask
  thread is not a durable cloud job.
- **Production BYOK vault:** authenticated account ownership, managed encryption
  keys separate from ciphertext, explicit retention/expiry/deletion, no raw keys
  in browser storage, logs, prompts or analytics.

Before enabling a public deployment: provision those services, implement OIDC/SSO
or account authentication and tenancy tests, migrate the storage/job interfaces,
connect private uploads/artifact delivery, enforce quotas and cost limits,
test retries/restarts and restore/deletion procedures, and perform security and
accessibility review. Never remove the hosting guard just to make deployment
appear to work. No cloud resources, credentials, billing or deployments are
created by this source change.

Useful references:
[Vercel Function limits](https://vercel.com/docs/functions/limitations),
[Vercel client uploads](https://vercel.com/docs/vercel-blob/client-upload),
[OWASP browser-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html).

AI-powered Canvas LMS course to HAX site builder — a local web app for instructors.

## What it does

Pulls a course from Canvas, runs it through an AI evaluation and enhancement pipeline, and produces a deployable [HAX](https://haxtheweb.org/) site with:

- **AI Dashboard** — visual overview with summary cards, risk vs leverage chart, due dates, and filterable item table
- **AI Readiness Report** — detailed per-item scores, strengths, concerns, and prioritized recommendations
- **AI Changes Report** — side-by-side diff of every item: original vs. AI-enhanced
- **HAX Site** — the final AI-enhanced course, served locally

## Prerequisites

| Requirement | How to get it |
|---|---|
| **Node.js 22+** | [nodejs.org](https://nodejs.org/en/download) |
| **Python 3.10+** | [python.org/downloads](https://www.python.org/downloads/) |
| **HAX CLI** | `npm install -g @haxtheweb/create` |
| **Canvas course source** | A local `.imscc` course export, or a Canvas URL/API token |
| **AI API key** | NebulaONE, Anthropic, OpenAI, or Gemini |

## Run

```bash
npx hax-ai-canvas
```

That's it. On first run, this will:
1. Copy the app to `~/.canvas-course-builder/`
2. Create a Python virtual environment
3. Install Python dependencies (~1 minute)
4. Open your browser at `http://127.0.0.1:5050`

Subsequent runs start in seconds (setup is cached).

## First-time setup (in the browser)

1. **Requirements** tab — confirms Python, Node, and packages are ready (with beginner-friendly install guides)
2. **Configuration** tab — enter your AI provider key. Canvas URL/token are only needed for the API source.
3. **Run** tab — choose **Connect to Canvas API** and enter a Course ID, or **Use a local Canvas export file (.imscc)**. For a file, select it and click **Import & Inspect File**, review the coverage/warnings, then click **Run Pipeline**.
4. **Done** — buttons appear to open the HAX site, dashboard, readiness report, and changes report
5. **History** tab — revisit reports for any previously processed course

## Analyze a saved Canvas course without a Canvas token

In Canvas, choose **Settings → Export Course Content → Course → Create Export**,
then download the `.imscc` file. On this app's **Run** page, select the local
export-file source and choose that file. Do not unzip it or enter its filesystem
path. The browser transfers the selected file to this local application; no
cloud-storage service is required.

Importing and inspecting a file makes no Canvas or AI requests. AI evaluation
and enhancement still require your selected AI provider's credentials and send
course text to that provider. **Skip AI** preserves the imported original HTML
and question prompts without making AI calls; it does not generate AI scores.
Uncheck site build/install/start when you only want pipeline processing.

The importer supports common Canvas pages, assignments, discussion prompts,
HTML syllabi, bundled documents/media and text/choice extraction from supported
QTI quizzes. It preserves module metadata, but the current HAX builder groups
content by type. Quiz imports are for instructional analysis, not executable
assessment/grade restoration. Unsupported quiz formats, missing resources,
external tools and excluded executable assets produce preview warnings.
Student submissions, grades, external-tool content and externally hosted media
may not be included in a Canvas export.

Each import gets a new internal course ID and separate normalized export folder;
it does not synchronize with or overwrite an existing Canvas course. Imports
are limited to **256 MiB compressed**, **1 GiB expanded**, **10,000 entries**
and **128 MiB per entry** (XML has a tighter limit). Large media-heavy exports
may need to be reduced before importing. Files are validated for unsafe paths,
archive links and XML entities. Active HTML is removed before rendering.
Normalized content remains under `exports/` until you remove it.

**Import a Shared Course** is a separate feature for this tool's own processed
course ZIPs; it does not accept Canvas IMSCC files.

### Running source changes locally

The public `npx hax-ai-canvas` command runs the published package, not unpublished
changes in this checkout. With Python dependencies installed, launch this source
using `python3 python/app/app.py` from the repository root.

### Offline tests

Run `python3 -m unittest discover -s tests -v` from the repository root.
Tests create synthetic cartridges and temporary outputs; they do not call Canvas,
AI providers, npm or the HAX CLI.

## Where your data is stored

For the npm-installed app, user data lives in `~/.canvas-course-builder/`.
When launching directly from this checkout, it lives under `python/` instead:

| Path | Contents |
|---|---|
| `.env` | Your credentials (Canvas token, API keys) |
| `exports/` | Raw Canvas export JSON |
| `course_pipeline.db` | SQLite pipeline database |
| `hax_prep/` | Intermediate markdown/HTML files |

HAX sites are created at `~/.hax-ai/sites/<course-code>/`.

## Updating

```bash
npx hax-ai-canvas@latest
```

Your `.env` and course data are preserved on updates.

## Supported AI providers

| Provider | Env var needed |
|---|---|
| NebulaONE (default) | `NEBULA_API_KEY`, `NEBULA_BASE_URL` |
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Gemini | `GEMINI_API_KEY` |

## Stop the app

Press `Ctrl+C` in the terminal.
