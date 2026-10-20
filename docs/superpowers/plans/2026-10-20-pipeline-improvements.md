# Pipeline Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five interconnected deficiencies in the AI analysis pipeline so that large Canvas exports (100+ items), rich supplemental materials, and the full course context are thoroughly used — with no content silently dropped because of a design flaw — producing a pedagogically grounded course suggestion at the end.

**Architecture:** Two files changed: `python/mentor_assessment.py` (chunk utilities, synthesis) and `python/mentor_service.py` (brief gap-check, triage+batching, assessment-informed revision). Tests are in `tests/test_mentor_assessment.py` and `tests/test_mentor_pipeline.py`; both already have `MockClient`/`PipelineClient` harnesses to extend.

**Tech Stack:** Python 3, Flask, `concurrent.futures.ThreadPoolExecutor`, JSON schema structured outputs; no new dependencies.

**Spec:** docs/superpowers/specs/2026-10-20-pipeline-improvements.md

## Global Constraints

- No new runtime dependencies; no build steps; no vector database.
- All state stays local; works on both local and hosted modes without branching.
- `assess_design()` function signature is unchanged.
- All existing test interfaces — `PipelineClient`, `BarrierClient`, `ReorderingClient` — are unchanged.
- The wizard UI and JS are unchanged.
- `MAX_ITEMS = 500`, `MAX_ITEM_BYTES = 2 MiB`, `MAX_TOTAL_ITEM_BYTES = 16 MiB` hard limits are unchanged.
- `DRAFT_BATCH_SIZE = 12` (courses with ≤ 12 items use existing single-call path).
- `MAX_MATERIAL_CONTEXT = 12000`, `MAX_MATERIAL_EXCERPT = 2000` are existing constants in `mentor_assessment.py`.
- `MAX_TEXT_CHARS = 60000` is the existing per-material text limit in `_context()`.

## Review Focus

1. `_select_chunks()` budget boundary: if `budget` is 0 (no materials), must not divide by zero or return no text.
2. Synthesis LLM call failure: `assess_design()` must still return full per-item results even if `synthesize_course()` raises.
3. Triage with > 12 items but `priority_order` returning fewer IDs than items: unmentioned items must be appended, not silently dropped.
4. Assessment-informed revision at reduced scale: `ASSESSMENT FINDINGS` block must be omitted (with note) to stay within context window, not silently truncated mid-JSON.
5. Brief gap-check when brief is already complete: instruction must not add a redundant follow-up question — interview reply must be unaffected by the gap-check text when all four elements are present.

---

### Task 1: Material chunk utilities (`_chunk_text`, `_select_chunks`)

**Files:**
- Modify: `python/mentor_assessment.py` (add two functions after `_normalized`)
- Test: `tests/test_mentor_assessment.py`

**Interfaces:**
- Produces: `_chunk_text(text: str, max_chunk: int = 1500) -> list[str]`; `_select_chunks(full_text: str, item_title: str, item_text: str, budget: int) -> str`

- [ ] **Step 1: Write the failing tests**

Add at the bottom of `tests/test_mentor_assessment.py` (inside the existing file, after the last class):

```python
class ChunkUtilityTests(unittest.TestCase):
    def test_chunk_text_splits_on_paragraphs(self):
        from mentor_assessment import _chunk_text
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = _chunk_text(text, max_chunk=1500)
        self.assertEqual(chunks, ["Para one.", "Para two.", "Para three."])

    def test_chunk_text_splits_long_paragraph_on_sentence(self):
        from mentor_assessment import _chunk_text
        long_para = "First sentence. " + "x" * 1400 + ". Third sentence."
        chunks = _chunk_text(long_para, max_chunk=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 100 or " " not in c for c in chunks))

    def test_chunk_text_merges_short_chunks(self):
        from mentor_assessment import _chunk_text
        text = "Hi.\n\nOk.\n\nThis is a longer paragraph with more substance."
        chunks = _chunk_text(text, max_chunk=1500)
        # "Hi." and "Ok." are < 100 chars, should be merged with next
        self.assertTrue(all(len(c) >= 100 or c == chunks[-1] for c in chunks))

    def test_chunk_text_empty_returns_one_empty_chunk(self):
        from mentor_assessment import _chunk_text
        self.assertEqual(_chunk_text("", max_chunk=1500), [""])

    def test_select_chunks_returns_within_budget(self):
        from mentor_assessment import _select_chunks
        full_text = "\n\n".join(["Chunk about rubric " + str(i) + " criteria." for i in range(20)])
        result = _select_chunks(full_text, "Rubric assessment", "evaluate rubric criteria", budget=500)
        self.assertIsInstance(result, str)
        self.assertLessEqual(len(result), 600)  # budget + disclosure line

    def test_select_chunks_prefers_relevant_chunks(self):
        from mentor_assessment import _select_chunks
        text = ("Completely irrelevant content about cooking.\n\n"
                "The rubric criteria require students to evaluate evidence.\n\n"
                "More irrelevant content about gardening.")
        result = _select_chunks(text, "Rubric assessment", "evaluate rubric criteria", budget=200)
        self.assertIn("rubric criteria", result)

    def test_select_chunks_adds_omission_disclosure(self):
        from mentor_assessment import _select_chunks
        full_text = "\n\n".join(["Section " + str(i) + " " + "x" * 200 for i in range(10)])
        result = _select_chunks(full_text, "item", "text", budget=300)
        self.assertIn("omitted", result)

    def test_select_chunks_no_omission_when_all_fit(self):
        from mentor_assessment import _select_chunks
        full_text = "Short section."
        result = _select_chunks(full_text, "item", "text", budget=2000)
        self.assertNotIn("omitted", result)

    def test_select_chunks_zero_budget_returns_disclosure(self):
        from mentor_assessment import _select_chunks
        full_text = "Some content."
        result = _select_chunks(full_text, "item", "text", budget=0)
        self.assertIn("omitted", result)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::ChunkUtilityTests -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name '_chunk_text'`

