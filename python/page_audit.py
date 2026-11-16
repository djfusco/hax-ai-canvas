"""Checks on the course's pages, following HAX's own page audits (no AI).

PRAW (https://github.com/haxtheweb/praw) audits authored HAX pages with a set of skills. This
module applies the parts of three of them that a rule can decide, to the pages students will see:

- ``hax-content-chunking-audit``: walls of text, long runs without a heading, lists that are too
  long. PRAW's thresholds, with the actual number reported so a person can judge.
- ``hax-instructional-events-audit``: Gagné's events for each week. A week that presents content
  but never asks the learner to do anything is "Presentational", PRAW's "common failure".
- ``hax-udl-audit``: whether students can respond (Action & Expression, the gap PRAW finds most
  often in HAX courses), see a goal and reflect (Engagement), and meet more than text
  (Representation).

Every finding names a HAX element that fixes it, from the verified list in ``hax_elements``.
What a rule cannot decide (whether an image teaches or decorates, how long a video is, whether
a worked example is a good one) is listed as not checked.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

WALL_OF_TEXT = 200      # words in one paragraph
HEADING_GAP = 400       # words between headings
LIST_FATIGUE = 7        # items in one list
RESPONSE = ("multiple-choice", "true-false-question", "self-check")
EVENTS = {
    1: "Gain attention", 2: "State the objective", 3: "Recall earlier learning", 4: "Present the content",
    6: "Ask the learner to do something", 7: "Give feedback", 8: "Assess", 9: "Help it last and transfer",
}
NOT_CHECKED = [
    "Whether pictures and video teach or only decorate, and whether a video is too long without a pause "
    "(media is not read).",
    "Learning guidance (worked examples, cues on what to notice): event 5.",
    "Whether a long paragraph is one coherent idea or several: the word counts are given so you can judge.",
]


def _words(text):
    return len(re.findall(r"\w+", text or ""))


def chunking(html):
    """Chunking findings for one page: [{"rule", "detail", "fix"}]."""
    soup = BeautifulSoup(html or "", "html.parser")
    for inside in soup.find_all(list(RESPONSE) + ["stop-note", "a11y-collapse", "table"]):
        inside.decompose()  # their text is already set apart or revealed on request
    findings = []
    longest = max((_words(p.get_text(" ")) for p in soup.find_all("p")), default=0)
    walls = sum(1 for p in soup.find_all("p") if _words(p.get_text(" ")) > WALL_OF_TEXT)
    if walls:
        findings.append({"rule": "wall-of-text",
                         "detail": f"{walls} paragraph{'s' if walls != 1 else ''} over {WALL_OF_TEXT} words "
                                   f"(the longest is {longest}).",
                         "fix": "Split at a natural break and put a self-check there, so students stop and recall."})
    run, worst = 0, 0
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        if node.name.startswith("h"):
            run = 0
        elif not node.find_parent(["li"]) or node.name == "li":
            run += _words(node.get_text(" "))
            worst = max(worst, run)
    if worst > HEADING_GAP:
        findings.append({"rule": "heading-gap", "detail": f"A run of {worst} words with no heading.",
                         "fix": "Add a subheading that says what the section is for, and a stop-note or self-check."})
    lists = [len(l.find_all("li", recursive=False)) for l in soup.find_all(["ul", "ol"])]
    long_lists = [n for n in lists if n > LIST_FATIGUE]
    if long_lists:
        findings.append({"rule": "list-fatigue",
                         "detail": f"{len(long_lists)} list{'s' if len(long_lists) != 1 else ''} with more than "
                                   f"{LIST_FATIGUE} items (the longest has {max(long_lists)}).",
                         "fix": "Group the items under a few headings, or put the detail in a collapsible section."})
    return findings


def _has(soup, names):
    return soup.find(list(names)) is not None


def week_events(page_html, item_htmls, graded, has_later_review):
    """Gagné's events for one week: {event number: True/False}.

    ``page_html`` is the week page as students see it (with any review from earlier weeks),
    ``item_htmls`` the week's other pages, ``graded`` whether the week has graded work, and
    ``has_later_review`` whether this week's questions come back later in the course.
    """
    week = BeautifulSoup(page_html or "", "html.parser")
    everything = BeautifulSoup((page_html or "") + "".join(item_htmls), "html.parser")
    opening = next((n for n in week.find_all(True) if n.name not in ("ul", "li", "oer-schema")), None)
    questions = everything.find_all(list(RESPONSE))
    feedback = any(q.find(attrs={"slot": "feedbackCorrect"}) or q.find(attrs={"slot": "feedbackIncorrect"})
                   or (q.name == "self-check" and q.find(lambda t: t.name == "p" and not t.get("slot")))
                   for q in questions)
    return {
        1: bool(opening is not None and opening.name in ("stop-note", "video-player", "img")) or _has(week, ["video-player"]),
        2: _has(week, ["oer-schema"]) or bool(re.search(r"(?i)you will be able to|objectives", week.get_text(" "))),
        3: "Review from earlier weeks" in week.get_text(" "),
        4: _words(everything.get_text(" ")) > 60,
        6: bool(questions),
        7: feedback,
        8: bool(graded),
        9: bool(has_later_review),
    }


def events_rating(events):
    if all(events.values()):
        return "Complete"
    if not any(events[n] for n in (6, 7, 8, 9)):
        return "Presentational"
    return "Partial"


def udl(html_parts):
    """UDL's three principles for one week: {"action": bool, "engagement": bool, "representation": bool}."""
    soup = BeautifulSoup("".join(html_parts), "html.parser")
    return {
        "action": _has(soup, RESPONSE),
        "engagement": _has(soup, ["oer-schema", "stop-note"]) and _has(soup, ["self-check", "stop-note"]),
        "representation": _has(soup, ["img", "video-player", "video", "audio", "table"]),
    }


