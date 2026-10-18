"""Offline review packages, not complete Canvas backups.

Only validated normalized records and bounded passive documents cross this
boundary. Supplied archive names, IDs, asset names and export paths are never
used as output destinations. HTML remains untrusted: callers must run
imscc_content.prepare_import_content before rendering or downstream processing.
"""

import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zipfile
import zlib

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from imscc_import import IMSCCImportError, _slug

MIB = 1024 * 1024
MAX_PACKAGE_BYTES = 64 * MIB
MAX_PACKAGE_EXPANDED = 128 * MIB
MAX_JSON_BYTES = 10 * MIB
MAX_XML_BYTES = 10 * MIB
MAX_DOCUMENT_BYTES = 4 * MIB
MAX_CENTRAL_BYTES = 32 * MIB
FOLDERS = ("pages", "assignments", "quizzes", "discussions", "syllabus", "files", "modules")
KINDS = dict(zip(FOLDERS, ("page", "assignment", "quiz", "discussion", "syllabus", "file", "module")))
DOCUMENT_EXTENSIONS = {".txt", ".csv", ".tsv", ".vtt", ".srt", ".pdf", ".docx", ".pptx"}


def safe_path(name):
    """Match the raw IMSCC parser's path convention, including case collisions."""
    if (not isinstance(name, str) or not name or len(name) > 4096 or name.count("/") > 128
            or "\x00" in name or "\\" in name
            or name.startswith("/") or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") or ":" in p for p in name.rstrip("/").split("/"))):
        raise IMSCCImportError("Unsafe archive or resource path.")
    return name


def _central_directory_bound(path):
    """Bound zipfile's eager central-directory allocation before opening it."""
    with Path(path).open("rb") as source:
        source.seek(0, 2)
        size = source.tell()
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or offset + 22 > len(tail):
            raise IMSCCImportError("Invalid ZIP end record.")
        record = struct.unpack_from("<4s4H2IH", tail, offset)
        if offset + 22 + record[-1] != len(tail) or record[1] or record[2]:
            raise IMSCCImportError("Unsupported ZIP end record.")
        count, central_size = record[4], record[5]
        if count == 65535 or central_size == 0xffffffff or record[6] == 0xffffffff:
            end_position = size - len(tail) + offset
            if end_position < 20:
                raise IMSCCImportError("Invalid ZIP64 locator.")
            source.seek(end_position - 20)
            locator = struct.unpack("<4sIQI", source.read(20))
            if locator[0] != b"PK\x06\x07" or locator[1] or locator[3] != 1:
                raise IMSCCImportError("Unsupported ZIP64 locator.")
            source.seek(locator[2])
            data = source.read(56)
            if len(data) != 56:
                raise IMSCCImportError("Invalid ZIP64 end record.")
            end = struct.unpack("<4sQ2H2I4Q", data)
            if end[0] != b"PK\x06\x06" or end[4] or end[5]:
                raise IMSCCImportError("Unsupported ZIP64 end record.")
            count, central_size = end[7], end[8]
        if central_size > MAX_CENTRAL_BYTES or count > 50000:
            raise IMSCCImportError("ZIP central directory exceeds safety limits.")


def inspect_zip(archive, *, max_entries=50000, max_expanded=32 * 1024**3):
    entries = {}
    names = set()
    total = 0
    if len(archive.infolist()) > max_entries:
        raise IMSCCImportError("Archive contains too many entries.")
    for info in archive.infolist():
        safe_path(info.filename)
        if "\x00" in info.orig_filename:
            raise IMSCCImportError("Unsafe archive path.")
        key = info.filename.rstrip("/").casefold()
        if key in names:
            raise IMSCCImportError("Duplicate or case-colliding archive path.")
        names.add(key)
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise IMSCCImportError("Archive links or special files are not allowed.")
        if info.flag_bits & 1:
            raise IMSCCImportError("Encrypted archive entries are not supported.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise IMSCCImportError("Unsupported ZIP compression.")
        total += info.file_size
        if total > max_expanded:
            raise IMSCCImportError("Archive declared expansion exceeds the size limit.")
        if not info.is_dir():
            entries[info.filename] = info
    # A regular file may not masquerade as another entry's directory.
    file_names = {n.casefold() for n in entries}
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in file_names for i in range(1, len(parts))):
            raise IMSCCImportError("Archive file/directory collision.")
    return entries


def bounded_read(archive, info, limit):
    if info.file_size > limit:
        raise IMSCCImportError("Archive content exceeds its size limit.")
    chunks = []
    total = 0
    with archive.open(info) as source:
        while True:
            chunk = source.read(min(65536, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise IMSCCImportError("Archive content exceeds its size limit.")
            chunks.append(chunk)
    return b"".join(chunks)


def validate_xml(data):
    if len(data) > MAX_XML_BYTES:
        raise IMSCCImportError("XML exceeds the 10 MiB safety limit.")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        pending = [(root, 1)]
        while pending:
            node, depth = pending.pop()
            if depth > 128:
                raise IMSCCImportError("XML nesting exceeds the safety limit.")
            pending.extend((child, depth + 1) for child in node)
        return root
    except (ParseError, DefusedXmlException) as exc:
        raise IMSCCImportError("Unsafe or malformed XML.") from exc


def validate_document(data, suffix):
    if len(data) > MAX_DOCUMENT_BYTES:
        raise IMSCCImportError("Document exceeds the 4 MiB review limit.")
    if suffix in (".docx", ".pptx"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = inspect_zip(archive, max_entries=2000, max_expanded=32 * MIB)
                if "[Content_Types].xml" not in entries:
                    raise IMSCCImportError("Invalid Office document.")
                for name, info in entries.items():
                    ext = PurePosixPath(name).suffix.lower()
                    if (ext not in {".xml", ".rels", ".png", ".jpg", ".jpeg", ".gif"}
                            or any(p in name.lower() for p in ("embeddings/", "activex/", "vbaproject"))):
                        raise IMSCCImportError("Active or unsupported Office attachment.")
                    if info.file_size > MIB and info.file_size > max(1, info.compress_size) * 200:
                        raise IMSCCImportError("Office attachment compression exceeds safety limit.")
                    value = bounded_read(archive, info, MAX_XML_BYTES)
                    if ext in (".xml", ".rels"):
                        root = validate_xml(value)
                        if ext == ".rels" and any(
                                n.get("TargetMode", "").lower() == "external" for n in root.iter()):
                            raise IMSCCImportError("External Office relationships are not retained.")
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise IMSCCImportError("Invalid Office document.") from exc
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF-") or re.search(
                rb"/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction|AA)\b", data, re.I):
            raise IMSCCImportError("Active or invalid PDF document.")
    else:
        if b"\x00" in data or data.startswith((b"SQLite format 3", b"PK\x03\x04", b"MZ")):
            raise IMSCCImportError("Non-text content in a text attachment.")
        try:
            data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise IMSCCImportError("Text attachments must be UTF-8.") from exc


def _json(data):
    if len(data) > MAX_JSON_BYTES:
        raise IMSCCImportError("Package JSON exceeds the size limit.")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise IMSCCImportError("Duplicate JSON key.")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IMSCCImportError("Invalid package JSON.") from exc
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 140 or count > 500000:
            raise IMSCCImportError("Package JSON nesting/complexity exceeds limits.")
        if isinstance(node, dict):
            pending.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            pending.extend((v, depth + 1) for v in node)
        elif isinstance(node, float) and not math.isfinite(node):
            raise IMSCCImportError("Nonfinite package number.")
    if not isinstance(value, dict):
        raise IMSCCImportError("Package JSON must be an object.")
    return value


def _string(value, *, maximum=4096, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise IMSCCImportError("Invalid package text field.")
    return value


def _strings(value, maximum=50000):
    if not isinstance(value, list) or len(value) > maximum:
        raise IMSCCImportError("Invalid package text list.")
    return [_string(v) for v in value]


def _record(folder, raw):
    common = {"id", "source_path", "source_type", "source_identifier", "published"}
    fields = {
        "pages": {"title", "body", "url"},
        "assignments": {"name", "description", "points_possible", "due_at", "submission_types"},
        "quizzes": {"title", "description", "questions"},
        "discussions": {"title", "message"},
        "syllabus": {"title", "body"},
        "files": {"filename", "display_name"},
        "modules": {"name", "position", "items", "source_metadata"},
    }
    if set(raw) - common - fields[folder]:
        raise IMSCCImportError("Unknown normalized record field.")
    _string(raw.get("id"), maximum=512)
    safe_path(raw.get("source_path"))
    if raw.get("source_type") != "imscc":
        raise IMSCCImportError("Invalid normalized source type.")
    if "source_identifier" in raw:
        _string(raw["source_identifier"], nullable=True)
    if "published" in raw and type(raw["published"]) is not bool:
        raise IMSCCImportError("Invalid published state.")
    for key in ("title", "name", "display_name", "filename", "url", "due_at"):
        if key in raw:
            _string(raw[key], nullable=key == "due_at")
    required = {"pages": ("title", "body", "url"), "assignments": ("name", "description"),
                "quizzes": ("title", "questions"), "discussions": ("title", "message"),
                "syllabus": ("title", "body"), "files": ("filename", "display_name"),
                "modules": ("name", "items")}[folder]
    if any(k not in raw for k in required):
        raise IMSCCImportError("Incomplete normalized record.")
    for key in ("body", "description", "message"):
        if key in raw:
            _string(raw[key], maximum=MAX_JSON_BYTES)
    if "submission_types" in raw:
        _strings(raw["submission_types"], 100)
    if "points_possible" in raw and raw["points_possible"] is not None:
        if type(raw["points_possible"]) not in (int, float):
            raise IMSCCImportError("Invalid assignment points.")
    if folder == "quizzes":
        questions = raw["questions"]
        if not isinstance(questions, list) or len(questions) > 50000:
            raise IMSCCImportError("Invalid quiz questions.")
        for question in questions:
            if not isinstance(question, dict) or set(question) - {
                    "id", "question_name", "question_text", "question_type", "answers", "source_identifier"}:
                raise IMSCCImportError("Invalid quiz question.")
            for field in ("id", "question_name", "question_text", "question_type"):
                _string(question.get(field), maximum=MAX_JSON_BYTES if field == "question_text" else 4096)
            if "source_identifier" in question:
                _string(question["source_identifier"], nullable=True)
            if not isinstance(question.get("answers"), list) or len(question["answers"]) > 10000:
                raise IMSCCImportError("Invalid quiz answers.")
            for answer in question["answers"]:
                if not isinstance(answer, dict) or set(answer) != {"id", "text"}:
                    raise IMSCCImportError("Invalid quiz answer.")
                _string(answer["id"], nullable=True)
                _string(answer["text"], maximum=MAX_JSON_BYTES)
    if folder == "modules":
        if "position" in raw and type(raw["position"]) not in (str, int):
            raise IMSCCImportError("Invalid module ordering.")
        if not isinstance(raw["items"], list) or len(raw["items"]) > 50000:
            raise IMSCCImportError("Invalid module items.")
        for item in raw["items"]:
            if not isinstance(item, dict) or set(item) - {
                    "id", "title", "source_identifier", "identifierref", "content_id",
                    "type", "position", "indent", "external_url"}:
                raise IMSCCImportError("Invalid module item.")
            for key in ("id", "title", "source_identifier", "identifierref", "content_id", "type", "external_url"):
                if key in item:
                    _string(item[key], nullable=key not in ("id", "title"))
            for key in ("position", "indent"):
                if key in item and type(item[key]) not in (str, int):
                    raise IMSCCImportError("Invalid module ordering.")
        if "source_metadata" in raw:
            # Retain descriptive XML metadata, never arbitrary nested JSON fields.
            pending = [raw["source_metadata"]]
            while pending:
                node = pending.pop()
                if (not isinstance(node, dict) or set(node) != {"tag", "attributes", "text", "children"}
                        or not isinstance(node["attributes"], dict) or not isinstance(node["children"], list)):
                    raise IMSCCImportError("Invalid module source metadata.")
                _string(node["tag"])
                _string(node["text"], maximum=MAX_JSON_BYTES)
                for key, value in node["attributes"].items():
                    _string(key)
                    _string(value)
                pending.extend(node["children"])
    return raw


def _manifest(raw):
    if (raw.get("format") != "hax-course-review" or type(raw.get("version")) is not int
            or raw["version"] != 1 or raw.get("source_type") != "prepared"):
        raise IMSCCImportError("Unsupported course review package.")
    _string(raw.get("course_name"))
    _strings(raw.get("warnings"), 50000)
    if not isinstance(raw.get("exclusions"), list) or len(raw["exclusions"]) > 50000:
        raise IMSCCImportError("Invalid exclusion inventory.")
    for entry in raw["exclusions"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "reason"}:
            raise IMSCCImportError("Invalid exclusion record.")
        safe_path(entry["path"])
        _string(entry["reason"])
        if type(entry["size"]) is not int or not 0 <= entry["size"] <= 32 * 1024**3:
            raise IMSCCImportError("Invalid exclusion size.")
    if not isinstance(raw.get("counts"), dict) or set(raw["counts"]) != set(FOLDERS):
        raise IMSCCImportError("Invalid content counts.")
    if any(type(v) is not int or not 0 <= v <= 50000 for v in raw["counts"].values()):
        raise IMSCCImportError("Invalid content count.")
    return raw


def _import(package_path, export_base):
    package_path = Path(package_path)
    if package_path.stat().st_size > MAX_PACKAGE_BYTES:
        raise IMSCCImportError("Review package exceeds the 64 MiB compressed limit.")
    _central_directory_bound(package_path)
    with zipfile.ZipFile(package_path) as archive:
        entries = inspect_zip(archive, max_entries=50000, max_expanded=MAX_PACKAGE_EXPANDED)
        if not {"haxcourse.json", "course/export_summary.json"} <= set(entries):
            raise IMSCCImportError("Incomplete course review package.")
        manifest = _manifest(_json(bounded_read(archive, entries["haxcourse.json"], MAX_JSON_BYTES)))
        supplied = _json(bounded_read(archive, entries["course/export_summary.json"], MAX_JSON_BYTES))
        records, assets = [], {}
        for name, info in entries.items():
            if name in ("haxcourse.json", "course/export_summary.json"):
                continue
            parts = name.split("/")
            if name == "course/syllabus.html":
                # The importer regenerates this from the validated syllabus record.
                bounded_read(archive, info, MAX_JSON_BYTES)
                continue
            if len(parts) != 3 or parts[0] != "course" or parts[1] not in FOLDERS:
                raise IMSCCImportError("Unexpected package path.")
            folder, filename = parts[1:]
            suffix = PurePosixPath(filename).suffix.lower()
            if suffix == ".json":
                raw = _record(folder, _json(bounded_read(archive, info, MAX_JSON_BYTES)))
                records.append((folder, raw))
            elif folder == "files" and suffix in DOCUMENT_EXTENSIONS:
                data = bounded_read(archive, info, MAX_DOCUMENT_BYTES)
                validate_document(data, suffix)
                assets[filename] = data
            else:
                raise IMSCCImportError("Active or unsupported package asset.")
        counts = {folder: sum(f == folder for f, _ in records) for folder in FOLDERS}
        if counts != manifest["counts"]:
            raise IMSCCImportError("Package counts do not match normalized content.")
        id_map = {}
        course_id = "imscc-" + uuid.uuid4().hex
        by_id = {}
        for folder, raw in records:
            old = raw["id"]
            if old in id_map:
                raise IMSCCImportError("Duplicate normalized content ID.")
            id_map[old] = course_id + "-" + KINDS[folder] + "-" + uuid.uuid4().hex
            by_id[old] = (folder, raw)
        file_map = {}
        claimed = set()
        for folder, raw in records:
            if folder != "files":
                continue
            old = raw["filename"]
            if old not in assets or old in claimed:
                raise IMSCCImportError("Missing or duplicate package file reference.")
            claimed.add(old)
            suffix = PurePosixPath(old).suffix.lower()
            file_map[old] = hashlib.sha256(assets[old]).hexdigest() + suffix
        if claimed != set(assets):
            raise IMSCCImportError("Unreferenced package assets are not allowed.")
        paths = {}
        supplied_paths = supplied.get("resource_paths")
        if not isinstance(supplied_paths, dict):
            raise IMSCCImportError("Invalid package resource mapping.")
        for source, ref in supplied_paths.items():
            safe_path(source)
            if not isinstance(ref, dict) or set(ref) != {"id", "kind", "target"}:
                raise IMSCCImportError("Invalid resource reference.")
            _string(ref["id"], maximum=512)
            if ref["id"] not in by_id:
                raise IMSCCImportError("Unknown resource content ID.")
            folder, raw = by_id[ref["id"]]
            if ref["kind"] != KINDS[folder] or folder in ("syllabus", "modules"):
                raise IMSCCImportError("Resource kind mismatch.")
            target = (file_map[raw["filename"]] if folder == "files"
                      else "page-" + id_map[raw["id"]] if folder == "pages"
                      else id_map[raw["id"]] + ".json")
            paths[source] = {"kind": KINDS[folder], "id": id_map[raw["id"]], "target": target}
        export_base = Path(export_base).resolve()
        export_base.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".haxcourse-", dir=export_base))
        try:
            for folder, raw in records:
                old = raw["id"]
                raw["id"], raw["source_type"] = id_map[old], "imscc"
                if folder == "pages":
                    raw["url"] = "page-" + raw["id"]
                if folder == "files":
                    filename = file_map[raw["filename"]]
                    (temporary / "files").mkdir(exist_ok=True)
                    (temporary / "files" / filename).write_bytes(assets[raw["filename"]])
                    raw["filename"] = filename
                for question in raw.get("questions", []):
                    question["id"] = course_id + "-question-" + uuid.uuid4().hex
                    for answer in question["answers"]:
                        answer["id"] = course_id + "-answer-" + uuid.uuid4().hex
                for item in raw.get("items", []):
                    item["id"] = course_id + "-module-item-" + uuid.uuid4().hex
                    old_ref = item.get("content_id")
                    if old_ref is not None and (old_ref not in by_id or by_id[old_ref][0] in ("modules", "syllabus")):
                        raise IMSCCImportError("Unknown module content reference.")
                    item["content_id"] = id_map.get(old_ref)
                (temporary / folder).mkdir(exist_ok=True)
                (temporary / folder / (raw["id"] + ".json")).write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                if folder == "syllabus":
                    (temporary / "syllabus.html").write_text(raw["body"], encoding="utf-8")
            title = manifest["course_name"]
            final = export_base / ("course_" + course_id + "_" + _slug(title))
            summary = {
                "course_id": course_id, "course_name": title, "export_dir": str(final),
                "export_directory": str(final), "source_type": "imscc",
                "original_filename": package_path.name, "original_title": title,
                "manifest_identifier": _string(supplied.get("manifest_identifier"), nullable=True),
                "resource_identifiers": _strings(supplied.get("resource_identifiers", [])),
                "counts": counts, "warnings": list(dict.fromkeys(manifest["warnings"] + [
                    "Prepared review package: bulk media excluded; not a complete course backup."])),
                "exclusions": manifest["exclusions"], "resource_paths": paths,
                "provenance": {"source": "prepared", "media_excluded": True,
                               "format": "hax-course-review", "version": 1},
            }
            (temporary / "export_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            os.rename(temporary, final)
            temporary = None
            return summary
        finally:
            if temporary is not None:
                shutil.rmtree(temporary)


def import_course_package(package_path, export_base):
    """Validate and remap a local review package; do not render its HTML yet."""
    try:
        return _import(package_path, export_base)
    except IMSCCImportError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError, EOFError, struct.error, zlib.error) as exc:
        raise IMSCCImportError(f"Cannot import course review package: {exc}") from exc
