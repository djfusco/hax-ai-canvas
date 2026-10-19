"""Read-only student-view presentation, not a HAX runtime or public publisher."""
import base64
import hashlib
from html import escape
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from imscc_content import sanitize_import_html, rewrite_import_item_links, URL_ATTRIBUTES

PREVIEW_CSS = """
* { box-sizing: border-box; }
html { color-scheme: light; }
body { margin: 0; background: #f4f7fb; color: #233044; font: 17px/1.65 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: #1256a0; text-underline-offset: .18em; overflow-wrap: anywhere; }
a:hover { color: #09396b; }
a:focus-visible, summary:focus-visible { outline: 3px solid #d27400; outline-offset: 4px; }
.preview-banner { background: #142e50; color: #fff; padding: 12px 24px; text-align: center; font-size: .85rem; }
.preview-header { padding: 28px 32px; border-bottom: 1px solid #dce4ee; background: #fff; }
.preview-brand { display: block; max-width: 1200px; margin: auto; color: #142e50; font-weight: 750; font-size: 1.45rem; text-decoration: none; }
.preview-layout { display: grid; grid-template-columns: minmax(190px, 260px) minmax(0, 1fr); gap: 32px; max-width: 1264px; margin: auto; padding: 32px; align-items: start; }
.preview-sidebar { position: sticky; top: 24px; border: 1px solid #dce4ee; border-radius: 12px; padding: 18px; background: #fff; min-width: 0; }
.preview-sidebar summary { cursor: pointer; font-weight: 700; }
.preview-sidebar ol { padding: 0; list-style: none; margin: 14px 0 0; max-height: calc(100vh - 260px); overflow: auto; }
.preview-sidebar li { margin: 3px 0; }
.preview-sidebar a { display: block; padding: 8px 10px; color: #36465c; border-radius: 6px; text-decoration: none; font-size: .9rem; }
.preview-sidebar a:hover { background: #edf4fc; }
.preview-sidebar a[aria-current="page"] { color: #0b4b89; background: #e5effc; border-left: 3px solid #1256a0; font-weight: 700; }
.preview-main { min-width: 0; background: #fff; border: 1px solid #dce4ee; border-radius: 14px; padding: 32px; }
.preview-kicker { margin: 0 0 8px; color: #53647a; font-size: .82rem; font-weight: 650; letter-spacing: .03em; text-transform: uppercase; }
.preview-main h1 { color: #142e50; font-size: clamp(1.6rem, 4vw, 2.25rem); line-height: 1.25; margin: 0 0 24px; overflow-wrap: anywhere; }
.approved-content { max-width: 78ch; overflow-wrap: anywhere; }
.approved-content h2, .approved-content h3, .approved-content h4 { color: #183b63; line-height: 1.35; margin-top: 1.7em; }
.approved-content p, .approved-content ul, .approved-content ol { margin-bottom: 1.1em; }
.approved-content img, .approved-content video { max-width: 100%; height: auto; border-radius: 8px; }
.approved-content audio { max-width: 100%; }
.approved-content table { display: block; max-width: 100%; overflow: auto; border-collapse: collapse; }
.approved-content th, .approved-content td { padding: 10px 14px; border: 1px solid #dce4ee; text-align: left; }
.approved-content th { background: #edf4fc; }
.approved-content blockquote { margin-left: 0; padding: 12px 20px; border-left: 4px solid #a7c3e6; background: #f4f7fb; }
.approved-content pre { overflow: auto; background: #f1f4f8; padding: 16px; border-radius: 8px; }
.approved-content code { font-family: ui-monospace, monospace; font-size: .9em; }
.preview-item-list { list-style: none; padding: 0; display: grid; gap: 12px; }
.preview-item-list a { display: block; border: 1px solid #dce4ee; border-radius: 10px; padding: 16px 20px; text-decoration: none; font-weight: 650; }
.preview-item-list a:hover { background: #f4f8ff; border-color: #9dbbe0; }
.preview-item-list small { display: block; color: #53647a; font-weight: 400; margin-top: 4px; }
.preview-pagination { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 16px; margin-top: 36px; padding-top: 22px; border-top: 1px solid #dce4ee; }
.preview-pagination a { max-width: 100%; font-weight: 650; }
.preview-note { background: #fff8e6; border-left: 3px solid #bd7900; padding: 12px 16px; margin: 20px 0; font-size: .9rem; }
.preview-footer { margin-top: 32px; color: #596a80; font-size: .8rem; }
.preview-skip { position: absolute; left: -10000px; top: 8px; }
.preview-skip:focus { left: 8px; padding: 10px; background: #fff; color: #142e50; z-index: 2; }
@media (max-width: 760px) {
  .preview-header { padding: 20px; }
  .preview-layout { display: block; padding: 16px; }
  .preview-sidebar { position: static; margin-bottom: 16px; padding: 14px; }
  .preview-sidebar ol { max-height: 180px; }
  .preview-main { padding: 24px 20px; }
  .preview-pagination { flex-direction: column; }
}
"""
PREVIEW_STYLE_HASH = "sha256-" + base64.b64encode(hashlib.sha256(PREVIEW_CSS.encode()).digest()).decode()
TYPE_LABELS = {"pages": "Page", "assignments": "Assignment", "discussions": "Discussion",
               "quizzes": "Quiz reference", "syllabus": "Syllabus"}


def document_policy(style_hash=None):
    # Hashes are server-generated build metadata, never arbitrary CSP directives.
    if not isinstance(style_hash, str) or not re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", style_hash):
        style_hash = PREVIEW_STYLE_HASH
    return ("default-src 'none'; script-src 'none'; style-src '" + style_hash +
            "'; img-src 'self'; media-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; connect-src 'none'")


