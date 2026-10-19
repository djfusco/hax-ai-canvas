# hax-ai-canvas: Wizard Redesign + Logic Fixes

**Date:** 2026-10-19  
**Goal:** Make the app genuinely usable for non-technical instructors running `npx hax-ai-canvas`. Stack stays Flask + Jinja2 + vanilla JS — no build step, no SPA.

---

## Problem statement

The current mentor page shows all 6 workflow steps at once, hidden/shown by a JS state machine, with a disconnected "guide card" at the top. Non-technical instructors open the page and see a wall of forms with no clear starting point. The nav exposes both the Faculty Mentor and a legacy pipeline (Requirements / Configuration / Run / History / Import), doubling the confusion. Three logic bugs also exist in the Python backend.

---

## Design

### 1. Step-by-step wizard (mentor.html + mentor.js)

Replace the all-on-one-page layout with a true linear wizard. One step is active at a time. A persistent progress bar at the top shows where the user is. Each step has exactly one primary action button. Steps are HTML sections that get `data-active` / `data-past` / `data-future` attributes; CSS handles visibility.

**Wizard steps:**

| # | Slug | Heading shown to user | Primary action |
|---|------|-----------------------|----------------|
| 1 | `workspace` | Choose or start a course | Create workspace / Resume |
| 2 | `ai-setup` | Set up your AI assistant | Save key & choose model |
| 3 | `content` | Add your course content | Upload .imscc or Start fresh |
| 4 | `discuss` | Tell the mentor your goals | Send message / Approve direction |
| 5 | `draft` | Review the AI draft | Approve this exact draft |
| 6 | `build` | Build your HAX site | Build & Open site |

Navigation: **Back** / **Continue** buttons in each step footer. The JS decides whether Continue is enabled based on step completion. Users can also click a completed step in the progress bar to return to it.

### 2. Progress bar

Sticky top bar (below the app header) showing the 6 step labels as pill-shaped chips. Active step is highlighted. Completed steps show a checkmark and are clickable. Future steps are greyed out and not clickable.

### 3. Nav simplification (base.html)

In local mode (`not is_hosted`), hide the legacy pipeline links (Requirements / Configuration / Run / History / Import) entirely. Replace with a single "Advanced" link that reveals them in a dropdown — or just remove them from the nav and let the footer link to them. The app already redirects `/` to `/mentor` in local mode, so nothing breaks.

### 4. Language simplification

Replace technical jargon with plain English throughout:

| Current | Replacement |
|---------|-------------|
| IMSCC | Canvas course export file |
| .haxcourse | prepared course file |
| Neon Auth Base URL | (hidden in local mode — not needed) |
| constructive alignment / AIAS | (keep in reports but explain inline) |
| chunk coverage | (collapse into a summary sentence) |
| instructional JSON | course content |
| provider | AI service |

### 5. AI setup flow simplification (Step 2)

Currently 4 separate actions. Collapse into one inline flow:
1. Pick AI service from cards (Anthropic / OpenAI / Gemini / NebulaONE / No AI)
2. Enter API key → one "Set up" button fetches models and shows a picker in-place
3. Pick model → "Use this model" saves everything

The `<select>` for conversation checkpoint (Initial interview vs. Final draft review) is removed. The JS automatically selects the right mode based on workflow state.

### 6. Chat UX (Step 4)

Remove the "Conversation checkpoint" select. The backend already knows which phase is active (pre-approval vs. post-draft). The JS infers the correct endpoint from workspace state:
- No approved direction → `interview` mode
- Approved direction + draft exists → `review` mode

### 7. Button sequencing

Each step shows only its primary action. Secondary/destructive actions (Forget key, Cancel job, etc.) are collapsed under an expandable "More options" section or displayed only when contextually relevant.

---

## Bug fixes (Python)

### Bug 1: review_draft missing schema (mentor_service.py:499)

```python
# Current (wrong):
data = parse_json(_call_fitted(client, prompt, 7000))

# Fixed:
data = parse_json(_call_fitted(client, prompt, 7000, schema=schema))
```

### Bug 2: draft-gen preflight race (mentor_service.py:420)

The preflight check runs before `assessment_context` is built. If baseline assessment adds substantial text, the second `_fit_prompt` call may silently scale down excerpts even though the preflight passed. Fix: move the preflight check to after `assessment_context` is populated, or do the preflight with a conservative placeholder to account for the assessment context size.

Fix: add a second preflight after baseline assessment runs, passing a sentinel `assessment_context` of maximum expected size. If it fails, raise `ModelCapabilityError` before making any paid calls.

### Bug 3: serial assessment calls (mentor_assessment.py)

`assess_design` iterates items serially. Use `concurrent.futures.ThreadPoolExecutor` to parallelize item assessment calls. Each item's chunks remain sequential within the item (they depend on each other for coverage accounting), but items are independent.

Constraint: keep progress callbacks working — use a thread-safe counter and lock.

---

## Files changed

| File | Change |
|------|--------|
| `python/mentor_service.py` | Fix bugs 1 and 2 |
| `python/mentor_assessment.py` | Fix bug 3 (parallel items) |
| `python/app/templates/base.html` | Hide legacy nav in local mode |
| `python/app/templates/mentor.html` | Rebuild as stepper wizard |
| `python/app/static/style.css` | Add wizard/stepper CSS, simplify |
| `python/app/static/mentor.js` | Rebuild state machine around step transitions |

No changes to routes, APIs, or Python backend logic beyond the three bug fixes. The mentor API contract is unchanged.

---

## What does NOT change

- All approval gates and security invariants (AI cannot approve its own work)
- The mentor API routes in `mentor_routes.py`
- The `mentor_service.py` / `mentor_assessment.py` logic (except the 3 bug fixes)
- The `ai_client.py`, `model_capabilities.py`, `provider_models.py` files
- The `npx hax-ai-canvas` entry point and Python venv setup
- Hosted mode compatibility (`is_hosted` flag drives conditional rendering as before)

---

## Success criteria

- A non-technical instructor can complete the full workflow (workspace → AI setup → import course → discuss → approve → build site) without reading documentation
- Only one primary action is visible at each step
- The legacy pipeline links are not visible in the default local flow
- All 3 bugs are fixed
- The existing test suite passes unchanged
