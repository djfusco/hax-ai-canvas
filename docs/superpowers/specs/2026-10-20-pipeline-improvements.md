# Pipeline Improvements: Large-Course Ingestion and Thorough Analysis

**Goal:** Fix five interconnected deficiencies in the AI analysis pipeline so that large Canvas exports (100+ items), rich supplemental materials, and the full course context are thoroughly used — with no content silently dropped because of a design flaw — producing a pedagogically grounded course suggestion at the end.

**Constraints:** Flask + Python, no new runtime dependencies, no build steps, no vector database. All state stays local. Works on both local and hosted modes without branching.

---

## Problem Diagnosis

### 1. Draft generation collapses for large courses

`context()` in `mentor_service.py` shares 18,000 chars uniformly across all items. With 50 items each item gets 360 chars; with 100 items, 180 chars. The model proposes changes to 20 items it chose arbitrarily from fragments. Instructors get shallow revisions that miss the point of individual items.

### 2. Materials are truncated to near-uselessness

Supplemental materials (syllabus, rubric, outcomes document) are truncated to `min(2000, 12000 / num_materials)` chars per call. With 10 materials: 1,200 chars each. A real syllabus is 5–15 pages. The full text is already stored (up to 60,000 chars per material). The bottleneck is purely in how much gets sent — and it's sent uniformly regardless of how relevant a section is to the item being assessed.

### 3. No course-level view

Items are assessed in parallel, independently. The instructor gets per-item scores but cannot see: whether Bloom's levels build logically across the course, whether three assessments test identical skills, whether AI-use expectations are inconsistent across modules. These patterns require a cross-item view that no current call produces.

### 4. The brief is free text with no guidance on completeness

The brief travels to every AI call. If it says "make it more engaging," all downstream outputs are vague. There's no check that it covers learning outcomes, student context, constraints, or AI use policy — the four things that most determine assessment quality.

### 5. Revision doesn't use what the assessment found

When `review_draft()` is called after an assessment, the AI sees the instructor's text feedback and the current draft, but not the assessment's specific concerns and recommendations for each item. It cannot know "the assessment flagged this assignment for unclear criteria" unless the instructor types it.

---

## Design

### Improvement 1 — Smarter Brief Interview

**File:** `python/mentor_service.py` (`interview()`)

Append a gap-checking instruction to the interview prompt:

> "BRIEF COMPLETENESS CHECK: After your response, scan the instructor design brief above for these four elements: (1) explicit learning outcomes — what students will be able to demonstrate; (2) student context — course level, prior knowledge; (3) constraints — what must not change; (4) AI use policy for student work. If any element is absent and the instructor has not already addressed it in this exchange, ask specifically about the most important missing one. If all four are present, do not mention them."

No new form fields, no new data structures. The interview reply naturally includes a follow-up question when needed. No behavior change for complete briefs.

### Improvement 2 — Batched Draft Generation with Triage

**File:** `python/mentor_service.py` (`generate_draft()`, new `_triage_items()`, new `_draft_batch()`)

**Threshold:** `DRAFT_BATCH_SIZE = 12`. Courses with ≤ 12 items use the existing single-call path unchanged.

**Triage phase** (courses > 12 items):

A single structured call receives item titles, types, and the design brief, and returns:
```json
{
  "priority_order": ["item-id-1", "item-id-5", ...],
  "key_concerns": {
    "item-id-1": "One-sentence concern this item needs addressed",
    "item-id-5": "One-sentence concern"
  },
  "course_gaps": ["Gap description", "Gap description"]
}
```

`priority_order` contains every item ID sorted by revision urgency. `key_concerns` covers the subset the triage considers most misaligned. Any item ID not in `priority_order` is appended at the end. Instructor sees: `progress("triaging", f"Analyzing {n} items against your design brief to identify revision priorities...")`.

**Batching phase:**

Items are ordered by `priority_order`. Batches of `DRAFT_BATCH_SIZE` items are processed sequentially. For each batch:
- Context is built using only the batch items at full 1,200-char excerpts (not compressed).
- `key_concerns` for batch items is injected as a `TRIAGE PRIORITIES` block in the prompt.
- The model may propose changes to any item in the batch; unchanged items are retained.
- Max proposals per batch call: 20 (same as today, but now scoped to the batch).
- Progress: `progress("drafting", f"Drafting batch {i} of {total_batches} ({count} items, {start}–{end})...")`.

