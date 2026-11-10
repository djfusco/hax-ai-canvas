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
OUTPUT = {"consult": 1200, "plan": 1800, "document": 2500, "rubric": 1500}
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


def estimate(action, *, model, course_map, consult_state, history, plan, count=1, capabilities=None, pricing=None):
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
