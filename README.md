# hax-ai-canvas
## Local instructor workflow
**Node.js 22+ and Python 3.10+ are required.** No global HAX installation,
Canvas token, cloud storage, `.env` file or environment-variable setup is needed.
The launcher checks Python, creates its own virtual environment, installs
dependencies and includes the official pinned HAX CLI.

```bash
npx hax-ai-canvas@latest
```

**Unpublished source:** these changes reach the public `npx` command only after
an npm release. Test this checkout now with `npm install`, then
`node bin/index.js`. For isolated testing, add `--data-dir "/path/test-data"`.
The app opens `http://127.0.0.1:5050/mentor`. Keep the terminal open; Ctrl+C
stops the app and its managed HAX servers. First-run downloads require internet.

### From course idea to a real HAX site
1. Create or resume a workspace. Multiple courses retain separate inputs,
   conversations, drafts, assessments and approval history.
2. Choose an AI provider, enter its key **in the browser**, fetch available
   models, select one and choose **Use selected model**. OpenAI, Anthropic,
   Gemini and institutional Nebula gateways are supported; gateways also need
   their HTTPS URL entered in the UI. No provider environment variables.
3. Import a Canvas `.imscc` export, or explicitly start a new course without
   a file. Optionally add documents, public links and notes labelled reference,
   instructor policy or student-facing source.
4. Discuss learner context, learning goals, constraints and AI expectations.
   Refine the brief, complete the interview and approve the direction.
5. **Analyze & generate draft** assesses the existing instructional items,
   generates proposed content, and assesses the resulting draft. New courses
   have no fabricated original-course baseline.
6. Inspect the actual HTML and instructor-only baseline/proposed reports.
   Request revisions or edit pages. Edits invalidate the assessment; finish an
   AI final review to refresh it, then **approve this exact draft**.
7. **Build & open HAX site** scaffolds a real HAX runtime with the CLI, writes
   approved pages and supported referenced assets, and starts a loopback server.
   Open is enabled only after readiness checks. Start/restart/stop controls
   reopen the same saved site later; rebuilds create a new version.

**Guided mode is not AI assessment:** it is an explicitly chosen no-AI
walkthrough retaining originals or a design-brief outline, not generated
curriculum or framework scores. Models cannot approve or publish their work.

