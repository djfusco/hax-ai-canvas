"""Automatic accessibility checks for course content (no AI).

Checks what students would see: approved revised versions where they exist,
otherwise the original course items. Rules map to WCAG 2.1 success criteria.
Automatic checks find common, objective problems; they cannot judge
everything (for example whether an image description is accurate), and the
report says so.
"""

from __future__ import annotations

import posixpath
import re

from bs4 import BeautifulSoup

GENERIC_LINK_TEXT = {"click here", "here", "click", "read more", "more", "link", "this link", "learn more",
                     "this", "go", "download", "website", "page", "this page", "info", "details"}
FILENAME = re.compile(r"^[\w\-. ]+\.(jpe?g|png|gif|webp|svg|bmp|tiff?)$", re.I)
URL_TEXT = re.compile(r"^(https?://|www\.)\S+$", re.I)

RULES = {
    "image-alt-missing": ("error", "1.1.1", "Image has no description (alt text)."),
    "image-alt-filename": ("warning", "1.1.1", "Image description is a file name, not a description."),
    "linked-image-no-text": ("error", "2.4.4", "Linked image has no description, so the link has no name."),
    "link-empty": ("error", "2.4.4", "Link has no text."),
    "link-generic": ("warning", "2.4.4", "Link text does not say where it goes (e.g. “click here”)."),
    "link-url-text": ("warning", "2.4.4", "Link text is a raw web address."),
    "heading-skip": ("warning", "1.3.1", "Heading levels skip (e.g. a level 2 heading followed by level 4)."),
    "table-no-headers": ("warning", "1.3.1", "Table has no header cells."),
    "frame-no-title": ("warning", "4.1.2", "Embedded frame has no title."),
    "media-no-captions": ("warning", "1.2.2", "Video or audio has no captions track."),
    "course-video-no-captions": ("warning", "1.2.2", "Course video has no caption file alongside it."),
}
LIMITATIONS = ("Automatic checks find common problems; they cannot confirm that descriptions are accurate, "
               "that colour contrast is sufficient in your theme, or that videos hosted elsewhere have captions. "
               "Review with your institution's accessibility guidance.")


def check_html(html):
    """Issues in one piece of HTML: {rule: count}."""
    soup = BeautifulSoup(html or "", "html.parser")
    found = {}

    def hit(rule, n=1):
        found[rule] = found.get(rule, 0) + n

    for image in soup.find_all("img"):
        inside_link = image.find_parent("a") is not None
        if not image.has_attr("alt"):
            hit("linked-image-no-text" if inside_link else "image-alt-missing")
        elif FILENAME.match(image["alt"].strip()):
            hit("image-alt-filename")
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        label = text or link.get("aria-label", "").strip() or " ".join(
            i.get("alt", "").strip() for i in link.find_all("img"))
        if not label.strip():
            if not link.find("img"):
                hit("link-empty")
        elif label.lower().strip(" .:!") in GENERIC_LINK_TEXT:
            hit("link-generic")
        elif URL_TEXT.match(label):
            hit("link-url-text")
    previous = None
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        level = int(heading.name[1])
        if previous is not None and level > previous + 1:
            hit("heading-skip")
        previous = level
    for table in soup.find_all("table"):
        if len(table.find_all("tr")) > 1 and not table.find("th"):
            hit("table-no-headers")
    for frame in soup.find_all("iframe"):
        if not frame.get("title", "").strip():
            hit("frame-no-title")
    for media in soup.find_all(["video", "audio"]):
        if not media.find("track", kind=re.compile("captions|subtitles", re.I)):
            hit("media-no-captions")
    return found


def course_report(model, documents, workspace=None):
    """Accessibility report for the whole course site (the built course when there is one)."""
    import course_site
    items = (course_site.course_items(model, workspace) if workspace is not None
             else course_site.build_items(model, documents))
    results, totals = [], {}
    for item in items:
        if item["id"].startswith("module--") and "audience" not in item:
            continue  # the plain site's generated module lists (built courses' section pages are checked)
        issues = check_html(item["html"])
        for rule, count in issues.items():
            totals[rule] = totals.get(rule, 0) + count
        if issues:
            results.append({
                "id": item["id"], "title": item["title"], "revised": item["revised"],
                "fixable": item["id"] == "syllabus" or any(i["id"] == item["id"] for i in model["items"]),
                "issues": [{"rule": rule, "count": count, "severity": RULES[rule][0], "wcag": RULES[rule][1],
                            "message": RULES[rule][2]} for rule, count in sorted(issues.items())],
            })
    captions = {posixpath.splitext(m["path"])[0].lower() for m in model["media_inventory"]
                if posixpath.splitext(m["path"])[1].lower() in (".vtt", ".srt")}
    videos = [m for m in model["media_inventory"]
              if m.get("mime", "").startswith("video/") and posixpath.splitext(m["path"])[0].lower() not in captions]
    if videos:
        totals["course-video-no-captions"] = len(videos)
    severity_rank = {"error": 0, "warning": 1}
    results.sort(key=lambda r: (min(severity_rank[i["severity"]] for i in r["issues"]), r["title"]))
    return {
        "items_checked": sum(1 for i in items if not i["id"].startswith("module--") or "audience" in i),
        "items_with_issues": len(results), "totals": totals,
        "errors": sum(c for rule, c in totals.items() if RULES[rule][0] == "error"),
        "warnings": sum(c for rule, c in totals.items() if RULES[rule][0] == "warning"),
        "items": results,
        "videos_without_captions": [posixpath.basename(m["path"]) for m in videos][:50],
        "rules": {rule: {"severity": v[0], "wcag": v[1], "message": v[2]} for rule, v in RULES.items()},
        "limitations": LIMITATIONS,
    }


def fix_instruction(entry):
    """The instruction used for 'Fix with AI' on one item."""
    problems = "; ".join(f"{i['message']} ({i['count']}x, WCAG {i['wcag']})" for i in entry["issues"])
    return ("Fix these accessibility problems and change nothing else: " + problems +
            " Write image descriptions from the surrounding context; if you cannot tell what an image shows, "
            "write [Instructor to describe: image of ...]. Keep every link destination.")