**Merging:** Proposals from all batches are merged. If the same item ID appears in multiple batches (should not happen but is guarded), the last proposal wins. Items not proposed by any batch are retained as originals.

**Backward compat:** The `generate_draft()` function signature is unchanged. The caller cannot distinguish batched from non-batched results.

### Improvement 3 — Relevant Material Chunks

**Files:** `python/mentor_assessment.py` (`_context()`, `_plan_item()`, new `_chunk_text()`, new `_select_chunks()`), `python/mentor_service.py` (`context()`)

**Core functions:**

```python
def _chunk_text(text, max_chunk=1500):
    """Split text at paragraph then sentence boundaries. Returns list of str."""
```

Splits on `\n\n` first; if a paragraph exceeds `max_chunk`, splits further on `. ` / `? ` / `! `. Each chunk is at most `max_chunk` chars. Chunks smaller than 100 chars are merged with the next chunk.

```python
def _select_chunks(full_text, item_title, item_text, budget):
    """Return the highest-relevance chunks from full_text up to budget chars total."""
```

Scores each chunk against `item_title + " " + item_text[:500]` using word-overlap: `len(title_words & chunk_words) / log(1 + len(title_words | chunk_words))`. Returns chunks sorted by score, accumulated until `budget` chars are used. Disclosure: if any chunks are omitted, returns `"[{omitted} of {total} sections omitted — not scored for this item]"` appended as a final string.

**In `mentor_assessment.py:_context()`:** Store the full material text (up to `MAX_TEXT_CHARS = 60000`) in the source dict rather than truncating. The per-item budget is applied in `_plan_item()`.

**In `mentor_assessment.py:_plan_item()`:** When building `sources`, replace `source["text"]` with `_select_chunks(source["text"], item["title"], item["text"][:500], per_material_budget)` for each non-brief source. `per_material_budget = MAX_MATERIAL_CONTEXT // max(1, len(material_sources))`.

**In `mentor_service.py:context()`:** For each batch item in draft generation, use `_select_chunks(material["text"], item["title"], item_text[:500], 1500)` instead of `text[:material_limit]`. The overall material budget per batch stays the same (14,000 chars total), but the content chosen is item-relevant.

**No change to material upload, storage, or the `material.text` field.**

### Improvement 4 — Course-Level Synthesis

**File:** `python/mentor_assessment.py` (`assess_design()`, new `_compute_statistics()`, new `synthesize_course()`)

**When it runs:** At the end of `assess_design()`, after all per-item assessments complete, when `len(prepared) > 1`.

**Statistics computed locally** (no LLM call, zero cost):

```python
def _compute_statistics(assessed_items):
    """Compute Bloom/AIAS distributions and dimension averages from assessed items."""
    # Returns bloom_distribution, aias_distribution, dimension_averages
    # bloom_distribution: {"1": n, "2": n, ..., "6": n, "null": n}
    # aias_distribution: {"observed": {...}, "recommended": {...}}
    # dimension_averages: {"ai_leverage": 6.2, ..., "alignment": None}
```

**LLM synthesis call:**

Input (compact, scales to 500+ items):
```json
{
  "course_title": "...",
  "brief_excerpt": "first 3000 chars of brief",
  "item_count": 47,
  "bloom_distribution": {"1": 5, "2": 12, ...},
  "aias_distribution": {...},
  "dimension_averages": {...},
  "top_concerns": ["top 10 most-repeated concern strings"],
  "top_recommendations": ["top 10 most-repeated recommendation strings"],
  "item_titles_by_type": {"assignments": ["title1", ...], "quizzes": [...]}
}
```

Output schema:
```json
{
  "scaffolding_analysis": "2-3 sentences on Bloom progression and cognitive scaffolding",
  "cross_cutting_concerns": ["concern1", "concern2"],
  "coverage_gaps": ["gap1"],
  "course_recommendations": ["rec1", "rec2", "rec3"],
  "limitations": ["what wasn't assessed"]
}
```

`course_recommendations` is the synthesis's most actionable output: 3–5 concrete, prioritized changes the instructor should consider at the course level (not item-level).

**Progress:** `progress("synthesizing", f"Computing course-level patterns across {n} assessed items...")` then `progress("synthesizing", "Generating course analysis and recommendations...")`.

**Result key:** `assessment["synthesis"]` — merged statistics + LLM qualitative output.

