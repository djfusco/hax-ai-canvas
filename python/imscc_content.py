"""HTML and resource handling for normalized local Canvas imports."""

from __future__ import annotations

import html
import json
import posixpath
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

ITEM_PREFIX = "$IMSCC_ITEM_REFERENCE$/"
CONTENT_FIELDS = ("body", "description", "message", "question_text", "text")
URL_ATTRIBUTES = ("href", "src", "poster")
PASSIVE_TAGS = set(
    "html head body title p div span section article header footer main nav aside "
    "h1 h2 h3 h4 h5 h6 a img audio video source track br hr strong em b i u s "
    "small sub sup code pre blockquote ul ol li dl dt dd table caption thead "
    "tbody tfoot tr th td figure figcaption details summary abbr time mark".split()
)


def sanitize_import_html(content: str) -> str:
    """Remove active content before rendering uploaded or model-generated HTML."""
    soup = BeautifulSoup(content or "", "html.parser")
    for node in list(soup.find_all(
        ["script", "iframe", "object", "embed", "base", "link", "meta", "style",
         "form", "svg", "math", "template"]
    )):
        if node.parent is not None:
            node.decompose()
    for node in list(soup.find_all(True)):
        if node.name not in PASSIVE_TAGS:
            node.unwrap()
            continue
        for attr in list(node.attrs):
            if attr.lower() not in (
                "href", "src", "poster", "id", "class", "title", "alt", "width",
                "height", "controls", "preload", "type", "colspan", "rowspan",
                "scope", "start", "reversed", "open", "lang", "dir", "kind",
                "srclang", "label",
            ):
                del node[attr]
            elif attr.lower() in URL_ATTRIBUTES:
                value = str(node[attr]).strip()
                # Browsers ignore ASCII controls inside dangerous URL schemes.
                compact = "".join(c for c in value if ord(c) > 32)
                scheme = urlsplit(compact).scheme.lower()
                if scheme and scheme not in ("http", "https", "mailto", "tel"):
                    del node[attr]
    return str(soup.body.decode_contents()) if soup.body else str(soup)


