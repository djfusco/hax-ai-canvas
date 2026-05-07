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
| **Node.js 16+** | [nodejs.org](https://nodejs.org/en/download) |
| **Python 3.10+** | [python.org/downloads](https://www.python.org/downloads/) |
| **HAX CLI** | `npm install -g @haxtheweb/create` |
| **Canvas API token** | Canvas → Account → Settings → New Access Token |
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
2. **Configuration** tab — enter your Canvas URL, Canvas API token, and AI provider key (with Test Connection buttons)
3. **Run** tab — enter a Course ID, choose your AI provider, and click **Run Pipeline**
4. **Done** — buttons appear to open the HAX site, dashboard, readiness report, and changes report
5. **History** tab — revisit reports for any previously processed course

## Where your data is stored

All user data lives in `~/.canvas-course-builder/`:

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