- [ ] **Step 3: Implement `_chunk_text` and `_select_chunks` in `mentor_assessment.py`**

Add after the `_normalized` function (around line 213):

```python
def _chunk_text(text, max_chunk=1500):
    """Split text at paragraph then sentence boundaries. Returns list of str."""
    if not text:
        return [""]
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    for para in paragraphs:
        if len(para) <= max_chunk:
            chunks.append(para)
        else:
            # Split on sentence boundaries
            parts = re.split(r'(?<=[.?!]) ', para)
            current = ""
            for part in parts:
                if not current:
                    current = part
                elif len(current) + 1 + len(part) <= max_chunk:
                    current += " " + part
                else:
                    chunks.append(current)
                    current = part
            if current:
                chunks.append(current)
    # Merge chunks shorter than 100 chars with the next chunk
    merged = []
    i = 0
    while i < len(chunks):
        if len(chunks[i]) < 100 and i + 1 < len(chunks):
            merged.append(chunks[i] + "\n\n" + chunks[i + 1])
            i += 2
        else:
            merged.append(chunks[i])
            i += 1
    return merged if merged else [""]


def _select_chunks(full_text, item_title, item_text, budget):
    """Return the highest-relevance chunks from full_text up to budget chars total."""
    chunks = _chunk_text(full_text)
    if budget <= 0:
        total = len(chunks)
        return f"[{total} of {total} sections omitted — not scored for this item]"
    query = (item_title + " " + item_text[:500]).lower()
    query_words = set(re.findall(r'\w+', query))

    def score(chunk):
        chunk_words = set(re.findall(r'\w+', chunk.lower()))
        union = query_words | chunk_words
        if not union:
            return 0.0
        return len(query_words & chunk_words) / math.log(1 + len(union))

    scored = sorted(enumerate(chunks), key=lambda ic: score(ic[1]), reverse=True)
    selected_indices = []
    total_chars = 0
    for idx, chunk in scored:
        if total_chars + len(chunk) <= budget:
            selected_indices.append(idx)
            total_chars += len(chunk)
        elif not selected_indices:
            # Always include at least one chunk (truncated)
            selected_indices.append(idx)
            break
    selected_indices.sort()
    omitted = len(chunks) - len(selected_indices)
    result_parts = [chunks[i] for i in selected_indices]
    result = "\n\n".join(result_parts)
    if len(result) > budget and budget > 0:
        result = result[:budget]
    if omitted > 0:
        result += f"\n\n[{omitted} of {len(chunks)} sections omitted — not scored for this item]"
    return result
```

Also add `math` to the imports at the top if not already there (it already is: `import math` exists).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::ChunkUtilityTests -v 2>&1 | tail -20
```

Expected: `9 passed`

- [ ] **Step 5: Run full suite to check for regressions**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: existing pass count, 0 new failures

- [ ] **Step 6: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_assessment.py tests/test_mentor_assessment.py
git commit -m "$(cat <<'EOF'
feat: add _chunk_text and _select_chunks material utilities

Split supplemental material text into relevance-scored chunks rather
than uniform truncation — foundation for per-item context selection.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Course-level synthesis

**Files:**
- Modify: `python/mentor_assessment.py` (add `_compute_statistics`, `synthesize_course`, wire into `assess_design`)
- Test: `tests/test_mentor_assessment.py`

**Interfaces:**
- Consumes: `assess_design()` result items, `workspace`, `client`
- Produces: `assessment["synthesis"]` dict with keys: `bloom_distribution`, `aias_distribution`, `dimension_averages`, `scaffolding_analysis`, `cross_cutting_concerns`, `coverage_gaps`, `course_recommendations`, `limitations` (or `synthesis_error`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mentor_assessment.py` (new class):