def prepare_import_content(export_dir: Path) -> list[str]:
    """Resolve manifest paths to generated local targets; never fetch a URL."""
    summary_path = export_dir / "export_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    resources = summary.get("resource_paths", {})
    warnings = []
    source_ids = {}
    for folder in ("pages", "assignments", "quizzes", "discussions", "syllabus"):
        for path in (export_dir / folder).glob("*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            for alias in (raw.get("source_identifier"), raw.get("url"),
                          Path(raw.get("source_path", "")).stem):
                if alias:
                    source_ids[(folder, alias)] = raw["id"]

    def resolve(value: str, source_path: str) -> str:
        value = html.unescape(value).strip()
        if not value or value.startswith("#"):
            return value
        parsed = urlsplit(value)
        if parsed.scheme or value.startswith("//"):
            return value
        path = unquote(parsed.path)
        if path.startswith(("$WIKI_REFERENCE$/pages/", "$CANVAS_COURSE_REFERENCE$/pages/")):
            ident = path.rsplit("/", 1)[-1]
            item_id = source_ids.get(("pages", ident))
            if item_id:
                return ITEM_PREFIX + item_id + ("#" + parsed.fragment if parsed.fragment else "")
        candidates = [path.lstrip("/")]
        if path.startswith("$IMS-CC-FILEBASE$"):
            suffix = path[len("$IMS-CC-FILEBASE$"):].lstrip("/")
            candidates = ["web_resources/" + suffix, suffix]
        elif path.startswith("$CANVAS_OBJECT_REFERENCE$/"):
            parts = path.split("/")
            if len(parts) >= 3:
                kind = {"wiki_pages": "pages", "discussion_topics": "discussions"}.get(parts[-2], parts[-2])
                item_id = source_ids.get((kind, parts[-1]))
                if item_id:
                    return ITEM_PREFIX + item_id + ("#" + parsed.fragment if parsed.fragment else "")
        else:
            candidates.insert(0, posixpath.normpath(
                posixpath.join(posixpath.dirname(source_path), path)
            ))
        for candidate in candidates:
            resource = resources.get(candidate)
            if resource:
                fragment = "#" + parsed.fragment if parsed.fragment else ""
                if resource["kind"] == "file":
                    return "files/" + resource["target"] + fragment
                return ITEM_PREFIX + resource["id"] + fragment
        warnings.append(f"Unresolved course resource: {value[:160]}")
        # Leave no dangling archive placeholders or browser-local filesystem paths.
        return ""

    def rewrite(content: str, source_path: str) -> str:
        soup = BeautifulSoup(sanitize_import_html(content), "html.parser")
        for tag in soup.find_all(True):
            for attr in URL_ATTRIBUTES:
                if tag.has_attr(attr):
                    result = resolve(tag[attr], source_path)
                    if result:
                        tag[attr] = result
                    else:
                        del tag[attr]
        return str(soup)

    for folder in ("pages", "assignments", "quizzes", "discussions", "syllabus"):
        for path in (export_dir / folder).glob("*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["source_type"] = "imscc"
            for field in ("title", "name"):
                if isinstance(raw.get(field), str):
                    raw[field] = (
                        BeautifulSoup(raw[field], "html.parser").get_text()
                        .replace("<", "").replace(">", "")
                    ) or "Untitled"
            source_path = raw.get("source_path", "")
            for field in CONTENT_FIELDS:
                if isinstance(raw.get(field), str):
                    raw[field] = rewrite(raw[field], source_path)
            for question in raw.get("questions", []):
                for field in ("question_text", "text"):
                    if isinstance(question.get(field), str):
                        question[field] = rewrite(question[field], source_path)
                for answer in question.get("answers", []):
                    if isinstance(answer.get("text"), str):
                        answer["text"] = sanitize_import_html(answer["text"])
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["warnings"] = list(dict.fromkeys(summary.get("warnings", []) + warnings))
    summary["course_name"] = (
        BeautifulSoup(summary["course_name"], "html.parser").get_text()
        .replace("<", "").replace(">", "")
    ) or "Imported Canvas course"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return list(dict.fromkeys(warnings))


def original_import_html(raw: dict) -> str:
    """Keep original HTML and question prompts in no-AI mode, without truncation."""
    title = html.escape(str(raw.get("title") or raw.get("name") or "Untitled"))
    body = raw.get("body") or raw.get("description") or raw.get("message") or ""
    questions = ""
    for question in raw.get("questions", []):
        choices = "".join(
            f"<li>{answer.get('text', '')}</li>"
            for answer in question.get("answers", []) if answer.get("text")
        )
        questions += (
            f"<li>{question.get('question_text', question.get('text', ''))}"
            + (f"<ul>{choices}</ul>" if choices else "") + "</li>"
        )
    return sanitize_import_html(
        f"<h1>{title}</h1>{body}" + (f"<ol>{questions}</ol>" if questions else "")
    )


def preserve_import_resources(raw: dict, enhanced_html: str) -> str:
    """Keep source links/media available when the LLM receives a text preview."""
    enhanced_html = sanitize_import_html(enhanced_html)
    original = BeautifulSoup(original_import_html(raw), "html.parser")
    enhanced = BeautifulSoup(enhanced_html, "html.parser")
    present = {str(tag.get(attr)) for tag in enhanced.find_all(True)
               for attr in URL_ATTRIBUTES if tag.has_attr(attr)}
    resources = []
    for tag in original.find_all(["a", "img", "audio", "video"]):
        if tag.find_parent(["a", "audio", "video"]):
            continue
        urls = {str(node.get(attr)) for node in [tag, *tag.find_all(True)]
                for attr in URL_ATTRIBUTES if node.has_attr(attr)}
        if urls and not urls.issubset(present):
            resources.append(str(tag))
            present.update(urls)
    if resources:
        enhanced_html += (
            "<section><h2>Original course resources</h2>"
            + "\n".join(resources) + "</section>"
        )
    return enhanced_html


def rewrite_import_item_links(content: str, item_locations: dict[str, str]) -> str:
    """Resolve normalized cross-item tokens after final page locations exist."""
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup.find_all(True):
        for attr in URL_ATTRIBUTES:
            value = str(tag.get(attr, ""))
            if value.startswith(ITEM_PREFIX):
                item_id, sep, fragment = value[len(ITEM_PREFIX):].partition("#")
                location = item_locations.get(item_id)
                if location:
                    tag[attr] = location + (sep + fragment if sep else "")
                else:
                    del tag[attr]
    return str(soup)
