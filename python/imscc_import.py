"""Local, network-free Common Cartridge normalization.

Archive names are never used as filesystem destinations. Quiz extraction is
deliberately text-oriented, not a promise of assessment or grading fidelity.
"""

import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import tempfile
from urllib.parse import unquote, urlsplit
import uuid
import zipfile
import zlib

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError


MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_XML_DEPTH = 128
MAX_COMPRESSION_RATIO = 200
CHUNK_BYTES = 64 * 1024
ASSET_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".mp3", ".mp4", ".m4a", ".m4v", ".wav", ".ogg", ".webm", ".mov",
    ".txt", ".csv", ".tsv", ".css", ".vtt", ".srt", ".doc", ".docx",
    ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".odp", ".ods", ".rtf",
}


class IMSCCImportError(ValueError):
    """The supplied cartridge cannot be safely imported."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _nodes(root, name):
    return [node for node in root.iter() if _local(node.tag) == name]


def _text(root, name, default=""):
    nodes = _nodes(root, name)
    return "".join(nodes[0].itertext()).strip() if nodes else default


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "untitled"


class _HTMLMetadata(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.title = ""
        self.in_title = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.metadata[attrs.get("name", "")] = attrs.get("content", "")
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


class _Cartridge:
    def __init__(self, archive, output, course_id):
        self.archive = archive
        self.output = output
        self.course_id = course_id
        self.entries = {}
        self.warnings = []
        self.paths = {}
        self.resources = {}
        self.resource_items = {}
        self.seen = {}
        self.consumed = set()
        self.counts = dict.fromkeys(
            ("pages", "assignments", "quizzes", "discussions", "files", "modules", "syllabus"), 0
        )

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def inspect(self):
        infos = self.archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise IMSCCImportError("Archive contains too many entries.")
        total = 0
        names = set()
        for info in infos:
            name = info.filename
            parts = name.rstrip("/").split("/")
            if (not name or "\x00" in info.orig_filename or "\\" in name
                    or name.startswith("/") or any(p in ("", ".", "..") for p in parts)
                    or any(":" in p for p in parts)
                    or any(ord(c) < 32 for c in name)):
                raise IMSCCImportError("Unsafe archive path.")
            if name.rstrip("/").casefold() in names:
                raise IMSCCImportError("Duplicate or case-colliding archive path.")
            names.add(name.rstrip("/").casefold())
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise IMSCCImportError("Archive links or special files are not allowed.")
            if info.flag_bits & 1:
                raise IMSCCImportError("Encrypted archive entries are not supported.")
            if info.file_size > MAX_ENTRY_BYTES:
                raise IMSCCImportError("Archive entry exceeds the size limit.")
            total += info.file_size
            if total > MAX_EXPANDED_BYTES:
                raise IMSCCImportError("Archive expansion exceeds the size limit.")
            # Tiny compressed files can have high ratios without posing a bomb risk.
            if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
                raise IMSCCImportError("Archive compression ratio exceeds the safety limit.")
            if not info.is_dir():
                self.entries[name] = info
        if "imsmanifest.xml" not in self.entries:
            raise IMSCCImportError("Archive is missing imsmanifest.xml.")
        # Validate all XML, including unknown metadata, without retaining trees.
        for name in self.entries:
            if name.lower().endswith(".xml"):
                self.xml(name)

    def chunks(self, name, limit):
        info = self.entries[name]
        if info.file_size > limit:
            raise IMSCCImportError("Archive content exceeds its size limit.")
        total = 0
        with self.archive.open(info) as stream:
            while True:
                chunk = stream.read(min(CHUNK_BYTES, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise IMSCCImportError("Archive content exceeds its size limit.")
                yield chunk

    def read(self, name):
        return b"".join(self.chunks(name, MAX_ENTRY_BYTES)).decode("utf-8-sig", errors="replace")

    def xml(self, name):
        try:
            root = SafeET.fromstring(
                b"".join(self.chunks(name, MAX_XML_BYTES)),
                forbid_dtd=True, forbid_entities=True, forbid_external=True,
            )
            stack = [(root, 1)]
            while stack:
                node, depth = stack.pop()
                if depth > MAX_XML_DEPTH:
                    raise IMSCCImportError(f"XML nesting exceeds the safety limit: {name}")
                stack.extend((child, depth + 1) for child in node)
            return root
        except (ParseError, DefusedXmlException) as exc:
            raise IMSCCImportError(f"Unsafe or malformed XML: {name}") from exc

    def reference(self, href, base="imsmanifest.xml"):
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            self.warn(f"External resource not imported: {href}")
            return None
        decoded = unquote(parsed.path)
        if "\\" in decoded or decoded.startswith("/") or ":" in decoded:
            self.warn(f"Unsafe resource reference ignored: {href}")
            return None
        name = posixpath.normpath(posixpath.join(posixpath.dirname(base), decoded))
        if name.startswith("../") or name == "..":
            self.warn(f"Unsafe resource reference ignored: {href}")
            return None
        if name not in self.entries:
            self.warn(f"Missing archive reference: {name}")
            return None
        return name

    def item_id(self, kind, source):
        digest = hashlib.sha256(source.encode()).hexdigest()[:24]
        return f"{self.course_id}-{kind}-{digest}"

    def write_json(self, folder, name, data):
        directory = self.output / folder
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, kind, source, data, aliases=()):
        key = (kind, source)
        if key in self.seen:
            item = self.seen[key]
        else:
            item = dict(data, id=self.item_id(kind, source), source_path=source, source_type="imscc")
            item.setdefault("published", True)
            if kind == "page":
                item["url"] = f"{_slug(item['title'])}-{hashlib.sha256(source.encode()).hexdigest()[:10]}"
            folder = {"page": "pages", "assignment": "assignments", "quiz": "quizzes",
                      "discussion": "discussions", "syllabus": "syllabus"}[kind]
            self.write_json(folder, item["id"], item)
            self.counts[folder] += 1
            self.seen[key] = item
        for path in (source, *aliases):
            self.consumed.add(path)
            if kind != "syllabus":
                self.paths[path] = {
                    "kind": kind, "target": item.get("url", item["id"] + ".json"), "id": item["id"]
                }
        return item

    @staticmethod
    def published(root=None, html=None):
        value = _text(root, "workflow_state") or _text(root, "published") if root is not None else ""
        if not value and html:
            value = html.metadata.get("workflow_state", "") or html.metadata.get("published", "")
        return value.lower() not in ("unpublished", "false", "0", "deleted")

    def resource_files(self, resource):
        result = []
        pending = [resource]
        visited = set()
        while pending:
            resource = pending.pop()
            if id(resource) in visited:
                continue
            visited.add(id(resource))
            for href in [resource.get("href")] + [n.get("href") for n in _nodes(resource, "file")]:
                if href:
                    name = self.reference(href)
                    if name and name not in result:
                        result.append(name)
            for dep in _nodes(resource, "dependency"):
                ref = dep.get("identifierref")
                if ref not in self.resources:
                    self.warn(f"Missing resource dependency: {ref}")
                else:
                    pending.append(self.resources[ref])
        return result

    def quiz(self, paths, identifier):
        questions = []
        title = ""
        description = ""
        meta = None
        usable_sources = []
        for path in paths:
            if not path.lower().endswith(".xml"):
                continue
            root = self.xml(path)
            if _local(root.tag) in ("quiz", "assessment_meta") or path.endswith("assessment_meta.xml"):
                meta = root
                title = _text(root, "title") or title
                description = _text(root, "description")
            assessments = _nodes(root, "assessment") + _nodes(root, "assessmentTest")
            if assessments:
                title = title or assessments[0].get("title", "")
            for ref in _nodes(root, "assessmentItemRef"):
                href = ref.get("href")
                if href:
                    referenced = self.reference(href, path)
                    if referenced and referenced not in paths:
                        paths.append(referenced)
            items = _nodes(root, "item") + _nodes(root, "assessmentItem")
            for node in items:
                # QTI 1.2 presentation excludes response processing/feedback.
                presentations = _nodes(node, "presentation")
                bodies = _nodes(node, "itemBody")
                presentation = presentations[0] if presentations else (bodies[0] if bodies else node)
                materials = _nodes(presentation, "material")
                prompt = "\n".join(_text(m, "mattext") for m in materials if not any(
                    m is sub for response in _nodes(presentation, "response_label") for sub in response.iter()
                )).strip()
                if bodies:
                    prompt = "".join(bodies[0].itertext()).strip()
                if not prompt:
                    self.warn(f"Quiz question missing usable text: {path}")
                    continue
                fields = {}
                for field in _nodes(node, "qtimetadatafield"):
                    fields[_text(field, "fieldlabel")] = _text(field, "fieldentry")
                answers = [
                    {"id": answer.get("ident") or answer.get("identifier"),
                     "text": _text(answer, "mattext") or "".join(answer.itertext()).strip()}
                    for answer in (_nodes(node, "response_label") + _nodes(node, "simpleChoice"))
                ]
                questions.append({
                    "id": self.item_id("question", path + ":" + str(len(questions))),
                    "question_name": node.get("title", ""),
                    "question_text": prompt,
                    "question_type": fields.get("question_type", "qti_text"),
                    "answers": answers,
                    "source_identifier": node.get("ident") or node.get("identifier"),
                })
                usable_sources.append(path)
        self.warn(f"Partial quiz coverage ({identifier}): question text/choices only; grading, feedback, banks, interactions and full quiz fidelity are not preserved.")
        if not questions:
            self.warn(f"Quiz missing usable question text: {identifier}")
        source = next(iter(usable_sources), paths[0] if paths else identifier)
        return self.add("quiz", source, {
            "title": title or identifier, "description": description, "questions": questions,
            "published": self.published(meta), "source_identifier": identifier,
        }, paths)

    def content(self, kind, paths, identifier):
        html_paths = [p for p in paths if PurePosixPath(p).suffix.lower() in (".html", ".htm")]
        xml_paths = [p for p in paths if p.lower().endswith(".xml")]
        roots = [(p, self.xml(p)) for p in xml_paths]
        html_path = html_paths[0] if html_paths else None
        body = self.read(html_path) if html_path else ""
        html_meta = _HTMLMetadata(body)
        title = html_meta.title.strip()
        primary = None
        discussion_roots = []
        for path, root in roots:
            if kind == "assignment" and (_local(root.tag) == "assignment" or path.endswith("assignment_settings.xml")):
                primary = root
            elif kind == "discussion" and (_nodes(root, "topic") or _local(root.tag) in ("topic", "discussion", "topicMeta")):
                discussion_roots.append(root)
                if primary is None or _text(root, "text") or _text(root, "message"):
                    primary = root
        if primary is not None:
            title = _text(primary, "title") or _text(primary, "name") or title
            body = _text(primary, "text") or _text(primary, "description") or _text(primary, "message") or body
        if not body:
            self.warn(f"Resource missing usable content: {identifier}")
        title = title or identifier or "Untitled"
        data = {"published": self.published(primary, html_meta), "source_identifier": identifier}
        if discussion_roots:
            data["published"] = all(self.published(root) for root in discussion_roots)
        if kind == "assignment":
            points = _text(primary, "points_possible") if primary is not None else ""
            try:
                points = float(points) if points else None
            except ValueError:
                points = None
            submissions = _text(primary, "submission_types") if primary is not None else ""
            data.update(name=title, description=body, points_possible=points,
                        due_at=_text(primary, "due_at") or None if primary is not None else None,
                        submission_types=[v.strip() for v in submissions.split(",") if v.strip()])
        elif kind == "discussion":
            data.update(title=title, message=body)
        else:
            data.update(title=title, body=body)
        return self.add(kind, html_path or (xml_paths[0] if xml_paths else identifier), data, paths)

    def assets(self):
        for path, info in self.entries.items():
            if path in self.consumed or path == "imsmanifest.xml":
                continue
            suffix = PurePosixPath(path).suffix.lower()
            if suffix not in ASSET_EXTENSIONS:
                if suffix in (".html", ".htm", ".js", ".svg"):
                    self.warn(f"Unsupported executable/HTML asset not imported: {path}")
                elif not path.startswith("course_settings/") and suffix != ".xml":
                    self.warn(f"Unsupported asset not imported: {path}")
                continue
            filename = hashlib.sha256(path.encode()).hexdigest() + suffix
            directory = self.output / "files"
            directory.mkdir(exist_ok=True)
            with (directory / filename).open("wb") as destination:
                for chunk in self.chunks(path, MAX_ENTRY_BYTES):
                    destination.write(chunk)
            item_id = self.item_id("file", path)
            self.write_json("files", item_id, {
                "id": item_id, "filename": filename, "display_name": PurePosixPath(path).name,
                "source_path": path, "source_type": "imscc",
            })
            self.paths[path] = {"kind": "file", "target": filename, "id": item_id}
            self.counts["files"] += 1

    def modules(self, manifest):
        path = "course_settings/module_meta.xml"
        if path in self.entries:
            root = self.xml(path)
            modules = _nodes(root, "module")
        else:
            modules = []
            for org in _nodes(manifest, "organization"):
                modules.extend(n for n in org if _local(n.tag) == "item")
        for position, module in enumerate(modules, 1):
            identifier = module.get("identifier", str(position))
            items = []
            for item in module.iter():
                if item is module or _local(item.tag) != "item":
                    continue
                ref = item.get("identifierref") or _text(item, "identifierref")
                target = self.resource_items.get(ref)
                if ref and not target:
                    self.warn(f"Module reference has no imported content: {ref}")
                items.append({
                    "id": self.item_id("module-item", identifier + ":" + str(len(items))),
                    "title": _text(item, "title"), "source_identifier": item.get("identifier"),
                    "identifierref": ref, "content_id": target["id"] if target else None,
                    "type": _text(item, "content_type") or (target.get("kind") if target else None),
                    "position": _text(item, "position") or len(items) + 1,
                    "indent": _text(item, "indent") or 0,
                    "external_url": _text(item, "url") or None,
                })
            data = {
                "id": self.item_id("module", identifier), "name": _text(module, "title"),
                "source_identifier": identifier, "position": _text(module, "position") or position,
                "published": self.published(module), "items": items,
                "source_path": path if path in self.entries else "imsmanifest.xml", "source_type": "imscc",
                "source_metadata": self.element_data(module),
            }
            self.write_json("modules", data["id"], data)
            self.counts["modules"] += 1
        if modules:
            self.warn("Module metadata preserved; downstream module navigation may not preserve ordering or prerequisites.")

    @staticmethod
    def element_data(node):
        return {"tag": _local(node.tag), "attributes": dict(node.attrib),
                "text": node.text or "", "children": [_Cartridge.element_data(n) for n in node]}

    def parse(self):
        self.inspect()
        manifest = self.xml("imsmanifest.xml")
        if _local(manifest.tag) != "manifest":
            raise IMSCCImportError("imsmanifest.xml does not contain a manifest.")
        title = ""
        metadata = next((n for n in manifest if _local(n.tag) == "metadata"), None)
        if metadata is not None:
            title = _text(metadata, "string") or _text(metadata, "title")
        settings_path = "course_settings/course_settings.xml"
        if settings_path in self.entries:
            title = _text(self.xml(settings_path), "title") or title
        title = title or "Imported Canvas course"
        for resource in _nodes(manifest, "resource"):
            identifier = resource.get("identifier", "")
            if not identifier or identifier in self.resources:
                raise IMSCCImportError("Missing or duplicate manifest resource identifier.")
            self.resources[identifier] = resource
        # Process semantic resources before generic HTML to avoid duplicate assignments.
        ordered = sorted(self.resources.items(), key=lambda pair: (
            "associatedcontent" in pair[1].get("type", "").lower(),
            "webcontent" in pair[1].get("type", "").lower(),
        ))
        for identifier, resource in ordered:
            resource_type = resource.get("type", "").lower()
            paths = self.resource_files(resource)
            if "basiclti" in resource_type or "imswl" in resource_type:
                self.warn(f"External LTI/web link resource not imported: {identifier}")
                continue
            if not paths:
                self.warn(f"Resource has no usable bundled files: {identifier}")
                continue
            for path in list(paths):
                if path.lower().endswith((".html", ".htm")):
                    companion = posixpath.join(posixpath.dirname(path), "assignment_settings.xml")
                    if companion in self.entries and companion not in paths:
                        paths.append(companion)
            if "associatedcontent" in resource_type:
                # Companion metadata is processed after its owning HTML/QTI.
                # Canvas assignments themselves can be LOR resources.
                if not any(p.endswith(("assignment_settings.xml", "topicMeta.xml", "assessment_meta.xml", "syllabus.html"))
                           for p in paths):
                    continue
            # Dependencies may include other instructional resources. Their
            # assignment metadata must not reclassify or overwrite this HTML.
            html_path = next((p for p in paths if p.lower().endswith((".html", ".htm"))), None)
            if html_path:
                paths = [p for p in paths if not p.endswith("assignment_settings.xml")
                         or posixpath.dirname(p) == posixpath.dirname(html_path)]
            syllabus = next((p for p in paths if PurePosixPath(p).name.lower() == "syllabus.html"), None)
            if syllabus:
                body = self.read(syllabus)
                self.add("syllabus", syllabus, {"title": "Course syllabus", "body": body})
                (self.output / "syllabus.html").write_text(body, encoding="utf-8")
                continue
            kind = None
            if "qti" in resource_type or any(p.endswith(("assessment_qti.xml", "assessment_meta.xml")) for p in paths):
                kind = "quiz"
            elif "imsdt" in resource_type or "discussion" in resource_type or any(p.endswith("topicMeta.xml") for p in paths):
                kind = "discussion"
            elif "assignment" in resource_type or any(p.endswith("assignment_settings.xml") for p in paths):
                kind = "assignment"
            elif "webcontent" in resource_type and any(p.lower().endswith((".html", ".htm")) for p in paths):
                kind = "page"
            if kind:
                content_paths = [p for p in paths if p.lower().endswith((".html", ".htm", ".xml"))]
                if kind in ("page", "assignment") and html_path:
                    content_paths = [p for p in content_paths if p == html_path
                                     or (p.lower().endswith(".xml")
                                         and posixpath.dirname(p) == posixpath.dirname(html_path))]
                if not content_paths:
                    self.warn(f"Resource missing usable content: {identifier}")
                    continue
                existing = next((self.paths[p] for p in content_paths if p in self.paths and self.paths[p]["kind"] != "file"), None)
                if existing:
                    self.resource_items[identifier] = existing
                    for path in content_paths:
                        self.paths.setdefault(path, existing)
                        self.consumed.add(path)
                    continue
                item = self.quiz(content_paths, identifier) if kind == "quiz" else self.content(kind, content_paths, identifier)
                self.resource_items[identifier] = {"id": item["id"], "kind": kind}
            elif "associatedcontent" not in resource_type and not all(PurePosixPath(p).suffix.lower() in ASSET_EXTENSIONS for p in paths):
                self.warn(f"Unhandled resource type {resource_type or '(unspecified)'}: {identifier}")
        # Canvas exports may omit explicit manifest entries for wiki/assignment HTML.
        for path in self.entries:
            if path in self.consumed or not path.lower().endswith((".html", ".htm")):
                continue
            if "syllabus" in PurePosixPath(path).name.lower():
                body = self.read(path)
                self.add("syllabus", path, {"title": "Course syllabus", "body": body})
                (self.output / "syllabus.html").write_text(body, encoding="utf-8")
            elif path.startswith("wiki_content/"):
                self.content("page", [path], path)
            elif "assignment" in path.lower():
                companion = posixpath.join(posixpath.dirname(path), "assignment_settings.xml")
                self.content("assignment", [path] + ([companion] if companion in self.entries else []), path)
        self.assets()
        self.modules(manifest)
        if not any(self.counts[k] for k in ("pages", "assignments", "discussions", "syllabus")) and not any(
                item.get("questions") for (kind, _), item in self.seen.items() if kind == "quiz"):
            self.warn("No supported instructional content was found in this cartridge.")
        return title, manifest.get("identifier"), list(self.resources)


def import_imscc(archive_path, export_base):
    """Normalize a trusted local archive path, publishing only after full success."""
    archive_path = Path(archive_path)
    export_base = Path(export_base).resolve()
    temporary = None
    try:
        if archive_path.stat().st_size > MAX_UPLOAD_BYTES:
            raise IMSCCImportError("Archive upload exceeds the size limit.")
        export_base.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".imscc-", dir=export_base))
        course_id = "imscc-" + uuid.uuid4().hex
        with zipfile.ZipFile(archive_path) as archive:
            cartridge = _Cartridge(archive, temporary, course_id)
            title, manifest_id, resource_ids = cartridge.parse()
        final = export_base / f"course_{course_id}_{_slug(title)}"
        summary = {
            "course_id": course_id, "course_name": title, "export_dir": str(final),
            "export_directory": str(final), "source_type": "imscc",
            "original_filename": archive_path.name, "original_title": title,
            "manifest_identifier": manifest_id, "resource_identifiers": resource_ids,
            "counts": cartridge.counts, "warnings": cartridge.warnings,
            "resource_paths": cartridge.paths,
        }
        (temporary / "export_summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.rename(temporary, final)
        temporary = None
        return summary
    except IMSCCImportError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
            NotImplementedError, EOFError, ValueError, zlib.error) as exc:
        raise IMSCCImportError(f"Cannot import cartridge: {exc}") from exc
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