**Error handling:** If the synthesis LLM call fails, `assessment["synthesis"]` contains only the locally-computed statistics plus a `synthesis_error` key with the error message. Per-item results are unaffected. The instructor sees complete item assessments even if synthesis fails.

### Improvement 5 — Assessment-Informed Revision

**File:** `python/mentor_service.py` (`review_draft()`)

In the `build()` closure inside `review_draft()`, append assessment findings to the prompt when they exist:

```python
assessment = (workspace.get("draft") or {}).get("assessment") or {}
item_findings = {}
for a in assessment.get("items", []):
    iid = a.get("item_id")
    concerns = a.get("concerns", [])[:3]
    recs = a.get("recommendations", [])[:3]
    if iid and (concerns or recs):
        item_findings[iid] = {"concerns": concerns, "recommendations": recs}
if item_findings:
    prompt += "\n\nASSESSMENT FINDINGS — address these when revising:\n" + json.dumps(item_findings, ensure_ascii=False)

synthesis = assessment.get("synthesis", {})
course_concerns = synthesis.get("cross_cutting_concerns", [])
course_gaps = synthesis.get("coverage_gaps", [])
if course_concerns or course_gaps:
    prompt += "\n\nCOURSE-LEVEL FINDINGS:\n" + json.dumps({
        "cross_cutting_concerns": course_concerns,
        "coverage_gaps": course_gaps,
    }, ensure_ascii=False)
```

This is injected at full scale when `excerpt_scale == 1.0` and omitted (with a note) at reduced scale to stay within the model's context window.

---

## Progress Reporting

All five improvements use the existing `progress(phase, message)` callback. No new phases are added; new messages are added within existing phases:

| Phase | New messages |
|---|---|
| `"triaging"` | "Analyzing {n} items against your design brief to identify revision priorities..." |
| `"drafting"` | "Drafting batch {i} of {total} ({items} items, prioritized)..." |
| `"synthesizing"` | "Computing course-level patterns across {n} assessed items..." |
| `"synthesizing"` | "Generating course analysis and recommendations..." |

---

## Data Flow Summary

```
interview()
  → gap-check prompt → [brief completeness question if needed]

generate_draft(assess=True)
  → assess_design(baseline)       # per-item, chunked material context
      → synthesize_course()       # compact statistics + one LLM call
  → _triage_items()               # one small LLM call (if > 12 items)
  → _draft_batch() × N batches    # full excerpts per batch
  → assess_design(draft)          # per-item, chunked material context
      → synthesize_course()       # again on the draft

review_draft(assess=True)
  → context + draft + ASSESSMENT FINDINGS + COURSE-LEVEL FINDINGS
  → [instructor's revised draft]
  → assess_design() if stale      # per-item, chunked material context
      → synthesize_course()
```

---

## What Doesn't Change

- Material upload, storage, and the `material.text` field — unchanged.
- `source_items()`, `inventory()`, import pipeline — unchanged.
- The `assess_design()` function signature and per-item chunking/combining logic — unchanged.
- All existing test interfaces: `PipelineClient`, `BarrierClient`, `ReorderingClient` — unchanged.
- The wizard UI and JS — unchanged.
- The 500-item / 16 MiB / 2 MiB-per-item hard limits — unchanged (they protect against local DoS).

---

## Testing Strategy

Each improvement has focused unit/integration tests using the existing test harnesses:

1. **Brief gap-check:** Mock `PipelineClient`; verify interview prompt contains gap-check text; verify reply includes follow-up question when brief is incomplete; verify no follow-up when all four elements are present.

2. **Triage + batching:** `PipelineClient` records calls; verify that a 15-item course triggers triage + 2 batch calls; verify that a 10-item course uses a single call; verify merged proposals contain items from all batches; verify `priority_order` re-sorts items.

3. **Material chunking:** Unit tests for `_chunk_text()` (paragraph split, sentence fallback, min-merge); unit tests for `_select_chunks()` (returns within budget, higher-relevance chunk scores first, omission disclosure); integration test verifying assessment context uses relevant chunks over uniform truncation.

4. **Course synthesis:** `PipelineClient` records calls; verify `_compute_statistics()` produces correct distributions; verify synthesis call is made after per-item assessment; verify `synthesis_error` is set and items intact when synthesis call fails.

5. **Assessment-informed revision:** Verify `review_draft()` prompt contains `ASSESSMENT FINDINGS` when draft has assessment; verify `COURSE-LEVEL FINDINGS` when synthesis is present; verify both are omitted when no assessment exists.