def audit(items, model, build=None):
    """The page check for a course site (``course_site.course_items(..., students_only=True)``)."""
    pages = []
    for item in items:
        findings = chunking(item["html"])
        if findings:
            pages.append({"id": item["id"], "title": item["title"], "findings": findings})
    weeks = []
    blueprint = (build or {}).get("blueprint")
    if blueprint:
        by_id = {i["id"]: i for i in items}
        built = {p["id"]: p for p in (build or {}).get("pages", [])}
        numbers = [w["number"] for w in blueprint["weeks"]]
        review_page = "module--review" in by_id
        for week in blueprint["weeks"]:
            page = by_id.get(f"module--week-{week['number']}")
            if page is None or f"week-{week['number']}" not in built:
                continue
            others = [by_id[i]["html"] for i in week["items"] if i in by_id]
            has_practice = bool(built[f"week-{week['number']}"].get("practice_blocks"))
            later = has_practice and (review_page or any(n in numbers for n in (week["number"] + 1, week["number"] + 3)))
            events = week_events(page["html"], others, any(i in model["assignments"] for i in week["items"]), later)
            missing = [EVENTS[n] for n, present in events.items() if not present]
            weeks.append({"number": week["number"], "title": page["title"], "rating": events_rating(events),
                          "missing": missing, "udl": udl([page["html"], *others])})
    summary = {
        "pages_checked": len(items), "pages_with_findings": len(pages),
        "weeks": len(weeks),
        "presentational": sum(1 for w in weeks if w["rating"] == "Presentational"),
        "complete": sum(1 for w in weeks if w["rating"] == "Complete"),
        "no_response": sum(1 for w in weeks if not w["udl"]["action"]),
        "text_only": sum(1 for w in weeks if not w["udl"]["representation"]),
    }
    if not weeks:
        overall = "Build the course to check each week against the events of instruction."
    elif summary["no_response"] == len(weeks):
        overall = ("Single-path: in no week are students asked to respond. They read, and nothing checks or "
                   "strengthens what they took in.")
    elif summary["presentational"] or summary["no_response"]:
        overall = (f"{summary['no_response']} of {len(weeks)} weeks never ask students to respond; "
                   f"{summary['complete']} follow the full sequence of instruction.")
    else:
        overall = f"Every week asks students to respond; {summary['complete']} of {len(weeks)} follow the full sequence."
    return {"summary": summary, "overall": overall, "pages": pages, "weeks": weeks, "not_checked": NOT_CHECKED,
            "fixes": {"no_response": "Rebuild the week so practice questions are written from its pages, or add a "
                                     "self-check by hand.",
                      "text_only": "Add a picture, diagram or video where it explains something words cannot."},
            "source": "HAX's page audits in PRAW: content chunking, instructional events (Gagné), "
                      "Universal Design for Learning."}
