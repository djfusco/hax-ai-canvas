# hax-ai-canvas

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
