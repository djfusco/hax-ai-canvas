# Wizard Redesign + Logic Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the all-on-one-page mentor UI with a linear 6-step wizard, fix three Python backend bugs, simplify nav and language, all within the existing Flask+Jinja2+vanilla JS stack.

**Architecture:** The mentor.html page is reorganized into six `<section class="wizard-step" data-step="N">` sections shown one at a time. A persistent `<nav class="step-nav">` progress bar sits below the header. The existing mentor.js state machine is extended with a thin wizard layer (`wizardStep`, `activateStep`, `renderWizard`) that syncs step visibility to the existing `getFlowState()` output. All existing API calls, form handlers, upload logic, and backend routes are unchanged.

**Tech Stack:** Python 3.10+, Flask, Jinja2, vanilla JS (ES2020), custom CSS. No build step, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-19-wizard-redesign.md`

## Global Constraints

- No changes to mentor_routes.py, ai_client.py, model_capabilities.py, provider_models.py, or any test file.
- No new Python or JS dependencies.
- All existing HTML element IDs must remain unchanged (existing JS references them by ID).
- Hosted mode (`is_hosted` flag) must continue to work — wizard renders the same HTML, hosted-only elements are controlled by the existing `hidden` attribute patterns.
- `npx hax-ai-canvas` entry point unchanged; tests run with `python3 -m unittest discover -s tests -v`.

## Review Focus

1. **Wizard step shows before workspace loads** — `renderWizard()` must guard against `!mentorAuthenticated`; if called early, step-nav stays hidden and no step is active.
2. **Back button at step 1** — must be disabled/hidden; no step 0 exists.
3. **Active job blocks step navigation** — clicking progress chips must not navigate while `workspace.active_job` or `uploadController` is truthy.
4. **`chat-kind` select removal** — `el('mentor-chat-form').onsubmit` reads `el('chat-kind').value`; after removing the select, this must derive `kind` from workspace state.
5. **Hosted-only elements in step 2** — `el('hosted-upload-help')`, `el('local-upload-help')` in step 3 are controlled by `updateCredentials()`; they must still exist in the new HTML at their original IDs.

---

## Task 1 — Bug Fix: review_draft missing schema

**Files:**
- Modify: `python/mentor_service.py:499`

**Interfaces:**
- Produces: `review_draft()` now passes the JSON schema to `_call_fitted`, enabling native structured output on Anthropic/OpenAI/Gemini clients.

- [ ] **Step 1: Verify the bug exists**

```bash
grep -n "_call_fitted(client, prompt, 7000)" python/mentor_service.py
```
Expected: line 499 shows `_call_fitted(client, prompt, 7000)` without `schema=`.

- [ ] **Step 2: Confirm the schema variable is in scope**

```bash
grep -n "schema = mentor_response_schema" python/mentor_service.py
```
Expected: line 493 sets `schema = mentor_response_schema("review", known, ...)` before line 499.

- [ ] **Step 3: Apply the one-line fix**

In `python/mentor_service.py`, find line 499:
```python
    data = parse_json(_call_fitted(client, prompt, 7000))
```
Replace with:
```python
    data = parse_json(_call_fitted(client, prompt, 7000, schema=schema))
```

- [ ] **Step 4: Run the test that covers review_draft**

```bash
python3 -m unittest tests.test_mentor_pipeline -v 2>&1 | head -40
```
Expected: all tests pass. If `test_mentor_pipeline` doesn't exist, run the full suite:
```bash
python3 -m unittest discover -s tests -v 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add python/mentor_service.py
git commit -m "$(cat <<'EOF'
Fix review_draft to pass schema to _call_fitted

The final-review conversation was not using native structured output
even when the client supports it, because schema= was omitted from the
_call_fitted call. This could produce less reliable JSON for the most
trust-sensitive workflow step.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Bug Fix: draft-gen preflight race

**Files:**
- Modify: `python/mentor_service.py:418-434`

**Interfaces:**
- Produces: `generate_draft()` preflights with an estimated max-size assessment_context when `assess=True`, so model-too-small errors surface before any paid baseline assessment calls.

- [ ] **Step 1: Read the current preflight region**

```bash
sed -n '418,434p' python/mentor_service.py
```
Expected output:
```python
    # Prove the final generation's essential directions/brief fit before a
    # baseline assessment can make paid calls. Priorities remain optional.
    _fit_prompt(workspace, client, build, 7000, items=originals, schema=schema)
    if assess:
        from mentor_assessment import assess_design
        if originals:
            baseline = assess_design(workspace, originals, client, scope="baseline", progress=progress)
            assessment_context = "\n\nBASELINE ASSESSMENT PRIORITIES:\n" + json.dumps([
                {"item_id": item["item_id"], "concerns": item["concerns"],
                 "recommendations": item["recommendations"]}
                for item in baseline["items"]
            ], ensure_ascii=False)
```

- [ ] **Step 2: Apply the fix**

In `python/mentor_service.py`, find the preflight block (the comment + single `_fit_prompt` call just before the `if assess:` block, around line 418–420). Replace these lines:

```python
    # Prove the final generation's essential directions/brief fit before a
    # baseline assessment can make paid calls. Priorities remain optional.
    _fit_prompt(workspace, client, build, 7000, items=originals, schema=schema)
```

With:

```python
    # Preflight with an estimated max-size assessment_context so that a model
    # too small to fit the prompt after baseline is caught before any paid calls.
    # The estimate uses 3 short concerns/recommendations per item; the real
    # context is structurally identical but text may vary in length.
    if assess and originals:
        _est_assessment_context = "\n\nBASELINE ASSESSMENT PRIORITIES:\n" + json.dumps(
            [{"item_id": it["id"], "concerns": ["c" * 80] * 3, "recommendations": ["r" * 80] * 3}
             for it in originals], ensure_ascii=False)
        assessment_context = _est_assessment_context
        try:
            _fit_prompt(workspace, client, build, 7000, items=originals, schema=schema)
        finally:
            assessment_context = ""
    else:
        _fit_prompt(workspace, client, build, 7000, items=originals, schema=schema)
```

- [ ] **Step 3: Run tests**