### Evidence and honest assessment limits
The local AI reports use revised Bloom's Taxonomy, the
[AI Assessment Scale 2.1](https://aiassessmentscale.com/), constructive
alignment, TILT purpose/task/criteria and authentic-assessment design lenses.
They include attributed references, quotes from supplied evidence, strengths,
concerns, recommendations and limitations. Higher Bloom/AIAS levels are **not
inherently better**. Six numeric dimensions are application-defined AI
heuristics, not validated research instruments, misconduct probabilities,
student grades or a composite readiness score.

Every supplied item in the admitted scope is assessed, using at most 12,000
visible-text characters per item plus bounded brief/material excerpts.
Truncation is disclosed. Review limits remain 500 items / 16 MiB instructional
JSON, 2 MiB per item, and 30 optional materials. Media and unextracted text are
not semantically assessed. Provider/schema failures produce no counterfeit
neutral scores. Analysis/review can make multiple paid provider requests.

### Large local files and final-site media
Original archives may be up to **8 GiB**, subject to archive safety,
instructional quotas and free disk. Browser selections copy to the local app
in chunks of at most 8 MiB, **not to cloud storage**, with progress, cancel and
saved-copy recovery. Keep the app running during preparation.
For direct access without that local staging copy:

```bash
npx hax-ai-canvas --course "/path/course.imscc"
```

The original remains untouched. Supported media/documents are validated and
streamed separately from AI text. Final HAX sites include supported assets
referenced by approved pages; unsafe/missing/external resources are reported,
not reconstructed. Active Office/PDF content, opaque PDF object streams,
scripts and executable attachments are conservatively excluded.
Prepared `.haxcourse` imports cannot recover previously excluded media.
The secondary static preview/download has a 64 MiB local asset budget and
discloses omissions, avoiding mandatory recompression of multi-gigabyte videos.
The primary HAX site retains supported referenced media.

A HAX course site is not a recreated Canvas LMS: grading, submissions, LTI
and executable quizzes are not restored. HAX editor changes are separate from
mentor snapshots. The site is served locally; themes may request external
fonts. Its separate loopback editor is not an email-account/OS security boundary.

### Local data and optional email accounts
All course work, conversations, assessments, provider keys and sites stay in
`~/.canvas-course-builder/mentor_data/`; generated sites are under
`sites/<workspace>/<build>/`. Keys and upstream auth sessions are encrypted
locally. Updates preserve user data.
By default the existing signed browser-local identity resumes your work;
clearing its cookie can lose access to that identity. Optional email setup is
collapsed in the UI and never required to start. Enter the existing **public
Neon Auth Base URL** there; an administrator must enable email OTP and trust
the exact `http://127.0.0.1:5050` origin in Neon.
Neon handles authentication/account data only, not course storage. The same
verified account resumes multiple courses on this computer, not synchronized
courses on other devices. Selected course excerpts still go to the AI provider.

### Verification and dependency maintenance
Run `python3 -m unittest discover -s tests -v`, `npm run test:upload` and
`npm run build:hosted`. Actual-browser checks are in
`tests/browser_local_mentor.py` and `tests/browser_local_end_to_end.py`;
`scripts/verify_local_hax.py` exercises the real CLI/runtime.
Synthetic provider fixtures exercise real SDK request shapes without paid
calls; they do not establish live-model quality or real-course throughput.
The pinned upstream HAX dependencies retain npm advisories. The source
checkout patches compatible YAML resolution through an override, but npm
ignores dependency-level overrides in consumer installs; do not assume a public
`npx` install inherits that fix or that `npm audit` is clean.

## Hosted and legacy documentation
The sections below preserve optional hosted setup and the earlier direct
converter. They are not prerequisites for the local workflow above.

## Vercel-hosted Faculty Mentor beta

The source checkout includes private browser-to-Blob multipart uploads,
PostgreSQL persistence, durable Vercel Workflows and isolated Sandbox preparation.
Instructors can upload an IMSCC archive of up to **8 GiB** without installing
Python or copying it to Google Drive. Keep the upload tab open until transfer
completes; preparation then continues independently. Large files never pass
through ordinary Flask/Function request bodies.

**Raw retention:** a successful job saves the compact prepared result, downloads
it again to verify its hash/importability, attaches it to the exact workspace
revision, and only then deletes the raw upload. Failed deletion and Sandbox
destruction are tracked for retry. Abandoned uploads expire after 24 hours;
the 15-minute cleanup cron reconciles them and late uploads while their one-hour
authorization remains valid. This is logical object deletion, not a promise of
immediate physical erasure of provider caches/backups. Prepared content,
conversations and approved artifacts remain stored.

This is an **invitation-gated beta with verified email accounts**, not institutional
SSO. Enter an emailed sign-in code to resume your own workspaces and encrypted
provider settings across devices. A typed email alone does not grant access.
Sessions are validated server-side against Neon Auth using same-origin
Secure/HttpOnly/SameSite cookies, not browser storage.
Provider keys are encrypted server-side and scoped to the verified user ID.
Obtain institutional review before
using regulated/student records. Separate staging/production projects, stores,
databases and secrets.

### Accounts and model choice

The mentor page follows six numbered steps: workspace, AI setup, content,
discussion, draft review, and download. A next-step card links to the relevant
control and explains missing prerequisites. It never grants approval itself.
AI setup is expanded, with no silently selected Guided mode: choose a provider
and save its model, or explicitly choose the no-AI walkthrough.

When no content is attached, upload a course/add supporting sources or explicitly
choose to design a new course without a file. That UI choice does not delete
any stored source. Optional inputs and processing/revision details are grouped
separately. Polling preserves unsaved edits and conversation reading position,
and running-job status clears when the job completes.


Hosted mode now defaults to verified email accounts through Neon managed Better
Auth. In your Neon project/branch's **Auth / Better Auth** settings, enable email
sign-in and Email OTP, configure a dedicated production SMTP sender, and add the
exact production origin to trusted domains. Copy the **Auth Base URL** into
`NEON_AUTH_BASE_URL`; the Neon integration may already supply it in Vercel.
SMTP credentials belong in Neon's sender settings, not this app. Google OAuth
is not enabled in this release.

Save a provider key to fetch its model catalog, choose a model, then select
**Use selected model**. Catalogs are encrypted with the credential record,
isolated by user/key version and refreshable. Selection does not extend key
retention. No course content or generation request is sent during discovery.
Mentoring requires an explicit selected model; it does not silently choose a
hard-coded default. Existing explicit model overrides remain supported.

OpenAI, Anthropic and Gemini use their model-list APIs. Known non-text models
are excluded; listing is not proof of inference permission, funding or request
compatibility. Nebula gateways may not expose a catalog: the UI explains the
limitation and permits a manual deployment/model ID. The key is never sent to
a different upstream host. Queued jobs retain their selected provider/model;
replacing or forgetting a key invalidates its catalog and fences queued work
against silently using the replacement.

On first email sign-in, optionally select **Claim this browser's old beta work**.
The server requires the old signed browser identity as well as verified email.
Ownership moves transactionally while stored file paths stay unchanged.
Running jobs/uploads and ambiguous account merges are refused. Another account
cannot claim the same legacy identity. Retire old browser-only deployments
when migrating; do not keep them available against the production database.

`HAX_AUTH_MODE=browser-beta` is an explicit compatibility/rollback option for
the old browser-only beta, not real user accounts. The default is `neon`.

### Private student-view preview

After approving the exact draft and building its content, select **Preview
student view (opens in a new tab)**. Newly built packages include a responsive
course overview, course navigation, styled approved pages and previous/next
links. A persistent **Private student-view preview — not published** banner
distinguishes the preview from a public course. This is a static presentation,
not an installed HAX runtime, a HAX theme preview, or the HAX editor.

The preview uses only approved draft items, not chats, model rationale or raw
reference/policy context. Retained images and supported media stay local to the
package; linked documents download rather than run inside the page. Missing
and external media are not silently restored or fetched. Every hosted page and
asset request remains owner-authorized. There are no preview scripts, remote
fonts or external stylesheets; only the server-generated stylesheet is allowed
by its content-security hash.

The downloaded ZIP also opens as a static course through `index.html`.
Already-built packages are immutable: **build the approved content again** to
get this layout for an older workspace. This does not publish anything or alter
the approved draft; `site.json` still references the content fragments.

### Administrator setup

1. Use a **Vercel Pro project** (the 15-minute cron is not a Hobby schedule),
   Node.js 22+ and Python 3.12. Import this Git repository at its root. Keep the
   Flask framework and checked-in `vercel.json` / `pyproject.toml` entrypoints:
   both Python mentor/Workflow and Node Blob callback endpoints are required.
2. Connect a **private Vercel Blob store** and managed **PostgreSQL**, for example
   through the Vercel Marketplace. Use the provider's TLS-capable connection URL,
   preferably pooled. Tables initialize idempotently on first access. Local
   SQLite workspaces/provider keys are not automatically migrated.
3. Enable Workflow/Sandbox access for the project/team; review availability,
   quotas and billing, and set spend alerts. Runtime OIDC provides SDK project
   authentication. No personal Vercel token belongs in the browser or guest.
   An actual deployed job must verify this account-specific setup.
4. Set every variable below in Vercel's environment settings. `HAX_APP_ORIGIN`
   must be the exact stable HTTPS origin **without a trailing slash**. Update
   it when assigning a different domain; previews need matching configuration.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Private PostgreSQL connection URL |
| `BLOB_READ_WRITE_TOKEN` | Server-only token for the private store |
| `HAX_APP_ORIGIN` | Canonical HTTPS origin |
| `HAX_SESSION_SECRET` | Signed browser identities |
| `HAX_UPLOAD_SIGNING_SECRET` | Scoped upload authorization |
| `HAX_INTERNAL_SECRET` | Node-to-Python completion authentication |
| `HAX_BETA_ACCESS_CODE` | Private beta invitation code |
| `HAX_MENTOR_ENCRYPTION_KEY` | Fernet key; preserve to decrypt saved keys |
| `CRON_SECRET` | Vercel cron bearer authentication |
| `NEON_AUTH_BASE_URL` | Neon branch Auth Base URL, ending in `/database/auth` |
| `HAX_AUTH_MODE` | `neon` (default): require verified email accounts |

Session/upload/internal/invitation/cron secrets must be **different**, random
values of at least 32 printable characters. Optionally set `HAX_ALLOWED_AI_HOSTS`
for administrator-reviewed institutional gateways.

An optional administrator helper generates secrets/a Fernet key, prompts for
storage credentials with hidden input, asks for the Neon Auth Base URL, and creates `.env.local` with owner-only
permissions. It never prints values or overwrites an existing file. Install root
`requirements.txt` first, then run:

```bash
python3 scripts/configure_hosted.py
python3 scripts/check_hosted.py
```

Transfer variables securely into Vercel settings. Do not commit the file, source
it as a shell script, or paste it into chat. Retrieve/share the generated
`HAX_BETA_ACCESS_CODE` privately. `.env.example` contains names only.
The preflight is offline by default. After configuring your own services,
explicitly running `python3 scripts/check_hosted.py --live` initializes tables
and PUT/GET/DELETEs a tiny private probe. It neither provisions paid services nor
verifies queue delivery or Sandbox execution.

The helper now asks for four inputs: pooled `DATABASE_URL` from Neon connection
details, the stable production origin, `BLOB_READ_WRITE_TOKEN` from the private
Blob store, and `NEON_AUTH_BASE_URL` from Neon Auth configuration. It generates
the other secrets. If you already generated a private file, add the Auth URL
without regenerating your encryption/signing secrets.

Build/verification commands from the repository root:

```bash
npm ci
npm run test:upload
npm run build:hosted
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
```

Vercel runs the bundle build automatically. Missing hosted configuration returns
`HOSTED_SETUP_REQUIRED`, never shared local state. Legacy configuration,
filesystem-launch and direct-converter routes are not exposed.

### Deployed smoke test (required before inviting users)

- Visit `/healthz`, then `/mentor`; enter the invite code and create a workspace.
  Verify the emailed code. The same account in another browser must resume its
  work; a different verified account must not see it. Test logout/expiry too.
- Upload a small synthetic IMSCC. Check multipart progress/cancellation, durable
  preparation after navigation/reload, and retained inventory/exclusion warnings.
- Verify the raw `raw/...` object is removed only after prepared content is
  persisted/attached; confirm Sandbox destruction and scheduled cleanup.
  Repeat with a canceled upload and a failed preparation.
- Complete both conversations/approvals in **Guided mode**, build and download
  the approved static content ZIP. A second browser must not access its artifact
  or preview. Only then test a bounded real provider request, understanding its
  cost and course-text transmission.
- Finally test a representative multi-gigabyte archive and review resource
  limits, logs and billing. The 8 GiB cap is an application ceiling, not a
  guarantee every archive fits processing limits.

Deployment Protection must permit the Blob webhook and server-to-server callback
to reach their routes. Protected previews may block them before application
authentication runs. Test a suitably configured deployment, or the stable
production URL behind the application's invite gate. Unit tests do not establish
live deployment success.

The beta produces **reviewable static HAX-compatible content**, not an installed
HAX CMS runtime or automatic public publication. Hosted Canvas API acquisition
is disabled: upload an export or start from scratch. Institutional SSO/Google
OAuth, comprehensive retention administration, accessibility/security review
and provider restore/deletion testing remain production follow-ups. No cloud
resources or deployments are created by installing this source.

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

The local launcher now adds the real HAX runtime build/serve flow described
above. Hosted builds remain static content packages. The older direct converter
is a separate tool and does not enforce the mentor approval gates.

Select a configured AI provider for actual conversational mentoring and draft
proposals. **Guided mode** is an offline walkthrough: it inventories inputs and
retains original content/design-brief text; it does not pretend to provide
LLM analysis, recommendations, baseline readiness scores or a generated full
curriculum. Local AI mode now assesses every item in the admitted review scope
using bounded excerpts, with separate baseline/proposed reports. It is not
unbounded full-course retrieval/indexing. Split large text courses (maximum 500 items,
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

### Hosted versus local execution

The instructions above supersede the earlier fail-closed-only hosted milestone.
Valid hosted configuration selects PostgreSQL/private Blob and Workflows/Sandbox
instead of local files/background threads. Missing configuration still fails
closed. Local execution retains SQLite, a private local vault and thread jobs.

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
| **HAX CLI** | Included by the current launcher; legacy standalone scripts may need their own CLI |
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
