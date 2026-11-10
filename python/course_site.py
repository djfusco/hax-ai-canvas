"""The whole course as a site: revised versions where approved, originals elsewhere.

Used for the in-app preview (stable page addresses, rendered on request) and
for the downloadable HAX-compatible package. Pages follow the course's module
order, starting with the syllabus. Quizzes show their instructions and
question count only, never questions or answers.
"""

from __future__ import annotations

from html import escape
import hashlib

from course_map import html_text

TYPE = {"page": "pages", "assignment": "assignments", "quiz": "quizzes", "discussion": "discussions",
        "external_link": "pages", "external_tool": "pages"}


def _approved(documents, kind, item_id=None):
    """The most recently approved document of a kind (for an item), if any."""
    matches = [d for d in documents or [] if d["status"] == "approved" and d["kind"] == kind
               and (item_id is None or d.get("item_id") == item_id)]
    return matches[-1] if matches else None


def _details(model, item):
    details = model["assignments"].get(item["id"])
    if not details:
        return ""
    parts = []
    if details.get("points") is not None:
        parts.append(f"{details['points']:g} points")
    if details.get("due_at"):
        parts.append("due " + escape(details["due_at"][:10]))
    return f"<p><em>{' · '.join(parts)}</em></p>" if parts else ""


def _original(model, item):
    if item["kind"] in ("external_link", "external_tool"):
        url = item.get("url") or ""
        link = f'<a href="{escape(url, quote=True)}">{escape(item["title"])}</a>' if url.startswith(("http://", "https://")) else escape(item["title"])
        kind = "External tool" if item["kind"] == "external_tool" else "External link"
        return f"<p>{kind}: {link}</p>"
    html = item.get("html") or ""
    if item["kind"] == "quiz":
        quiz = model["quizzes"].get(item["id"]) or {}
        count = len(quiz.get("questions", []))
        html += f"<p><em>Quiz with {count} question{'s' if count != 1 else ''}, taken in Canvas.</em></p>"
    if not html_text(html)[0]:
        html += "<p><em>No text content.</em></p>"
    return _details(model, item) + html


def build_items(model, documents):
    """Ordered site items: [{id, title, type, html, revised, module}]."""
    items, used = [], set()

    def add(ident, title, kind, html, revised=False, module=None):
        items.append({"id": ident, "title": title or "Untitled", "type": kind, "html": html,
                      "revised": revised, "module": module})

    syllabus = _approved(documents, "syllabus")
    if syllabus:
        add("syllabus", syllabus["title"], "syllabus", syllabus["html"], True)
    elif model["course"].get("syllabus_html", "").strip():
        add("syllabus", "Syllabus", "syllabus", model["course"]["syllabus_html"])
    by_id = {i["id"]: i for i in model["items"]}
    modules = sorted(model["modules"], key=lambda m: (m.get("position") is None, m.get("position") or 0))

    def add_item(item, module):
        if item["id"] in used:
            return
        used.add(item["id"])
        revised = _approved(documents, "assignment", item["id"])
        if revised:
            add(item["id"], revised["title"], TYPE[item["kind"]], _details(model, item) + revised["html"], True, module)
        else:
            add(item["id"], item["title"], TYPE[item["kind"]], _original(model, item), False, module)
        rubric = _approved(documents, "rubric", item["id"])
        if rubric:
            add(f"{item['id']}--rubric", rubric["title"], "pages", rubric["html"], True, module)

    for module in modules:
        members = [by_id[i] for i in module["items"] if i in by_id]
        if not members:
            continue
        listing = "".join(f"<li>{escape(m['title'])}</li>" for m in members)
        add(f"module--{module['id']}", module["title"] or "Module", "pages",
            f"<p>In this module:</p><ol>{listing}</ol>", False, module["title"])
        for member in members:
            add_item(member, module["title"])
    loose = [i for i in model["items"] if i["id"] not in used]
    if loose:
        add("module--other", "Other course content", "pages",
            "<ol>" + "".join(f"<li>{escape(i['title'])}</li>" for i in loose) + "</ol>", False, "Other")
        for item in loose:
            add_item(item, "Other")
    return items


def locations(items):
    """Stable page addresses (same across requests), keyed by item id."""
    result, seen = {}, set()
    for item in items:
        slug = hashlib.sha256(item["id"].encode()).hexdigest()[:12]
        while slug in seen:
            slug = hashlib.sha256(slug.encode()).hexdigest()[:12]
        seen.add(slug)
        result[item["id"]] = f"pages/{slug}/preview.html"
    return result


def render(title, items, path):
    """HTML for one preview path ('index.html' or a page location), or None."""
    from mentor_preview import render_preview
    places = locations(items)
    workspace = {"title": title, "draft": {"items": items}}
    if path in ("", "index.html"):
        return render_preview(workspace, places, set())
    for position, item in enumerate(items):
        if places[item["id"]] == path:
            return render_preview(workspace, places, set(), position=position)
    return None


def package(workspace_id, title, items, destination):
    """The downloadable HAX-compatible package (static site + site.json)."""
    from mentor_service import build_artifact
    workspace = {"id": workspace_id, "title": title, "revision": 1, "draft_revision": 1, "export_dir": None,
                 "draft": {"items": [{k: i[k] for k in ("id", "title", "type", "html")} for i in items],
                           "warnings": [], "mode": "ai"}}
    return build_artifact(workspace, destination)
