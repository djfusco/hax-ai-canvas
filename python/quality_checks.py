"""Automatic quality checks for the consultant's work (no AI).

The consultation engine already rejects malformed output (unknown topics,
evidence IDs that are not in the course, over-long text). These checks look
for problems that pass validation but make the advice weaker: claims not
grounded in the course itself, statements attributed to an instructor who
never said them, missing topics or AI dimensions, vague outcomes, documents
with accessibility errors, and personal information in AI-written text.

Each check is "pass", "warn" or "fail" with a plain-language detail. They are
signals for a person reviewing the work, not a grade of teaching quality.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

GENERIC_EVIDENCE = {"course-map", "syllabus", "instructor"}
VAGUE_VERBS = {"understand", "know", "learn", "appreciate", "be aware", "be familiar", "grasp", "comprehend",
               "realize", "gain", "become"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
PLACEHOLDER = re.compile(r"\[Instructor to [^\]]*\]", re.I)


def _check(results, name, status, detail):
    results.append({"check": name, "status": status, "detail": detail})


def _text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def consultation_checks(model, state, messages, *, instructor_spoke):
    import consultation
    results = []
    state = state or consultation.new_state()
    topics = state["topics"]
    covered = [t for t in consultation.TOPICS if topics[t]["status"] == "covered"]
    skipped = [t for t in consultation.TOPICS if topics[t]["status"] == "skipped"]
    missing = [consultation.TOPIC_LABELS[t] for t in consultation.TOPICS if t not in covered and t not in skipped]
    _check(results, "All topics discussed", "pass" if not missing else "warn",
           "All six topics are covered or skipped." if not missing else "Not yet covered: " + ", ".join(missing) + ".")

    facts = [f for t in consultation.TOPICS for f in topics[t]["facts"]]
    course_ids = consultation.evidence_ids(model) - GENERIC_EVIDENCE
    grounded = [f for f in facts if set(f.get("evidence", [])) & course_ids]
    if not facts:
        _check(results, "Findings cite the course", "warn", "No findings recorded yet.")
    else:
        share = len(grounded) / len(facts)
        _check(results, "Findings cite the course", "pass" if share >= 0.5 else "warn",
               f"{len(grounded)} of {len(facts)} findings cite specific course items, outcomes or rubrics "
               "(the rest cite only the course map, syllabus or the instructor).")
    thin = [consultation.TOPIC_LABELS[t] for t in covered if not topics[t]["facts"]]
    _check(results, "Covered topics have findings", "pass" if not thin else "warn",
           "Every covered topic records what was learned." if not thin
           else "Covered without any recorded findings: " + ", ".join(thin) + ".")

    if not instructor_spoke:
        invented = [f["text"] for f in facts if "instructor" in f.get("evidence", [])]
        _check(results, "No invented instructor statements", "pass" if not invented else "fail",
               "Nothing is attributed to the instructor, who did not take part." if not invented
               else f"{len(invented)} finding(s) cite the instructor, who did not take part, e.g. “{invented[0][:120]}”.")
        replies = " ".join(m["content"] for m in messages if m.get("role") == "assistant")
        goals = [g for t in consultation.TOPICS for g in topics[t]["goals"]]
        marked = "assum" in replies.lower() or any("assum" in g.lower() for g in goals)
        _check(results, "Assumptions are marked", "pass" if marked else "warn",
               "The advisor marks what it assumed about students and goals." if marked
               else "The advisor answered for the instructor without marking any assumptions.")

    proposals = state.get("proposals", [])
    _check(results, "Proposals made", "pass" if len(proposals) >= 3 else "warn",
           f"{len(proposals)} proposed change(s).")
    if proposals:
        ungrounded = [p["title"] for p in proposals if not set(p.get("evidence", [])) & course_ids]
        _check(results, "Proposals cite the course", "pass" if not ungrounded else "warn",
               "Every proposal points to specific course items." if not ungrounded
               else "Only general evidence for: " + "; ".join(ungrounded[:5]) + ".")
        no_principle = [p["title"] for p in proposals if not (p.get("principle") or "").strip()]
        _check(results, "Proposals name a principle", "pass" if not no_principle else "warn",
               "Every proposal names the teaching principle behind it." if not no_principle
               else "No principle named for: " + "; ".join(no_principle[:5]) + ".")
        dimensions = {p.get("ai_dimension") for p in proposals} - {"none", None}
        absent = [d.replace("_", " ") for d in consultation.AI_DIMENSIONS if d != "none" and d not in dimensions]
        _check(results, "AI dimensions considered", "pass" if not absent else "warn",
               "Proposals cover all four AI dimensions." if not absent
               else "No proposal addresses: " + ", ".join(absent) + ".")
        titles = [p["title"].strip().lower() for p in proposals]
        repeats = sorted({t for t in titles if titles.count(t) > 1})
        _check(results, "No duplicate proposals", "pass" if not repeats else "warn",
               "Each proposal is distinct." if not repeats else "Repeated: " + "; ".join(repeats[:5]) + ".")
    return results


def outcome_text(outcome):
    """Plan outcomes are {"text", "note"} objects (older plans: plain strings)."""
    return outcome.get("text", "") if isinstance(outcome, dict) else str(outcome)


def plan_checks(plan):
    results = []
    plan = plan or {}
    _check(results, "Plan has a vision", "pass" if (plan.get("vision") or "").strip() else "warn",
           "The plan states a course vision." if (plan.get("vision") or "").strip() else "The plan has no vision yet.")
    outcomes = [t for t in (outcome_text(o) for o in plan.get("outcomes") or []) if t.strip()]
    if not outcomes:
        _check(results, "Outcomes are measurable", "warn", "The plan lists no outcomes yet.")
    else:
        vague = [o for o in outcomes if any(re.search(rf"\b{v}\b", o.lower()) for v in VAGUE_VERBS)]
        _check(results, "Outcomes are measurable", "pass" if not vague else "warn",
               f"All {len(outcomes)} outcomes use observable verbs." if not vague
               else f"{len(vague)} of {len(outcomes)} outcomes use verbs that are hard to assess "
                    f"(such as “understand”), e.g. “{vague[0][:120]}”.")
    policy = (plan.get("ai_policy") or "").strip()
    _check(results, "Student AI policy", "pass" if len(policy) >= 80 else "warn",
           "The plan includes a student AI policy." if len(policy) >= 80
           else "The student AI policy is missing or very short.")
    return results


def document_checks(documents, state):
    import accessibility
    results = []
    approved = [d for d in documents or [] if d.get("status") == "approved"]
    _check(results, "Documents approved", "pass" if approved else "warn",
           f"{len(approved)} approved of {len(documents or [])} document(s).")
    accepted = {p["id"] for p in (state or {}).get("proposals", []) if p.get("status") == "accepted"}
    errors, placeholders, stray, empty = [], 0, [], []
    for document in documents or []:
        text = _text(document.get("html"))
        if len(text) < 40:
            empty.append(document["title"])
        issues = accessibility.check_html(document.get("html"))
        if any(accessibility.RULES[rule][0] == "error" for rule in issues):
            errors.append(document["title"])
        placeholders += len(PLACEHOLDER.findall(text))
        stray += [p for p in document.get("applied_proposals", []) if p not in accepted]
    if documents:
        _check(results, "Documents have content", "pass" if not empty else "fail",
               "Every document has substantial content." if not empty else "Nearly empty: " + "; ".join(empty) + ".")
        _check(results, "Documents pass accessibility errors", "pass" if not errors else "warn",
               "No accessibility errors in the documents." if not errors
               else "Accessibility errors in: " + "; ".join(errors[:5]) + ".")
        _check(results, "Documents apply accepted changes only", "pass" if not stray else "warn",
               "Every applied change was accepted." if not stray
               else f"{len(set(stray))} applied change(s) were not accepted (they may have been rejected later).")
        _check(results, "Placeholders for the instructor", "pass" if not placeholders else "warn",
               "No placeholders left." if not placeholders
               else f"{placeholders} “[Instructor to …]” placeholder(s) need the instructor's input.")
    return results


def privacy_checks(messages, plan, documents):
    texts = [m["content"] for m in messages or [] if m.get("role") == "assistant"]
    texts += [str(v) for k, v in (plan or {}).items() if k in ("vision", "ai_policy", "outcomes", "open_questions")]
    texts += [_text(d.get("html")) for d in documents or []]
    blob = "\n".join(texts)
    found = len(EMAIL.findall(blob)) + len(PHONE.findall(blob))
    results = []
    _check(results, "No personal contact details in AI text", "pass" if not found else "warn",
           "No email addresses or phone numbers in AI-written text." if not found
           else f"{found} email address(es) or phone number(s) appear; check they are the instructor's own.")
    return results


def evaluate(model, state, messages, plan, documents):
    """All automatic checks, grouped, with a summary."""
    consult_messages = [m for m in messages or [] if m.get("kind") == "consult"]
    instructor_spoke = any(m.get("role") == "user" for m in consult_messages)
    groups = [
        ("Consultation", consultation_checks(model, state, consult_messages, instructor_spoke=instructor_spoke)),
        ("Plan", plan_checks(plan)),
        ("Documents", document_checks(documents, state)),
        ("Privacy", privacy_checks(consult_messages, plan, documents)),
    ]
    flat = [c for _, checks in groups for c in checks]
    return {
        "summary": {s: sum(1 for c in flat if c["status"] == s) for s in ("pass", "warn", "fail")},
        "groups": [{"name": name, "checks": checks} for name, checks in groups],
        "note": "Automatic signals for a person to review; they cannot judge whether advice is wise or accurate.",
    }
