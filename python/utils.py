"""
Shared utilities for the Canvas-to-HAX ETL pipeline.

Contains helpers used across multiple phases that don't belong
to any single pipeline stage.
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, Comment


# Regex to extract file_id from Canvas URLs like /courses/12345/files/67890
_FILE_ID_RE = re.compile(r'courses/\d+/files/(\d+)')

# Regex to extract page slug from Canvas page URLs
# e.g. "https://foo.instructure.com/courses/12345/pages/my-page-slug"
_ABS_PAGE_RE = re.compile(r'instructure\.com/courses/\d+/pages/([^?#/]+)')
# e.g. "$WIKI_REFERENCE$/pages/my-page-slug"
_WIKI_PAGE_RE = re.compile(r'^\$WIKI_REFERENCE\$/pages/([^?#/]+)')
# e.g. "$CANVAS_COURSE_REFERENCE$/pages/my-page-slug"
_CANVAS_REF_PAGE_RE = re.compile(r'^\$CANVAS_COURSE_REFERENCE\$/pages/([^?#/]+)')
# Fabricated LLM placeholder tokens in href attrs, e.g. "[LINK_TO_CANVAS_PAGE]"
# or "/courses/[COURSE_ID]/pages/[PAGE_ID]". Any ALL_CAPS bracketed segment
# in an href is almost certainly an LLM hallucination.
_PLACEHOLDER_HREF_RE = re.compile(r'\[[A-Z_]+\]')
# data-api-endpoint shape used to recover an absolute Canvas file URL when
# the local export doesn't have the file (e.g. cross-course references).
_API_FILE_ENDPOINT_RE = re.compile(
    r'(https?)://([^/]+)/api/v1/courses/(\d+)/files/(\d+)'
)

# Signals that an <a> tag is a Canvas file link (any one is sufficient)
_CANVAS_LINK_SIGNALS = [
    lambda tag: 'instructure_file_link' in (tag.get('class') or []),
    lambda tag: bool(_FILE_ID_RE.search(tag.get('href', ''))),
    lambda tag: tag.get('href', '').startswith('$CANVAS_COURSE_REFERENCE$'),
]

# Canvas-specific attributes to strip after rewriting
_CANVAS_ATTRS = ['data-api-endpoint', 'data-api-returntype', 'data-canvas-previewable']


def _sanitize_filename(name):
    """
    Strip unsafe characters from a filename.

    Must match the sanitization in load.py's copy_media_files exactly,
    so that lookups here find files that were already copied to disk.
    """
    return ''.join(c for c in name if c.isalnum() or c in ('.', '_', '-'))


def build_file_id_map(export_dir):
    """
    Read all .json metadata files in the export's files/ directory
    and return a mapping of {file_id_str: on_disk_filename}.

    Uses the 'filename' field (not 'display_name') because that matches
    what copy_media_files sees when it rglobs the export directory.
    """
    export_dir = Path(export_dir)
    file_id_map = {}

    for json_path in (export_dir / "files").glob("*.json"):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            # 'filename' is the on-disk name that copy_media_files will sanitize
            file_id_map[str(meta["id"])] = meta["filename"]
        except (json.JSONDecodeError, KeyError):
            continue  # Skip malformed metadata

    return file_id_map


def _extract_page_slug(href):
    """Return the Canvas page slug from an <a> href, or None if not a page link.

    Returns (slug, kind) where kind is one of:
      - "wiki"        — $WIKI_REFERENCE$/pages/<slug>
      - "canvas_ref"  — $CANVAS_COURSE_REFERENCE$/pages/<slug>
      - "absolute"    — https://*.instructure.com/courses/<id>/pages/<slug>
    """
    if not href:
        return None
    m = _WIKI_PAGE_RE.match(href)
    if m:
        return m.group(1), "wiki"
    m = _CANVAS_REF_PAGE_RE.match(href)
    if m:
        return m.group(1), "canvas_ref"
    m = _ABS_PAGE_RE.search(href)
    if m:
        return m.group(1), "absolute"
    return None


def _replace_with_span(soup, tag, title_text, comment_text):
    """Replace <a> with <span>, preserving anchor text + adding a debug comment."""
    comment = Comment(f" {comment_text} ")
    span = soup.new_tag("span", title=title_text)
    span.string = tag.get_text()
    tag.insert_before(comment)
    tag.replace_with(span)


def rewrite_canvas_links(html, file_id_map, files_dir, slug_to_location=None):
    """
    Rewrite Canvas-authenticated file and page links so they work in a mirrored
    HAX site without poisoning the SPA router.

    File link resolution priority:
      1. file_id from href (covers absolute instructure.com URLs)
      2. file_id from data-api-endpoint attr
      3. title attr as direct filename ($CANVAS_COURSE_REFERENCE$ fallback)
      4. data-api-endpoint → reconstruct an absolute Canvas URL
         (target=_blank so the click escapes the HAX SPA)
      5. Unresolved $CANVAS_COURSE_REFERENCE$ → replace <a> with <span>

    Page link handling (when slug_to_location is provided):
      - Matches instructure.com/courses/*/pages/<slug>,
        $WIKI_REFERENCE$/pages/<slug>, and $CANVAS_COURSE_REFERENCE$/pages/<slug>
      - In-map slug → rewrite href to the HAX page location
      - Unresolved absolute Canvas URL → left unchanged (resolves back to Canvas)
      - Unresolved $WIKI_REFERENCE$ / $CANVAS_COURSE_REFERENCE$ → replaced with
        <span title="..."> (a live href to either token would hijack HAX routing)

    Defensive handling (always on):
      - Empty / missing href → replace <a> with <span> (LLM frequently emits
        <a title="...">text</a> with no href, which renders as dead styled text)
      - Bracketed ALL_CAPS placeholder hrefs (e.g. [LINK_TO_CANVAS_PAGE]) → span
      - Bare href="#" → span (LLM fallback pattern; clicking routes to HAX home)

    All unresolved / neutralized cases get an HTML comment annotation for debugging.
    """
    if not html:
        return html

    soup = BeautifulSoup(html, 'html.parser')
    files_dir = Path(files_dir)
    slug_to_location = slug_to_location or {}
    modified = False

    for tag in list(soup.find_all('a')):
        href = tag.get('href', '')

        # --- Empty / missing href: LLM produces <a title="...">text</a> with
        # no href; renders as dead styled text. Surface as a span. ---
        if not href or not href.strip():
            _replace_with_span(
                soup, tag,
                title_text="Original link not available in mirror",
                comment_text="Anchor with empty/missing href neutralized",
            )
            modified = True
            continue

        # --- LLM-fabricated placeholder handling (checked first — these look like nothing else) ---
        # Bare href="#" (no fragment target) is also an LLM fallback pattern — clicking
        # it routes to the HAX welcome page. Anchor-to-top links (legitimate "#") are
        # rare here and converting them to <span> is a tolerable regression.
        if _PLACEHOLDER_HREF_RE.search(href) or href.strip() == '#':
            _replace_with_span(
                soup, tag,
                title_text="Original page link not available in mirror",
                comment_text=f"LLM-fabricated placeholder: {href}",
            )
            modified = True
            continue

        # --- Page link handling (checked first — page links never look like file links) ---
        page_match = _extract_page_slug(href)
        if page_match:
            slug, kind = page_match
            hax_location = slug_to_location.get(slug)
            if hax_location:
                tag['href'] = hax_location
                modified = True
            elif kind in ("wiki", "canvas_ref"):
                # Bare Canvas token href would hijack HAX routing — replace.
                _replace_with_span(
                    soup, tag,
                    title_text="Original page not available in mirror",
                    comment_text=f"Canvas page link unresolved: {href}",
                )
                modified = True
            else:
                # Absolute Canvas URL — leave href, still resolves back to Canvas
                tag.insert_before(Comment(f" Canvas page link unresolved: {href} "))
                modified = True
            continue  # Page link handled; don't fall through to file-link logic

        # --- File link handling ---
        if not any(signal(tag) for signal in _CANVAS_LINK_SIGNALS):
            continue

        resolved_path = None

        # Priority 1: extract file_id from the href itself
        match = _FILE_ID_RE.search(href)

        # Priority 2: fall back to data-api-endpoint attribute
        api_endpoint = tag.get('data-api-endpoint', '')
        if not match:
            match = _FILE_ID_RE.search(api_endpoint)

        if match:
            file_id = match.group(1)
            filename = file_id_map.get(file_id)
            if filename:
                safe_name = _sanitize_filename(filename)
                candidate = files_dir / safe_name
                if candidate.exists():
                    resolved_path = f"files/{safe_name}"

        # Priority 3: use title attr as direct filename lookup
        if not resolved_path:
            title = tag.get('title', '')
            if title:
                safe_title = _sanitize_filename(title)
                candidate = files_dir / safe_title
                if candidate.exists():
                    resolved_path = f"files/{safe_title}"

        if resolved_path:
            tag['href'] = resolved_path
            modified = True
        else:
            # Priority 4: reconstruct an absolute Canvas URL from data-api-endpoint
            # so the link still works (opens Canvas in a new tab) instead of
            # hijacking HAX routing.
            api_match = _API_FILE_ENDPOINT_RE.search(api_endpoint)
            if api_match:
                scheme, host, cid, fid = api_match.groups()
                tag['href'] = f"{scheme}://{host}/courses/{cid}/files/{fid}/download"
                tag['target'] = '_blank'
                tag['rel'] = 'noopener'
                tag.insert_before(Comment(
                    f" Canvas file link unresolved → fallback to Canvas: {href} "
                ))
                modified = True
            elif href.startswith('$CANVAS_COURSE_REFERENCE$'):
                # No usable absolute URL and the bare token would hijack routing.
                _replace_with_span(
                    soup, tag,
                    title_text="Original file not available in mirror",
                    comment_text=f"Canvas file link unresolved: {href}",
                )
                modified = True

        # Strip Canvas-specific attributes regardless of resolution
        for attr in _CANVAS_ATTRS:
            if tag.has_attr(attr):
                del tag[attr]
                modified = True

    return str(soup) if modified else html