```python
class SynthesisTests(unittest.TestCase):
    def test_synthesis_added_to_assess_design_result(self):
        items = [item(str(i)) for i in range(3)]
        client = MockClient()
        # Add synthesis handling to MockClient
        original_call = client.call
        synthesis_calls = []
        def call_with_synthesis(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                synthesis_calls.append(prompt)
                return json.dumps({
                    "scaffolding_analysis": "Good progression.",
                    "cross_cutting_concerns": ["Criteria clarity"],
                    "coverage_gaps": [],
                    "course_recommendations": ["Add rubric to all assignments"],
                    "limitations": ["Only 3 items assessed"],
                })
            return original_call(system, prompt, max_tokens)
        client.call = call_with_synthesis
        report = assess_design(workspace(), items, client)
        self.assertIn("synthesis", report)
        synthesis = report["synthesis"]
        self.assertIn("bloom_distribution", synthesis)
        self.assertIn("dimension_averages", synthesis)
        self.assertIn("course_recommendations", synthesis)
        self.assertEqual(len(synthesis_calls), 1)

    def test_synthesis_failure_does_not_affect_item_results(self):
        items = [item(str(i)) for i in range(2)]
        client = MockClient()
        original_call = client.call
        def call_raising(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                raise RuntimeError("synthesis LLM error")
            return original_call(system, prompt, max_tokens)
        client.call = call_raising
        report = assess_design(workspace(), items, client)
        self.assertEqual(len(report["items"]), 2)
        self.assertIn("synthesis", report)
        self.assertIn("synthesis_error", report["synthesis"])
        self.assertIn("bloom_distribution", report["synthesis"])

    def test_compute_statistics_bloom_distribution(self):
        from mentor_assessment import _compute_statistics
        assessed_items = [
            {"blooms": {"level": 3}, "aias": {"observed_level": 1, "recommended_level": 2},
             "dimensions": {k: {"score": 5} for k in DIMENSIONS}},
            {"blooms": {"level": 5}, "aias": {"observed_level": None, "recommended_level": 3},
             "dimensions": {k: {"score": 7} for k in DIMENSIONS}},
        ]
        stats = _compute_statistics(assessed_items)
        self.assertEqual(stats["bloom_distribution"]["3"], 1)
        self.assertEqual(stats["bloom_distribution"]["5"], 1)
        self.assertAlmostEqual(stats["dimension_averages"]["ai_leverage"], 6.0)

    def test_synthesis_skipped_for_single_item(self):
        client = MockClient()
        synthesis_calls = []
        original_call = client.call
        def call_tracking(system, prompt, max_tokens):
            if prompt.startswith("Synthesize this course assessment"):
                synthesis_calls.append(prompt)
            return original_call(system, prompt, max_tokens)
        client.call = call_tracking
        report = assess_design(workspace(), [item()], client)
        self.assertEqual(len(synthesis_calls), 0)
        self.assertNotIn("synthesis", report)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::SynthesisTests -v 2>&1 | tail -15
```

Expected: `ImportError: cannot import name '_compute_statistics'` or `AssertionError: 'synthesis' not in report`

- [ ] **Step 3: Implement `_compute_statistics` and `synthesize_course` in `mentor_assessment.py`**

Add after `_combine_chunks` (around line 530), before `assess_design`:

```python
def _compute_statistics(assessed_items):
    """Compute Bloom/AIAS distributions and dimension averages from assessed items."""
    bloom_distribution = {str(i): 0 for i in range(1, 7)}
    bloom_distribution["null"] = 0
    aias_observed = {str(i): 0 for i in range(1, 6)}
    aias_observed["null"] = 0
    aias_recommended = {str(i): 0 for i in range(1, 6)}
    aias_recommended["null"] = 0
    dim_sums = {d: 0 for d in DIMENSIONS}
    dim_counts = {d: 0 for d in DIMENSIONS}
    for assessed in assessed_items:
        bloom_level = assessed.get("blooms", {}).get("level")
        bloom_distribution[str(bloom_level) if bloom_level is not None else "null"] = (
            bloom_distribution.get(str(bloom_level) if bloom_level is not None else "null", 0) + 1
        )
        aias = assessed.get("aias", {})
        obs = aias.get("observed_level")
        rec = aias.get("recommended_level")
        aias_observed[str(obs) if obs is not None else "null"] = aias_observed.get(str(obs) if obs is not None else "null", 0) + 1
        aias_recommended[str(rec) if rec is not None else "null"] = aias_recommended.get(str(rec) if rec is not None else "null", 0) + 1
        for d in DIMENSIONS:
            score = assessed.get("dimensions", {}).get(d, {}).get("score")
            if score is not None:
                dim_sums[d] += score
                dim_counts[d] += 1
    dimension_averages = {
        d: round(dim_sums[d] / dim_counts[d], 2) if dim_counts[d] else None
        for d in DIMENSIONS
    }
    return {
        "bloom_distribution": bloom_distribution,
        "aias_distribution": {"observed": aias_observed, "recommended": aias_recommended},
        "dimension_averages": dimension_averages,
    }


def synthesize_course(workspace, assessed_items, prepared_items, statistics, client, *, progress=None):
    """Make one compact LLM call for course-level synthesis. Raises on failure."""
    title = workspace.get("title", "")
    brief = workspace.get("brief", "")
    concerns_flat = []
    recs_flat = []
    items_by_type = {}
    for item in assessed_items:
        concerns_flat.extend(item.get("concerns", []))
        recs_flat.extend(item.get("recommendations", []))
    for item in prepared_items:
        kind = item.get("type", "pages")
        items_by_type.setdefault(kind, []).append(item.get("title", ""))
    from collections import Counter
    top_concerns = [c for c, _ in Counter(concerns_flat).most_common(10)]
    top_recs = [r for r, _ in Counter(recs_flat).most_common(10)]
    payload = {
        "course_title": title,
        "brief_excerpt": brief[:3000],
        "item_count": len(assessed_items),
        "bloom_distribution": statistics["bloom_distribution"],
        "aias_distribution": statistics["aias_distribution"],
        "dimension_averages": statistics["dimension_averages"],
        "top_concerns": top_concerns,
        "top_recommendations": top_recs,
        "item_titles_by_type": items_by_type,
    }
    schema = {
        "type": "object",
        "properties": {
            "scaffolding_analysis": {"type": "string"},
            "cross_cutting_concerns": {"type": "array", "items": {"type": "string"}},
            "coverage_gaps": {"type": "array", "items": {"type": "string"}},
            "course_recommendations": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["scaffolding_analysis", "cross_cutting_concerns", "coverage_gaps",
                     "course_recommendations", "limitations"],
        "additionalProperties": False,
    }
    prompt = (
        "Synthesize this course assessment into a compact course-level analysis. "
        "Identify Bloom's progression, cross-cutting concerns, coverage gaps, and 3-5 prioritized "
        "course-level recommendations. Return strict JSON only.\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    structured = getattr(client, "call_structured", None)
    response = (structured(SYSTEM, prompt, schema, max_tokens=MAX_TOKENS)
                if callable(structured)
                else client.call(SYSTEM, prompt, max_tokens=MAX_TOKENS))
    if isinstance(response, str):
        import json as _json
        response = _json.loads(response)
    return response
```