def response_policy(style_hash=None):
    return "sandbox allow-same-origin allow-downloads; " + document_policy(style_hash)


def preview_content(content, locations, assets, prefix):
    """Use preview-to-preview links without a <base> that breaks #anchors.

    Only registered page/asset paths remain relative links. External hyperlinks
    are explicit user navigation; remote media is not fetched in this preview.
    """
    soup = BeautifulSoup(rewrite_import_item_links(sanitize_import_html(content), locations), "html.parser")
    allowed = set(locations.values()) | set(assets)
    unavailable = False
    for tag in soup.find_all(True):
        for attr in URL_ATTRIBUTES:
            if not tag.has_attr(attr):
                continue
            value = str(tag[attr])
            if value.startswith("#"):
                continue
            try:
                parsed = urlsplit(value)
                if parsed.scheme or parsed.netloc:
                    if attr == "href":
                        if parsed.netloc and not parsed.scheme:
                            tag[attr] = "https:" + value
                        tag["rel"] = ["noopener", "noreferrer"]
                    else:
                        del tag[attr]
                        unavailable = True
                    continue
                path = posixpath.normpath(unquote(parsed.path).lstrip("/"))
                if "\\" in path or path not in allowed:
                    del tag[attr]
                    unavailable = True
                    continue
                tag[attr] = urlunsplit(("", "", prefix + quote(path, safe="/"), parsed.query, parsed.fragment))
            except ValueError:
                del tag[attr]
                unavailable = True
    return str(soup), unavailable


def delivery_headers(filename, style_hash=None):
    headers = {"Content-Security-Policy": response_policy(style_hash)}
    suffix = posixpath.splitext(filename)[1].lower()
    if suffix not in (".html", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
                      ".mp3", ".mp4", ".m4a", ".m4v", ".wav", ".ogg", ".webm", ".mov", ".vtt"):
        # Do not execute or embed retained documents/stylesheet files as pages.
        headers["Content-Disposition"] = "attachment"
    return headers


def render_preview(workspace, locations, assets, *, position=None):
    items = workspace["draft"]["items"]
    prefix = "" if position is None else "../../"
    current = items[position] if position is not None else None
    course_title = escape(workspace["title"])
    title = escape(current["title"]) if current else course_title
    navigation = [f'<li><a href="{prefix}index.html"' +
                  (' aria-current="page"' if current is None else '') + '>Course overview</a></li>']
    for number, item in enumerate(items, 1):
        active = ' aria-current="page"' if current and current["id"] == item["id"] else ""
        navigation.append(f'<li><a href="{prefix}{locations[item["id"]]}"{active}>'
                          f'{number}. {escape(item["title"])}</a></li>')
    if current is None:
        cards = "".join(f'<li><a href="{locations[item["id"]]}">{escape(item["title"])}'
                        f'<small>{escape(TYPE_LABELS.get(item.get("type"), "Course item"))}</small></a></li>'
                        for item in items)
        body = (f'<p class="preview-kicker">{len(items)} approved course items</p><h1>{course_title}</h1>'
                '<p>Explore the approved course content using the navigation or choose an item below.</p>'
                f'<ol class="preview-item-list">{cards}</ol>')
    else:
        content, unavailable = preview_content(current["html"], locations, assets, prefix)
        # Avoid displaying the source's identical title twice; exported content
        # fragments remain unchanged. This is presentation only.
        soup = BeautifulSoup(content, "html.parser")
        first = soup.find(True)
        if first and first.name == "h1" and first.get_text(" ", strip=True) == current["title"].strip():
            first.decompose()
        previous = '<a href="../../index.html" rel="prev">Previous: Course overview</a>' if position == 0 else (
            f'<a href="{prefix}{locations[items[position - 1]["id"]]}" rel="prev">'
            f'Previous: {escape(items[position - 1]["title"])}</a>')
        following = (f'<a href="{prefix}{locations[items[position + 1]["id"]]}" rel="next">'
                     f'Next: {escape(items[position + 1]["title"])}</a>') if position + 1 < len(items) else (
                         '<a href="../../index.html">Back to course overview</a>')
        notice = ('<p class="preview-note">Some linked resources were not retained, or are hosted externally. '
                  'They are not loaded in this private preview.</p>') if unavailable else ""
        body = (f'<p class="preview-kicker">Item {position + 1} of {len(items)} · '
                f'{escape(TYPE_LABELS.get(current.get("type"), "Course item"))}</p><h1>{title}</h1>'
                f'<article class="approved-content">{soup}</article>{notice}'
                f'<nav class="preview-pagination" aria-label="Previous and next items">{previous}{following}</nav>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow">'
            f'<meta http-equiv="Content-Security-Policy" content="{escape(document_policy(), quote=True)}">'
            f'<title>{title} — Private course preview</title><style>{PREVIEW_CSS}</style></head><body>'
            '<a class="preview-skip" href="#preview-main">Skip to course content</a>'
            '<div class="preview-banner"><strong>Private student-view preview — not published</strong></div>'
            f'<header class="preview-header"><a class="preview-brand" href="{prefix}index.html">{course_title}</a></header>'
            '<div class="preview-layout"><aside class="preview-sidebar"><details open>'
            '<summary>Course contents</summary><nav aria-label="Course contents"><ol>'
            + "".join(navigation) + '</ol></nav></details></aside><main id="preview-main" class="preview-main">'
            + body + '<footer class="preview-footer">Approved instructional content preview. '
            'Only retained resources are available. This is not the HAX editor or a restored Canvas LMS.</footer>'
            '</main></div></body></html>')
