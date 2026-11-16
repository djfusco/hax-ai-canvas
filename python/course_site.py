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


def _latest_revision(documents, item_id):
    """The most recently approved assignment or page revision of an item."""
    matches = [d for d in documents or [] if d["status"] == "approved" and d["kind"] in ("assignment", "page")
               and d.get("item_id") == item_id]
    return max(matches, key=lambda d: d.get("updated_at") or d.get("created_at") or "", default=None)


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
        revised = _latest_revision(documents, item["id"])
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


def course_items(model, workspace, *, students_only=False):
    """The course as students and instructors will see it: the AI-built course when one has been
    built, otherwise the original course with approved revisions in place.

    ``students_only`` leaves out everything meant for the instructor. Use it for anything that
    leaves the instructor's own signed-in view (a published or downloaded site, a shared link).
    """
    import course_build
    documents = workspace.get("documents") or []
    built = course_build.site_items(workspace.get("build"), model, documents, workspace.get("consultation"),
                                    workspace.get("design"))
    items = built if built is not None else build_items(model, documents)
    return student_items(model, items) if students_only else items


def student_items(model, items):
    """Only what students may see: no instructor section, answer keys or unpublished items.

    A built course marks each page's audience. The original course has no such marking, so its
    items are checked the same way the build decides (title or Canvas state).
    """
    import course_build
    by_id = {i["id"]: i for i in model["items"]}
    hidden = course_build.instructor_only_ids(model)
    kept = []
    for item in items:
        if item.get("audience") == "instructor":
            continue
        source = item["id"][:-len("--rubric")] if item["id"].endswith("--rubric") else item["id"]
        if "audience" not in item and source in by_id and source in hidden:
            continue
        kept.append(item)
    return kept


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


def slugs(items):
    """Readable, unique page addresses for the HAX site, keyed by item id ("week-1-cells").

    HAX keeps the slug it is given as the page's web address, so it should say what the page is.
    ``x`` is avoided: HAX reserves the ``x/`` routes.
    """
    import re
    result, seen = {}, set()
    for item in items:
        base = re.sub(r"[^a-z0-9]+", "-", (item.get("title") or "").lower()).strip("-")[:60].strip("-")
        if not base or base == "x":
            base = "page"
        slug, number = base, 1
        while slug in seen:
            number += 1
            slug = f"{base}-{number}"
        seen.add(slug)
        result[item["id"]] = slug
    return result


def render(title, items, path, stored=None):
    """HTML for one preview path ('index.html' or a page location), or None.

    This preview runs no JavaScript, so HAX elements are shown in their plain form. ``stored``
    (export path -> file name) names the images kept with the course; they are served beside
    the preview at ``files/<name>``.
    """
    import course_files
    import hax_elements
    from mentor_preview import render_preview
    stored = stored or {}
    from imscc_content import sanitize_import_html
    # Sanitize first: that is where an embedded video becomes a HAX element, which then gets its plain form.
    items = [dict(item, html=course_files.resolve(hax_elements.to_plain(sanitize_import_html(item["html"])),
                                                  stored, "files/")) for item in items]
    places = locations(items)
    assets = {"files/" + name for name in stored.values()}
    workspace = {"title": title, "draft": {"items": items}}
    if path in ("", "index.html"):
        return render_preview(workspace, places, assets)
    for position, item in enumerate(items):
        if places[item["id"]] == path:
            return render_preview(workspace, places, assets, position=position)
    return None


def package(workspace_id, title, items, destination, theme_element=None, files=(), stored=None):
    """The downloadable course site: a static HAX site plus an offline plain preview.

    Top level: HAX's site shell (index.html, config.js), a HAX-format site.json
    and one page fragment per item; served from any static host it renders
    with a HAX theme. preview/: plain pages that open offline with a double-click.
    """
    import json
    import zipfile
    from pathlib import Path
    import hax_preview
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    places = locations(items)
    images, stored = list(files), stored or {}
    files = {
        "index.html": hax_preview.index_html(title, "./"),
        "config.js": hax_preview.config_js(),
        "site.json": json.dumps(hax_preview.site_json(workspace_id, title, items, places, theme_element), indent=2),
        "preview/index.html": render(title, items, "index.html"),
        "README.txt": (
            f"{title}: course site from Course Consultant\n\n"
            "Quick look (works offline): open preview/index.html in your browser.\n\n"
            "HAX theme: put this folder on any static web host (GitHub Pages, Netlify, your web server), or run\n"
            "  python3 -m http.server 8000\n"
            "in this folder and open http://localhost:8000/ . The site loads HAX's components from cdn.hax.cloud.\n\n"
            "To keep editing in HAX: create a HAX site and import site.json and the pages/ folder.\n"),
    }
    for item in items:
        slug = places[item["id"]].split("/")[1]
        files[f"pages/{slug}/index.html"] = hax_preview.page_html(item, slugs(items), stored)
        files[f"preview/{places[item['id']]}"] = render(title, items, places[item["id"]])
    package_path = destination.with_suffix(".zip")
    with zipfile.ZipFile(package_path, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
        for name, _, data in images:  # HAX's files/ folder: the images the pages show
            archive.writestr("files/" + name, data, zipfile.ZIP_STORED)
    return {"path": str(package_path), "items": len(items), "kind": "course-site"}
