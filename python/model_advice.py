"""Which models suit Course Consultant: a short, explained suggestion.

Course Consultant needs a model that follows structured instructions, reads
a whole course (large input), writes long answers (a full syllabus) and
answers within a minute or two per step. Mid-to-large "workhorse" models do
this best. Very large or "thinking" models are slower and costlier per step
and can run out of room; very small models are fast but give thinner advice.

The advice is a starting point based on model families and catalog limits,
not a test of each model.
"""

from __future__ import annotations

import re

# Families that do this job well (checked against the model ID, in order of preference).
RECOMMENDED = (
    ("claude-sonnet", "Strong, well-grounded advice at moderate speed and cost."),
    ("claude-3.7-sonnet", "Strong, well-grounded advice at moderate speed and cost."),
    ("claude-3-7-sonnet", "Strong, well-grounded advice at moderate speed and cost."),
    ("gpt-4.1", "Reliable structured answers, fast, large context."),
    ("gpt-4o", "Reliable structured answers and fast."),
    ("gemini-2.5-pro", "Very large context; good for big courses."),
    ("gemini-2.5-flash", "Fast and inexpensive, with a very large context."),
    ("gemini-3", "Very large context; good for big courses."),
)
SLOW = re.compile(r"opus|(^|[/-])o[134](-|$)|gpt-5(?!-mini|-nano|\.\d-mini)|deepseek-r1|thinking|reason", re.I)
SMALL = re.compile(r"nano|haiku|flash-lite|(^|[-/])mini|[-_]\d{1,2}b\b|tiny|small", re.I)

MIN_INPUT = 64_000
MIN_OUTPUT = 8_000


def advise(model_id, capabilities=None):
    """{"tier": "recommended"|"ok"|"slow"|"small"|"limited", "note": str} for one model."""
    lower = model_id.lower()
    caps = capabilities or {}
    input_limit, output_limit = caps.get("input_limit"), caps.get("output_limit")
    if (isinstance(input_limit, int) and input_limit < MIN_INPUT) or (
            isinstance(output_limit, int) and output_limit < MIN_OUTPUT):
        return {"tier": "limited", "note": "Small limits: it may not fit a large course or a full syllabus."}
    slow = {"tier": "slow", "note": "Thinks before answering: slower and costlier per step, and can run out "
                                    "of room on large courses. Fine for single questions; not ideal for the "
                                    "quick AI version."}
    if SLOW.search(lower):
        return slow
    if SMALL.search(lower):
        return {"tier": "small", "note": "Fast and inexpensive, but advice and documents may be thinner."}
    for family, note in RECOMMENDED:
        if family in lower:
            return {"tier": "recommended", "note": note}
    # Catalogs mark many models as "supports reasoning" when it is optional, so the
    # flag only decides for families not named above.
    if caps.get("reasoning") is True:
        return slow
    return {"tier": "ok", "note": "Not specifically tested with Course Consultant."}


def annotate(models, limit=4):
    """Add advice to each catalog row; return the IDs to suggest first (newest of each family)."""
    for row in models:
        row["advice"] = advise(row["id"], row.get("capabilities"))
    suggested, families = [], set()
    for family, _ in RECOMMENDED:
        for row in models:  # catalogs list newer models first
            lower = row["id"].lower()
            if (family in lower and row["advice"]["tier"] == "recommended" and family not in families
                    and ":free" not in lower and "preview" not in lower):
                suggested.append(row["id"])
                families.add(family)
                break
        if len(suggested) >= limit:
            break
    return suggested