```bash
python3 -m unittest discover -s tests -v 2>&1 | tail -20
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add python/mentor_service.py
git commit -m "$(cat <<'EOF'
Fix draft-gen preflight to account for assessment context size

The initial preflight ran with an empty assessment_context, so a model
that fits without baseline priorities could still fail after they are
added. Now preflight uses a worst-case size estimate so model-too-small
errors surface before any paid baseline assessment calls.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — Bug Fix: parallel item assessment

**Files:**
- Modify: `python/mentor_assessment.py:553-671` (the `assess_design` function)

**Interfaces:**
- Produces: `assess_design()` runs item-level LLM calls in parallel using `ThreadPoolExecutor`. Items with multiple chunks remain sequential within the item. Progress callbacks are thread-safe. The returned dict is identical in structure; only wall-clock time improves.

- [ ] **Step 1: Add the import at the top of mentor_assessment.py**

Find the existing imports block at the top of `python/mentor_assessment.py`. After the `from copy import deepcopy` line, add:

```python
import concurrent.futures
import threading
```

- [ ] **Step 2: Replace the serial item loop in assess_design**

In `python/mentor_assessment.py`, find the serial loop that begins at approximately line 591:
```python
    report_items = []
    for position, (item, chunks) in enumerate(zip(prepared, requests), 1):
        if progress:
            progress("assessing_" + scope, ...
```

Replace the entire block from `report_items = []` down to (and including) `report_items.append(assessed)` with:

```python
    report_items_by_position = {}
    progress_lock = threading.Lock()
    completed_count = [0]

    def _assess_item(args):
        position, item, chunks = args
        judgments = []
        for index, chunk in enumerate(chunks, 1):
            if progress and len(chunks) > 1:
                with progress_lock:
                    progress("assessing_" + scope,
                             f"AI-assessing {scope} item {position} of {len(prepared)}, "
                             f"chunk {index} of {len(chunks)}; {chunks_total} total planned chunk requests.")
            try:
                _preflight(client, chunk["prompt"])
                structured = getattr(client, "call_structured", None)
                response = (structured(SYSTEM, chunk["prompt"], response_schema(), max_tokens=MAX_TOKENS)
                            if callable(structured)
                            else client.call(SYSTEM, chunk["prompt"], max_tokens=MAX_TOKENS))
            except Exception as exc:
                from ai_client import AIResponseError
                if isinstance(exc, AIResponseError):
                    raise AssessmentError(
                        f"AI assessment stopped at {scope} item {position} of {len(prepared)}. {exc}"
                    ) from None
                raise AssessmentError(
                    f"AI assessment could not complete item {position} of {len(prepared)}. "
                    "Check the selected provider key, model, quota and connection, then retry. "
                    "No complete assessment or default scores were saved."
                ) from None
            try:
                judgment = _validate(_parse(response), item, chunk["sources"])
            except AssessmentError as exc:
                raise AssessmentError(
                    f"AI assessment stopped at {scope} item {position} of {len(prepared)}, "
                    f"chunk {index} of {len(chunks)}. {exc}"
                ) from None
            if len(chunks) > 1:
                text = item["text"][chunk["start"]:chunk["end"]]
                judgment.update(
                    chunk_id=f"{item['id']}:chunk:{index}", index=index,
                    start=chunk["start"], end=chunk["end"],
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            judgments.append(judgment)
        assessed = _combine_chunks(judgments) if len(chunks) > 1 else judgments[0]
        if item["truncated"]:
            assessed["limitations"].append(
                f"Only the first {MAX_ITEM_EXCERPT} visible-text characters of this item were supplied; omitted content was not assessed.")
        if not item["text"]:
            assessed["limitations"].append("This item has no visible instructional text; media were not semantically assessed.")
        with progress_lock:
            completed_count[0] += 1
            if progress:
                progress("assessing_" + scope,
                         f"AI-assessing {scope}: {completed_count[0]} of {len(prepared)} items complete.")
        return position, assessed

    max_workers = min(4, len(prepared)) if prepared else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        args_list = [(pos, item, chunks) for pos, (item, chunks) in enumerate(zip(prepared, requests), 1)]
        futures = {executor.submit(_assess_item, args): args[0] for args in args_list}
        for future in concurrent.futures.as_completed(futures):
            position, assessed = future.result()  # raises AssessmentError on failure
            report_items_by_position[position] = assessed

    report_items = [report_items_by_position[pos] for pos in range(1, len(prepared) + 1)]
```

- [ ] **Step 3: Run the assessment tests**

```bash
python3 -m unittest tests.test_mentor_assessment -v 2>&1 | head -60
```
If that module doesn't exist:
```bash
python3 -m unittest discover -s tests -v 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add python/mentor_assessment.py
git commit -m "$(cat <<'EOF'
Parallelize item-level assessment calls

assess_design() now runs each item's LLM calls concurrently (up to 4
workers) using ThreadPoolExecutor. Chunks within one item remain
sequential. Progress callbacks are thread-safe. Result order matches
item order in the input list.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4 — Nav simplification

**Files:**
- Modify: `python/app/templates/base.html`

**Interfaces:**
- Produces: In local mode, the nav shows only "Course Mentor" (links to `/mentor`). The legacy pipeline links (Requirements / Configuration / Run / History / Import) are removed from the visible nav; they still exist as routes.

- [ ] **Step 1: Read the current nav**

```bash
grep -n "nav-link\|main-nav\|not is_hosted" python/app/templates/base.html
```

- [ ] **Step 2: Simplify the header in base.html**

Find the `<header class="app-header">` block in `python/app/templates/base.html`. Replace the entire header with:

```html
<header class="app-header">
  <div class="header-inner">
    <div class="brand">
      <span class="brand-icon">🎓</span>
      <div>
        <div class="brand-name">Course Mentor</div>
        <div class="brand-sub">AI-Assisted HAX Course Builder</div>
      </div>
    </div>
    <nav class="main-nav">
      <a href="{{ url_for('mentor.page') }}" class="nav-link {% if request.blueprint == 'mentor' %}active{% endif %}">My Courses</a>
      {% if not is_hosted %}
      <details class="nav-advanced">
        <summary class="nav-link">Advanced ▾</summary>
        <div class="nav-dropdown">
          <a href="{{ url_for('index') }}"  class="nav-link {% if request.endpoint == 'index' %}active{% endif %}">Requirements</a>
          <a href="{{ url_for('setup') }}"  class="nav-link {% if request.endpoint == 'setup' %}active{% endif %}">Configuration</a>
          <a href="{{ url_for('run_page') }}" class="nav-link {% if request.endpoint in ('run_page','run_start','stream') %}active{% endif %}">Legacy Run</a>
          <a href="{{ url_for('history') }}" class="nav-link {% if request.endpoint == 'history' %}active{% endif %}">Legacy History</a>
          <a href="{{ url_for('import_page') }}" class="nav-link {% if request.endpoint == 'import_page' %}active{% endif %}">Legacy Import</a>
        </div>
      </details>
      {% endif %}
    </nav>
  </div>
</header>
```

- [ ] **Step 3: Add nav-advanced CSS to style.css**

At the end of `python/app/static/style.css`, append:

```css
/* ── Nav advanced dropdown ───────────────────────────────────────────────── */
.nav-advanced { position: relative; list-style: none; }
.nav-advanced > summary { cursor: pointer; list-style: none; }
.nav-advanced > summary::-webkit-details-marker { display: none; }
.nav-dropdown {
  position: absolute; right: 0; top: 110%;
  background: #1565c0; border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,.25);
  padding: 6px; min-width: 160px; z-index: 200;
  display: flex; flex-direction: column; gap: 2px;
}
.nav-dropdown .nav-link { white-space: nowrap; border-radius: 6px; }
```

- [ ] **Step 4: Verify the page loads**

```bash
python3 python/app/app.py &
sleep 2
curl -s http://127.0.0.1:5050/mentor | grep -c "My Courses"
kill %1
```
Expected: `1` (the nav link appears).

- [ ] **Step 5: Commit**

```bash
git add python/app/templates/base.html python/app/static/style.css
git commit -m "$(cat <<'EOF'
Simplify nav: hide legacy pipeline links behind Advanced dropdown

Non-technical users running locally see only 'My Courses'. The legacy
Requirements/Configuration/Run/History/Import links move under an
Advanced collapsible for developers and power users.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5 — CSS wizard styles

**Files:**
- Modify: `python/app/static/style.css`

**Interfaces:**
- Produces: `.step-nav` progress bar, `.wizard-step` sections, `.provider-card` grid for AI setup, `.chat-bubble` message styling, `.step-nav-chip` states (past/active/future).

- [ ] **Step 1: Append wizard CSS to style.css**

At the end of `python/app/static/style.css` (after the nav-advanced block from Task 4), append the following:

```css
/* ── Wizard step nav ─────────────────────────────────────────────────────── */
.step-nav {
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  padding: 0 24px;
  position: sticky;
  top: 64px;
  z-index: 90;
}
.step-nav-inner {
  max-width: 860px;
  margin: 0 auto;
  display: flex;
  gap: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.step-nav-inner::-webkit-scrollbar { display: none; }
.step-chip {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 10px 14px 8px;
  border: none;
  background: transparent;
  cursor: default;
  font-family: inherit;
  font-size: .78rem;
  color: #aaa;
  border-bottom: 3px solid transparent;
  white-space: nowrap;
  transition: color .15s, border-color .15s;
  flex: 1;
  min-width: 80px;
}
.step-chip[data-state="past"] {
  color: #2e7d32;
  cursor: pointer;
}
.step-chip[data-state="past"]:hover { color: #1b5e20; background: #f5fff5; }
.step-chip[data-state="active"] {
  color: #1565c0;
  border-bottom-color: #1565c0;
  font-weight: 700;
  cursor: default;
}
.step-chip[data-state="future"] { color: #ccc; cursor: default; }
.step-chip-num {
  width: 22px; height: 22px;
  border-radius: 50%;
  background: currentColor;
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: .75rem; font-weight: 800;
  flex-shrink: 0;
}
.step-chip[data-state="past"] .step-chip-num { background: #2e7d32; }
.step-chip[data-state="past"] .step-chip-num::after { content: "✓"; color: #fff; font-size: .7rem; }
.step-chip[data-state="past"] .step-chip-num-text { display: none; }
.step-chip[data-state="active"] .step-chip-num { background: #1565c0; }
.step-chip[data-state="future"] .step-chip-num { background: #ddd; }
.step-chip-label { font-size: .72rem; }
@media (max-width: 600px) {
  .step-chip { min-width: 56px; padding: 8px 8px 6px; }
  .step-chip-label { display: none; }
}

/* ── Wizard step sections ────────────────────────────────────────────────── */
.wizard-step { display: none; }
.wizard-step[data-active] { display: block; }
.wizard-step-header {
  margin-bottom: 28px;
}
.wizard-step-header h1 { font-size: 1.7rem; font-weight: 700; color: #1a237e; margin-bottom: 6px; }
.wizard-step-header p { color: #666; max-width: 600px; }
.wizard-nav-footer {
  display: flex;
  gap: 12px;
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid #f0f0f0;
  align-items: center;
}
.wizard-spacer { flex: 1; }

/* ── Provider cards (Step 2) ─────────────────────────────────────────────── */
.provider-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.provider-card {
  border: 2px solid #e0e6f0;
  border-radius: 12px;
  padding: 16px 12px;
  text-align: center;
  cursor: pointer;
  background: #fff;
  transition: all .15s;
  font-family: inherit;
}
.provider-card:hover { border-color: #1565c0; background: #f5f9ff; }
.provider-card[aria-pressed="true"] { border-color: #1565c0; background: #eef4ff; }
.provider-card-icon { font-size: 1.8rem; margin-bottom: 6px; }
.provider-card-name { font-weight: 700; font-size: .9rem; color: #1a1a2e; }
.provider-card-desc { font-size: .75rem; color: #888; margin-top: 3px; }

/* ── Chat bubbles (Step 4) ───────────────────────────────────────────────── */
.chat-log { display: flex; flex-direction: column; gap: 12px; margin: 16px 0; max-height: 380px; overflow-y: auto; }
.chat-bubble { padding: 12px 16px; border-radius: 12px; max-width: 88%; font-size: .92rem; line-height: 1.55; white-space: pre-wrap; }
.chat-bubble-user { background: #1565c0; color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
.chat-bubble-mentor { background: #f0f4fa; color: #1a1a2e; align-self: flex-start; border-bottom-left-radius: 4px; }
.chat-bubble-label { font-size: .72rem; font-weight: 700; opacity: .7; margin-bottom: 4px; }

/* ── Step 3 content cards ────────────────────────────────────────────────── */
.source-choice { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 16px 0; }
@media (max-width: 600px) { .source-choice { grid-template-columns: 1fr; } }
.source-card {
  border: 2px solid #e0e6f0; border-radius: 12px; padding: 20px;
  background: #fff; cursor: pointer; transition: all .15s; text-align: left;
}
.source-card:hover { border-color: #1565c0; background: #f5f9ff; }
.source-card[aria-selected="true"] { border-color: #1565c0; background: #eef4ff; }
.source-card-icon { font-size: 1.6rem; margin-bottom: 8px; }
.source-card-title { font-weight: 700; font-size: 1rem; color: #1a1a2e; margin-bottom: 4px; }
.source-card-desc { font-size: .82rem; color: #777; }
```

- [ ] **Step 2: Verify CSS parses (no syntax errors)**

```bash
python3 -c "
import re, sys
css = open('python/app/static/style.css').read()
# Rough check: balanced braces
opens = css.count('{')
closes = css.count('}')
print(f'Braces: {opens} open, {closes} close')
assert opens == closes, 'Brace mismatch!'
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add python/app/static/style.css
git commit -m "$(cat <<'EOF'
Add wizard CSS: step-nav, provider cards, chat bubbles, source cards

Adds all new visual components for the stepper wizard redesign.
No existing styles are changed; new rules are appended.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6 — HTML wizard template

**Files:**
- Modify: `python/app/templates/mentor.html` (full rewrite)

**Interfaces:**
- Consumes: All existing element IDs used by mentor.js must remain present in the output; they are referenced by the JS directly (e.g. `el('mentor-error')`, `el('workspace-choice')`, `el('provider-settings')`, etc.)
- Produces: Six `<section class="wizard-step" data-step="N">` sections, a `<nav class="step-nav" id="step-nav">` progress bar, and a hidden compatibility block preserving all IDs that the existing JS expects but that are not visible in any step.

**Critical:** Every existing element ID in the table below must appear in the new HTML exactly once. Do not rename or remove any ID. The wizard only changes *which section* contains them and *how they are styled/grouped*.

IDs that must be preserved (from mentor.js reads): `mentor-error`, `mentor-status`, `mentor-guide` (keep but hide — replaced by step-nav), `guide-workspace`, `next-step-number`, `next-step-title`, `next-step-description`, `next-step-button`, `flow-step-1` through `flow-step-6`, `mentor-mode`, `mentor-login-card`, `mentor-invite-code`, `local-auth-setup`, `local-auth-url`, `account-login-card`, `account-email`, `account-send-code`, `account-code-status`, `account-verify-form`, `account-code`, `claim-browser-work`, `account-verify-code`, `account-verify-status`, `account-profile`, `account-label`, `workspace-choice`, `workspace-list`, `new-workspace`, `new-title`, `provider-settings`, `mentor-provider`, `ai-setup-state`, `assessment-cost-help`, `provider-privacy-help`, `credential-form`, `mentor-key`, `gateway-settings`, `mentor-gateway`, `remember-key`, `save-provider-key`, `forget-provider-key`, `model-settings`, `mentor-model`, `refresh-models`, `manual-model-label`, `manual-model-enabled`, `manual-model`, `save-model`, `model-status`, `model-capabilities`, `credential-status`, `workspace-content`, `local-job-error`, `course-sources`, `workspace-title`, `workspace-stage`, `source-readiness`, `start-blank-course`, `source-form`, `source-file`, `upload-course-button`, `local-upload-help`, `local-source-choice`, `local-source-label`, `import-local-source`, `hosted-upload-help`, `upload-feedback`, `upload-feedback-title`, `upload-feedback-file`, `upload-feedback-status`, `upload-step-1`, `upload-step-2`, `upload-step-3`, `upload-progress-label`, `hosted-upload-progress`, `upload-feedback-bytes`, `upload-feedback-elapsed`, `upload-feedback-help`, `abort-upload`, `check-upload-progress`, `upload-recovery`, `upload-state`, `compact-package-help`, `supplemental-sources`, `material-role`, `material-file-form`, `material-file`, `upload-material-button`, `material-url-form`, `material-url`, `material-text-form`, `material-text`, `material-list`, `course-inventory`, `discussion-readiness`, `mentor-messages`, `mentor-chat-form`, `chat-kind` (keep as hidden select — JS still reads it), `mentor-message`, `send-mentor-message`, `design-brief`, `approve-direction`, `analyze-course`, `analysis-feedback`, `analysis-feedback-title`, `analysis-feedback-message`, `analysis-feedback-progress`, `analysis-feedback-elapsed`, `analysis-feedback-help`, `analysis-feedback-cancel`, `draft-panel`, `draft-summary`, `draft-coverage`, `draft-warnings`, `assessment-panel`, `assessment-title`, `assessment-readiness`, `baseline-assessment`, `baseline-assessment-content`, `proposed-assessment`, `proposed-assessment-content`, `draft-items`, `save-draft`, `approve-build`, `build-course`, `completion-title`, `completion-help`, `hax-site-panel`, `hax-site-title`, `hax-site-status`, `hax-site-error`, `hax-site-directory`, `open-hax-site`, `start-hax-site`, `stop-hax-site`, `refresh-hax-site`, `artifact-links`, `processing-details`, `workspace-revisions`, `mentor-jobs`, `cancel-mentor-job`.

- [ ] **Step 1: Write the new mentor.html**

Write `python/app/templates/mentor.html` with this content:

```html
{% extends "base.html" %}
{% block title %}Course Mentor{% endblock %}
{% block content %}

<!-- Global status / error (always visible) -->
<div id="mentor-error" role="alert" class="banner banner-warn" style="max-width:860px;margin:16px auto 0" hidden></div>
<p id="mentor-status" role="status" aria-live="polite" style="max-width:860px;margin:8px auto 0;padding:0 24px;font-size:.9rem;color:#555"></p>

<!-- Wizard progress bar -->
<nav class="step-nav" id="step-nav" aria-label="Course preparation steps" hidden>
  <div class="step-nav-inner">
    <button class="step-chip" data-step="1" data-state="active" onclick="chipClick(1)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">1</span></span>
      <span class="step-chip-label">Your course</span>
    </button>
    <button class="step-chip" data-step="2" data-state="future" onclick="chipClick(2)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">2</span></span>
      <span class="step-chip-label">AI helper</span>
    </button>
    <button class="step-chip" data-step="3" data-state="future" onclick="chipClick(3)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">3</span></span>
      <span class="step-chip-label">Add content</span>
    </button>
    <button class="step-chip" data-step="4" data-state="future" onclick="chipClick(4)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">4</span></span>
      <span class="step-chip-label">Discuss</span>
    </button>
    <button class="step-chip" data-step="5" data-state="future" onclick="chipClick(5)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">5</span></span>
      <span class="step-chip-label">Review draft</span>
    </button>
    <button class="step-chip" data-step="6" id="flow-step-6-chip" data-state="future" onclick="chipClick(6)" type="button">
      <span class="step-chip-num"><span class="step-chip-num-text">6</span></span>
      <span class="step-chip-label" id="step-6-chip-label">Build site</span>
    </button>
  </div>
</nav>

<!-- Hidden legacy guide card (JS reads/updates these IDs; kept for compatibility) -->
<div id="mentor-guide" hidden aria-hidden="true">
  <p id="guide-workspace"></p>
  <ol aria-hidden="true">
    <li id="flow-step-1"></li><li id="flow-step-2"></li><li id="flow-step-3"></li>
    <li id="flow-step-4"></li><li id="flow-step-5"></li><li id="flow-step-6"></li>
  </ol>
  <p id="next-step-number"></p>
  <h2 id="next-step-title"></h2>
  <p id="next-step-description"></p>
  <button id="next-step-button" type="button" hidden></button>
  <p id="mentor-mode"></p>
</div>

<div class="page-wrap">

<!-- ── Step 1: Choose or start a course ─────────────────────────────────── -->
<section class="wizard-step" data-step="1" id="step-1">
  <div class="wizard-step-header">
    <h1>Choose or start a course</h1>
    <p>Each workspace holds one course and its review history. You can have multiple courses.</p>
  </div>

  <!-- Hosted invite / account login -->
  <div class="card" id="mentor-login-card" hidden>
    <h2>Welcome — enter your invitation code</h2>
    <p>New users enter the administrator's invitation code, then verify their email below.</p>
    <form id="mentor-login-form">
      <label class="field-label" for="mentor-invite-code">Invitation code</label>
      <input class="field-input" id="mentor-invite-code" type="password" autocomplete="off" required>
      <button class="btn btn-primary" type="submit">Enter Course Mentor</button>
    </form>
  </div>

  <div class="card">
    <details id="local-auth-setup" hidden>
      <summary>Optional: email sign-in for this computer</summary>
      <p class="field-help">Enter your administrator's Neon Auth Base URL to link workspaces to a verified email account. Neon stores identity only — courses, conversations, keys and sites stay on this computer.</p>
      <form id="local-auth-form">
        <label class="field-label" for="local-auth-url">Neon Auth Base URL</label>
        <input class="field-input" id="local-auth-url" type="url" required>
        <button class="btn btn-outline" type="submit">Configure email sign-in</button>
      </form>
    </details>

    <div id="account-login-card" hidden>
      <h2>Sign in to your account</h2>
      <p id="account-login-help" class="field-help">Use an emailed sign-in code to resume your courses.</p>
      <form id="account-code-form">
        <label class="field-label" for="account-email">Email address</label>
        <input class="field-input" id="account-email" type="email" autocomplete="email" maxlength="320" required>
        <button class="btn btn-primary" id="account-send-code" type="submit">Email me a sign-in code</button>
        <p id="account-code-status" class="banner banner-info" role="status" aria-live="polite" hidden></p>
      </form>
      <form id="account-verify-form" hidden>
        <label class="field-label" for="account-code">Six-digit code from your email</label>
        <input class="field-input" id="account-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required>
        <label class="step-check-item"><input id="claim-browser-work" type="checkbox"> Claim this browser's old work (only if it is mine)</label>
        <button class="btn btn-primary" id="account-verify-code" type="submit">Sign in</button>
        <p id="account-verify-status" class="banner banner-info" role="status" aria-live="polite" hidden></p>
      </form>
    </div>

    <div id="account-profile" hidden>
      <p id="account-label"></p>
      <button class="btn btn-ghost" type="button" onclick="signOutAccount()">Sign out</button>
    </div>

    <div id="workspace-choice" hidden>
      <h2>Resume an existing course</h2>
      <p id="workspace-storage-help" class="field-help">{% if is_hosted %}Saved in your private account.{% else %}Saved on this computer.{% endif %}</p>
      <select id="workspace-list" class="field-input" onchange="openWorkspace(this.value)">
        <option value="">— Choose a course —</option>
      </select>

      <hr style="margin:20px 0;border:none;border-top:1px solid #f0f0f0">
      <h2>Or start a new course</h2>
      <form id="new-workspace">
        <label class="field-label" for="new-title">Course name</label>
        <input class="field-input" id="new-title" maxlength="240" required placeholder="e.g. Introduction to Psychology">
        <button class="btn btn-primary" type="submit">Create course workspace</button>
      </form>
    </div>
  </div>
</section>

<!-- ── Step 2: Set up AI helper ──────────────────────────────────────────── -->
<section class="wizard-step" data-step="2" id="step-2">
  <div class="wizard-step-header">
    <h1>Set up your AI assistant</h1>
    <p>Choose an AI service and enter your API key. Or choose "No AI" to review your course manually.</p>
  </div>

  <section class="card" id="provider-settings" tabindex="-1">
    <p id="ai-setup-state" class="banner banner-info" role="status">Choose how you want to work.</p>
    <p id="assessment-cost-help" class="field-help" hidden>AI analysis and draft generation make several provider requests and may incur charges from your selected provider.</p>
    <p id="provider-privacy-help" class="field-help">Course text is sent to the AI service you choose. Keys are encrypted on this computer and never stored in your browser or chat.</p>

    <label class="field-label">Choose an AI service</label>
    <!-- Provider cards (visual) drive the hidden select below -->
    <div class="provider-cards" id="provider-cards-ui">
      <button type="button" class="provider-card" data-provider="anthropic" onclick="selectProviderCard('anthropic')">
        <div class="provider-card-icon">🤖</div>
        <div class="provider-card-name">Anthropic</div>
        <div class="provider-card-desc">Claude models</div>
      </button>
      <button type="button" class="provider-card" data-provider="openai" onclick="selectProviderCard('openai')">
        <div class="provider-card-icon">✨</div>
        <div class="provider-card-name">OpenAI</div>
        <div class="provider-card-desc">GPT models</div>
      </button>
      <button type="button" class="provider-card" data-provider="gemini" onclick="selectProviderCard('gemini')">
        <div class="provider-card-icon">💎</div>
        <div class="provider-card-name">Google Gemini</div>
        <div class="provider-card-desc">Gemini models</div>
      </button>
      <button type="button" class="provider-card" data-provider="nebula" onclick="selectProviderCard('nebula')">
        <div class="provider-card-icon">🏛️</div>
        <div class="provider-card-name">NebulaONE</div>
        <div class="provider-card-desc">Institutional gateway</div>
      </button>
      <button type="button" class="provider-card" data-provider="guided" onclick="selectProviderCard('guided')">
        <div class="provider-card-icon">📋</div>
        <div class="provider-card-name">No AI</div>
        <div class="provider-card-desc">Manual walkthrough</div>
      </button>
    </div>
    <!-- Hidden select kept for JS compatibility -->
    <select id="mentor-provider" class="field-input" style="display:none" aria-hidden="true">
      <option value="" selected disabled></option>
      <option value="nebula">NebulaONE</option>
      <option value="anthropic">Anthropic</option>
      <option value="openai">OpenAI</option>
      <option value="gemini">Gemini</option>
      <option value="guided">Guided</option>
    </select>

    <form id="credential-form" hidden>
      <label class="field-label" for="mentor-key">API key</label>
      <input class="field-input" id="mentor-key" type="password" autocomplete="off" placeholder="Paste your API key here">
      <div id="gateway-settings" hidden>
        <label class="field-label" for="mentor-gateway">Institutional gateway URL</label>
        <input class="field-input" id="mentor-gateway" type="url" placeholder="https://your-gateway.azure-api.net/anthropic">
      </div>
      <label class="step-check-item"><input type="checkbox" id="remember-key"> Remember for 30 days</label>
      <button class="btn btn-primary" id="save-provider-key" type="submit">Save key &amp; load models</button>
      <button class="btn btn-ghost" id="forget-provider-key" type="button" onclick="forgetKey()">Remove saved key</button>
    </form>

    <div id="model-settings" hidden>
      <label class="field-label" for="mentor-model">Model</label>
      <select class="field-input" id="mentor-model"></select>
      <button class="btn btn-ghost btn-sm" id="refresh-models" type="button" onclick="refreshModels()">Refresh model list</button>
      <label class="step-check-item" id="manual-model-label" hidden>
        <input id="manual-model-enabled" type="checkbox"> Enter a gateway deployment/model ID manually
      </label>
      <input class="field-input" id="manual-model" maxlength="120" aria-label="Institutional model ID" hidden>
      <button class="btn btn-primary" id="save-model" type="button" onclick="saveModel()">Use this model</button>
      <p id="model-status" role="status" class="field-help"></p>
      <section id="model-capabilities" class="mentor-capabilities field-help" aria-label="Model details" aria-live="polite" hidden></section>
    </div>
    <p id="credential-status" class="field-help"></p>
  </section>

  <div class="wizard-nav-footer">
    <button class="btn btn-ghost" type="button" onclick="wizardBack()">← Back</button>
    <span class="wizard-spacer"></span>
    <button class="btn btn-primary btn-lg" type="button" id="step2-continue" onclick="wizardNext()">Continue →</button>
  </div>
</section>

<!-- ── Step 3: Add course content ────────────────────────────────────────── -->
<section class="wizard-step" data-step="3" id="step-3">
  <div id="workspace-content" hidden>
    <p id="local-job-error" class="banner banner-error" role="alert" aria-live="polite" hidden></p>
    <section class="card" id="course-sources" tabindex="-1">
      <div class="wizard-step-header">
        <h1 id="workspace-title"></h1>
        <p id="workspace-stage" class="field-help"></p>
      </div>

      <p id="source-readiness" class="banner banner-info" role="status"></p>

      <div class="source-choice">
        <div class="source-card" id="source-card-file" onclick="focusSourceFile()">
          <div class="source-card-icon">📦</div>
          <div class="source-card-title">Import a Canvas course</div>
          <div class="source-card-desc">Upload your .imscc export file from Canvas</div>
        </div>
        <div class="source-card" id="source-card-blank" onclick="startBlankCourse()">
          <div class="source-card-icon">✏️</div>
          <div class="source-card-title">Design a new course</div>
          <div class="source-card-desc">Start from scratch without a file</div>
        </div>
      </div>

      <form id="source-form" style="margin-top:8px">
        <label class="field-label" for="source-file">Canvas export file (.imscc or .haxcourse)</label>
        <input class="field-input" id="source-file" type="file" accept=".imscc,.haxcourse">
        <button class="btn btn-primary" id="upload-course-button" type="submit">Import course file</button>
      </form>

      <p id="local-upload-help" class="field-help" hidden>Files are copied to this computer in chunks. Your original is unchanged. Supported media is retained for the HAX site.</p>
      <p id="hosted-upload-help" class="field-help" hidden>Large exports upload directly to private storage. Your original is unchanged.</p>

      <div id="local-source-choice" hidden>
        <p id="local-source-label"></p>
        <button class="btn btn-outline" id="import-local-source" type="button" onclick="importLocalSource()">Use this startup file</button>
      </div>

      <section id="upload-feedback" class="mentor-upload" tabindex="-1" aria-labelledby="upload-feedback-title" hidden>
        <h4 id="upload-feedback-title"></h4>
        <p id="upload-feedback-file"></p>
        <ol class="mentor-upload-steps" aria-label="Import stages">
          <li id="upload-step-1">1. Start</li>
          <li id="upload-step-2">2. Transfer</li>
          <li id="upload-step-3">3. Prepare</li>
        </ol>
        <p id="upload-feedback-status" role="status" aria-live="polite"></p>
        <label id="upload-progress-label" for="hosted-upload-progress" hidden>Upload progress</label>
        <progress id="hosted-upload-progress" max="100" hidden></progress>
        <p id="upload-feedback-bytes"></p>
        <p id="upload-feedback-elapsed" aria-live="off"></p>
        <p id="upload-feedback-help" class="field-help"></p>
        <button class="btn btn-ghost" id="abort-upload" type="button" onclick="abortHostedUpload()" hidden>Cancel</button>
        <button class="btn btn-outline btn-sm" id="check-upload-progress" type="button" onclick="checkSavedUploadProgress()">Check saved progress</button>
        <button class="btn btn-ghost btn-sm" id="upload-recovery" type="button" onclick="showUploadRecovery()" hidden>Recovery options</button>
      </section>
      <p id="upload-state" role="status" aria-live="polite" class="field-help"></p>

      <details>
        <summary id="compact-package-help">Large file? Prepare it locally first</summary>
        <p class="field-help">For exports over ~500 MB, run the preparation tool on your computer and upload the resulting .haxcourse file here. Your original is never modified.</p>
        <pre style="background:#f5f5f5;padding:12px;border-radius:6px;font-size:.82rem;overflow-x:auto">python3 python/prepare_course.py "/path/course.imscc" --output "/path/course.haxcourse"</pre>
      </details>

      <details {% if is_hosted %}hidden{% endif %} style="margin-top:12px">
        <summary>Connect via Canvas API instead</summary>
        <p class="field-help">Requires a Canvas URL and token configured on the legacy Configuration page.</p>
        <form id="mentor-canvas-form">
          <label class="field-label" for="mentor-canvas-id">Canvas course ID</label>
          <input class="field-input" id="mentor-canvas-id" inputmode="numeric" pattern="[0-9]+" required>
          <button class="btn btn-primary" type="submit">Acquire course</button>
        </form>
      </details>

      <details id="supplemental-sources" style="margin-top:16px">
        <summary>Add supporting documents or notes (optional)</summary>
        <p class="field-help">Add syllabi, policies, rubrics or teaching notes. These inform the AI; they are not automatically added to student pages.</p>
        <label class="field-label" for="material-role">What kind of document is this?</label>
        <select id="material-role" class="field-input">
          <option value="reference">Background reference</option>
          <option value="policy">Instructor / institutional policy</option>
          <option value="student">Student-facing source</option>
        </select>
        <form id="material-file-form">
          <label class="field-label" for="material-file">Upload document (10 MB max)</label>
          <input class="field-input" id="material-file" type="file" accept=".txt,.md,.csv,.html,.pdf,.docx,.pptx">
          <button class="btn btn-primary" id="upload-material-button" type="submit">Add document</button>
        </form>
        <form id="material-url-form" style="margin-top:8px">
          <label class="field-label" for="material-url">Or paste a public URL</label>
          <input class="field-input" id="material-url" type="url" placeholder="https://example.edu/resource">
          <button class="btn btn-primary" type="submit">Add URL</button>
        </form>
        <form id="material-text-form" style="margin-top:8px">
          <label class="field-label" for="material-text">Or type notes directly</label>
          <textarea class="field-input" id="material-text" rows="3" maxlength="60000" placeholder="Paste text or type additional context here"></textarea>
          <button class="btn btn-primary" type="submit">Add notes</button>
        </form>
        <ul id="material-list" style="margin-top:12px"></ul>
      </details>

      <h3 style="margin-top:20px">What the mentor can see</h3>
      <div id="course-inventory"></div>
    </section>
  </div>

  <div class="wizard-nav-footer">
    <button class="btn btn-ghost" type="button" onclick="wizardBack()">← Back</button>
    <span class="wizard-spacer"></span>
    <button class="btn btn-primary btn-lg" type="button" id="step3-continue" onclick="wizardNext()">Continue →</button>
  </div>
</section>

<!-- ── Step 4: Discuss goals ─────────────────────────────────────────────── -->
<section class="wizard-step" data-step="4" id="step-4">
  <div class="wizard-step-header">
    <h1>Tell the mentor your goals</h1>
    <p>Describe who your students are, what you want them to learn, and what must stay unchanged. The mentor will ask focused questions.</p>
  </div>

  <div class="card">
    <p id="discussion-readiness" class="banner banner-info" role="status"></p>

    <!-- Chat log -->
    <div id="mentor-messages" class="chat-log" aria-live="polite"></div>

    <!-- Chat input — chat-kind select hidden but preserved for JS -->
    <form id="mentor-chat-form">
      <select id="chat-kind" aria-hidden="true" style="display:none">
        <option value="interview">interview</option>
        <option value="review">review</option>
      </select>
      <label class="field-label" for="mentor-message">Your message</label>
      <textarea class="field-input" id="mentor-message" rows="3" maxlength="6000" required
        placeholder="What should students be able to do? What must stay unchanged? What's not working?"></textarea>
      <button class="btn btn-primary" id="send-mentor-message" type="submit">Send to mentor</button>
    </form>
  </div>

  <div class="card" style="margin-top:0">
    <h2>Your design brief</h2>
    <p class="field-help">The mentor fills this in from your conversation. Review and edit it, then approve it when it reflects your intent.</p>
    <textarea class="field-input" id="design-brief" rows="7" maxlength="30000"
      placeholder="Your goals, constraints and AI-use expectations will appear here after the conversation."></textarea>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
      <button class="btn btn-ghost" onclick="saveBrief()">Save brief</button>
      <button class="btn btn-primary" id="approve-direction" onclick="approveDirection()">Approve this direction</button>
    </div>

    <section id="analysis-feedback" class="mentor-upload" tabindex="-1" aria-labelledby="analysis-feedback-title" hidden style="margin-top:16px">
      <h3 id="analysis-feedback-title"></h3>
      <p id="analysis-feedback-message" role="status" aria-live="polite"></p>
      <progress id="analysis-feedback-progress" aria-label="Processing"></progress>
      <p id="analysis-feedback-elapsed" aria-live="off"></p>
      <p id="analysis-feedback-help" class="field-help"></p>
      <button id="analysis-feedback-cancel" class="btn btn-ghost btn-sm" type="button" onclick="cancelMentorJob()" hidden>Cancel</button>
    </section>

    <div style="margin-top:16px">
      <button class="btn btn-success" id="analyze-course" onclick="analyzeCourse()" style="width:100%">Analyze course &amp; generate draft →</button>
      <p class="field-help" style="margin-top:8px">Approve your direction first. The AI will assess the course and propose a revised draft.</p>
    </div>
  </div>

  <div class="wizard-nav-footer">
    <button class="btn btn-ghost" type="button" onclick="wizardBack()">← Back</button>
  </div>
</section>

<!-- ── Step 5: Review the draft ──────────────────────────────────────────── -->
<section class="wizard-step" data-step="5" id="step-5">
  <div class="wizard-step-header">
    <h1>Review the proposed draft</h1>
    <p>Open each item to read the proposed changes. Use the chat to request revisions. Approve only when you're satisfied.</p>
  </div>

  <section class="card" id="draft-panel" tabindex="-1">
    <p id="draft-summary"></p>
    <p id="draft-coverage" class="field-help"></p>
    <ul id="draft-warnings" style="padding-left:20px;color:#666;font-size:.88rem"></ul>

    <section id="assessment-panel" class="mentor-assessment" aria-labelledby="assessment-title" hidden>
      <h3 id="assessment-title">Instructor assessment report</h3>
      <p class="field-help">This report supports your judgment; it is not a quality grade. Bloom's describes cognitive demand. AIAS describes AI use in task design.</p>
      <p id="assessment-readiness" class="banner banner-info" role="status" aria-live="polite"></p>
      <details id="baseline-assessment" hidden>
        <summary>Original course assessment (before changes)</summary>
        <div id="baseline-assessment-content"></div>
      </details>
      <details id="proposed-assessment" open hidden>
        <summary>Proposed draft assessment</summary>
        <div id="proposed-assessment-content"></div>
      </details>
    </section>

    <div id="draft-items"></div>

    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px">
      <button class="btn btn-ghost" id="save-draft" onclick="saveDraft()">Save edits</button>
      <button class="btn btn-primary" id="approve-build" onclick="approveBuild()">Approve this exact draft</button>
    </div>
    <p class="field-help" style="margin-top:8px">Use the chat below to request revisions. Editing or requesting changes requires a new approval.</p>
  </section>

  <!-- Final-review chat (reuses the same form/message elements from step 4, scrolled to) -->
  <div class="card" style="margin-top:0">
    <h2>Request revisions</h2>
    <p class="field-help">Tell the mentor what to change. Your approved direction and draft are kept separate until you approve again.</p>
    <div id="review-messages" aria-live="polite" class="chat-log"></div>
    <p class="field-help" style="margin-top:8px">Use the chat in step 4 to send a review message (select "Final draft review" conversation type there), or use the shortcut below.</p>
    <form id="review-chat-shortcut">
      <textarea class="field-input" id="review-shortcut-message" rows="3" maxlength="6000"
        placeholder="What would you like to change?"></textarea>
      <button class="btn btn-primary" type="submit">Request changes</button>
    </form>
  </div>

  <div class="wizard-nav-footer">
    <button class="btn btn-ghost" type="button" onclick="wizardBack()">← Back</button>
  </div>
</section>

<!-- ── Step 6: Build the HAX site ────────────────────────────────────────── -->
<section class="wizard-step" data-step="6" id="step-6">
  <div class="wizard-step-header">
    <h1 id="completion-title">Build your HAX site</h1>
    <p id="completion-help">Build the exact approved draft into a local HAX site. Nothing is published.</p>
  </div>

  <div class="card">
    <button class="btn btn-success btn-lg" id="build-course" onclick="buildCourse()" style="width:100%;font-size:1.05rem;padding:14px">
      Build &amp; open HAX site
    </button>
    <p class="field-help" style="margin-top:10px">Build is only available after you approve the exact draft above.</p>

    <section id="hax-site-panel" class="mentor-hax-site" tabindex="-1" aria-labelledby="hax-site-title" hidden style="margin-top:16px">
      <h4 id="hax-site-title">Your local HAX site</h4>
      <p id="hax-site-status" role="status" aria-live="polite"></p>
      <p id="hax-site-error" role="alert" class="banner banner-warn" hidden></p>
      <p class="field-help">Output folder: <code id="hax-site-directory"></code></p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        <a id="open-hax-site" class="btn btn-primary" target="_blank" rel="noopener noreferrer" hidden>Open HAX site</a>
        <button id="start-hax-site" class="btn btn-outline" type="button" onclick="startHaxSite()" hidden>Start site</button>
        <button id="stop-hax-site" class="btn btn-ghost" type="button" onclick="stopHaxSite()" hidden>Stop site</button>
        <button id="refresh-hax-site" class="btn btn-ghost btn-sm" type="button" onclick="refreshHaxSite()">Check status</button>
      </div>
    </section>

    <div id="artifact-links" style="margin-top:16px"></div>
  </div>

  <div class="wizard-nav-footer">
    <button class="btn btn-ghost" type="button" onclick="wizardBack()">← Back</button>
  </div>
</section>

<!-- Processing details (available from any step via the details element) -->
<details class="card" id="processing-details" style="margin-top:16px">
  <summary>Processing details &amp; job history</summary>
  <p id="workspace-revisions" class="field-help"></p>
  <pre id="mentor-jobs" style="white-space:pre-wrap;font-size:.82rem;color:#555;margin-top:8px"></pre>
  <button class="btn btn-ghost btn-sm" id="cancel-mentor-job" onclick="cancelMentorJob()" hidden>Cancel / recover interrupted job</button>
  <p class="field-help">Cancellation rejects late results but cannot undo a provider request already sent.</p>
</details>

</div><!-- /.page-wrap -->
{% endblock %}
{% block scripts %}
{% if is_hosted %}
<script src="{{ url_for('static', filename='hosted-upload.js') }}"></script>
{% endif %}
<script src="{{ url_for('static', filename='mentor.js') }}"></script>
{% endblock %}
```

- [ ] **Step 2: Verify all required IDs exist in the new template**

```bash
python3 -c "
ids_required = [
  'mentor-error','mentor-status','mentor-guide','guide-workspace','next-step-number',
  'next-step-title','next-step-description','next-step-button','flow-step-1','flow-step-2',
  'flow-step-3','flow-step-4','flow-step-5','flow-step-6','mentor-mode','mentor-login-card',
  'mentor-invite-code','local-auth-setup','local-auth-url','account-login-card','account-email',
  'account-send-code','account-code-status','account-verify-form','account-code',
  'claim-browser-work','account-verify-code','account-verify-status','account-profile',
  'account-label','workspace-choice','workspace-list','new-workspace','new-title',
  'provider-settings','mentor-provider','ai-setup-state','assessment-cost-help',
  'provider-privacy-help','credential-form','mentor-key','gateway-settings','mentor-gateway',
  'remember-key','save-provider-key','forget-provider-key','model-settings','mentor-model',
  'refresh-models','manual-model-label','manual-model-enabled','manual-model','save-model',
  'model-status','model-capabilities','credential-status','workspace-content','local-job-error',
  'course-sources','workspace-title','workspace-stage','source-readiness','start-blank-course',
  'source-form','source-file','upload-course-button','local-upload-help','local-source-choice',
  'local-source-label','import-local-source','hosted-upload-help','upload-feedback',
  'upload-feedback-title','upload-feedback-file','upload-feedback-status','upload-step-1',
  'upload-step-2','upload-step-3','upload-progress-label','hosted-upload-progress',
  'upload-feedback-bytes','upload-feedback-elapsed','upload-feedback-help','abort-upload',
  'check-upload-progress','upload-recovery','upload-state','compact-package-help',
  'supplemental-sources','material-role','material-file-form','material-file',
  'upload-material-button','material-url-form','material-url','material-text-form',
  'material-text','material-list','course-inventory','discussion-readiness','mentor-messages',
  'mentor-chat-form','chat-kind','mentor-message','send-mentor-message','design-brief',
  'approve-direction','analyze-course','analysis-feedback','analysis-feedback-title',
  'analysis-feedback-message','analysis-feedback-progress','analysis-feedback-elapsed',
  'analysis-feedback-help','analysis-feedback-cancel','draft-panel','draft-summary',
  'draft-coverage','draft-warnings','assessment-panel','assessment-title','assessment-readiness',
  'baseline-assessment','baseline-assessment-content','proposed-assessment',
  'proposed-assessment-content','draft-items','save-draft','approve-build','build-course',
  'completion-title','completion-help','hax-site-panel','hax-site-title','hax-site-status',
  'hax-site-error','hax-site-directory','open-hax-site','start-hax-site','stop-hax-site',
  'refresh-hax-site','artifact-links','processing-details','workspace-revisions','mentor-jobs',
  'cancel-mentor-job',
]
html = open('python/app/templates/mentor.html').read()
missing = [id for id in ids_required if f'id=\"{id}\"' not in html]
if missing:
    print('MISSING IDs:', missing)
else:
    print('All', len(ids_required), 'required IDs present.')
"
```
Expected: `All N required IDs present.`

- [ ] **Step 3: Commit**

```bash
git add python/app/templates/mentor.html
git commit -m "$(cat <<'EOF'
Rebuild mentor.html as 6-step wizard

Reorganizes all content into wizard-step sections with a step-nav
progress bar. All existing element IDs are preserved for JS
compatibility. Provider cards replace the hidden select. Chat bubbles
replace the raw message div. Language simplified throughout.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7 — JS wizard layer

**Files:**
- Modify: `python/app/static/mentor.js`

This task adds the wizard layer without removing any existing JS. Four changes are made:

1. Add `wizardStep` variable and `activateStep` / `chipClick` / `wizardNext` / `wizardBack` / `selectProviderCard` / `focusSourceFile` functions.
2. Add `renderWizard()` call at end of `renderFlow()`.
3. Fix the `renderWorkspace()` message rendering to use `.chat-bubble` classes.
4. Wire up the review chat shortcut form (`review-chat-shortcut`).

- [ ] **Step 1: Read the bottom of mentor.js to find where to append**

```bash
wc -l python/app/static/mentor.js
tail -20 python/app/static/mentor.js
```
Note the last line number (approximately 1700+).

- [ ] **Step 2: Update renderWorkspace() to use chat bubbles**

Find this block in `renderWorkspace()` (around line 1381):
```javascript
    workspace.messages.forEach(message => {
      const box = document.createElement('div');
      box.style.cssText = 'padding:0.8rem;margin:0.5rem 0;border-left:3px solid #1565c0;background:#f5f7fa;white-space:pre-wrap';
      itemText(box, 'strong', `${message.role} · ${message.kind}`);
      itemText(box, 'p', message.content);
      messageBox.appendChild(box);
    });
```

Replace with:
```javascript
    workspace.messages.forEach(message => {
      const box = document.createElement('div');
      const isUser = message.role === 'user';
      box.className = 'chat-bubble ' + (isUser ? 'chat-bubble-user' : 'chat-bubble-mentor');
      const label = document.createElement('div');
      label.className = 'chat-bubble-label';
      label.textContent = isUser ? 'You' : 'Mentor';
      box.appendChild(label);
      const text = document.createElement('div');
      text.textContent = message.content;
      box.appendChild(text);
      messageBox.appendChild(box);
    });
```

- [ ] **Step 3: Add renderWizard call at the end of renderFlow()**

Find the last line of the `renderFlow()` function. It ends with the closing `}` after line ~710. Just before that closing `}`, add:
```javascript
  renderWizard();
```

So `renderFlow()` now ends:
```javascript
  // ... existing code ...
  renderWizard();
}
```

- [ ] **Step 4: Wire up review-chat-shortcut form**

After the existing `el('material-text-form').onsubmit` handler (around line 1665), add:

```javascript
el('review-chat-shortcut').onsubmit = event => {
  event.preventDefault(); action(async () => {
    requireMentorReady();
    const msg = el('review-shortcut-message').value;
    if (!msg.trim()) throw new Error('Type a message first.');
    workspace = await mentorRequest(endpoint('/chat'), {message: msg, kind: 'review', provider: provider()});
    el('review-shortcut-message').value = '';
    await renderWorkspace();
  });
};
```

- [ ] **Step 5: Append wizard functions to end of mentor.js**

Append the following to the very end of `python/app/static/mentor.js`:

```javascript
// ── Wizard layer ──────────────────────────────────────────────────────────

let wizardStep = 1;

function selectProviderCard(value) {
  el('mentor-provider').value = value;
  document.querySelectorAll('.provider-card').forEach(card => {
    card.setAttribute('aria-pressed', String(card.dataset.provider === value));
  });
  el('mentor-provider').dispatchEvent(new Event('change'));
}

function focusSourceFile() {
  el('source-file').focus();
  el('source-file').scrollIntoView({block: 'center'});
}

function chipClick(n) {
  const flow = getFlowState();
  // Only allow jumping to already-completed steps; never past the active step
  if (busy || workspace?.active_job || uploadController) return;
  if (n < flow.number) activateStep(n);
}

function wizardNext() {
  const flow = getFlowState();
  if (wizardStep < flow.number) {
    activateStep(wizardStep + 1);
  } else {
    goToNextStep();
  }
}

function wizardBack() {
  if (wizardStep > 1) activateStep(wizardStep - 1);
}

function activateStep(n) {
  wizardStep = n;
  document.querySelectorAll('.wizard-step').forEach(section => {
    const stepNum = parseInt(section.dataset.step, 10);
    if (stepNum === n) section.setAttribute('data-active', '');
    else section.removeAttribute('data-active');
  });
  renderWizardChips();
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function renderWizardChips() {
  const flow = getFlowState();
  const maxReached = flow.number;
  document.querySelectorAll('.step-chip').forEach(chip => {
    const n = parseInt(chip.dataset.step, 10);
    const state = n < wizardStep ? 'past'
      : n === wizardStep ? 'active'
      : n <= maxReached ? 'past'
      : 'future';
    chip.dataset.state = state;
    chip.disabled = state === 'future';
  });
}

function renderWizard() {
  if (!mentorAuthenticated) {
    el('step-nav').hidden = true;
    // Show step 1 for login/workspace creation
    activateStep(1);
    return;
  }
  el('step-nav').hidden = false;
  const flow = getFlowState();

  // Auto-advance to the flow's recommended step if wizard is behind
  const targetStep = flow.number;
  if (wizardStep < targetStep && !busy && !workspace?.active_job && !uploadController) {
    activateStep(targetStep);
  }

  // Sync chip states
  renderWizardChips();

  // Sync step-6 chip label
  const step6label = el('step-6-chip-label');
  if (step6label) step6label.textContent = localHax ? 'Build site' : 'Download';

  // Continue button states
  const step2btn = el('step2-continue');
  if (step2btn) {
    const mode = mentorMode();
    step2btn.disabled = busy || !mode.ready;
    step2btn.textContent = mode.ready ? 'Continue →' : mode.message.length < 40 ? mode.message : 'Continue →';
  }
  const step3btn = el('step3-continue');
  if (step3btn) {
    const inputs = courseInputs();
    step3btn.disabled = busy || !inputs.ready;
  }

  // workspace-content is shown once a workspace is open (in step 3+)
  if (el('workspace-content')) {
    el('workspace-content').hidden = !workspace;
  }
}

// Sync provider card aria-pressed state when updateCredentials runs
const _origUpdateCredentials = updateCredentials;
// (updateCredentials is async; after it resolves, sync the cards)
document.addEventListener('DOMContentLoaded', () => {
  // Sync card state on provider change
  el('mentor-provider').addEventListener('change', () => {
    const v = provider();
    document.querySelectorAll('.provider-card').forEach(card => {
      card.setAttribute('aria-pressed', String(card.dataset.provider === v));
    });
  });
});
```

- [ ] **Step 6: Run the full test suite**

```bash
python3 -m unittest discover -s tests -v 2>&1 | tail -20
```
Expected: all tests pass (JS changes have no Python test coverage; browser tests are manual).

- [ ] **Step 7: Start the app and manually verify the wizard**

```bash
HAX_NO_BROWSER=1 python3 python/app/app.py &
sleep 2
echo "App running — open http://127.0.0.1:5050/mentor in a browser"
```

Manual checklist:
- [ ] Step 1 shows on load. Step nav visible after auth.
- [ ] "Create course workspace" creates workspace and advances to step 2.
- [ ] Provider cards change the hidden `mentor-provider` select value.
- [ ] Step 2 Continue is disabled until AI is configured or "No AI" selected.
- [ ] Back/forward navigation works for completed steps.
- [ ] Chat bubbles render with `.chat-bubble-user` / `.chat-bubble-mentor` classes.
- [ ] Legacy pipeline links are in the "Advanced" dropdown.

```bash
kill %1
```

- [ ] **Step 8: Commit**

```bash
git add python/app/static/mentor.js
git commit -m "$(cat <<'EOF'
Add wizard layer to mentor.js

Adds wizardStep state, activateStep/renderWizard/chipClick/wizardNext/
wizardBack functions. renderFlow() now calls renderWizard() to sync
step visibility to workflow state. Chat messages render as bubbles.
Review chat shortcut wired up. Provider cards sync the hidden select.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8 — Final integration check

**Files:** None modified.

- [ ] **Step 1: Run the full Python test suite**

```bash
python3 -m unittest discover -s tests -v
```
Expected: all tests pass.

- [ ] **Step 2: Run the JS tests**

```bash
npm run test:upload
```
Expected: all tests pass.

- [ ] **Step 3: Verify the hosted build still works**

```bash
npm run build:hosted
```
Expected: exits 0.

- [ ] **Step 4: Final commit if any lint/build fixes needed**

```bash
git add -p  # review any stray changes
git commit -m "$(cat <<'EOF'
Final integration: all tests passing post-wizard redesign

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```
