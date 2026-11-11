"""Estimate the AI requests, tokens and (when prices are known) cost of an action.

Input sizes come from the same prompt builders the actions use, so estimates
track the real course. Token counts use about 4 characters per token, a
common rule of thumb; output allowances are typical sizes for each step.
Prices are shown only when the provider's catalog publishes them
(OpenRouter); other providers' prices depend on the instructor's plan.
"""

from __future__ import annotations

import json

CHARS_PER_TOKEN = 4
OUTPUT = {"consult": 1200, "plan": 1800, "document": 2500, "rubric": 1500,
          "blueprint": 2500, "front": 1500, "week": 1200, "pages": 4500}
REASONING_EXTRA = 2000  # thinking tokens a reasoning model may add per request (upper estimate)
QUICK_MAX_DOCUMENTS = 13


def _tokens(*texts):
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + 50


def _consult_input(state, history, model, course_map):
    import consultation
    prompt = consultation._build_prompt(state, history, model, course_map, "auto", None, 1.0)
    schema = json.dumps(consultation._schema(consultation.evidence_ids(model)))
    return _tokens(consultation.SYSTEM, prompt, schema)


def _plan_input(model, course_map, state):
    import improvement_plan
    return _tokens(improvement_plan.SYSTEM, improvement_plan._prompt(model, course_map, state))


def _document_input(model, course_map, state, plan):
    import revised_documents
    return _tokens(revised_documents.SYSTEM, revised_documents._prompt("syllabus", model, course_map, state, plan, None))


def estimate(action, *, model, course_map, consult_state, history, plan, count=1, capabilities=None, pricing=None,
             documents=None, build=None):
    import consultation
    state = consult_state or consultation.new_state()
    reasoning = bool((capabilities or {}).get("reasoning"))
    steps = []  # (label, requests_min, requests_max, input_each, output_each)
    if action == "quick":
        remaining = sum(1 for t in consultation.TOPICS if state["topics"][t]["status"] not in ("covered", "skipped"))
        consult_in = _consult_input(state, history, model, course_map)
        steps.append(("Advisor works through the topics", remaining, remaining, consult_in, OUTPUT["consult"]))
        steps.append(("Draft the plan", 1, 1, _plan_input(model, course_map, state), OUTPUT["plan"]))
        steps.append(("Write revised documents", 1, QUICK_MAX_DOCUMENTS,
                      _document_input(model, course_map, state, plan), OUTPUT["document"]))
    elif action == "turn":
        steps.append(("Consultation reply", 1, 1, _consult_input(state, history, model, course_map), OUTPUT["consult"]))
    elif action == "plan":
        steps.append(("Draft the plan", 1, 1, _plan_input(model, course_map, state), OUTPUT["plan"]))
    elif action in ("document", "bulk"):
        n = max(1, min(int(count), 500))
        steps.append(("Write documents", n, n, _document_input(model, course_map, state, plan), OUTPUT["document"]))
    elif action in ("build_essential", "build_full"):
        steps += _build_steps(action == "build_full", model, state, plan, documents or [], build)
    else:
        raise ValueError("Unknown action to estimate.")
    extra = REASONING_EXTRA if reasoning else 0
    low = {"requests": 0, "input": 0, "output": 0}
    high = {"requests": 0, "input": 0, "output": 0}
    for _, rmin, rmax, inp, out in steps:
        low["requests"] += rmin
        high["requests"] += rmax
        low["input"] += rmin * inp
        high["input"] += rmax * inp
        low["output"] += rmin * out
        high["output"] += rmax * (out + extra)
    result = {
        "action": action,
        "requests": [low["requests"], high["requests"]],
        "input_tokens": [low["input"], high["input"]],
        "output_tokens": [low["output"], high["output"]],
        "steps": [{"label": label, "requests": [rmin, rmax]} for label, rmin, rmax, _, _ in steps],
        "cost_usd": None,
        "note": "Approximate. Token counts use about 4 characters per token.",
    }
    per_in = (pricing or {}).get("input_per_million")
    per_out = (pricing or {}).get("output_per_million")
    if isinstance(per_in, (int, float)) and isinstance(per_out, (int, float)):
        result["cost_usd"] = [round((low["input"] * per_in + low["output"] * per_out) / 1e6, 4),
                              round((high["input"] * per_in + high["output"] * per_out) / 1e6, 4)]
    else:
        result["note"] += " Your provider's price per token depends on your plan; check its pricing page."
    return result


def _build_steps(full, model, state, plan, documents, build):
    """Building the course: blueprint, front pages, one request per week, and (full) pages in threes."""
    import course_build as cb
    header = len(cb._course_header(model, plan))
    blueprint_in = _tokens(cb.SYSTEM, cb._blueprint_prompt(model, state, plan, documents))
    rewritable = [i["id"] for i in model["items"] if cb._rewritable(model, documents, i["id"])]
    page_chars = sum(len(cb._item_html(model, documents, i)) for i in model["items"] if i["id"] in rewritable)
    if build and build.get("blueprint"):
        weeks = len(build["blueprint"]["weeks"])
        low_weeks = high_weeks = weeks
        chunks = sum(-(-len([i for i in w["items"] if i in rewritable]) // cb.ITEMS_PER_CALL)
                     for w in build["blueprint"]["weeks"])
        low_chunks = high_chunks = chunks
        need_blueprint = 0
    else:
        low_weeks, high_weeks = 10, 16
        low_chunks = -(-len(rewritable) // cb.ITEMS_PER_CALL)
        high_chunks = low_chunks + high_weeks
        need_blueprint = 1
    result = []
    if need_blueprint:
        result.append(("Propose the week-by-week structure", 1, 1, blueprint_in, OUTPUT["blueprint"]))
    syllabus = len(model["course"].get("syllabus_html") or "")
    result.append(("Home, Start here and instructor guide", 3, 3,
                   _tokens(cb.SYSTEM) + (header + min(syllabus, 20000) + 4000) // CHARS_PER_TOKEN, OUTPUT["front"]))
    result.append(("Weekly overviews and instructor notes", low_weeks, high_weeks,
                   _tokens(cb.SYSTEM) + (header + 4000) // CHARS_PER_TOKEN, OUTPUT["week"]))
    if full and rewritable:
        per_chunk = (header + page_chars // max(1, low_chunks) + 1500) // CHARS_PER_TOKEN + _tokens(cb.SYSTEM)
        result.append(("Rewrite course pages", low_chunks, high_chunks, per_chunk, OUTPUT["pages"]))
    return result