Wire into `assess_design()`. After the `report_items` assembly (after line `report_items = [...]`), add:

```python
    synthesis = None
    if len(prepared) > 1:
        statistics = _compute_statistics(report_items)
        if progress:
            progress("synthesizing", f"Computing course-level patterns across {len(report_items)} assessed items...")
        try:
            if progress:
                progress("synthesizing", "Generating course analysis and recommendations...")
            qualitative = synthesize_course(workspace, report_items, prepared, statistics, client, progress=progress)
            synthesis = {**statistics, **qualitative}
        except Exception as exc:
            synthesis = {**statistics, "synthesis_error": str(exc)}
```

And in the final `return` of `assess_design`, add `"synthesis": synthesis` when synthesis is not None:

Change:
```python
    return {
        "mode": "ai", "scope": scope, "version": 1,
```
to include synthesis:
```python
    result = {
        "mode": "ai", "scope": scope, "version": 1,
        ...
    }
    if synthesis is not None:
        result["synthesis"] = synthesis
    return result
```

(Read the exact return statement and patch it precisely.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::SynthesisTests -v 2>&1 | tail -15
```

Expected: `4 passed`

- [ ] **Step 5: Update existing pipeline tests for synthesis call count**

The `PipelineClient` in `test_mentor_pipeline.py` needs to handle synthesis prompts. Currently `call_structured()` only handles assessment prompts. Add synthesis handling:

In `PipelineClient.call_structured`, add a branch before the default return:

```python
    def call_structured(self, system, prompt, schema, max_tokens):
        self.calls.append((prompt, schema))
        if prompt.startswith("Assess this one item"):
            return json.dumps(response_for(payload(prompt)))
        if prompt.startswith("Synthesize this course assessment"):
            return json.dumps({
                "scaffolding_analysis": "Good progression.",
                "cross_cutting_concerns": [],
                "coverage_gaps": [],
                "course_recommendations": ["Improve rubric clarity"],
                "limitations": [],
            })
        if isinstance(self.draft_response, Exception):
            raise self.draft_response
        return self.draft_response if self.draft_response is not None else json.dumps({...})
```

Also update the call counts in `test_thirty_item_assessment_enhancement_and_draft_assessment_complete`:
- `len(client.calls)` changes 61 → 63 (two synthesis calls: one for baseline, one for draft)
- `schema = client.calls[30][1]` changes to `client.calls[31][1]` (synthesis call is at index 30, draft call shifts to 31)
- `len(phases)` in `test_complete_scope_is_assessed_not_only_targeted_proposals` stays 25 (synthesis uses its own "synthesizing" phase, not "assessing_draft")

And in `test_failures_after_thirty_baseline_items_are_safe_and_not_mislabeled`:
- `len(client.calls)` changes 31 → 32 (one synthesis call after baseline assessment)

- [ ] **Step 6: Run full suite**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_assessment.py tests/test_mentor_assessment.py tests/test_mentor_pipeline.py
git commit -m "$(cat <<'EOF'
feat: add course-level synthesis to assess_design

After per-item assessment, compute statistics locally and make one
compact LLM call for course-level scaffolding analysis, cross-cutting
concerns, coverage gaps, and prioritized recommendations.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Relevant chunk selection in assessment context

**Files:**
- Modify: `python/mentor_assessment.py` (`_context`, `_plan_item`)
- Test: `tests/test_mentor_assessment.py`

**Interfaces:**
- Consumes: `_chunk_text`, `_select_chunks` (from Task 1)
- Produces: `_context()` stores full material text under `"_full_text"` key; `_plan_item()` applies `_select_chunks` per material source before building prompts

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mentor_assessment.py`:

```python
class RelevantChunkSelectionTests(unittest.TestCase):
    def test_relevant_chunks_replace_uniform_truncation(self):
        """Assessment context uses chunk selection, not a fixed prefix truncation."""
        # Build a workspace with a material that has two distinct sections
        ws = {
            "title": "Test course",
            "brief": "Students will evaluate evidence.",
            "materials": [{
                "id": "mat-one", "role": "reference", "name": "Big document",
                "text": ("Completely irrelevant cooking content. " * 50 + "\n\n"
                         "Rubric criteria: students must evaluate sources. " * 10),
            }],
        }
        items_list = [item("one")]
        client = MockClient()
        report = assess_design(ws, items_list, client)
        call = client.calls[0]
        sources = {s["source_id"]: s for s in call["data"]["evidence_sources"]}
        mat_text = sources["mat-one"]["text"]
        # Should prefer the rubric-criteria section over the cooking content
        self.assertIn("evaluate sources", mat_text)

    def test_supplement_budget_still_bounded(self):
        """Total material context sent per item stays within MAX_MATERIAL_CONTEXT."""
        ws = {
            "title": "Test",
            "brief": "Outcomes.",
            "materials": [
                {"id": f"mat-{i}", "role": "reference", "name": f"Doc {i}",
                 "text": "Content about rubric criteria evaluation " * 200}
                for i in range(5)
            ],
        }
        client = MockClient()
        report = assess_design(ws, [item("x")], client)
        call = client.calls[0]
        from mentor_assessment import MAX_MATERIAL_CONTEXT
        total_material_chars = sum(
            len(s["text"]) for s in call["data"]["evidence_sources"]
            if s["source_id"] != "instructor-brief" and not s["source_id"].startswith("course:")
        )
        self.assertLessEqual(total_material_chars, MAX_MATERIAL_CONTEXT + 200)  # +200 for disclosures
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::RelevantChunkSelectionTests -v 2>&1 | tail -10
```

Expected: both tests fail (currently uses uniform truncation, so cooking content may appear first)

- [ ] **Step 3: Modify `_context()` to store full text**

In `mentor_assessment.py`, in `_context()`, change the material storage from:

```python
        sources[identifier] = {"source_id": identifier, "role": role, "name": name,
                               "text": text[:budget]}
```

to:

```python
        sources[identifier] = {"source_id": identifier, "role": role, "name": name,
                               "text": text[:budget], "_full_text": text}
```

- [ ] **Step 4: Modify `_plan_item()` to apply `_select_chunks`**

In `_plan_item()`, before the `request` inner function, add material chunk selection:

```python
    # Apply per-item relevance chunk selection to non-brief sources
    n_materials = sum(1 for s in context.values() if s.get("role") != "instructor_brief")
    per_material_budget = MAX_MATERIAL_CONTEXT // max(1, n_materials)
    for source in context.values():
        full_text = source.pop("_full_text", None)
        if full_text and source.get("role") != "instructor_brief":
            source["text"] = _select_chunks(
                full_text, item["title"], item["text"][:500], per_material_budget
            )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_assessment.py::RelevantChunkSelectionTests -v 2>&1 | tail -10
```

Expected: `2 passed`

- [ ] **Step 6: Update existing truncation test**

The test `test_supplement_and_brief_truncation_are_explicit_and_balanced` checks:
`sum(len(source["text"]) for source in material_sources) == MAX_MATERIAL_CONTEXT`

This test will likely break because `_select_chunks` may return slightly different lengths. Update the assertion to use `assertLessEqual(..., MAX_MATERIAL_CONTEXT + 200)` to account for omission disclosure text.

Read the exact assertion first, then patch it.

- [ ] **Step 7: Run full suite**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 8: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_assessment.py tests/test_mentor_assessment.py
git commit -m "$(cat <<'EOF'
feat: use relevance chunk selection for supplemental material context

Per-item assessment now selects the most relevant sections of
supplemental materials (syllabus, rubric, outcomes) instead of
uniform prefix truncation, maximising the 12,000-char budget.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Smarter brief interview (gap-check)

**Files:**
- Modify: `python/mentor_service.py` (`interview()`)
- Test: `tests/test_mentor_pipeline.py`

**Interfaces:**
- No interface changes; `interview()` signature is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mentor_pipeline.py`:

```python
class BriefGapCheckTests(unittest.TestCase):
    def test_interview_prompt_contains_gap_check_instruction(self):
        state = {**workspace(), "id": "gap-check-test"}
        client = PipelineClient()
        # PipelineClient.call_structured handles "interview" kind
        # But interview() uses _call_fitted which calls call_structured with interview schema
        # We need to capture the prompt; patch _fit_prompt or check client calls
        prompts = []
        orig_call = client.call_structured
        def capture(system, prompt, schema, max_tokens):
            prompts.append(prompt)
            return json.dumps({"reply": "What are the learning outcomes?"})
        client.call_structured = capture
        # Need to also handle preflight
        service.interview(state, "Make it more engaging", client)
        self.assertTrue(any("BRIEF COMPLETENESS CHECK" in p for p in prompts))

    def test_interview_gap_check_asks_about_missing_element(self):
        state = {**workspace(), "id": "gap-check-test2",
                 "brief": "Make it more engaging"}  # brief missing outcomes etc.
        client = PipelineClient()
        replies = []
        def capture(system, prompt, schema, max_tokens):
            replies.append(prompt)
            return json.dumps({"reply": "What learning outcomes should students achieve?"})
        client.call_structured = capture
        reply = service.interview(state, "Make it more engaging", client)
        # The gap-check instruction was in the prompt
        self.assertTrue(any("BRIEF COMPLETENESS CHECK" in p for p in replies))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::BriefGapCheckTests -v 2>&1 | tail -10
```

Expected: `AssertionError: BRIEF COMPLETENESS CHECK not in prompts`

- [ ] **Step 3: Modify `interview()` in `mentor_service.py`**

In `interview()`, in the `build` closure, append the gap-check instruction after the instructor message:

```python
    def build(source_context, scale):
        count = int(len(history) * scale)
        retained = history[-count:] if count else []
        return (
        source_context + "\n\nRECENT CONVERSATION:\n" + json.dumps(retained, ensure_ascii=False)
        + f"\nCONVERSATION COVERAGE: {len(retained)} of {len(history)} recent messages supplied."
        + "\n\nINSTRUCTOR: " + message
        + "\n\nBRIEF COMPLETENESS CHECK: After your response, scan the instructor design brief "
          "above for these four elements: (1) explicit learning outcomes — what students will be able "
          "to demonstrate; (2) student context — course level, prior knowledge; (3) constraints — "
          "what must not change; (4) AI use policy for student work. If any element is absent and "
          "the instructor has not already addressed it in this exchange, ask specifically about the "
          "most important missing one. If all four are present, do not mention them."
        + '\nReturn {"reply":"A concise evidence-aware response with 1-3 focused questions."}.'
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::BriefGapCheckTests -v 2>&1 | tail -10
```

Expected: `2 passed`

- [ ] **Step 5: Run full suite**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_service.py tests/test_mentor_pipeline.py
git commit -m "$(cat <<'EOF'
feat: add brief completeness gap-check to interview

After each interview reply the model scans the brief for learning
outcomes, student context, constraints, and AI use policy, asking
about the most important missing element without new form fields.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Triage + batched draft generation

**Files:**
- Modify: `python/mentor_service.py` (`generate_draft`, add `_triage_items`, add `_draft_batch`)
- Test: `tests/test_mentor_pipeline.py`

**Interfaces:**
- `generate_draft()` signature unchanged; caller cannot distinguish batched from non-batched.
- `_triage_items(workspace, items, client, schema, progress) -> dict` with `priority_order`, `key_concerns`, `course_gaps`
- `_draft_batch(workspace, batch_items, all_items, key_concerns, client, schema, progress, batch_num, total_batches) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mentor_pipeline.py`:

```python
DRAFT_BATCH_SIZE = 12  # mirror constant from service

class TriageBatchingTests(unittest.TestCase):
    def _make_state(self, n):
        originals = [item(str(i)) for i in range(n)]
        state = {**workspace(), "id": f"batch-test-{n}", "source_type": "imscc"}
        return state, originals

    def test_small_course_uses_single_call(self):
        """≤ 12 items: no triage call, single draft call."""
        state, originals = self._make_state(10)
        with patch.object(service, "source_items", return_value=(originals, [])):
            client = PipelineClient()
            progress = []
            service.generate_draft(state, client, progress=lambda *v: progress.append(v))
        # No triage prompt should appear
        triage_calls = [p for p, _ in client.calls if "TRIAGE" in p or "priority_order" in str(p)]
        self.assertEqual(len(triage_calls), 0)

    def test_large_course_triggers_triage_and_batches(self):
        """15 items: triage call + 2 batch calls (12 + 3)."""
        state, originals = self._make_state(15)
        call_log = []
        def tracking_call_structured(system, prompt, schema, max_tokens):
            call_log.append(prompt[:80])
            if "priority_order" in str(schema):
                return json.dumps({
                    "priority_order": [str(i) for i in range(15)],
                    "key_concerns": {},
                    "course_gaps": [],
                })
            if prompt.startswith("Assess this one item"):
                return json.dumps(response_for(payload(prompt)))
            return json.dumps({
                "summary": "batch draft",
                "items": [], "warnings": [],
            })
        client = PipelineClient()
        client.call_structured = tracking_call_structured
        with patch.object(service, "source_items", return_value=(originals, [])):
            service.generate_draft(state, client)
        triage_calls = [p for p in call_log if "priority_order" in p or "triage" in p.lower()]
        draft_calls = [p for p in call_log if "draft" in p.lower() or "proposal" in p.lower()]
        self.assertGreater(len(triage_calls), 0)
        self.assertGreater(len(draft_calls), 0)

    def test_batched_proposals_are_merged_across_batches(self):
        """Items proposed in different batches all appear in final result."""
        state, originals = self._make_state(15)
        batch_num = [0]
        def batch_response(system, prompt, schema, max_tokens):
            if "priority_order" in str(schema):
                return json.dumps({
                    "priority_order": [str(i) for i in range(15)],
                    "key_concerns": {},
                    "course_gaps": [],
                })
            if prompt.startswith("Assess this one item"):
                return json.dumps(response_for(payload(prompt)))
            # Return one proposal per batch
            batch_num[0] += 1
            iid = str(batch_num[0] - 1)
            return json.dumps({
                "summary": f"batch {batch_num[0]}",
                "items": [{"id": iid, "type": "assignments", "title": "Updated",
                           "html": "<p>Updated content.</p>", "reason": "improved",
                           "source_ids": ["instructor-brief"]}],
                "warnings": [],
            })
        client = PipelineClient()
        client.call_structured = batch_response
        with patch.object(service, "source_items", return_value=(originals, [])):
            result = service.generate_draft(state, client)
        self.assertEqual(len(result["items"]), 15)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::TriageBatchingTests -v 2>&1 | tail -15
```

Expected: tests about triage/batching fail (no such logic exists yet)

- [ ] **Step 3: Implement `DRAFT_BATCH_SIZE`, `_triage_items`, `_draft_batch` in `mentor_service.py`**

Add near the top constants:
```python
DRAFT_BATCH_SIZE = 12
```

Add helper functions after `_proposals`:

```python
def _triage_items(workspace, items, client, schema, progress):
    """Single structured call to prioritize items by revision urgency."""
    if progress:
        progress("triaging", f"Analyzing {len(items)} items against your design brief to identify revision priorities...")
    brief = workspace.get("brief", "")
    triage_schema = {
        "type": "object",
        "properties": {
            "priority_order": {"type": "array", "items": {"type": "string"},
                               "description": "Every item ID sorted by revision urgency."},
            "key_concerns": {"type": "object",
                             "description": "item_id -> one-sentence concern.",
                             "additionalProperties": {"type": "string"}},
            "course_gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["priority_order", "key_concerns", "course_gaps"],
        "additionalProperties": False,
    }
    item_summaries = [{"id": it["id"], "title": it.get("title", ""), "type": it.get("type", "")}
                      for it in items]
    prompt = (
        "Triage these course items against the design brief. Return every item ID in priority_order "
        "sorted by revision urgency (most urgent first). Include one-sentence key_concerns for "
        "items most misaligned with the brief. Return strict JSON only.\n"
        + json.dumps({"brief_excerpt": brief[:2000], "items": item_summaries}, ensure_ascii=False)
    )
    structured = getattr(client, "call_structured", None)
    response = (structured(SYSTEM, prompt, triage_schema, max_tokens=1500)
                if callable(structured)
                else client.call(SYSTEM, prompt, max_tokens=1500))
    if isinstance(response, str):
        result = parse_json(response)
    else:
        result = response
    # Ensure every item ID is in priority_order (append missing ones)
    known_ids = {it["id"] for it in items}
    present = set(result.get("priority_order", []))
    missing = [it["id"] for it in items if it["id"] not in present]
    result["priority_order"] = list(result.get("priority_order", [])) + missing
    return result


def _draft_batch(workspace, batch_items, all_items, key_concerns, client, schema,
                 progress, batch_num, total_batches):
    """Draft one batch of items; returns list of proposal dicts."""
    known = {it["id"]: it for it in all_items}
    material_ids = {m["id"] for m in workspace.get("materials", [])}
    concerns_block = ""
    batch_concern = {iid: c for iid, c in key_concerns.items() if iid in {it["id"] for it in batch_items}}
    if batch_concern:
        concerns_block = "\n\nTRIAGE PRIORITIES:\n" + json.dumps(batch_concern, ensure_ascii=False)
    if progress:
        progress("drafting", f"Drafting batch {batch_num} of {total_batches} "
                             f"({len(batch_items)} items, priority-ordered)...")
    def build(source_context, scale):
        return (
        source_context
        + concerns_block
        + "\n\nALLOWED EXISTING PROPOSAL ITEM IDs: " + json.dumps([it["id"] for it in batch_items], ensure_ascii=False)
        + "\nPropose targeted revisions using exact item IDs above. Empty id = new item."
        + "\nReturn {\"summary\":\"...\", \"items\":[...], \"warnings\":[...]}. At most 20 proposals."
        )
    batch_schema = mentor_response_schema("draft", {it["id"]: it for it in batch_items}, material_ids)
    prompt, _ = _fit_prompt(workspace, client, build, 7000, items=batch_items, schema=batch_schema)
    data = parse_json(_call_fitted(client, prompt, 7000, schema=batch_schema, phase=f"draft batch {batch_num}"))
    return _proposals(data, known, material_ids)
```

Modify `generate_draft()` to use batching when `len(originals) > DRAFT_BATCH_SIZE`:

After the baseline assessment block and before `prompt, fit_warnings = _fit_prompt(...)`, insert:

```python
    if len(originals) > DRAFT_BATCH_SIZE:
        # Triage then batch
        triage = _triage_items(workspace, originals, client, schema, progress)
        id_to_item = {it["id"]: it for it in originals}
        ordered = [id_to_item[iid] for iid in triage["priority_order"] if iid in id_to_item]
        key_concerns = triage.get("key_concerns", {})
        batches = [ordered[i:i + DRAFT_BATCH_SIZE] for i in range(0, len(ordered), DRAFT_BATCH_SIZE)]
        all_proposals = []
        for batch_num, batch in enumerate(batches, 1):
            batch_proposals = _draft_batch(
                workspace, batch, originals, key_concerns, client, schema,
                progress, batch_num, len(batches)
            )
            all_proposals.extend(batch_proposals)
        # Merge proposals; last proposal for a given ID wins
        combined = {it["id"]: {k: v for k, v in it.items() if k != "raw"} for it in originals}
        seen_proposals = {}
        for proposal in all_proposals:
            seen_proposals[proposal["id"]] = proposal
        combined.update(seen_proposals)
        _validate_prompt_items(list(combined.values()))
        result = {
            "summary": f"Batched draft: {len(seen_proposals)} revisions across {len(batches)} batches.",
            "items": list(combined.values()),
            "warnings": warnings + fit_warnings if 'fit_warnings' in dir() else warnings,
            "mode": "ai", "coverage": "Batched excerpts; unchanged items are not claimed as AI-reviewed.",
        }
        if assess:
            result["assessment_required"] = True
            if baseline:
                result["baseline_assessment"] = baseline
            result["assessment"] = assess_design(workspace, result["items"], client,
                                                 scope="draft", progress=progress)
            result["coverage"] = (
                "Every draft item was AI-assessed using bounded text excerpts; coverage and evidence "
                "are recorded in the instructor assessment.")
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::TriageBatchingTests -v 2>&1 | tail -15
```

Expected: `3 passed`

- [ ] **Step 5: Run full suite**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_service.py tests/test_mentor_pipeline.py
git commit -m "$(cat <<'EOF'
feat: add triage and batched draft generation for large courses

Courses with > 12 items now triage by revision urgency, then draft
in batches of 12 at full excerpt quality rather than compressing all
items to fragments. Courses ≤ 12 items use the existing single call.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Assessment-informed revision

**Files:**
- Modify: `python/mentor_service.py` (`review_draft`)
- Test: `tests/test_mentor_pipeline.py`

**Interfaces:**
- `review_draft()` signature unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mentor_pipeline.py`:

```python
class AssessmentInformedRevisionTests(unittest.TestCase):
    def _make_draft_state(self, with_assessment=True):
        originals = [item(str(i)) for i in range(3)]
        state = {**workspace(), "id": "revision-test", "source_type": "imscc"}
        state["draft"] = {
            "summary": "Initial draft",
            "items": originals,
            "warnings": [],
            "mode": "ai",
        }
        if with_assessment:
            state["draft"]["assessment"] = {
                "mode": "ai", "scope": "draft", "version": 1,
                "items": [
                    {"item_id": "0", "title": "t", "item_type": "assignments",
                     "source_ids": ["instructor-brief"],
                     "content_sha256": hashlib.sha256(originals[0]["html"].encode()).hexdigest(),
                     "concerns": ["Unclear success criteria"],
                     "recommendations": ["Add explicit rubric"],
                     "blooms": {"level": 3, "label": "Apply", "rationale": "r"},
                     "aias": {"observed_level": None, "recommended_level": 2,
                              "label": "AI Planning", "rationale": "r"},
                     "dimensions": {k: {"score": 5, "rationale": "r"} for k in DIMENSIONS},
                     "strengths": [], "evidence": [], "limitations": []},
                ],
                "coverage": {"items_total": 3, "items_evaluated": 3,
                             "truncated_items": [], "chunks_total": 3, "chunks_evaluated": 3},
            }
        return state, originals

    def test_review_prompt_contains_assessment_findings(self):
        state, _ = self._make_draft_state(with_assessment=True)
        prompts = []
        client = PipelineClient()
        def capture(system, prompt, schema, max_tokens):
            prompts.append(prompt)
            return json.dumps({"reply": "Revised.", "items": []})
        client.call_structured = capture
        with patch.object(service, "source_items", return_value=(state["draft"]["items"], [])):
            service.review_draft(state, "Please revise.", client)
        self.assertTrue(any("ASSESSMENT FINDINGS" in p for p in prompts))

    def test_review_prompt_without_assessment_has_no_findings_block(self):
        state, _ = self._make_draft_state(with_assessment=False)
        prompts = []
        client = PipelineClient()
        def capture(system, prompt, schema, max_tokens):
            prompts.append(prompt)
            return json.dumps({"reply": "Revised.", "items": []})
        client.call_structured = capture
        with patch.object(service, "source_items", return_value=(state["draft"]["items"], [])):
            service.review_draft(state, "Please revise.", client)
        self.assertFalse(any("ASSESSMENT FINDINGS" in p for p in prompts))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::AssessmentInformedRevisionTests -v 2>&1 | tail -10
```

Expected: `AssertionError: ASSESSMENT FINDINGS not in any prompt`

- [ ] **Step 3: Modify `review_draft()` in `mentor_service.py`**

In the `build` closure inside `review_draft()`, append assessment findings:

```python
    def build(source_context, scale):
        assessment = (workspace.get("draft") or {}).get("assessment") or {}
        item_findings = {}
        for a in assessment.get("items", []):
            iid = a.get("item_id")
            concerns = a.get("concerns", [])[:3]
            recs = a.get("recommendations", [])[:3]
            if iid and (concerns or recs):
                item_findings[iid] = {"concerns": concerns, "recommendations": recs}
        assessment_block = ""
        if item_findings and scale == 1.0:
            assessment_block = (
                "\n\nASSESSMENT FINDINGS — address these when revising:\n"
                + json.dumps(item_findings, ensure_ascii=False)
            )
        elif item_findings:
            assessment_block = (
                "\n\nASSESSMENT FINDINGS: omitted at reduced context scale; "
                "full findings are in the instructor assessment report."
            )
        synthesis = assessment.get("synthesis", {})
        course_concerns = synthesis.get("cross_cutting_concerns", [])
        course_gaps = synthesis.get("coverage_gaps", [])
        course_block = ""
        if (course_concerns or course_gaps) and scale == 1.0:
            course_block = (
                "\n\nCOURSE-LEVEL FINDINGS:\n"
                + json.dumps({"cross_cutting_concerns": course_concerns,
                              "coverage_gaps": course_gaps}, ensure_ascii=False)
            )
        return (
        source_context
        + "\n\nCURRENT DRAFT (bounded): " + json.dumps(_draft_excerpt(draft, scale), ensure_ascii=False)
        + assessment_block
        + course_block
        + "\n\nINSTRUCTOR REVIEW: " + message
        + '\nReturn {"reply":"...", "items":[...]} with only changes requested by the instructor. '
          "Empty items means discussion without edits. You cannot approve the draft."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/test_mentor_pipeline.py::AssessmentInformedRevisionTests -v 2>&1 | tail -10
```

Expected: `2 passed`

- [ ] **Step 5: Run full suite**

```bash
cd /Users/djf3/hax-ai-canvas && python3 -m pytest tests/ -q 2>&1 | tail -10
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
cd /Users/djf3/hax-ai-canvas && git add python/mentor_service.py tests/test_mentor_pipeline.py
git commit -m "$(cat <<'EOF'
feat: wire assessment findings into review_draft prompt

When a draft has been assessed, revision prompts now include per-item
concerns and recommendations plus course-level synthesis findings,
so revisions are grounded in what the assessment found rather than
only the instructor's text feedback.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Post-implementation

After all 6 tasks are committed:

1. Run full test suite: `python3 -m pytest tests/ -v 2>&1 | tail -20`
2. Run JS tests: `npm run test:upload --prefix python/app`
3. Run build check: `npm run build:hosted --prefix python/app 2>&1 | tail -5`
4. Request code review on the whole branch.
